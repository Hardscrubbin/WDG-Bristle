#include "Owner.h"

#include <esp_random.h>

#include "Boards.h"
#include "BristleProto.h"
#include "Crypto.h"

namespace owner {

static NodeState* st = nullptr;
static const uint8_t* hw = nullptr;
static uint32_t bootDownAt = 0;
static bool bootArmed = false;

void begin(NodeState& state, const uint8_t hwid[6]) {
  st = &state;
  hw = hwid;
  pinMode(BRISTLE_BOOT_PIN, INPUT_PULLUP);
  bootArmed = digitalRead(BRISTLE_BOOT_PIN) == HIGH;   // not armed if it is held at power-up
}

String id() { return st->owned ? crypto::ownerId(st->key) : String(); }

bool onHi(Link& link, const codec::Msg& m) {
  const String* cn = m.get("cn");
  if (!cn || !crypto::fromHex(*cn, link.coreNonce, bp::AUTH_NONCE_BYTES)) return false;
  link.helloSeen = true;
  link.authed = false;
  esp_fill_random(link.nodeNonce, bp::AUTH_NONCE_BYTES);
  return true;
}

void fillHello(Link& link, codec::LineOut& hello) {
  hello.addRaw("nn", crypto::toHex(link.nodeNonce, bp::AUTH_NONCE_BYTES));
  if (!st->owned) return;
  uint8_t np[bp::AUTH_PROOF_BYTES];
  crypto::nodeProof(st->key, link.coreNonce, hw, np);
  hello.addRaw("np", crypto::toHex(np, bp::AUTH_PROOF_BYTES)).addRaw("oid", crypto::ownerId(st->key));
  if (st->number) hello.addu("n", st->number);
}

bool onAuth(Link& link, const codec::Msg& m) {
  const String* cp = m.get("cp");
  uint8_t got[bp::AUTH_PROOF_BYTES], want[bp::AUTH_PROOF_BYTES];
  link.authed = false;
  // helloSeen: the proof must answer the nonce we made for this HI, never an older one.
  if (!st->owned || !link.helloSeen || !cp || !crypto::fromHex(*cp, got, bp::AUTH_PROOF_BYTES)) return false;
  crypto::coreProof(st->key, link.nodeNonce, hw, want);
  link.authed = crypto::equalCT(got, want, bp::AUTH_PROOF_BYTES);
  return link.authed;
}

const char* onAdopt(Link& link, const codec::Msg& m) {
  if (!link.allowsAdoption()) return bp::ERR_NOT_USB;
  if (st->owned) return bp::ERR_OWNED;
  const String* k = m.get("k");
  long n = m.getInt("n", 0);
  uint8_t key[bp::AUTH_KEY_BYTES];
  if (!k || !crypto::fromHex(*k, key, bp::AUTH_KEY_BYTES) || n <= 0 || n > 65535) return bp::ERR_BAD_FIELD;
  memcpy(st->key, key, sizeof(key));
  st->owned = true;
  st->number = (uint16_t)n;
  store::saveOwner(*st);
  store::saveNumber(st->number);
  return nullptr;
}

bool bootHeld(uint32_t nowMs) {
  if (!bootArmed) return false;
  if (digitalRead(BRISTLE_BOOT_PIN) != LOW) {
    bootDownAt = 0;
    return false;
  }
  if (!bootDownAt) {
    bootDownAt = nowMs | 1;
    return false;
  }
  if (nowMs - bootDownAt <= bp::FORGET_HOLD_MS) return false;
  bootDownAt = 0;
  bootArmed = false;   // once per boot
  return true;
}

}  // namespace owner
