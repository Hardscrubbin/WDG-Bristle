#include "Console.h"

#include <esp_ota_ops.h>
#include <esp_partition.h>

#include "Boards.h"
#include "BristleProto.h"
#include "Clock.h"
#include "Codec.h"
#include "Crypto.h"
#include "Link_Usb.h"
#include "Node.h"
#include "Owner.h"
#include "Radio.h"

namespace console {

void say(const String& text) {
  String line = "# " + text + "\n";
  if ((size_t)Serial.availableForWrite() >= line.length()) Serial.print(line);   // never wait for the port
}

// The firmware expects partitions_bristle_8mb.csv. A board flashed some other way (app image only
// onto an old layout, or built from the IDE) still runs, but with the wrong slot or queue size.
static bool layoutOk(uint32_t& appBytes, uint32_t& queueBytes) {
  const esp_partition_t* app = esp_ota_get_running_partition();
  const esp_partition_t* fs =
      esp_partition_find_first(ESP_PARTITION_TYPE_DATA, ESP_PARTITION_SUBTYPE_DATA_SPIFFS, nullptr);
  appBytes = app ? app->size : 0;
  queueBytes = fs ? fs->size : 0;
  return appBytes == bp::FLASH_APP_SLOT_BYTES && queueBytes == bp::FLASH_QUEUE_BYTES &&
         esp_ota_get_next_update_partition(nullptr) != nullptr;
}

static String layoutText() {
  uint32_t app, fs;
  bool ok = layoutOk(app, fs);
  String s = String("flash layout ") + (ok ? "ok" : "WRONG") + " (app slot " + String(app / 1024) + " KB, queue " +
             String(fs / 1024) + " KB)";
  if (!ok) s += ": flash the FULL image at 0x0 to fix it";
  return s;
}

static String clockText() {
  if (!clk::valid()) return "not set (finds are stored by uptime until the core sets it)";
  uint32_t sec;
  uint16_t ms;
  clk::now(sec, ms);
  return String(sec) + (clk::setSinceBoot() ? " (set by the core)" : " (kept across the reset)");
}

void banner() {
  uint32_t app, fs;
  const NodeState& st = node::state();
  say(String(bp::PRODUCT_NAME) + " node build " + BRISTLE_BUILD + " (" + BRISTLE_BOARD + "), hw " + node::hwidHex() +
      (st.owned ? ", node " + String(st.number) + ", owner " + owner::id() : String(", no owner yet")) +
      (node::queue().flashOk() ? "" : ", WARNING: flash queue unavailable") +
      (layoutOk(app, fs) ? "" : ", WARNING: wrong flash layout (type status)"));
}

bool selftest() {
  bool ok = true;
  auto check = [&](const char* what, const String& got, const char* want) {
    bool pass = got == want;
    say(String(pass ? "PASS " : "FAIL ") + what + " got " + got + (pass ? "" : String(" want ") + want));
    ok &= pass;
  };
  {
    const char* in = bp::VEC_CRC16_0_INPUT;
    char b[8];
    snprintf(b, sizeof(b), "%04X", codec::crc16((const uint8_t*)in, strlen(in)));
    check("crc16", b, bp::VEC_CRC16_0_EXPECT);
  }
  check("encode", codec::pctEncode(bp::VEC_ENCODE_0_INPUT), bp::VEC_ENCODE_0_EXPECT);
  check("encode2", codec::pctEncode(bp::VEC_ENCODE_1_INPUT), bp::VEC_ENCODE_1_EXPECT);
  {
    radio::Plan p;
    bool parsed = radio::parsePlan(String(bp::VEC_PLAN_HASH_0_F), bp::VEC_PLAN_HASH_0_R, bp::VEC_PLAN_HASH_0_C,
                                   String(), p);
    check("plan_hash", parsed ? radio::planHash(p) : String("parse-error"), bp::VEC_PLAN_HASH_0_EXPECT);
    // The same plan sent the short way (the dwell once, as d=) must give the same hash.
    parsed = radio::parsePlan(String(bp::VEC_PLAN_HASH_0_F), bp::VEC_PLAN_HASH_0_R, "1/6/11", "120", p);
    check("plan_short", parsed ? radio::planHash(p) : String("parse-error"), bp::VEC_PLAN_HASH_0_EXPECT);
  }
  uint8_t key[16];
  crypto::fromHex(bp::VEC_OWNER_ID_0_KEY, key, 16);
  check("owner_id", crypto::ownerId(key), bp::VEC_OWNER_ID_0_EXPECT);
  {
    uint8_t cn[16], hw[6], out16[16];
    crypto::fromHex(bp::VEC_NODE_PROOF_0_CN, cn, 16);
    crypto::fromHex(bp::VEC_NODE_PROOF_0_HW, hw, 6);
    crypto::nodeProof(key, cn, hw, out16);
    check("node_proof", crypto::toHex(out16, 16), bp::VEC_NODE_PROOF_0_EXPECT);
    uint8_t nn[16];
    crypto::fromHex(bp::VEC_CORE_PROOF_0_NN, nn, 16);
    crypto::coreProof(key, nn, hw, out16);
    check("core_proof", crypto::toHex(out16, 16), bp::VEC_CORE_PROOF_0_EXPECT);
  }
  {
    uint8_t fr[64];
    const char* pl = bp::VEC_FRAME_0_PAYLOAD;
    size_t n = crypto::frameBuild(key, bp::VEC_FRAME_0_KIND, bp::VEC_FRAME_0_CTR, bp::VEC_FRAME_0_FRAG,
                                  (const uint8_t*)pl, strlen(pl), fr, sizeof(fr));
    check("frame", crypto::toHex(fr, n), bp::VEC_FRAME_0_EXPECT);
  }
  {   // codec round trip through the parser
    codec::LineOut l(bp::MSG_AP);
    l.add("e", String("Caf\xC3\xA9 ,=*:%")).addu("q", 1);
    String line = l.finish();
    codec::Msg m;
    bool parsed = codec::parseLine(line.c_str(), line.length(), m);
    const String* e = m.get("e");
    check("roundtrip", parsed && e ? *e : String("parse-error"), "Caf\xC3\xA9 ,=*:%");
  }
  say(ok ? "SELFTEST PASS" : "SELFTEST FAIL");
  return ok;
}

static long argSeconds(const String& cmd, long dflt) {
  int space = cmd.indexOf(' ');
  if (space < 0) return dflt;
  long v = cmd.substring(space + 1).toInt();
  return v > 0 ? v : dflt;
}

void run(const String& raw) {
  String cmd = raw;
  cmd.trim();
  const NodeState& st = node::state();
  FindQueue& q = node::queue();
  if (cmd == "help") {
    say("commands: status, queue, owner, forget, identify <s>, boot, selftest, version");
  } else if (cmd == "status") {
    Link* link = node::activeLink();
    String plan = node::planText();
    say(node::name() + " board " + BRISTLE_BOARD + " build " + BRISTLE_BUILD + " hw " + node::hwidHex() + " boot " +
        String(clk::bootId()));
    say(String("owned ") + st.owned + " link " + (link ? String(link->type()) : String("none")) + " run " +
        st.runMask + " session " + st.session + " plan " + (plan.length() ? plan : String("none")));
    say("clock " + clockText());
    say(layoutText());
    say("bad lines in " + String(node::badLines()) + ", lines not sent (port full) " + String(usbLink.unsent()) +
        ", free RAM " + String(ESP.getFreeHeap() / 1024) + " KB");
  } else if (cmd == "queue") {
    say("holding " + String(q.backlog()) + " finds, next seq " + String(q.nextSeq()) + ", oldest held " +
        String(q.ackSeq()));
    say("dropped " + String(q.dropped()) + " (" + String(q.untimed()) + " of them had no clock)");
    say(String("flash ") + (q.flashOk() ? "" : "UNAVAILABLE ") + String(q.usedBytes() / 1024) + " of " +
        String(q.capacityBytes() / 1024) + " KB used (" + String(q.fillPct()) + "%), " + String(q.segments()) +
        " segment file(s)");
  } else if (cmd == "owner") {
    say(st.owned ? "owner " + owner::id() : String("no owner"));
  } else if (cmd == "forget") {
    node::forgetEverything("console");
  } else if (cmd.startsWith("ident")) {   // ident / identify, with an optional number of seconds
    long d = argSeconds(cmd, 10);
    node::identify((uint32_t)d);
    say("identifying for " + String(d) + " s");
  } else if (cmd == "boot") {
    say("BOOT pin " + String(BRISTLE_BOOT_PIN) + " reads " + String(digitalRead(BRISTLE_BOOT_PIN)) +
        " (0 = pressed). Hold BOOT and run `boot` again to confirm the pin.");
  } else if (cmd == "selftest") {
    selftest();
  } else if (cmd == "version") {
    say(String(bp::PRODUCT_NAME) + " node build " + BRISTLE_BUILD + ", protocol " + bp::PROTOCOL_VERSION);
  } else if (cmd.length()) {
    say("unknown command; try help");
  }
}

}  // namespace console
