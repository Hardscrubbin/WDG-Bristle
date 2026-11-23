// Owner lock and frame signing (node side). Must match core/bristle/crypto.py.
#pragma once
#include <Arduino.h>

namespace crypto {

void hmacSha256(const uint8_t* key, size_t keyLen, const uint8_t* const* parts, const size_t* lens,
                int nParts, uint8_t out[32]);
String toHex(const uint8_t* d, size_t n);
bool fromHex(const String& s, uint8_t* out, size_t n);
bool equalCT(const uint8_t* a, const uint8_t* b, size_t n);   // constant time

String ownerId(const uint8_t key[16]);
void nodeProof(const uint8_t key[16], const uint8_t coreNonce[16], const uint8_t hwid[6], uint8_t out[16]);
void coreProof(const uint8_t key[16], const uint8_t nodeNonce[16], const uint8_t hwid[6], uint8_t out[16]);
// Builds an ESP-NOW frame into out (returns length, 0 on error).
size_t frameBuild(const uint8_t key[16], uint8_t kind, uint32_t ctr, uint8_t frag, const uint8_t* payload,
                  size_t plen, uint8_t* out, size_t outCap);

}  // namespace crypto
