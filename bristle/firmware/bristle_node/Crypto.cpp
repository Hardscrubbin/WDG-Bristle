#include "Crypto.h"

#include <mbedtls/md.h>

#include "BristleProto.h"

namespace crypto {

void hmacSha256(const uint8_t* key, size_t keyLen, const uint8_t* const* parts, const size_t* lens,
                int nParts, uint8_t out[32]) {
  mbedtls_md_context_t ctx;
  mbedtls_md_init(&ctx);
  mbedtls_md_setup(&ctx, mbedtls_md_info_from_type(MBEDTLS_MD_SHA256), 1);
  mbedtls_md_hmac_starts(&ctx, key, keyLen);
  for (int i = 0; i < nParts; i++) mbedtls_md_hmac_update(&ctx, parts[i], lens[i]);
  mbedtls_md_hmac_finish(&ctx, out);
  mbedtls_md_free(&ctx);
}

String toHex(const uint8_t* d, size_t n) {
  static const char H[] = "0123456789abcdef";
  String s;
  s.reserve(n * 2);
  for (size_t i = 0; i < n; i++) {
    s += H[d[i] >> 4];
    s += H[d[i] & 15];
  }
  return s;
}

static int hv(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

bool fromHex(const String& s, uint8_t* out, size_t n) {
  if (s.length() != n * 2) return false;
  for (size_t i = 0; i < n; i++) {
    int a = hv(s[2 * i]), b = hv(s[2 * i + 1]);
    if (a < 0 || b < 0) return false;
    out[i] = (uint8_t)((a << 4) | b);
  }
  return true;
}

bool equalCT(const uint8_t* a, const uint8_t* b, size_t n) {
  uint8_t d = 0;
  for (size_t i = 0; i < n; i++) d |= a[i] ^ b[i];
  return d == 0;
}

String ownerId(const uint8_t key[16]) {
  const uint8_t* parts[] = {(const uint8_t*)bp::LABEL_OWNER_ID};
  size_t lens[] = {strlen(bp::LABEL_OWNER_ID)};
  uint8_t h[32];
  hmacSha256(key, bp::AUTH_KEY_BYTES, parts, lens, 1, h);
  return toHex(h, bp::AUTH_OWNER_ID_BYTES);
}

static void proof(const char* label, const uint8_t key[16], const uint8_t nonce[16], const uint8_t hwid[6],
                  uint8_t out[16]) {
  const uint8_t* parts[] = {(const uint8_t*)label, nonce, hwid};
  size_t lens[] = {strlen(label), bp::AUTH_NONCE_BYTES, 6};
  uint8_t h[32];
  hmacSha256(key, bp::AUTH_KEY_BYTES, parts, lens, 3, h);
  memcpy(out, h, bp::AUTH_PROOF_BYTES);
}

void nodeProof(const uint8_t key[16], const uint8_t coreNonce[16], const uint8_t hwid[6], uint8_t out[16]) {
  proof(bp::LABEL_NODE, key, coreNonce, hwid, out);
}

void coreProof(const uint8_t key[16], const uint8_t nodeNonce[16], const uint8_t hwid[6], uint8_t out[16]) {
  proof(bp::LABEL_CORE, key, nodeNonce, hwid, out);
}

size_t frameBuild(const uint8_t key[16], uint8_t kind, uint32_t ctr, uint8_t frag, const uint8_t* payload,
                  size_t plen, uint8_t* out, size_t outCap) {
  size_t total = bp::FRAME_HEADER_BYTES + plen + bp::FRAME_TAG_BYTES;
  if (total > bp::FRAME_MAX_BYTES || total > outCap || plen > 255) return 0;
  out[0] = bp::FRAME_MAGIC0;
  out[1] = bp::FRAME_MAGIC1;
  out[2] = bp::PROTOCOL_VERSION;
  out[3] = kind;
  out[4] = ctr & 0xFF;
  out[5] = (ctr >> 8) & 0xFF;
  out[6] = (ctr >> 16) & 0xFF;
  out[7] = (ctr >> 24) & 0xFF;
  out[8] = frag;
  out[9] = (uint8_t)plen;
  memcpy(out + bp::FRAME_HEADER_BYTES, payload, plen);
  const uint8_t* parts[] = {(const uint8_t*)bp::FRAME_HMAC_LABEL, out};
  size_t lens[] = {strlen(bp::FRAME_HMAC_LABEL), bp::FRAME_HEADER_BYTES + plen};
  uint8_t h[32];
  hmacSha256(key, bp::AUTH_KEY_BYTES, parts, lens, 2, h);
  memcpy(out + bp::FRAME_HEADER_BYTES + plen, h, bp::FRAME_TAG_BYTES);
  return total;
}

}  // namespace crypto
