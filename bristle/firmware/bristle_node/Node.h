// The node itself: commands from the core, heartbeat, turning scan results into queued finds, and
// handing finds to the core over the best authenticated link (USB wins).
#pragma once
#include <Arduino.h>

#include "Codec.h"
#include "Link.h"
#include "Queue.h"
#include "Store.h"

namespace node {

void begin();                                   // settings, clock, queue, radio
void addLink(Link& link);
void tick(uint32_t nowMs);

void onMessage(Link& link, const codec::Msg& m);   // a valid protocol line arrived on a link
void badLine();                                    // a line with a bad CRC or too long arrived

void forgetEverything(const char* why);         // free the node: owner, number, plan, queue
void identify(uint32_t seconds);                // blink the LED

// For the console.
const NodeState& state();
FindQueue& queue();
const String& hwidHex();
String name();                                  // BRISTLE-<n> or BRISTLE-NEW
String planText();                              // "" if the node has no plan
uint32_t badLines();
Link* activeLink();                             // where finds are going, or nullptr

}  // namespace node
