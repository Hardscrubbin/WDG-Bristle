// Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
#include "Bridge.h"

#include <WiFi.h>

#include "BristleProto.h"
#include "Codec.h"
#include "Crypto.h"
#include "Link_EspNow.h"

namespace bridge {

// One pool for every node's mail. 40 nodes with a few frames each fit; a node that stays away
// loses its oldest mail first (counted), and mail older than ESPNOW_MAILBOX_MS is thrown out.
static const int SLOTS = 96;
struct Slot {
  bool used;
  uint8_t mac[6];
  uint8_t len;
  uint32_t at, order;
  uint8_t data[250];
};
static Slot* pool = nullptr;
static uint32_t order = 0;
static uint8_t channel = 1;
static uint32_t nRx = 0, nTx = 0, nDropped = 0, lastSweep = 0;

void begin(uint8_t ch) {
  if (!pool) pool = (Slot*)calloc(SLOTS, sizeof(Slot));
  WiFi.mode(WIFI_STA);
  WiFi.disconnect();
  enow::begin();
  setChannel(ch);
}

void setChannel(uint8_t ch) {
  channel = (ch >= 1 && ch <= 13) ? ch : bp::ESPNOW_CONTROL_CHANNEL;
  enow::setChannel(channel);
}

static int mailFor(const uint8_t mac[6]) {
  int n = 0;
  for (int i = 0; i < SLOTS; i++)
    if (pool[i].used && memcmp(pool[i].mac, mac, 6) == 0) n++;
  return n;
}

static int oldestFor(const uint8_t mac[6]) {
  int best = -1;
  for (int i = 0; i < SLOTS; i++)
    if (pool[i].used && memcmp(pool[i].mac, mac, 6) == 0 && (best < 0 || pool[i].order < pool[best].order)) best = i;
  return best;
}

bool hold(const uint8_t mac[6], const uint8_t* frame, size_t len, uint32_t nowMs) {
  if (!pool || !crypto::frameShape(frame, len)) return false;
  if (mailFor(mac) >= (int)bp::ESPNOW_MAILBOX_FRAMES) {
    int old = oldestFor(mac);                   // that node is not checking in: its oldest mail goes
    if (old >= 0) pool[old].used = false;
    nDropped++;
  }
  int slot = -1;
  for (int i = 0; i < SLOTS && slot < 0; i++)
    if (!pool[i].used) slot = i;
  if (slot < 0) {
    nDropped++;
    return false;
  }
  pool[slot].used = true;
  memcpy(pool[slot].mac, mac, 6);
  pool[slot].len = (uint8_t)len;
  pool[slot].at = nowMs;
  pool[slot].order = order++;
  memcpy(pool[slot].data, frame, len);
  return true;
}

static void deliver(const uint8_t mac[6]) {
  for (;;) {
    int slot = oldestFor(mac);
    if (slot < 0) return;
    if (enow::send(mac, pool[slot].data, pool[slot].len)) nTx++;
    pool[slot].used = false;
  }
}

void tick(uint32_t nowMs, Link& toCore) {
  if (!pool) return;
  enow::Rx rx;
  for (int i = 0; i < 12 && enow::take(rx); i++) {
    if (!crypto::frameShape(rx.data, rx.len)) continue;        // not a Bristle frame: not relayed
    nRx++;
    deliver(rx.mac);                                           // first: the node is listening right now
    if (toCore.authed) {
      codec::LineOut l(bp::MSG_ERX);
      l.addRaw("fr", crypto::toHex(rx.mac, 6)).add("rs", (long)rx.rssi).addRaw("d", codec::b64urlEncode(rx.data, rx.len));
      toCore.send(l.finish());
    }
  }
  if (nowMs - lastSweep >= 1000) {
    lastSweep = nowMs;
    for (int i = 0; i < SLOTS; i++) {
      if (pool[i].used && nowMs - pool[i].at > bp::ESPNOW_MAILBOX_MS) {
        pool[i].used = false;
        nDropped++;
      }
    }
  }
}

Stats stats() {
  Stats s = {nRx, nTx, enow::txFailed(), 0, nDropped};
  if (pool)
    for (int i = 0; i < SLOTS; i++)
      if (pool[i].used) s.held++;
  return s;
}

}  // namespace bridge
