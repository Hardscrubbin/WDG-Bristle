#include "Store.h"

#include <Preferences.h>

static Preferences prefs;

namespace store {

void begin() { prefs.begin("bristle", false); }

void load(NodeState& s) {
  s.owned = prefs.getBytesLength("okey") == 16 && prefs.getBytes("okey", s.key, 16) == 16;
  if (!s.owned) memset(s.key, 0, sizeof(s.key));
  s.number = prefs.getUShort("num", 0);
  s.plan = prefs.getString("plan", "");
  s.runMask = prefs.getUChar("run", 0);
  s.session = prefs.getULong("sess", 0);
  s.repeatApMs = prefs.getULong("rap", 0);
  s.repeatBleMs = prefs.getULong("rble", 0);
}

void saveOwner(const NodeState& s) {
  if (s.owned) prefs.putBytes("okey", s.key, 16);
  else prefs.remove("okey");
}
void saveNumber(uint16_t n) { prefs.putUShort("num", n); }
void savePlan(const String& plan) { prefs.putString("plan", plan); }
void saveRun(uint8_t mask, uint32_t session) {
  prefs.putUChar("run", mask);
  prefs.putULong("sess", session);
}
void saveRepeat(uint32_t ap, uint32_t ble) {
  prefs.putULong("rap", ap);
  prefs.putULong("rble", ble);
}

void forgetAll() {
  // The sequence reservation and the boot number stay: they must never go backwards.
  prefs.remove("okey");
  prefs.remove("num");
  prefs.remove("plan");
  prefs.remove("run");
  prefs.remove("sess");
  prefs.remove("bases");
}

uint32_t reserveSeqBlock(uint32_t blockSize) {
  uint32_t next = prefs.getULong("seqres", 0);
  prefs.putULong("seqres", next + blockSize);
  return next;
}

uint16_t nextBootId() {
  uint16_t boot = (uint16_t)(prefs.getUShort("boot", 0) + 1);
  prefs.putUShort("boot", boot);
  return boot;
}

size_t loadBases(TimeBase* out, size_t cap) {
  size_t bytes = prefs.getBytesLength("bases");
  if (bytes == 0 || bytes % sizeof(TimeBase) != 0 || bytes > cap * sizeof(TimeBase)) return 0;
  if (prefs.getBytes("bases", out, bytes) != bytes) return 0;
  return bytes / sizeof(TimeBase);
}

void saveBases(const TimeBase* in, size_t n) {
  if (n) prefs.putBytes("bases", in, n * sizeof(TimeBase));
  else prefs.remove("bases");
}

}  // namespace store
