// Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
#include "Ota.h"

#include <esp_ota_ops.h>
#include <esp_partition.h>
#include <mbedtls/sha256.h>

#include "Boards.h"
#include "BristleProto.h"
#include "Console.h"
#include "Crypto.h"
#include "Node.h"

// The Arduino core confirms a new image as soon as it starts unless this says "later". Bristle
// confirms it itself, once the image has shown it can pass its self-test and meet its owner.
extern "C" bool verifyRollbackLater() { return true; }

namespace ota {

enum State { IDLE, RECEIVING, READY };
static State state = IDLE;
static Link* from = nullptr;
static const esp_partition_t* slot = nullptr;
static esp_ota_handle_t slotHandle = 0;
static uint32_t total = 0, got = 0, lastChunkMs = 0, restartAt = 0;
static uint8_t wantSha[32];
static bool trial = false, selftestOk = false, selftestRun = false;
static String lastError;

static const uint32_t STALL_MS = 90000;   // no chunk for this long: the update is dropped

void begin() {
  esp_ota_img_states_t st;
  const esp_partition_t* running = esp_ota_get_running_partition();
  trial = running && esp_ota_get_state_partition(running, &st) == ESP_OK && st == ESP_OTA_IMG_PENDING_VERIFY;
}

bool onTrial() { return trial; }
bool active() { return state != IDLE; }
Link* link() { return state != IDLE ? from : nullptr; }

static void status(Link& to, const char* err = nullptr) {
  codec::LineOut l(bp::MSG_OTAS);
  l.add("st", err ? "error" : state == RECEIVING ? "receiving" : state == READY ? "ready" : "idle");
  if (state == RECEIVING) l.addu("o", got);
  if (err) l.add("e", err);
  to.send(l.finish());
}

static void rsp(Link& to, bool ok, const char* err, const String* id) {
  codec::LineOut l(bp::MSG_RSP);
  l.add("c", "OTA").add("ok", (long)(ok ? 1 : 0));
  if (err) l.add("e", err);
  if (id) l.add("id", *id);
  to.send(l.finish());
}

static void drop(const char* why) {
  if (state != IDLE && slotHandle) esp_ota_abort(slotHandle);
  slotHandle = 0;
  state = IDLE;
  from = nullptr;
  if (why) {
    lastError = why;
    console::say(String("update dropped (") + why + "): still running build " + String(BRISTLE_BUILD));
  }
  node::updateChanged();
}

// Reads the slot back out of flash and hashes it: what counts is what was really written.
static bool slotMatches() {
  mbedtls_sha256_context ctx;
  mbedtls_sha256_init(&ctx);
  mbedtls_sha256_starts(&ctx, 0);
  static uint8_t block[1024];
  for (uint32_t at = 0; at < total; at += sizeof(block)) {
    size_t n = total - at < sizeof(block) ? total - at : sizeof(block);
    if (esp_partition_read(slot, at, block, n) != ESP_OK) {
      mbedtls_sha256_free(&ctx);
      return false;
    }
    mbedtls_sha256_update(&ctx, block, n);
  }
  uint8_t have[32];
  mbedtls_sha256_finish(&ctx, have);
  mbedtls_sha256_free(&ctx);
  return crypto::equalCT(have, wantSha, 32);
}

void handle(Link& link, const codec::Msg& m, const String* id) {
  const String* op = m.get("op");
  if (!op) {
    rsp(link, false, bp::ERR_BAD_FIELD, id);
    return;
  }
  uint32_t now = millis();
  if (*op == "status") {
    status(link);
    return;
  }
  if (*op == "abort") {
    if (state != IDLE && from == &link) drop("the core stopped it");
    rsp(link, true, nullptr, id);
    status(link);
    return;
  }
  if (*op == "begin") {
    if (state != IDLE && from != &link) {
      rsp(link, false, bp::ERR_BUSY, id);
      return;
    }
    if (state != IDLE) drop(nullptr);                       // the core starts over
    const String* sha = m.get("sha");
    long size = m.getInt("size", 0);
    slot = esp_ota_get_next_update_partition(nullptr);
    if (!sha || !crypto::fromHex(*sha, wantSha, 32) || size <= 0) {
      rsp(link, false, bp::ERR_BAD_FIELD, id);
      return;
    }
    if (!slot || (uint32_t)size > slot->size) {
      rsp(link, false, bp::ERR_OTA_SPACE, id);
      return;
    }
    // Sequential writes: each part of the slot is erased just before it is written, so starting
    // an update never stalls the node for the seconds a whole-slot erase would take.
    if (esp_ota_begin(slot, OTA_WITH_SEQUENTIAL_WRITES, &slotHandle) != ESP_OK) {
      rsp(link, false, bp::ERR_OTA_STATE, id);
      return;
    }
    total = (uint32_t)size;
    got = 0;
    state = RECEIVING;
    from = &link;
    lastChunkMs = now;
    lastError = "";
    node::updateChanged();
    console::say("update started: " + String(total) + " bytes");
    rsp(link, true, nullptr, id);
    status(link);
    return;
  }
  if (state == IDLE || from != &link) {
    rsp(link, false, bp::ERR_OTA_STATE, id);
    status(link);
    return;
  }
  if (*op == "chunk") {
    const String* d = m.get("d");
    long o = m.getInt("o", -1);
    uint8_t data[bp::OTA_CHUNK_BYTES + 8];
    size_t n = 0;
    if (state == RECEIVING && d && (uint32_t)o == got && codec::b64urlDecode(*d, data, sizeof(data), n) && n > 0 &&
        got + n <= total) {
      if (esp_ota_write(slotHandle, data, n) != ESP_OK) {
        drop("flash write failed");
        status(link, bp::ERR_OTA_STATE);
        return;
      }
      got += n;
      lastChunkMs = now;
    }
    status(link);                   // every chunk, in order or not, is answered with the byte wanted next
    return;
  }
  if (*op == "verify") {
    if (state != RECEIVING || got != total) {
      rsp(link, false, bp::ERR_OTA_STATE, id);
      return;
    }
    if (!slotMatches()) {
      drop("the image does not match its SHA-256");
      status(link, bp::ERR_OTA_VERIFY);
      return;
    }
    state = READY;
    status(link);
    return;
  }
  if (*op == "commit") {
    if (state != READY) {
      rsp(link, false, bp::ERR_OTA_STATE, id);
      return;
    }
    // esp_ota_end checks that it is a valid app image for this chip before anything boots it.
    esp_err_t ended = esp_ota_end(slotHandle);
    slotHandle = 0;
    if (ended != ESP_OK || esp_ota_set_boot_partition(slot) != ESP_OK) {
      state = IDLE;
      from = nullptr;
      node::updateChanged();
      rsp(link, false, bp::ERR_OTA_VERIFY, id);
      status(link, bp::ERR_OTA_VERIFY);
      return;
    }
    rsp(link, true, nullptr, id);
    console::say("update complete: restarting into the new image (on trial until it meets its owner)");
    restartAt = now + 1200;         // long enough for the answer to leave on any link
    return;
  }
  rsp(link, false, bp::ERR_BAD_FIELD, id);
}

void linkClosed(Link& which) {
  if (state != IDLE && from == &which && !restartAt) drop("its link closed");
}

void tick(uint32_t nowMs, bool owned, bool ownerAuthed) {
  if (restartAt && (int32_t)(nowMs - restartAt) >= 0) {
    node::queue().flush();
    delay(50);
    ESP.restart();
  }
  if (state != IDLE && !restartAt && nowMs - lastChunkMs > STALL_MS) drop("nothing arrived for 90 s");
  if (!trial) return;
  if (!selftestRun && nowMs > 3000) {
    selftestRun = true;
    selftestOk = console::selftest(true);
  }
  bool met = owned ? ownerAuthed : nowMs > 60000;
  if (selftestRun && selftestOk && met) {
    esp_ota_mark_app_valid_cancel_rollback();
    trial = false;
    console::say("new firmware confirmed: build " + String(BRISTLE_BUILD) + " stays");
  } else if (nowMs > bp::OTA_CONFIRM_MS || (selftestRun && !selftestOk)) {
    console::say("new firmware not confirmed: going back to the previous build");
    node::queue().flush();
    delay(50);
    esp_ota_mark_app_invalid_rollback_and_reboot();
  }
}

String statusText() {
  String s = state == RECEIVING ? "receiving " + String(got) + " of " + String(total) + " bytes"
             : state == READY ? String("image checked, waiting for commit") : String("no update in progress");
  if (trial) s += "; THIS BUILD IS ON TRIAL (not confirmed yet)";
  if (lastError.length()) s += "; last update dropped: " + lastError;
  return s;
}

}  // namespace ota
