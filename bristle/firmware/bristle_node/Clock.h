// The node's clock. The core sets it (UTC). Until then a find is stored with the boot number and the
// uptime, and gets its real time once the core sets the clock during that same boot.
#pragma once
#include <Arduino.h>

namespace clk {

void begin();                       // call once, after store::begin()
uint16_t bootId();
uint64_t uptimeMs();

bool valid();                       // the node can put a real time on a find right now
bool setSinceBoot();                // the core has set the clock since this boot
bool keptAcrossReset();             // the clock survived a reset (not a power cut)
void setFromCore(uint32_t sec, uint16_t ms);
void now(uint32_t& sec, uint16_t& ms);

// Time fields for a find captured now. rel = true means "uptime of this boot", not unix time.
void stamp(bool& rel, uint32_t& sec, uint16_t& ms);
// Stored time fields -> unix time. False if that boot's clock is not known (yet).
bool resolve(bool rel, uint16_t boot, uint32_t sec, uint16_t ms, uint32_t& outSec, uint16_t& outMs);
// False if a stored find can never get a time: it was stored by uptime in an earlier boot that
// ended without the clock ever being set.
bool canResolve(bool rel, uint16_t boot);
void forget();                      // drop the remembered boots (the queue was cleared)

}  // namespace clk
