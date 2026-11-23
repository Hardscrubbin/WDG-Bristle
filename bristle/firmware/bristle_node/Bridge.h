// Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
// Bridge role: one Bristle board on the core's USB hub that relays ESP-NOW frames.
//
//   node  --ESP-NOW frame-->  bridge  --ERX line over USB-->  core
//   core  --ETX line over USB-->  bridge mailbox  --ESP-NOW frame, when that node checks in-->  node
//
// A scanning node listens on the control channel for a few milliseconds per visit, so the bridge
// keeps what the core sends in a small mailbox per node and hands it over the moment that node's
// next frame arrives. The bridge checks only that a frame has the shape of a Bristle frame. It
// holds no fleet logic, checks no tags and decides nothing: all of that is on the core.
#pragma once
#include <Arduino.h>

#include "Link.h"

namespace bridge {

void begin(uint8_t channel);                        // stay on the control channel and listen
void setChannel(uint8_t channel);
void tick(uint32_t nowMs, Link& toCore);            // relay what arrived; empty out old mail
// A frame from the core for the node with this MAC. False if that node's mailbox is full.
bool hold(const uint8_t mac[6], const uint8_t* frame, size_t len, uint32_t nowMs);

struct Stats {
  uint32_t rx, tx, txFailed, held, dropped;
};
Stats stats();

}  // namespace bridge
