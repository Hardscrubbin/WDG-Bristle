#include "Node.h"

#include <esp_mac.h>
#include <esp_ota_ops.h>

#include "Boards.h"
#include "Bridge.h"
#include "BristleProto.h"
#include "Clock.h"
#include "Console.h"
#include "Crypto.h"
#include "Link_Ble.h"
#include "Link_EspNow.h"
#include "Ota.h"
#include "Owner.h"
#include "Radio.h"

namespace node {

static NodeState st;
static FindQueue q;
static radio::Plan plan;
static bool planOk = false;
static uint8_t hw[6];
static String hwHex;
static bool otaSlots = false;

static const int MAX_LINKS = 3;
static Link* links[MAX_LINKS];
static int nLinks = 0;
static Link* active = nullptr;
static Link* usb = nullptr;            // the link that allows adoption

// delivery
static uint32_t sendCursor = 0, lastAckSeen = 0, lastAckMoveMs = 0;
static uint32_t badLineCount = 0;
static uint32_t identUntil = 0;
static uint32_t lastBeaconMs = 0, consoleQuietUntil = 0, restartAt = 0;
static uint8_t watchMac[6];            // hunt mode: the device being listened for

const NodeState& state() { return st; }
FindQueue& queue() { return q; }
const String& hwidHex() { return hwHex; }
uint32_t badLines() { return badLineCount; }
Link* activeLink() { return active; }
bool isBridge() { return st.role == bp::ROLE_BRIDGE; }
String planText() { return planOk ? radio::canonical(plan) : String(); }
String name() { return String(bp::NAME_PREFIX) + "-" + (st.number ? String(st.number) : String("NEW")); }
void badLine() { badLineCount++; }
void consoleUsed() { consoleQuietUntil = millis() + 60000; }   // someone is typing: no beacon lines for a minute
void addLink(Link& link) {
  if (nLinks < MAX_LINKS) links[nLinks++] = &link;
  if (link.allowsAdoption()) usb = &link;
}

// ------------------------------------------------------------------------------------ helpers
static void rsp(Link& link, const char* cmd, bool ok, const char* err = nullptr, const String* id = nullptr) {
  codec::LineOut l(bp::MSG_RSP);
  l.add("c", cmd).add("ok", (long)(ok ? 1 : 0));
  if (err) l.add("e", err);
  if (id) l.add("id", *id);
  link.send(l.finish());
}

static uint16_t flagsFor(const Link& link) {
  uint16_t f = bp::HB_CAP_5G | bp::HB_CAP_BLE;
  if (q.flashOk()) f |= bp::HB_QUEUE_FLASH;
  if (otaSlots) f |= bp::HB_OTA_SLOTS;
  if (radio::running()) f |= bp::HB_RUNNING;
  if (st.owned) f |= bp::HB_OWNED;
  if (link.authed) f |= bp::HB_AUTHED;
  if (clk::setSinceBoot()) f |= bp::HB_CLOCK;
  if (identUntil && millis() < identUntil) f |= bp::HB_IDENTIFYING;
  if (isBridge()) f |= bp::HB_BRIDGE;
  if (ota::onTrial()) f |= bp::HB_OTA_PENDING;
  return f;
}

static void heartbeat(Link& link) {
  codec::LineOut l(bp::MSG_HB);
  l.addu("n", st.number).addu("f", flagsFor(link)).addRaw("ph", planOk ? radio::planHash(plan) : String(""));
  l.addu("m", st.runMask).addu("s", st.session).addu("lk", link.type());
  l.addu("qb", q.backlog()).addu("qd", q.dropped());
  l.addu("qc", q.capacityBytes() / 1024).addu("qf", q.fillPct());
  l.addu("qn", q.nextSeq()).addu("qo", q.ackSeq()).addu("b", BRISTLE_BUILD);
  l.add("t", (long)temperatureRead()).addu("up", (unsigned long)(clk::uptimeMs() / 1000));
  l.addu("ram", ESP.getFreeHeap() / 1024);
  if (link.hasRssi()) l.add("rs", (long)link.rssi());
  link.send(l.finish());
  link.lastHbMs = millis();
  if (isBridge()) {
    bridge::Stats s = bridge::stats();
    codec::LineOut b(bp::MSG_BST);
    b.addu("rx", s.rx).addu("tx", s.tx).addu("tf", s.txFailed).addu("mb", s.held).addu("md", s.dropped);
    link.send(b.finish());
  }
}

static void sendHello(Link& link) {
  codec::LineOut l(bp::MSG_HELLO);
  l.addu("v", bp::PROTOCOL_VERSION).addRaw("hw", hwHex).add("bd", BRISTLE_BOARD).addu("b", BRISTLE_BUILD);
  l.addu("cap", flagsFor(link)).addu("ro", st.role);
  owner::fillHello(link, l);
  link.send(l.finish());
}

static bool linkFresh(const Link& l, uint32_t now) { return l.authed && now - l.lastRxMs < bp::LINK_TIMEOUT_MS; }

static void applyRun() {
  // A bridge never scans, and a node being updated gives the radio and the flash to the update.
  radio::setRun(planOk && !isBridge() && !ota::active() ? st.runMask : 0);
}

// Finds go out on one link: the authenticated one with the lowest type number (USB is 1).
static void pickActive() {
  Link* best = nullptr;
  for (int i = 0; i < nLinks; i++)
    if (links[i]->authed && (!best || links[i]->type() < best->type())) best = links[i];
  if (best != active) {
    active = best;
    sendCursor = q.ackSeq();   // a new link starts again from the oldest find the core hasn't acked
    lastAckMoveMs = millis();
  }
  // USB wins: while the core has the node on USB, the node spends no radio time on the other links.
  bool onUsb = usb && usb->authed;
  if (!isBridge()) {
    espLink.pause(onUsb);
    bleLink.allow(!onUsb);
  }
}

void linkClosed(Link& link) {
  ota::linkClosed(link);
  pickActive();
}

void updateChanged() {
  espLink.hurry(ota::active() && ota::link() == &espLink);
  applyRun();
}

void identify(uint32_t seconds) { identUntil = seconds ? millis() + seconds * 1000 : 0; }

static void wirelessIdentity() {
  if (isBridge()) return;
  espLink.setKey(st.key, st.owned);
  bleLink.rename(name(), st.owned);
}

void forgetEverything(const char* why) {
  uint8_t role = st.role, channel = st.channel;
  st = NodeState();
  st.role = role;              // what the board is for does not change with its owner
  st.channel = channel;
  store::forgetAll();
  q.clear();
  clk::forget();
  planOk = false;
  for (int i = 0; i < nLinks; i++) links[i]->authed = false;
  wirelessIdentity();
  pickActive();
  applyRun();
  console::say(String("owner forgotten (") + why + "): any core can adopt this node over USB now");
}

// ------------------------------------------------------------------------------------ finds
// Scan results become queue records straight away, with the time they were seen. No clock yet?
// The record keeps the boot number and uptime, and gets its time when the core sets the clock.
static void captured(const String& body) {
  bool rel;
  uint32_t sec;
  uint16_t ms;
  clk::stamp(rel, sec, ms);
  q.push(rel, sec, ms, st.session, body);
}

static void onWifi(const WifiFind& f) {
  codec::LineOut l(bp::MSG_AP);
  l.addRaw("b", crypto::toHex(f.bssid, 6)).addu("c", f.channel).add("r", (long)f.rssi).addu("k", f.security);
  if (f.ssid.length()) l.add("e", f.ssid.substring(0, 32));
  captured(l.body());
}

static void onBle(const BleFind& f) {
  codec::LineOut l(bp::MSG_BT);
  l.addRaw("m", crypto::toHex(f.mac, 6)).add("r", (long)f.rssi).addu("at", f.addrType);
  if (f.companyId >= 0) l.addu("mf", (unsigned long)f.companyId);
  if (f.mfgType >= 0) l.addu("mt", (unsigned long)f.mfgType);
  if (f.nServices) {
    String sv;
    char one[6];
    for (uint8_t i = 0; i < f.nServices; i++) {
      snprintf(one, sizeof(one), "%s%04x", i ? "." : "", f.services[i]);
      sv += one;
    }
    l.addRaw("sv", sv);
  }
  if (f.bristle) {
    l.addRaw("bh", crypto::toHex(f.bristleHw, 6));
    if (f.bristleOwned) l.addu("bo", 1);
  }
  // The name goes last and only as far as the line has room (a line also carries q, t, ms and s).
  if (f.name.length()) {
    String shown = f.name.substring(0, 29);
    while (shown.length() && l.body().length() + 4 + codec::pctEncode(shown).length() > 180) shown.remove(shown.length() - 1);
    if (shown.length()) l.add("nm", shown);
  }
  captured(l.body());
}

// Hunt mode: the watched device was heard just now. Straight to the core, not through the queue.
static void onSeen(int8_t rssi) {
  if (!active || !active->authed) return;
  codec::LineOut l(bp::MSG_SEEN);
  l.addRaw("m", crypto::toHex(watchMac, 6)).add("r", (long)rssi);
  active->send(l.finish());
}

static void setWatch(const uint8_t mac[6], uint32_t seconds) {
  memcpy(watchMac, mac, 6);
  radio::watch(mac, seconds, onSeen);
}

// A stored find -> the line the core gets: TYPE:q=..,t=..,ms=..,s=..,<the stored fields>*CRC
static String findLine(const FindQueue::Rec& r, uint32_t sec, uint16_t ms) {
  int colon = r.body.indexOf(':');
  String line;
  line.reserve(r.body.length() + 64);
  line = r.body.substring(0, colon + 1);
  line += "q=";
  line += String(r.seq);
  line += ",t=";
  line += String(sec);
  line += ",ms=";
  line += String(ms);
  line += ",s=";
  line += String(r.session);
  if ((int)r.body.length() > colon + 1) {
    line += ',';
    line += r.body.substring(colon + 1);
  }
  return codec::withCrc(line);
}

static void streamFinds(uint32_t now) {
  if (q.ackSeq() != lastAckSeen) {
    lastAckSeen = q.ackSeq();
    lastAckMoveMs = now;
  }
  if (!active || !active->authed || !clk::valid() || ota::active()) return;
  if (sendCursor < q.ackSeq()) sendCursor = q.ackSeq();
  for (int i = 0; i < 8; i++) {
    if (active->room() < bp::LINE_MAX_BYTES + 16) return;   // room for the longest line, or wait
    FindQueue::Rec r;
    if (!q.peek(sendCursor, r)) break;
    uint32_t sec;
    uint16_t ms;
    // No time for it (stored by uptime in a boot that never got a clock): never sent. The queue
    // drops and counts it when the ack point gets there.
    if (!clk::resolve(r.rel, r.boot, r.sec, r.ms, sec, ms)) {
      sendCursor = r.seq + 1;
      continue;
    }
    if (!active->send(findLine(r, sec, ms))) return;        // the link had no room after all: same find next time
    sendCursor = r.seq + 1;
  }
  // Everything sent, finds still unacked, and the ack hasn't moved for a while: send again from
  // the oldest unacked find.
  if (sendCursor >= q.nextSeq() && q.backlog() > 0 && now - lastAckMoveMs > bp::RESEND_STALL_MS) {
    sendCursor = q.ackSeq();
    lastAckMoveMs = now;
  }
}

// ------------------------------------------------------------------------------------ protocol
void onMessage(Link& link, const codec::Msg& m) {
  const String* id = m.get("id");
  String t(m.type);
  uint32_t now = millis();

  if (t == bp::MSG_HI) {
    // Only one core runs a node at a time. If another core (another id) holds a live link, a hello
    // that comes in over the air is answered "busy" and changes nothing. USB always gets through:
    // whoever has the node on a cable has it in their hands.
    const String* ci = m.get("ci");
    if (!link.allowsAdoption() && ci && ci->length()) {
      for (int i = 0; i < nLinks; i++) {
        if (linkFresh(*links[i], now) && links[i]->coreId.length() && links[i]->coreId != *ci) {
          rsp(link, "HI", false, bp::ERR_BUSY, id);
          return;
        }
      }
    }
    if (!owner::onHi(link, m)) {
      rsp(link, "HI", false, bp::ERR_BAD_FIELD, id);
      return;
    }
    link.coreId = ci ? *ci : String();
    pickActive();   // this link is no longer authenticated until AUTH arrives
    sendHello(link);
    return;
  }
  if (t == bp::MSG_PING) {
    rsp(link, "PING", true, nullptr, id);
    return;
  }
  if (t == bp::MSG_TIME) {
    // An owned node takes its clock from its authenticated owner only.
    if (st.owned && !link.authed) return;
    clk::setFromCore((uint32_t)m.getInt("u", 0), (uint16_t)(m.getInt("ms", 0) % 1000));
    return;
  }
  if (t == bp::MSG_IDENT) {
    long d = m.getInt("d", 10);
    identify(d > 0 ? (uint32_t)d : 0);
    rsp(link, "IDENT", true, nullptr, id);
    return;
  }
  if (t == bp::MSG_ADOPT) {
    const char* err = owner::onAdopt(link, m);
    if (!err) {
      console::say("adopted as node " + String(st.number) + ", owner " + owner::id());
      wirelessIdentity();
    }
    rsp(link, "ADOPT", err == nullptr, err, id);
    return;
  }
  if (t == bp::MSG_AUTH) {
    bool ok = owner::onAuth(link, m);
    pickActive();
    if (ok) heartbeat(link);
    else if (st.owned) console::say("core proof failed: not my owner");
    return;
  }

  if (!link.authed) {
    rsp(link, m.type, false, bp::ERR_NOT_AUTHED, id);
    return;
  }

  if (t == bp::MSG_ACK) {
    q.ack((uint32_t)m.getInt("q", 0));
    return;   // no RSP for ACK
  }
  if (t == bp::MSG_OTA) {
    ota::handle(link, m, id);
    return;
  }
  if (t == bp::MSG_ETX) {
    // Bridge only: a frame for one of the nodes. No answer line: the core sends many of these.
    const String *to = m.get("to"), *d = m.get("d");
    uint8_t mac[6], frame[bp::FRAME_MAX_BYTES];
    size_t n = 0;
    if (!isBridge()) rsp(link, "ETX", false, bp::ERR_ROLE, id);
    else if (to && d && crypto::fromHex(*to, mac, 6) && codec::b64urlDecode(*d, frame, sizeof(frame), n))
      bridge::hold(mac, frame, n, now);
    else badLineCount++;
    return;
  }
  if (t == bp::MSG_NUM) {
    long n = m.getInt("n", 0);
    if (n <= 0 || n > 65535) {
      rsp(link, "NUM", false, bp::ERR_BAD_FIELD, id);
      return;
    }
    st.number = (uint16_t)n;
    store::saveNumber(st.number);
    wirelessIdentity();
    rsp(link, "NUM", true, nullptr, id);
  } else if (t == bp::MSG_PLAN) {
    const String *f = m.get("f"), *r = m.get("r"), *c = m.get("c"), *d = m.get("d");
    radio::Plan p;
    if (!f || !r || !c || !radio::parsePlan(*f, *r, *c, d ? *d : String(), p)) {
      rsp(link, "PLAN", false, bp::ERR_BAD_PLAN, id);
      return;
    }
    plan = p;
    planOk = true;
    radio::setPlan(plan);
    store::savePlan(radio::toStore(plan));
    applyRun();
    rsp(link, "PLAN", true, nullptr, id);
  } else if (t == bp::MSG_RUN) {
    st.runMask = (uint8_t)m.getInt("m", 0) & (bp::RUN_WIFI | bp::RUN_BLE);
    st.session = (uint32_t)m.getInt("s", st.session);
    store::saveRun(st.runMask, st.session);
    applyRun();
    if (!st.runMask) q.flush();   // save everything when scanning stops
    rsp(link, "RUN", true, nullptr, id);
  } else if (t == bp::MSG_REPEAT) {
    st.repeatApMs = (uint32_t)m.getInt("ap", 0);
    st.repeatBleMs = (uint32_t)m.getInt("ble", 0);
    store::saveRepeat(st.repeatApMs, st.repeatBleMs);
    radio::setRepeat(st.repeatApMs, st.repeatBleMs);
    rsp(link, "REPEAT", true, nullptr, id);
  } else if (t == bp::MSG_ESPN) {
    long ch = m.getInt("ch", -1);
    if (ch < 0 || ch > 13) {
      rsp(link, "ESPN", false, bp::ERR_BAD_FIELD, id);
      return;
    }
    if ((uint8_t)ch != st.channel) {
      st.channel = (uint8_t)ch;
      store::saveChannel(st.channel);
      if (isBridge()) bridge::setChannel(st.channel);
      else espLink.setChannel(st.channel);
    }
    rsp(link, "ESPN", true, nullptr, id);
  } else if (t == bp::MSG_ROLE) {
    long r = m.getInt("r", -1);
    if (!link.allowsAdoption()) {
      rsp(link, "ROLE", false, bp::ERR_NOT_USB, id);
      return;
    }
    if (r != bp::ROLE_NODE && r != bp::ROLE_BRIDGE) {
      rsp(link, "ROLE", false, bp::ERR_BAD_FIELD, id);
      return;
    }
    rsp(link, "ROLE", true, nullptr, id);
    if ((uint8_t)r != st.role) {
      st.role = (uint8_t)r;
      store::saveRole(st.role);
      console::say(String("role is now ") + (isBridge() ? "bridge" : "node") + ": restarting");
      restartAt = now + 500;
    }
    return;
  } else if (t == bp::MSG_WATCH) {
    const String* mac = m.get("m");
    uint8_t raw[6];
    long d = m.getInt("d", 0);
    if (isBridge() || !mac || !crypto::fromHex(*mac, raw, 6) || d < 0) {
      rsp(link, "WATCH", false, isBridge() ? bp::ERR_ROLE : bp::ERR_BAD_FIELD, id);
      return;
    }
    setWatch(raw, (uint32_t)d);
    rsp(link, "WATCH", true, nullptr, id);
  } else if (t == bp::MSG_FORGET) {
    rsp(link, "FORGET", true, nullptr, id);
    forgetEverything("core asked");
    return;
  } else {
    rsp(link, m.type, false, bp::ERR_UNKNOWN, id);
    return;
  }
  heartbeat(link);
}

// ------------------------------------------------------------------------------------ LED
static void ledWrite(bool on) {
#if BRISTLE_LED_RGB
  rgbLedWrite(BRISTLE_LED_PIN, 0, on ? 40 : 0, on ? 40 : 0);
#else
  digitalWrite(BRISTLE_LED_PIN, (on ^ BRISTLE_LED_ACTIVE_LOW) ? HIGH : LOW);
#endif
}

static void ledTick(uint32_t now) {
  static bool lit = false;
  bool want = identUntil && now < identUntil && ((now / 150) % 2 == 0);
  if (want != lit) {
    ledWrite(want);
    lit = want;
  }
}

// ------------------------------------------------------------------------------------ setup/loop
void begin() {
#if !BRISTLE_LED_RGB
  pinMode(BRISTLE_LED_PIN, OUTPUT);
#endif
  ledWrite(false);

  esp_efuse_mac_get_default(hw);
  hwHex = crypto::toHex(hw, 6);
  otaSlots = esp_ota_get_next_update_partition(nullptr) != nullptr;

  store::begin();
  store::load(st);
  clk::begin();
  owner::begin(st, hw);
  ota::begin();
  q.begin();
  lastAckSeen = sendCursor = q.ackSeq();

  if (isBridge()) {
    bridge::begin(st.channel);
    return;                       // a bridge relays: no scanning, no wireless link of its own
  }
  radio::begin(onWifi, onBle);
  radio::setRepeat(st.repeatApMs, st.repeatBleMs);
  if (st.plan.length() && radio::fromStore(st.plan, plan)) {
    planOk = true;
    radio::setPlan(plan);
  }
  espLink.begin(st.key, st.owned, st.channel);
  addLink(espLink);
  bleLink.begin(hw, name(), st.owned);
  addLink(bleLink);
  radio::setLinkBusy([]() { return bleLink.connected(); });
  applyRun();   // a node that rebooted mid-drive keeps scanning
}

// A Bristle board that no core has spoken to says so once a second on USB, so a program on the
// other end can tell it from any other ESP32 without writing a byte to the port.
static void beacon(uint32_t now) {
  if (!usb || usb->helloSeen || now - lastBeaconMs < bp::BEACON_MS) return;
  if (consoleQuietUntil && (int32_t)(now - consoleQuietUntil) < 0) return;
  lastBeaconMs = now;
  codec::LineOut l(bp::MSG_BCN);
  l.addRaw("hw", hwHex).add("bd", BRISTLE_BOARD).addu("b", BRISTLE_BUILD).addu("o", st.owned ? 1 : 0).addu("ro", st.role);
  usb->send(l.finish());
}

void tick(uint32_t now) {
  if (restartAt && (int32_t)(now - restartAt) >= 0) {
    q.flush();
    delay(50);
    ESP.restart();
  }
  if (isBridge()) {
    if (usb) bridge::tick(now, *usb);
  } else {
    // The ESP-NOW visit happens between two scans, before the next one is started.
    if (espLink.visitDue(now) && radio::canVisit()) espLink.visit(now);
    bleLink.poll(now);
    radio::tick(now);
  }
  q.tick(now);
  streamFinds(now);
  bool ownerAuthed = false;
  for (int i = 0; i < nLinks; i++) {
    Link& l = *links[i];
    ownerAuthed |= l.authed;
    if (!l.helloSeen) continue;
    // The core pings an idle link. Silence this long means the cable or the core is gone.
    if (now - l.lastRxMs > bp::LINK_TIMEOUT_MS) {
      bool was = l.authed;
      l.drop();
      linkClosed(l);
      if (was) console::say("core went quiet: link closed, finds are being kept");
      continue;
    }
    if (l.authed && now - l.lastHbMs >= bp::HEARTBEAT_MS) heartbeat(l);
    if (!clk::setSinceBoot() && now - l.lastTreqMs >= bp::TIME_REQUEST_MS) {
      l.send(codec::LineOut(bp::MSG_TREQ).finish());
      l.lastTreqMs = now;
    }
  }
  ota::tick(now, st.owned, ownerAuthed);
  beacon(now);
  ledTick(now);
  if (owner::bootHeld(now)) forgetEverything("BOOT held");
}

}  // namespace node
