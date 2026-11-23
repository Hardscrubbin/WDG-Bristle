// Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
// Firmware updates over any link, with rollback.
//
//   OTA op=begin,size=..,sha=..   the spare app slot is opened; answer OTAS st=receiving,o=0
//   OTA op=chunk,o=..,d=..        written only if o is the next byte wanted; answer OTAS ..,o=<next>
//   OTA op=verify                 the slot is read back from flash and its SHA-256 compared
//   OTA op=commit                 the slot becomes the boot image; the node restarts
//   OTA op=abort / status
//
// Nothing changes what the node boots until commit, so an update that is cut off (cable pulled,
// link lost, power cut) leaves the node on the firmware it was running.
//
// After a commit the new image starts "on trial" (the bootloader's app rollback). It is confirmed
// when its own self-test passes and its owner's core has authenticated on some link (a node with
// no owner: after a minute of running). If that has not happened OTA_CONFIRM_MS after start, or
// the image resets before then, the node goes back to the firmware it had.
#pragma once
#include <Arduino.h>

#include "Codec.h"
#include "Link.h"

namespace ota {

void begin();                                  // at start: is this image on trial?
bool onTrial();                                // running a new image that is not confirmed yet
bool active();                                 // an update is coming in (scanning is paused)
Link* link();                                  // the link it is coming in on
void handle(Link& from, const codec::Msg& m, const String* id);
void linkClosed(Link& which);                  // the update's link went away: drop the update
void tick(uint32_t nowMs, bool owned, bool ownerAuthed);
String statusText();                           // for the console

}  // namespace ota
