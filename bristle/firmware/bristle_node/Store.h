// Settings kept in NVS across reboots and reflashes (unless the flash is erased).
#pragma once
#include <Arduino.h>

struct NodeState {
  bool owned = false;
  uint8_t key[16] = {0};
  uint16_t number = 0;
  String plan;             // PLAN fields as sent: "f=..,r=..,c=.." canonical pieces
  uint8_t runMask = 0;
  uint32_t session = 0;
  uint32_t repeatApMs = 0;
  uint32_t repeatBleMs = 0;
};

namespace store {

void begin();
void load(NodeState& s);
void saveOwner(const NodeState& s);
void saveNumber(uint16_t n);
void savePlan(const String& plan);
void saveRun(uint8_t mask, uint32_t session);
void saveRepeat(uint32_t ap, uint32_t ble);
void forgetAll();                       // owner, number, plan, run (queue cleared separately)

// Sequence numbers are reserved in blocks so a reboot never reuses one.
uint32_t reserveSeqBlock(uint32_t blockSize);   // returns the first seq of a fresh block

// Every boot gets the next boot number (finds captured before the clock is set are stored with it).
uint16_t nextBootId();

// For a boot that stored finds by uptime: unix time (ms) at that boot's uptime 0.
struct __attribute__((packed)) TimeBase {
  uint16_t boot;
  int64_t offsetMs;
};
size_t loadBases(TimeBase* out, size_t cap);
void saveBases(const TimeBase* in, size_t n);

}  // namespace store
