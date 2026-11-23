// Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
#include "Link_Ble.h"

#include <NimBLEDevice.h>
#include <freertos/FreeRTOS.h>
#include <freertos/queue.h>

#include "Codec.h"
#include "Node.h"
#include "Radio.h"

BleLink bleLink;

// Lines written by the core arrive on the Bluetooth stack's own task. They are handed to loop()
// through a queue, so the node's state is only ever touched from one task.
struct RxLine {
  uint16_t len;
  char text[bp::LINE_MAX_BYTES + 2];
};
static QueueHandle_t rxQueue = nullptr;
static NimBLECharacteristic* notifyChr = nullptr;
static NimBLEServer* server = nullptr;
static volatile bool linked = false, subscribed = false, dropped = false;
static volatile uint16_t connHandle = 0;
static volatile uint32_t rxLost = 0;

class ServerEvents : public NimBLEServerCallbacks {
  void onConnect(NimBLEServer*, NimBLEConnInfo& info) override {
    connHandle = info.getConnHandle();
    linked = true;
  }
  void onDisconnect(NimBLEServer*, NimBLEConnInfo&, int) override {
    linked = false;
    subscribed = false;
    dropped = true;                               // loop() closes the link and advertises again
  }
};

class CommandEvents : public NimBLECharacteristicCallbacks {
  void onWrite(NimBLECharacteristic* chr, NimBLEConnInfo&) override {
    NimBLEAttValue value = chr->getValue();
    RxLine line;
    if (!rxQueue || value.length() == 0 || value.length() > bp::LINE_MAX_BYTES) return;
    line.len = value.length();
    memcpy(line.text, value.data(), line.len);
    if (xQueueSend(rxQueue, &line, 0) != pdTRUE) rxLost = rxLost + 1;
  }
};

class NotifyEvents : public NimBLECharacteristicCallbacks {
  void onSubscribe(NimBLECharacteristic*, NimBLEConnInfo&, uint16_t value) override { subscribed = value != 0; }
};

static ServerEvents serverEvents;
static CommandEvents commandEvents;
static NotifyEvents notifyEvents;

void BleLink::begin(const uint8_t hwid[6], const String& name, bool owned) {
  memcpy(hwid_, hwid, 6);
  name_ = name;
  owned_ = owned;
  if (!rxQueue) rxQueue = xQueueCreate(8, sizeof(RxLine));
  radio::bleUp();
  NimBLEDevice::setMTU(bp::BLE_MTU);
  server = NimBLEDevice::createServer();
  server->setCallbacks(&serverEvents, false);
  server->advertiseOnDisconnect(false);           // loop() decides when to advertise
  NimBLEService* service = server->createService(bp::BLE_SERVICE_UUID);
  NimBLECharacteristic* command =
      service->createCharacteristic(bp::BLE_COMMAND_UUID, NIMBLE_PROPERTY::WRITE | NIMBLE_PROPERTY::WRITE_NR);
  command->setCallbacks(&commandEvents);
  notifyChr = service->createCharacteristic(bp::BLE_NOTIFY_UUID, NIMBLE_PROPERTY::NOTIFY);
  notifyChr->setCallbacks(&notifyEvents);
  server->start();                                // starts the service with it
  up_ = true;
  advertise();
}

void BleLink::advertise() {
  if (!up_) return;
  NimBLEAdvertising* adv = NimBLEDevice::getAdvertising();
  adv->stop();
  advertising_ = false;
  if (!allowed_ || linked) return;
  // The advertisement: flags + the service UUID with hwid and the owned flag as service data.
  // The 128-bit UUID is too long to send twice, so the name goes in the scan response.
  uint8_t data[7];
  memcpy(data, hwid_, 6);
  data[6] = owned_ ? bp::BLE_ADV_FLAG_OWNED : 0;
  NimBLEAdvertisementData main, response;
  main.setFlags(0x06);                            // LE general discoverable, no Bluetooth Classic
  main.setServiceData(NimBLEUUID(bp::BLE_SERVICE_UUID), data, sizeof(data));
  response.setName(std::string(name_.c_str()));
  adv->setAdvertisementData(main);
  adv->setScanResponseData(response);
  adv->setMinInterval(160);                       // 100 ms .. 200 ms
  adv->setMaxInterval(320);
  advertising_ = adv->start();
}

void BleLink::rename(const String& name, bool owned) {
  if (name == name_ && owned == owned_) return;
  name_ = name;
  owned_ = owned;
  advertise();
}

bool BleLink::connected() const { return linked; }

size_t BleLink::room() { return linked && subscribed ? 1024 : 0; }

bool BleLink::send(const String& line) {
  if (!linked || !subscribed || !notifyChr) return false;
  // The stack queues notifications; when it is out of buffers it says no and the caller tries
  // again on a later loop (finds stay in the node's queue until the core acks them anyway).
  if (!notifyChr->notify((const uint8_t*)line.c_str(), line.length(), connHandle)) {
    unsent_++;
    return false;
  }
  return true;
}

void BleLink::poll(uint32_t nowMs) {
  if (!up_) return;
  if (dropped) {
    dropped = false;
    drop();
    node::linkClosed(*this);
    advertise();
  }
  if (allowed_ != advertising_ && !linked && nowMs - lastTry_ >= 1000) {
    lastTry_ = nowMs;
    advertise();
  }
  if (!allowed_ && linked && server) server->disconnect(connHandle);   // USB took over
  RxLine line;
  while (xQueueReceive(rxQueue, &line, 0) == pdTRUE) {
    codec::Msg m;
    if (codec::parseLine(line.text, line.len, m)) {
      lastRxMs = nowMs;
      node::onMessage(*this, m);
    } else {
      node::badLine();
    }
  }
}
