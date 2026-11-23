#include "Clock.h"

#include <esp_timer.h>
#include <sys/time.h>

#include "BristleProto.h"
#include "Store.h"

namespace clk {

// Anything before this (2023-11-14) is "the clock was never set".
static const int64_t MIN_UNIX_S = 1700000000LL;
// A resync that moves the stored offset by less than this is not written to NVS again.
static const int64_t BASE_REWRITE_MS = 2000;

static uint16_t g_boot = 0;
static bool g_valid = false, g_since = false, g_kept = false, g_relUsed = false;
static int64_t g_offsetMs = 0;          // unix ms at this boot's uptime 0
static store::TimeBase g_bases[bp::QUEUE_TIME_BASES];
static size_t g_nBases = 0;

uint64_t uptimeMs() { return (uint64_t)(esp_timer_get_time() / 1000); }
uint16_t bootId() { return g_boot; }
bool valid() { return g_valid; }
bool setSinceBoot() { return g_since; }
bool keptAcrossReset() { return g_kept; }

void begin() {
  g_boot = store::nextBootId();
  g_nBases = store::loadBases(g_bases, bp::QUEUE_TIME_BASES);
  // The system clock keeps running through a reset (not through a power cut). If it still holds a
  // real date, the core set it before the reset: use it until the core sets it again.
  struct timeval tv;
  if (gettimeofday(&tv, nullptr) == 0 && (int64_t)tv.tv_sec >= MIN_UNIX_S) {
    g_offsetMs = (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000 - (int64_t)uptimeMs();
    g_valid = g_kept = true;
  }
}

static const store::TimeBase* findBase(uint16_t boot) {
  for (size_t i = 0; i < g_nBases; i++)
    if (g_bases[i].boot == boot) return &g_bases[i];
  return nullptr;
}

// Remember this boot's offset, so finds stored by uptime can still be timed after a reboot.
static void rememberBase() {
  for (size_t i = 0; i < g_nBases; i++) {
    if (g_bases[i].boot != g_boot) continue;
    int64_t d = g_bases[i].offsetMs - g_offsetMs;
    if (d < 0) d = -d;
    if (d < BASE_REWRITE_MS) return;
    g_bases[i].offsetMs = g_offsetMs;
    store::saveBases(g_bases, g_nBases);
    return;
  }
  if (g_nBases == bp::QUEUE_TIME_BASES) {   // full: the oldest boot goes
    memmove(&g_bases[0], &g_bases[1], (g_nBases - 1) * sizeof(g_bases[0]));
    g_nBases--;
  }
  g_bases[g_nBases].boot = g_boot;
  g_bases[g_nBases].offsetMs = g_offsetMs;
  g_nBases++;
  store::saveBases(g_bases, g_nBases);
}

void setFromCore(uint32_t sec, uint16_t ms) {
  if ((int64_t)sec < MIN_UNIX_S) return;
  g_offsetMs = (int64_t)sec * 1000 + (ms % 1000) - (int64_t)uptimeMs();
  g_valid = g_since = true;
  struct timeval tv;
  tv.tv_sec = (time_t)sec;
  tv.tv_usec = (suseconds_t)(ms % 1000) * 1000;
  settimeofday(&tv, nullptr);
  if (g_relUsed) rememberBase();
}

static void split(int64_t unixMs, uint32_t& sec, uint16_t& ms) {
  if (unixMs < 0) unixMs = 0;
  sec = (uint32_t)(unixMs / 1000);
  ms = (uint16_t)(unixMs % 1000);
}

void now(uint32_t& sec, uint16_t& ms) { split(g_offsetMs + (int64_t)uptimeMs(), sec, ms); }

void stamp(bool& rel, uint32_t& sec, uint16_t& ms) {
  if (g_valid) {
    rel = false;
    now(sec, ms);
    return;
  }
  rel = true;
  g_relUsed = true;
  uint64_t up = uptimeMs();
  sec = (uint32_t)(up / 1000);
  ms = (uint16_t)(up % 1000);
}

bool resolve(bool rel, uint16_t boot, uint32_t sec, uint16_t ms, uint32_t& outSec, uint16_t& outMs) {
  if (!rel) {
    outSec = sec;
    outMs = ms;
    return true;
  }
  int64_t up = (int64_t)sec * 1000 + ms;
  if (boot == g_boot) {
    if (!g_valid) return false;
    split(g_offsetMs + up, outSec, outMs);
    return true;
  }
  const store::TimeBase* b = findBase(boot);
  if (!b) return false;
  split(b->offsetMs + up, outSec, outMs);
  return true;
}

bool canResolve(bool rel, uint16_t boot) { return !rel || boot == g_boot || findBase(boot) != nullptr; }

void forget() {
  g_nBases = 0;
  g_relUsed = false;
  store::saveBases(g_bases, 0);
}

}  // namespace clk
