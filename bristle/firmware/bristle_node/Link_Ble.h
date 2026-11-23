// Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
// Bluetooth LE link: for a node placed away from the core, out of the bridge's reach or without one.
//
// The node is a GATT server with Bristle's own service (UUIDs in the spec). The core writes one
// protocol line per write to the command characteristic; the node sends one line per notification.
// The advertisement carries the service UUID with 7 bytes of service data (hwid + a flag byte: bit
// 0 = has an owner); the name BRISTLE-<n> is in the scan response. One core at a time.
//
// One radio: Wi-Fi scanning, Bluetooth scanning and this link take turns. While a core is
// connected the scan scheduler leaves the radio alone for a short slot at regular intervals
// (Radio.cpp), so the link's connection events are not starved by channel dwells.
#pragma once
#include <Arduino.h>

#include "BristleProto.h"
#include "Link.h"

class BleLink : public Link {
 public:
  void begin(const uint8_t hwid[6], const String& name, bool owned);
  void poll(uint32_t nowMs);                       // call every loop: lines that arrived, advertising
  void rename(const String& name, bool owned);     // adopted, renumbered or freed
  void allow(bool on) { allowed_ = on; }           // off while USB is the link: no advertising

  uint8_t type() const override { return bp::LINK_BLE; }
  size_t room() override;
  bool send(const String& line) override;
  bool connected() const;
  uint32_t unsent() const { return unsent_; }

 private:
  void advertise();
  uint8_t hwid_[6] = {0};
  String name_;
  bool owned_ = false, up_ = false, allowed_ = true, advertising_ = false;
  uint32_t unsent_ = 0, lastTry_ = 0;
};

extern BleLink bleLink;
