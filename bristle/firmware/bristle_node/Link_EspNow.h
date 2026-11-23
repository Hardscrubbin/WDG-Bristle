// Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
// ESP-NOW: the shared radio part (enow), used by the node's link and by the bridge, and the node's
// link to the core through the bridge.
//
// A scanning node is on its scan channels almost all the time. Every ESPNOW_VISIT_INTERVAL_MS,
// between two channel dwells, it goes to the control channel, sends what it has (or an empty poll),
// listens a few milliseconds for what the bridge holds for it, and goes back to scanning. That is a
// "visit". Frames are signed with the owner key and numbered with a counter that survives restarts
// on both ends (docs/PROTOCOL.md). A node with no owner sends nothing here: it cannot sign.
#pragma once
#include <Arduino.h>
#include <vector>

#include "BristleProto.h"
#include "Link.h"

namespace enow {

struct Rx {
  uint8_t mac[6];
  int8_t rssi;
  uint8_t len;
  uint8_t data[250];
};

bool begin();                                  // start ESP-NOW (Wi-Fi must be up). Safe to call again.
void setChannel(uint8_t channel);              // tune the radio (only between scans)
bool send(const uint8_t mac[6], const uint8_t* data, size_t len);   // nullptr mac = broadcast
bool take(Rx& out);                            // one received frame, if any (call from loop)
uint32_t rxLost();                             // frames dropped because loop was not taking them
uint32_t txFailed();                           // frames the radio could not hand over

}  // namespace enow

class EspNowLink : public Link {
 public:
  void begin(const uint8_t* key, bool owned, uint8_t channel);
  void setKey(const uint8_t* key, bool owned);       // adopted or freed
  void setChannel(uint8_t channel);                  // 0 = off
  uint8_t channel() const { return channel_; }
  bool enabled() const { return owned_ && channel_ != 0 && !paused_; }
  void pause(bool on) { paused_ = on; }              // while USB is the link: no visits, no scan time lost
  void hurry(bool on) { hurry_ = on; }               // an update is coming in: visit back to back

  uint8_t type() const override { return bp::LINK_ESPNOW; }
  size_t room() override;
  bool send(const String& line) override;
  bool hasRssi() const override { return haveRssi_; }
  int rssi() const override { return rssi_; }

  bool visitDue(uint32_t now) const;
  void visit(uint32_t now);                          // at most ESPNOW_VISIT_MAX_MS, then back to the caller
  bool bridgeKnown(uint32_t now) const { return heardMs_ && now - heardMs_ < bp::ESPNOW_LOST_MS; }
  uint32_t visits() const { return visits_; }
  uint32_t lastVisitMs() const { return lastCostMs_; }   // how long the last visit took
  uint32_t staleFrames() const { return stale_; }
  uint32_t badFrames() const { return bad_; }

 private:
  uint32_t nextCounter();
  bool accept(uint32_t ctr);
  // `to`: one address for this frame only; nullptr = the bridge, or everyone while it is not known.
  bool sendFrame(uint8_t kind, uint8_t frag, const uint8_t* payload, size_t len, const uint8_t* to = nullptr);
  int flush(int budget);                             // frames sent
  void handle(const enow::Rx& rx, uint32_t now);
  void lines(const uint8_t* payload, size_t len);

  const uint8_t* key_ = nullptr;
  bool owned_ = false, paused_ = false, hurry_ = false;
  uint8_t channel_ = 0, tuned_ = 0;
  std::vector<String> out_;                          // line bodies waiting for the next visit
  size_t outBytes_ = 0;
  uint32_t dropped_ = 0;
  uint32_t txNext_ = 0, txEnd_ = 0, rxFloor_ = 0;
  int32_t burned_ = -1;
  uint8_t bridge_[6] = {0};
  bool haveBridge_ = false, haveRssi_ = false;
  int rssi_ = 0;
  uint32_t heardMs_ = 0, lastVisit_ = 0, lastSync_ = 0, lostSince_ = 0;
  uint32_t visits_ = 0, lastCostMs_ = 0, stale_ = 0, bad_ = 0;
  uint8_t search_ = 0;
  // one line arriving in pieces
  String partial_;
  uint8_t partCount_ = 0, partNext_ = 0;
  uint32_t partCtr_ = 0;

  static constexpr size_t OUT_MAX_BYTES = 3072;
};

extern EspNowLink espLink;
