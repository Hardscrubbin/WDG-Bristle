// Scanning: Wi-Fi on the node's channel plan (2.4 + 5 GHz), BLE bursts between Wi-Fi sweeps (one
// radio), repeat suppression. Finds are handed to callbacks; the engine queues them.
#pragma once
#include <Arduino.h>
#include <functional>

struct WifiFind {
  uint8_t bssid[6];
  String ssid;
  uint8_t channel;
  int8_t rssi;
  uint8_t security;   // bp::Security
};

struct BleFind {
  uint8_t mac[6];     // printed order
  uint8_t addrType;   // 0 public, 1 random
  String name;
  int8_t rssi;
  int32_t companyId;  // -1 if no manufacturer data
};

namespace radio {

constexpr int MAX_PLAN = 64;

struct Plan {
  uint8_t flags = 0;              // bp::PLAN_FLAG_WIFI | bp::PLAN_FLAG_BLE
  char region = 'U';
  uint8_t n = 0;
  uint8_t ch[MAX_PLAN];
  uint16_t dwell[MAX_PLAN];
};

// Parses PLAN fields (f, r, c and the optional default dwell d: "" if it was not sent). Returns
// false if invalid (unknown channel for the region, bad dwell, an entry with no dwell and no d...).
bool parsePlan(const String& f, const String& r, const String& c, const String& d, Plan& out);
// Canonical text and FNV-1a 32 hash, exactly as core/bristle/plan.py.
String canonical(const Plan& p);
String planHash(const Plan& p);
// Serialise for NVS (same as the PLAN fields) and back.
String toStore(const Plan& p);
bool fromStore(const String& s, Plan& out);

void begin(std::function<void(const WifiFind&)> onWifi, std::function<void(const BleFind&)> onBle);
void setPlan(const Plan& p);
void setRun(uint8_t runMask);         // bp::RUN_WIFI | bp::RUN_BLE, 0 = stop
void setRepeat(uint32_t apMs, uint32_t bleMs);
void tick(uint32_t nowMs);
bool running();
bool cap5g();

}  // namespace radio
