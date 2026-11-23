#include "Node.h"

#include <esp_mac.h>
#include <esp_ota_ops.h>

#include "Boards.h"
#include "BristleProto.h"
#include "Clock.h"
#include "Console.h"
#include "Crypto.h"
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

// delivery
static uint32_t sendCursor = 0, lastAckSeen = 0, lastAckMoveMs = 0;
static uint32_t badLineCount = 0;
static uint32_t identUntil = 0;

const NodeState& state() { return st; }
FindQueue& queue() { return q; }
const String& hwidHex() { return hwHex; }
uint32_t badLines() { return badLineCount; }
Link* activeLink() { return active; }
String planText() { return planOk ? radio::canonical(plan) : String(); }
String name() { return String(bp::NAME_PREFIX) + "-" + (st.number ? String(st.number) : String("NEW")); }
void badLine() { badLineCount++; }
void addLink(Link& link) {
  if (nLinks < MAX_LINKS) links[nLinks++] = &link;
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
}

static void sendHello(Link& link) {
  codec::LineOut l(bp::MSG_HELLO);
  l.addu("v", bp::PROTOCOL_VERSION).addRaw("hw", hwHex).add("bd", BRISTLE_BOARD).addu("b", BRISTLE_BUILD);
  l.addu("cap", flagsFor(link));
  owner::fillHello(link, l);
  link.send(l.finish());
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
}

static void applyRun() { radio::setRun(planOk ? st.runMask : 0); }

void identify(uint32_t seconds) { identUntil = seconds ? millis() + seconds * 1000 : 0; }

void forgetEverything(const char* why) {
  st = NodeState();
  store::forgetAll();
  q.clear();
  clk::forget();
  planOk = false;
  for (int i = 0; i < nLinks; i++) links[i]->authed = false;
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
  if (f.name.length()) l.add("nm", f.name.substring(0, 29));
  if (f.companyId >= 0) l.addu("mf", (unsigned long)f.companyId);
  captured(l.body());
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
  char tail[8];
  snprintf(tail, sizeof(tail), "*%04X\n", codec::crc16((const uint8_t*)line.c_str(), line.length()));
  line += tail;
  return line;
}

static void streamFinds(uint32_t now) {
  if (q.ackSeq() != lastAckSeen) {
    lastAckSeen = q.ackSeq();
    lastAckMoveMs = now;
  }
  if (!active || !active->authed || !clk::valid()) return;
  if (sendCursor < q.ackSeq()) sendCursor = q.ackSeq();
  for (int i = 0; i < 8; i++) {
    if (active->room() < bp::LINE_MAX_BYTES + 16) return;   // room for the longest line, or wait
    FindQueue::Rec r;
    if (!q.peek(sendCursor, r)) break;
    sendCursor = r.seq + 1;
    uint32_t sec;
    uint16_t ms;
    // No time for it (stored by uptime in a boot that never got a clock): never sent. The queue
    // drops and counts it when the ack point gets there.
    if (!clk::resolve(r.rel, r.boot, r.sec, r.ms, sec, ms)) continue;
    active->send(findLine(r, sec, ms));
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

  if (t == bp::MSG_HI) {
    if (!owner::onHi(link, m)) {
      rsp(link, "HI", false, bp::ERR_BAD_FIELD, id);
      return;
    }
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
    if (!err) console::say("adopted as node " + String(st.number) + ", owner " + owner::id());
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
  if (t == bp::MSG_NUM) {
    long n = m.getInt("n", 0);
    if (n <= 0 || n > 65535) {
      rsp(link, "NUM", false, bp::ERR_BAD_FIELD, id);
      return;
    }
    st.number = (uint16_t)n;
    store::saveNumber(st.number);
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
  } else if (t == bp::MSG_FORGET) {
    rsp(link, "FORGET", true, nullptr, id);
    forgetEverything("core asked");
    return;
  } else if (t == bp::MSG_OTA) {
    rsp(link, "OTA", false, "not in this build yet", id);   // docs/PLAN.md: OTA step
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
  q.begin();
  lastAckSeen = sendCursor = q.ackSeq();

  radio::begin(onWifi, onBle);
  radio::setRepeat(st.repeatApMs, st.repeatBleMs);
  if (st.plan.length() && radio::fromStore(st.plan, plan)) {
    planOk = true;
    radio::setPlan(plan);
  }
  applyRun();   // a node that rebooted mid-drive keeps scanning
}

void tick(uint32_t now) {
  radio::tick(now);
  q.tick(now);
  streamFinds(now);
  for (int i = 0; i < nLinks; i++) {
    Link& l = *links[i];
    if (!l.helloSeen) continue;
    // The core pings an idle link. Silence this long means the cable or the core is gone.
    if (now - l.lastRxMs > bp::LINK_TIMEOUT_MS) {
      bool was = l.authed;
      l.drop();
      pickActive();
      if (was) console::say("core went quiet: link closed, finds are being kept");
      continue;
    }
    if (l.authed && now - l.lastHbMs >= bp::HEARTBEAT_MS) heartbeat(l);
    if (!clk::setSinceBoot() && now - l.lastTreqMs >= bp::TIME_REQUEST_MS) {
      l.send(codec::LineOut(bp::MSG_TREQ).finish());
      l.lastTreqMs = now;
    }
  }
  ledTick(now);
  if (owner::bootHeld(now)) forgetEverything("BOOT held");
}

}  // namespace node
