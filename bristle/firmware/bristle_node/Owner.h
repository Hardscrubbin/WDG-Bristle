// Owner lock. At adoption (USB only) the core gives the node a 16-byte owner key. After that every
// link has to pass a two-way HMAC-SHA256 handshake before the node takes commands or hands over
// finds. Freeing a node: hold BOOT for 5 s, type `forget` on the console, or the owning core says so.
#pragma once
#include <Arduino.h>

#include "Codec.h"
#include "Link.h"
#include "Store.h"

namespace owner {

void begin(NodeState& state, const uint8_t hwid[6]);
String id();                                             // owner id (8 hex), "" if there is no owner

// HI from the core: remember its nonce, make ours. False if the line was malformed.
bool onHi(Link& link, const codec::Msg& m);
// Adds nn (and np, oid, n when owned) to a HELLO line.
void fillHello(Link& link, codec::LineOut& hello);
// AUTH from the core. True if the core proved it holds our owner key.
bool onAuth(Link& link, const codec::Msg& m);
// ADOPT from the core. Returns nullptr when adopted, otherwise the error to report.
const char* onAdopt(Link& link, const codec::Msg& m);

// BOOT button held for FORGET_HOLD_MS: returns true once, and the caller frees the node.
bool bootHeld(uint32_t nowMs);

}  // namespace owner
