// USB serial link. The same port carries two things:
//   - protocol lines from/to the core:  TYPE:k=v,...*CRC   (docs/PROTOCOL.md)
//   - a human console: anything else you type (try `help`). Console output starts with "# ", which
//     the core ignores.
#pragma once
#include "BristleProto.h"
#include "Link.h"

class UsbLink : public Link {
 public:
  void begin();
  void poll();                                       // call every loop

  uint8_t type() const override { return bp::LINK_USB; }
  bool allowsAdoption() const override { return true; }
  size_t room() override;
  bool send(const String& line) override;
  uint32_t unsent() const { return unsent_; }        // lines dropped because the port had no room

  // The Arduino core's USB transmit buffer is 256 bytes by default: smaller than one batch of
  // finds. Ours holds several lines, so the node can queue a burst and carry on scanning.
  static constexpr size_t TX_BUFFER_BYTES = 4096;
  static constexpr size_t RX_BUFFER_BYTES = 1024;

 private:
  char buf_[bp::LINE_MAX_BYTES + 2];
  size_t len_ = 0;
  bool overflow_ = false;
  uint32_t unsent_ = 0;
};

extern UsbLink usbLink;
