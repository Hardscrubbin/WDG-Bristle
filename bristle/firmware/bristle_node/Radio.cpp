#include "Radio.h"

#include <NimBLEDevice.h>
#include <WiFi.h>
#include <esp_wifi.h>

#include "BristleProto.h"

namespace radio {

static std::function<void(const WifiFind&)> g_onWifi;
static std::function<void(const BleFind&)> g_onBle;
static Plan g_plan;
static uint8_t g_run = 0;
static uint32_t g_repAp = bp::REPEAT_AP_MS, g_repBle = bp::REPEAT_BLE_MS;

enum Phase { IDLE, WIFI_SCANNING, BLE_BURST };
static Phase g_phase = IDLE;
static uint8_t g_idx = 0;            // next plan entry to scan
static uint32_t g_phaseStart = 0;
static const uint32_t BLE_BURST_MS = 1200;
static bool g_bleUp = false;

// ---------------------------------------------------------------- repeat suppression
static const int SEEN = 1024;
static uint32_t seenKey[SEEN], seenAt[SEEN];

static uint32_t keyOf(uint8_t kind, const uint8_t mac[6]) {
  uint32_t h = 2166136261u ^ kind;
  for (int i = 0; i < 6; i++) h = (h ^ mac[i]) * 16777619u;
  return h ? h : 1;
}

static bool fresh(uint8_t kind, const uint8_t mac[6], uint32_t windowMs) {
  uint32_t k = keyOf(kind, mac), now = millis();
  int slot = k % SEEN;
  if (seenKey[slot] == k && now - seenAt[slot] < windowMs) return false;
  seenKey[slot] = k;
  seenAt[slot] = now;
  return true;
}

// ---------------------------------------------------------------- plan text
static const uint8_t* regionCh24(char r, size_t& n) {
  if (r == 'E') { n = sizeof(bp::REGION_E_CH24); return bp::REGION_E_CH24; }
  n = sizeof(bp::REGION_U_CH24);
  return bp::REGION_U_CH24;
}
static const uint8_t* regionCh5(char r, size_t& n) {
  if (r == 'E') { n = sizeof(bp::REGION_E_CH5); return bp::REGION_E_CH5; }
  n = sizeof(bp::REGION_U_CH5);
  return bp::REGION_U_CH5;
}
static bool allowed(char r, uint8_t ch) {
  size_t n;
  const uint8_t* a = regionCh24(r, n);
  for (size_t i = 0; i < n; i++) if (a[i] == ch) return true;
  a = regionCh5(r, n);
  for (size_t i = 0; i < n; i++) if (a[i] == ch) return true;
  return false;
}

bool parsePlan(const String& f, const String& r, const String& c, const String& d, Plan& out) {
  Plan p;
  p.flags = (uint8_t)f.toInt();
  if (r != "U" && r != "E") return false;
  p.region = r[0];
  int i = 0;
  while (i < (int)c.length()) {
    int slash = c.indexOf('/', i);
    if (slash < 0) slash = c.length();
    String item = c.substring(i, slash);
    int dot = item.indexOf('.');
    if (dot == 0 || item.length() == 0 || p.n >= MAX_PLAN) return false;
    if (dot < 0 && d.length() == 0) return false;   // no dwell of its own and no default
    long ch = (dot < 0 ? item : item.substring(0, dot)).toInt();
    long dw = (dot < 0 ? d : item.substring(dot + 1)).toInt();
    if (!allowed(p.region, (uint8_t)ch) || dw < bp::DWELL_MIN_MS || dw > bp::DWELL_MAX_MS) return false;
    p.ch[p.n] = (uint8_t)ch;
    p.dwell[p.n] = (uint16_t)dw;
    p.n++;
    i = slash + 1;
  }
  out = p;
  return true;
}

static String channelsText(const Plan& p) {
  String s;
  for (int i = 0; i < p.n; i++) {
    if (i) s += '/';
    s += String(p.ch[i]);
    s += '.';
    s += String(p.dwell[i]);
  }
  return s;
}

String canonical(const Plan& p) {
  return "f=" + String(p.flags) + ";r=" + String(p.region) + ";c=" + channelsText(p);
}

String planHash(const Plan& p) {
  String c = canonical(p);
  uint32_t h = 0x811C9DC5;
  for (size_t i = 0; i < c.length(); i++) {
    h ^= (uint8_t)c[i];
    h *= 0x01000193;
  }
  char buf[9];
  snprintf(buf, sizeof(buf), "%08lx", (unsigned long)h);
  return String(buf);
}

String toStore(const Plan& p) { return String(p.flags) + "|" + String(p.region) + "|" + channelsText(p); }

bool fromStore(const String& s, Plan& out) {
  int a = s.indexOf('|'), b = s.indexOf('|', a + 1);
  if (a < 0 || b < 0) return false;
  return parsePlan(s.substring(0, a), s.substring(a + 1, b), s.substring(b + 1), String(), out);
}

// ---------------------------------------------------------------- security mapping
static uint8_t secOf(wifi_auth_mode_t m) {
  switch (m) {
    case WIFI_AUTH_OPEN: return bp::SEC_OPEN;
    case WIFI_AUTH_WEP: return bp::SEC_WEP;
    case WIFI_AUTH_WPA_PSK: return bp::SEC_WPA_PSK;
    case WIFI_AUTH_WPA2_PSK: return bp::SEC_WPA2_PSK;
    case WIFI_AUTH_WPA_WPA2_PSK: return bp::SEC_WPA_WPA2_PSK;
    case WIFI_AUTH_WPA2_ENTERPRISE: return bp::SEC_WPA2_ENTERPRISE;
    case WIFI_AUTH_WPA3_PSK: return bp::SEC_WPA3_PSK;
    case WIFI_AUTH_WPA2_WPA3_PSK: return bp::SEC_WPA2_WPA3_PSK;
    case WIFI_AUTH_WAPI_PSK: return bp::SEC_WAPI;
    case WIFI_AUTH_OWE: return bp::SEC_OWE;
#ifdef WIFI_AUTH_WPA3_ENTERPRISE
    case WIFI_AUTH_WPA3_ENTERPRISE: return bp::SEC_WPA3_ENTERPRISE;
#endif
    default: return bp::SEC_UNKNOWN;
  }
}

// ---------------------------------------------------------------- BLE
class ScanCB : public NimBLEScanCallbacks {
  void onResult(const NimBLEAdvertisedDevice* d) override {
    BleFind f;
    const uint8_t* v = d->getAddress().getVal();      // NimBLE stores little-endian
    for (int i = 0; i < 6; i++) f.mac[i] = v[5 - i];
    if (!fresh(2, f.mac, g_repBle)) return;
    f.addrType = d->getAddress().getType() == BLE_ADDR_PUBLIC ? 0 : 1;
    f.name = d->haveName() ? String(d->getName().c_str()) : String();
    f.rssi = (int8_t)d->getRSSI();
    f.companyId = -1;
    if (d->haveManufacturerData()) {
      std::string md = d->getManufacturerData();
      if (md.size() >= 2) f.companyId = (uint8_t)md[0] | ((uint8_t)md[1] << 8);
    }
    if (g_onBle) g_onBle(f);
  }
};
static ScanCB g_scanCb;

static void bleEnsure() {
  if (g_bleUp) return;
  NimBLEDevice::init("");
  NimBLEScan* sc = NimBLEDevice::getScan();
  sc->setScanCallbacks(&g_scanCb, true);   // every advert; our own repeat window decides
  sc->setActiveScan(true);                 // scan responses carry many device names
  sc->setInterval(100);
  sc->setWindow(99);
  sc->setMaxResults(0);                    // don't keep results in RAM
  g_bleUp = true;
}

// ---------------------------------------------------------------- Wi-Fi
static bool wifiStarted = false;
static void wifiEnsure() {
  if (wifiStarted) return;
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  WiFi.setBandMode(WIFI_BAND_MODE_AUTO);   // 2.4 + 5 GHz on the C5
  wifiStarted = true;
}

static void applyCountry() {
  wifiEnsure();
  esp_wifi_set_country_code(g_plan.region == 'E' ? bp::REGION_E_COUNTRY : bp::REGION_U_COUNTRY, true);
}

static void harvestWifi(int16_t n) {
  for (int i = 0; i < n; i++) {
    WifiFind f;
    uint8_t* b = WiFi.BSSID(i);
    if (!b) continue;
    memcpy(f.bssid, b, 6);
    if (!fresh(1, f.bssid, g_repAp)) continue;
    f.ssid = WiFi.SSID(i);
    f.channel = (uint8_t)WiFi.channel(i);
    f.rssi = (int8_t)WiFi.RSSI(i);
    f.security = secOf(WiFi.encryptionType(i));
    if (g_onWifi) g_onWifi(f);
  }
  WiFi.scanDelete();
}

// ---------------------------------------------------------------- public
void begin(std::function<void(const WifiFind&)> onWifi, std::function<void(const BleFind&)> onBle) {
  g_onWifi = onWifi;
  g_onBle = onBle;
  memset(seenKey, 0, sizeof(seenKey));
  wifiEnsure();
}

void setPlan(const Plan& p) {
  g_plan = p;
  g_idx = 0;
  applyCountry();
}

void setRepeat(uint32_t apMs, uint32_t bleMs) {
  if (apMs) g_repAp = apMs;
  if (bleMs) g_repBle = bleMs;
}

void setRun(uint8_t mask) {
  g_run = mask;
  if (!mask) {
    if (g_phase == WIFI_SCANNING) WiFi.scanDelete();
    if (g_bleUp && NimBLEDevice::getScan()->isScanning()) NimBLEDevice::getScan()->stop();
    g_phase = IDLE;
  }
}

bool running() { return g_run != 0; }
bool cap5g() { return true; }   // every ESP32-C5 is dual-band

static bool wifiWanted() { return (g_run & bp::RUN_WIFI) && (g_plan.flags & bp::PLAN_FLAG_WIFI) && g_plan.n; }
static bool bleWanted() { return (g_run & bp::RUN_BLE) && (g_plan.flags & bp::PLAN_FLAG_BLE); }

void tick(uint32_t now) {
  if (!g_run) return;
  switch (g_phase) {
    case IDLE:
      if (wifiWanted()) {
        if (g_idx >= g_plan.n) g_idx = 0;
        uint8_t ch = g_plan.ch[g_idx];
        uint16_t dw = g_plan.dwell[g_idx];
        g_idx++;
        if (WiFi.scanNetworks(true, true, false, dw, ch) != WIFI_SCAN_FAILED) {
          g_phase = WIFI_SCANNING;
          g_phaseStart = now;
        }
      } else if (bleWanted()) {
        bleEnsure();
        NimBLEDevice::getScan()->start(BLE_BURST_MS, false, true);
        g_phase = BLE_BURST;
        g_phaseStart = now;
      }
      break;
    case WIFI_SCANNING: {
      int16_t r = WiFi.scanComplete();
      if (r == WIFI_SCAN_RUNNING && now - g_phaseStart < 3000) break;
      if (r >= 0) harvestWifi(r);
      else WiFi.scanDelete();
      g_phase = IDLE;
      // After a full sweep of the plan, give BLE a burst if it's in the plan too.
      if (g_idx >= g_plan.n && bleWanted()) {
        bleEnsure();
        NimBLEDevice::getScan()->start(BLE_BURST_MS, false, true);
        g_phase = BLE_BURST;
        g_phaseStart = now;
      }
      break;
    }
    case BLE_BURST:
      if (!NimBLEDevice::getScan()->isScanning() || now - g_phaseStart > BLE_BURST_MS + 500) {
        NimBLEDevice::getScan()->stop();
        g_phase = IDLE;
      }
      break;
  }
}

}  // namespace radio
