// Scanning: Wi-Fi on the node's channel plan (2.4 + 5 GHz), BLE bursts between Wi-Fi sweeps (one
// radio), repeat suppression. Finds are handed to callbacks; the engine queues them.
//
// One radio, several users. The scheduler below gives it out in turns:
//   WIFI_SCANNING  one channel dwell of the plan
//   BLE_BURST      a Bluetooth LE scan burst (after each sweep of the plan, or all the time for a
//                  node that only does Bluetooth, or while a device is being watched for a hunt)
//   LINK_SLOT      nothing is started: the Bluetooth link to the core gets the radio (only while
//                  a core is connected over Bluetooth)
//   IDLE           between two of the above; this is when the ESP-NOW link makes its visit to the
//                  control channel (Node.cpp asks canVisit()).
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
  int16_t mfgType;    // first byte after the company id, -1 if there is none
  uint16_t services[3];   // 16-bit service UUIDs (advertised, or carrying service data)
  uint8_t nServices;
  bool bristle;       // an advertisement of a Bristle board
  uint8_t bristleHw[6];
  bool bristleOwned;
};

namespace radio {

constexpr int MAX_PLAN = 64;

struct Plan {
  uint8_t flags = 0;              // bp::PLAN_FLAG_WIFI | bp::PLAN_FLAG_BLE | bp::PLAN_FLAG_BLE_ACTIVE
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

void bleUp();                         // start the Bluetooth stack (once); the Bluetooth link needs it too
bool canVisit();                      // true between two scans: the ESP-NOW link may use the radio now
void setLinkBusy(std::function<bool()> connected);   // is a core connected over Bluetooth?

// Hunt mode: for `seconds` listen for Bluetooth LE only and call onSeen(rssi) for every
// advertisement from this MAC (at most every WATCH_REPORT_MS). Nothing is sent to that device.
void watch(const uint8_t mac[6], uint32_t seconds, std::function<void(int8_t)> onSeen);
bool watching();
uint32_t bleLost();                   // advertisements dropped because loop was behind

}  // namespace radio
