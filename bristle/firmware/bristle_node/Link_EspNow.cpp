// Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
#include "Link_EspNow.h"

#include <WiFi.h>
#include <esp_now.h>
#include <esp_wifi.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include "Codec.h"
#include "Crypto.h"
#include "Node.h"
#include "Store.h"

// ---------------------------------------------------------------------------------------- radio
namespace enow {

static QueueHandle_t rxQueue = nullptr;
static bool up = false;
static volatile uint32_t lost = 0, failed = 0;
static const uint8_t BROADCAST[6] = {0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF};

// ESP-NOW holds 20 peers at most. A bridge talks to more nodes than that, so peers are added when
// a frame is sent and the one unused longest makes room.
static const int MAX_PEERS = 16;
struct Peer {
  uint8_t mac[6];
  uint32_t used;
  bool live;
};
static Peer peers[MAX_PEERS];

static void onReceive(const esp_now_recv_info_t* info, const uint8_t* data, int len) {
  if (!rxQueue || len <= 0 || len > 250 || !info || !info->src_addr) return;
  Rx rx;
  memcpy(rx.mac, info->src_addr, 6);
  rx.rssi = info->rx_ctrl ? (int8_t)info->rx_ctrl->rssi : 0;
  rx.len = (uint8_t)len;
  memcpy(rx.data, data, len);
  if (xQueueSend(rxQueue, &rx, 0) != pdTRUE) lost = lost + 1;   // loop is behind: counted, never waited for
}

bool begin() {
  if (up) return true;
  if (!rxQueue) rxQueue = xQueueCreate(24, sizeof(Rx));
  if (!rxQueue || esp_now_init() != ESP_OK) return false;
  esp_now_register_recv_cb(onReceive);
  memset(peers, 0, sizeof(peers));
  up = true;
  return true;
}

void setChannel(uint8_t channel) {
  if (channel >= 1 && channel <= 13) esp_wifi_set_channel(channel, WIFI_SECOND_CHAN_NONE);
}

static bool ensurePeer(const uint8_t mac[6]) {
  int freeSlot = -1, oldest = 0;
  for (int i = 0; i < MAX_PEERS; i++) {
    if (peers[i].live && memcmp(peers[i].mac, mac, 6) == 0) {
      peers[i].used = millis();
      return true;
    }
    if (!peers[i].live && freeSlot < 0) freeSlot = i;
    if (peers[i].live && peers[i].used < peers[oldest].used) oldest = i;
  }
  if (freeSlot < 0) {
    esp_now_del_peer(peers[oldest].mac);
    peers[oldest].live = false;
    freeSlot = oldest;
  }
  esp_now_peer_info_t info;
  memset(&info, 0, sizeof(info));
  memcpy(info.peer_addr, mac, 6);
  info.channel = 0;            // whatever channel the radio is on when the frame goes out
  info.ifidx = WIFI_IF_STA;
  info.encrypt = false;        // frames carry their own tag (HMAC with the owner key)
  if (esp_now_add_peer(&info) != ESP_OK) return false;
  memcpy(peers[freeSlot].mac, mac, 6);
  peers[freeSlot].used = millis();
  peers[freeSlot].live = true;
  return true;
}

bool send(const uint8_t mac[6], const uint8_t* data, size_t len) {
  if (!up) return false;
  const uint8_t* to = mac ? mac : BROADCAST;
  if (!ensurePeer(to) || esp_now_send(to, data, len) != ESP_OK) {
    failed = failed + 1;
    return false;
  }
  return true;
}

bool take(Rx& out) { return rxQueue && xQueueReceive(rxQueue, &out, 0) == pdTRUE; }
uint32_t rxLost() { return lost; }
uint32_t txFailed() { return failed; }

}  // namespace enow

// ---------------------------------------------------------------------------------------- node link
EspNowLink espLink;

static const size_t MAX_PAYLOAD = bp::FRAME_MAX_BYTES - bp::FRAME_HEADER_BYTES - bp::FRAME_TAG_BYTES;
static const uint8_t SEARCH[] = {0, 1, 6, 11};   // 0 stands for the configured channel

void EspNowLink::begin(const uint8_t* key, bool owned, uint8_t channel) {
  key_ = key;
  owned_ = owned;
  channel_ = channel;
  rxFloor_ = store::frameFloor();
  out_.reserve(16);
  if (enabled()) enow::begin();
}

void EspNowLink::setKey(const uint8_t* key, bool owned) {
  key_ = key;
  owned_ = owned;
  out_.clear();
  outBytes_ = 0;
  haveBridge_ = false;
  heardMs_ = 0;
  drop();
  if (enabled()) enow::begin();
}

void EspNowLink::setChannel(uint8_t channel) {
  channel_ = channel > 13 ? 0 : channel;
  haveBridge_ = false;
  tuned_ = 0;
  heardMs_ = 0;
  if (enabled()) enow::begin();
}

// ------------------------------------------------------------------ counters (docs/PROTOCOL.md)
uint32_t EspNowLink::nextCounter() {
  if (txNext_ >= txEnd_) {
    txNext_ = store::reserveFrameCounters(txNext_, bp::FRAME_COUNTER_BLOCK);
    txEnd_ = txNext_ + bp::FRAME_COUNTER_BLOCK;
  }
  return txNext_++;
}

bool EspNowLink::accept(uint32_t ctr) {
  if (ctr < rxFloor_) return false;
  int32_t block = (int32_t)(ctr / bp::FRAME_COUNTER_BLOCK);
  if (block != burned_) {
    store::burnFrameFloor(((uint32_t)block + 1) * bp::FRAME_COUNTER_BLOCK);   // after a restart: none of this block
    burned_ = block;
  }
  rxFloor_ = ctr + 1;
  return true;
}

// ------------------------------------------------------------------ out
size_t EspNowLink::room() { return enabled() && outBytes_ < OUT_MAX_BYTES ? OUT_MAX_BYTES - outBytes_ : 0; }

bool EspNowLink::send(const String& line) {
  if (!enabled()) return false;
  String body = codec::stripCrc(line);
  if (outBytes_ + body.length() > OUT_MAX_BYTES) {
    dropped_++;
    return false;
  }
  // Only the newest heartbeat needs to wait for the next visit.
  if (body.startsWith("HB:")) {
    for (size_t i = 0; i < out_.size(); i++) {
      if (out_[i].startsWith("HB:")) {
        outBytes_ -= out_[i].length();
        out_.erase(out_.begin() + i);
        break;
      }
    }
  }
  outBytes_ += body.length();
  out_.push_back(body);
  return true;
}

bool EspNowLink::sendFrame(uint8_t kind, uint8_t frag, const uint8_t* payload, size_t len, const uint8_t* to) {
  uint8_t frame[bp::FRAME_MAX_BYTES];
  size_t n = crypto::frameBuild(key_, kind, nextCounter(), frag, payload, len, frame, sizeof(frame));
  if (!to && haveBridge_) to = bridge_;
  return n && enow::send(to, frame, n);
}

int EspNowLink::flush(int budget) {
  int sent = 0;
  while (!out_.empty() && sent < budget) {
    if (out_.front().length() > MAX_PAYLOAD) {
      // One line too long for a frame: it goes alone, in pieces with consecutive counters.
      const String& body = out_.front();
      size_t count = (body.length() + MAX_PAYLOAD - 1) / MAX_PAYLOAD;
      if (count <= bp::FRAME_MAX_FRAGMENTS) {
        for (size_t i = 0; i < count; i++) {
          size_t from = i * MAX_PAYLOAD;
          size_t len = body.length() - from < MAX_PAYLOAD ? body.length() - from : MAX_PAYLOAD;
          sendFrame(bp::FRAME_KIND_LINES, (uint8_t)((i << 4) | count), (const uint8_t*)body.c_str() + from, len);
          sent++;
        }
      }
      outBytes_ -= out_.front().length();
      out_.erase(out_.begin());
      continue;
    }
    uint8_t payload[bp::FRAME_MAX_BYTES];
    size_t len = 0, taken = 0;
    while (taken < out_.size() && out_[taken].length() <= MAX_PAYLOAD) {
      size_t need = out_[taken].length() + (len ? 1 : 0);
      if (len + need > MAX_PAYLOAD) break;
      if (len) payload[len++] = '\n';
      memcpy(payload + len, out_[taken].c_str(), out_[taken].length());
      len += out_[taken].length();
      taken++;
    }
    sendFrame(bp::FRAME_KIND_LINES, 0x01, payload, len);
    sent++;
    for (size_t i = 0; i < taken; i++) outBytes_ -= out_[i].length();
    out_.erase(out_.begin(), out_.begin() + taken);
  }
  return sent;
}

// ------------------------------------------------------------------ in
void EspNowLink::lines(const uint8_t* payload, size_t len) {
  size_t start = 0;
  for (size_t i = 0; i <= len; i++) {
    if (i < len && payload[i] != '\n') continue;
    if (i > start) {
      codec::Msg m;
      if (codec::parseBody((const char*)payload + start, i - start, m)) {
        lastRxMs = millis();
        node::onMessage(*this, m);
      } else {
        node::badLine();
      }
    }
    start = i + 1;
  }
}

void EspNowLink::handle(const enow::Rx& rx, uint32_t now) {
  uint8_t kind, frag;
  uint32_t ctr;
  const uint8_t* payload;
  size_t plen;
  if (!crypto::frameOpen(key_, rx.data, rx.len, kind, ctr, frag, payload, plen)) {
    bad_++;                                   // not signed with my owner's key: not for me
    return;
  }
  if (kind == bp::FRAME_KIND_SYNC) {
    // "Use a counter of at least this." It can only move my counter up, so it needs no counter check.
    if (plen == 4) {
      uint32_t floor = (uint32_t)payload[0] | ((uint32_t)payload[1] << 8) | ((uint32_t)payload[2] << 16) |
                       ((uint32_t)payload[3] << 24);
      if (floor > txNext_) {
        txNext_ = store::reserveFrameCounters(floor, bp::FRAME_COUNTER_BLOCK);
        txEnd_ = txNext_ + bp::FRAME_COUNTER_BLOCK;
      }
    }
    return;
  }
  if (!accept(ctr)) {
    stale_++;
    if (now - lastSync_ >= 1000) {            // tell the core which counter will do, not too often
      lastSync_ = now;
      uint8_t floor[4] = {(uint8_t)rxFloor_, (uint8_t)(rxFloor_ >> 8), (uint8_t)(rxFloor_ >> 16),
                          (uint8_t)(rxFloor_ >> 24)};
      // The answer goes to whoever sent the old frame, and only this answer: a core whose counter
      // fell behind catches up with it. The sender is NOT taken for the bridge. An old frame is
      // one anybody could have recorded and played back, and up to build 4 the node then sent
      // everything to that address until it gave the bridge up for lost (20 s).
      sendFrame(bp::FRAME_KIND_SYNC, 0x01, floor, 4, rx.mac);
    }
    return;
  }
  // A frame from my owner: this is where the bridge is.
  memcpy(bridge_, rx.mac, 6);
  haveBridge_ = true;
  heardMs_ = now ? now : 1;
  lostSince_ = 0;
  rssi_ = rx.rssi;
  haveRssi_ = true;
  if (kind != bp::FRAME_KIND_LINES) return;
  uint8_t index = frag >> 4, count = frag & 0x0F;
  if (count <= 1) {
    partCount_ = 0;
    if (index == 0 && count == 1) lines(payload, plen);
    return;
  }
  if (index == 0) {
    partial_ = "";
    partial_.reserve(count * MAX_PAYLOAD);
    partCount_ = count;
    partNext_ = 0;
    partCtr_ = ctr;
  }
  if (partCount_ != count || index != partNext_ || ctr != partCtr_ || partial_.length() + plen > 1024) {
    partCount_ = 0;                           // a piece is missing or out of order: the line is dropped
    node::badLine();
    return;
  }
  partial_.concat((const char*)payload, plen);
  partNext_++;
  partCtr_ = ctr + 1;
  if (partNext_ == partCount_) {
    partCount_ = 0;
    lines((const uint8_t*)partial_.c_str(), partial_.length());
  }
}

// ------------------------------------------------------------------ visits
bool EspNowLink::visitDue(uint32_t now) const {
  if (!enabled()) return false;
  if (hurry_) return now - lastVisit_ >= 20;
  uint32_t every = bp::ESPNOW_VISIT_INTERVAL_MS;
  // No bridge for a minute: look for one now and then instead of every visit, so a node alone on
  // a power bank loses next to no scan time.
  if (lostSince_ && now - lostSince_ > 60000) every = bp::ESPNOW_SEARCH_MS;
  return now - lastVisit_ >= every;
}

void EspNowLink::visit(uint32_t now) {
  if (!enow::begin()) return;
  lastVisit_ = now;
  visits_++;
  if (!bridgeKnown(now)) {
    haveBridge_ = false;                      // ask everyone on the channel
    if (!lostSince_) lostSince_ = now ? now : 1;
    search_ = (uint8_t)((search_ + 1) % sizeof(SEARCH));
    tuned_ = SEARCH[search_] ? SEARCH[search_] : channel_;
  } else if (!tuned_) {
    tuned_ = channel_;
  }
  enow::setChannel(tuned_);
  int sent = flush(bp::ESPNOW_VISIT_FRAMES);
  if (!sent) sendFrame(bp::FRAME_KIND_POLL, 0x01, nullptr, 0);   // "anything for me?"
  uint32_t start = millis(), quietAt = start + bp::ESPNOW_VISIT_WINDOW_MS;
  enow::Rx rx;
  for (;;) {
    uint32_t t = millis();
    if (t - start >= bp::ESPNOW_VISIT_MAX_MS || (int32_t)(t - quietAt) >= 0) break;
    bool got = false;
    while (enow::take(rx)) {
      handle(rx, t);
      got = true;
    }
    if (got) {
      quietAt = millis() + bp::ESPNOW_VISIT_WINDOW_MS;           // more may follow: listen on
      if (sent < (int)bp::ESPNOW_VISIT_FRAMES) sent += flush(bp::ESPNOW_VISIT_FRAMES - sent);   // answers go out now
    } else {
      delay(1);
    }
  }
  if (bridgeKnown(millis())) tuned_ = tuned_ ? tuned_ : channel_;
  lastCostMs_ = millis() - start;
}
