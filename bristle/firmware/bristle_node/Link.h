// One connection to the core. USB today; the ESP-NOW and Bluetooth LE links (later steps) are the same
// thing to the rest of the firmware: something that carries protocol lines and has its own handshake.
#pragma once
#include <Arduino.h>

class Link {
 public:
  virtual ~Link() {}
  virtual uint8_t type() const = 0;                 // bp::LINK_USB / LINK_ESPNOW / LINK_BLE
  virtual bool allowsAdoption() const { return false; }   // the owner key is only ever sent over USB
  virtual size_t room() = 0;                        // bytes that can be sent now without waiting
  // A full line, "*CRC\n" included. Whole or not at all: false means there was no room and nothing
  // was sent. It never waits.
  virtual bool send(const String& line) = 0;
  virtual bool hasRssi() const { return false; }
  virtual int rssi() const { return 0; }

  // Handshake state for this link (Owner.cpp) and its timers (Node.cpp).
  bool helloSeen = false;       // the core said HI on this link
  bool authed = false;          // the core proved it holds the owner key
  uint8_t coreNonce[16] = {0};
  uint8_t nodeNonce[16] = {0};
  uint32_t lastRxMs = 0;        // last valid line from the core
  uint32_t lastHbMs = 0;
  uint32_t lastTreqMs = 0;

  void drop() { helloSeen = authed = false; }
};
