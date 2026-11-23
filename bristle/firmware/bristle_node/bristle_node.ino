// Bristle node firmware for ESP32-C5 (8 MB flash minimum).
//
// One sketch, two roles (the core sets the role over USB; it is kept in NVS):
//   node     scans Wi-Fi and Bluetooth on the core's plan and hands its finds over
//   bridge   sits on the core's USB hub and relays ESP-NOW frames between the core and the nodes
//
//   Boards.h            board pins, build number
//   BristleProto.h      generated from spec/bristle_proto.yaml: never edit
//   Codec / Crypto      protocol lines, HMAC-SHA256, signed frames
//   Store / Clock       NVS settings; the node's clock
//   Queue               finds in RAM + flash until the core acks them
//   Radio               Wi-Fi and BLE scanning on the channel plan; who gets the one radio when
//   Owner               owner lock and handshake
//   Link                a connection to the core: Link_Usb, Link_EspNow (through the bridge), Link_Ble
//   Bridge              the bridge role: relay and per-node mailboxes
//   Ota                 firmware updates over any link, with rollback
//   Node                commands, heartbeat, delivery
//   Console             what you can type on the USB port (`help`)

#include <Arduino.h>

#include "Console.h"
#include "Link_Usb.h"
#include "Node.h"

void setup() {
  usbLink.begin();
  node::addLink(usbLink);
  node::begin();          // adds the ESP-NOW and Bluetooth links when the board is a node
  console::banner();
}

void loop() {
  usbLink.poll();
  node::tick(millis());
  delay(2);
}
