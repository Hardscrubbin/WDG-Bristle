// The human console on the USB port: status, queue, owner, forget, identify, boot, selftest, version.
// Every line it prints starts with "# " so the core never mistakes it for a protocol line.
#pragma once
#include <Arduino.h>

namespace console {

void say(const String& text);
void banner();                    // one line at boot
void run(const String& command);  // a line someone typed
bool selftest(bool quiet = false);   // the spec's test vectors; prints PASS/FAIL per vector unless quiet

}  // namespace console
