// Bristle node firmware for ESP32-C5 (8 MB flash minimum).
//
// This build: the node role over USB. The ESP-NOW link, the bridge role, the Bluetooth LE link and
// over-the-air updates are later steps (docs/PLAN.md).
//
//   Boards.h            board pins, build number
//   BristleProto.h      generated from spec/bristle_proto.yaml: never edit
//   Codec / Crypto      protocol lines, HMAC-SHA256
//   Store / Clock       NVS settings; the node's clock
//   Queue               finds in RAM + flash until the core acks them
//   Radio               Wi-Fi and BLE scanning on the channel plan
//   Owner               owner lock and handshake
//   Link, Link_Usb      the connection to the core
//   Node                commands, heartbeat, delivery
//   Console             what you can type on the USB port (`help`)

#include <Arduino.h>

#include "Console.h"
#include "Link_Usb.h"
#include "Node.h"

void setup() {
  usbLink.begin();
  node::begin();
  node::addLink(usbLink);
  console::banner();
}

void loop() {
  usbLink.poll();
  node::tick(millis());
  delay(2);
}
