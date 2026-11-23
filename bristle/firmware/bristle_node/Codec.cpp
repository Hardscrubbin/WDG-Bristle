#include "Codec.h"

#include "BristleProto.h"

namespace codec {

static const char HEXU[] = "0123456789ABCDEF";

uint16_t crc16(const uint8_t* data, size_t len) {
  uint16_t crc = 0xFFFF;
  for (size_t i = 0; i < len; i++) {
    crc ^= (uint16_t)data[i] << 8;
    for (int b = 0; b < 8; b++) crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021) : (uint16_t)(crc << 1);
  }
  return crc;
}

static bool unreserved(uint8_t c) {
  return (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '-' ||
         c == '_' || c == '.' || c == '~' || c == '/';
}

String pctEncode(const String& in) {
  String out;
  out.reserve(in.length() + 8);
  for (size_t i = 0; i < in.length(); i++) {
    uint8_t c = (uint8_t)in[i];
    if (unreserved(c)) {
      out += (char)c;
    } else {
      out += '%';
      out += HEXU[c >> 4];
      out += HEXU[c & 15];
    }
  }
  return out;
}

static int hexv(char c) {
  if (c >= '0' && c <= '9') return c - '0';
  if (c >= 'a' && c <= 'f') return c - 'a' + 10;
  if (c >= 'A' && c <= 'F') return c - 'A' + 10;
  return -1;
}

bool pctDecode(const char* in, size_t len, String& out) {
  out = "";
  out.reserve(len);
  for (size_t i = 0; i < len; i++) {
    if (in[i] == '%') {
      if (i + 2 >= len) return false;   // need two hex digits after '%'
      int a = hexv(in[i + 1]), b = hexv(in[i + 2]);
      if (a < 0 || b < 0) return false;
      out += (char)((a << 4) | b);
      i += 2;
    } else {
      out += in[i];
    }
  }
  return true;
}

LineOut::LineOut(const char* type) {
  s_.reserve(160);
  s_ = type;
  s_ += ':';
}

void LineOut::sep() {
  if (!first_) s_ += ',';
  first_ = false;
}

LineOut& LineOut::add(const char* key, const String& value) {
  sep();
  s_ += key;
  s_ += '=';
  s_ += pctEncode(value);
  return *this;
}

LineOut& LineOut::add(const char* key, const char* value) { return add(key, String(value)); }

LineOut& LineOut::add(const char* key, long value) {
  sep();
  s_ += key;
  s_ += '=';
  if (value < 0) {   // '-' is unreserved, so negative numbers need no escaping
    s_ += '-';
    s_ += String((unsigned long)(-value));
  } else {
    s_ += String((unsigned long)value);
  }
  return *this;
}

LineOut& LineOut::addu(const char* key, unsigned long value) {
  sep();
  s_ += key;
  s_ += '=';
  s_ += String(value);
  return *this;
}

LineOut& LineOut::addRaw(const char* key, const String& value) {
  sep();
  s_ += key;
  s_ += '=';
  s_ += value;
  return *this;
}

String LineOut::finish() const { return withCrc(s_); }

String stripCrc(const String& line) {
  int end = line.length();
  while (end > 0 && (line[end - 1] == '\n' || line[end - 1] == '\r')) end--;
  if (end >= 5 && line[end - 5] == '*') end -= 5;
  return line.substring(0, end);
}

static const char B64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

String b64urlEncode(const uint8_t* data, size_t len) {
  String out;
  out.reserve((len * 4 + 2) / 3);
  for (size_t i = 0; i < len; i += 3) {
    uint32_t v = (uint32_t)data[i] << 16;
    if (i + 1 < len) v |= (uint32_t)data[i + 1] << 8;
    if (i + 2 < len) v |= data[i + 2];
    out += B64[(v >> 18) & 63];
    out += B64[(v >> 12) & 63];
    if (i + 1 < len) out += B64[(v >> 6) & 63];
    if (i + 2 < len) out += B64[v & 63];
  }
  return out;
}

static int b64v(char c) {
  if (c >= 'A' && c <= 'Z') return c - 'A';
  if (c >= 'a' && c <= 'z') return c - 'a' + 26;
  if (c >= '0' && c <= '9') return c - '0' + 52;
  if (c == '-') return 62;
  if (c == '_') return 63;
  return -1;
}

bool b64urlDecode(const String& text, uint8_t* out, size_t cap, size_t& len) {
  len = 0;
  size_t n = text.length();
  if (n % 4 == 1) return false;
  for (size_t i = 0; i < n; i += 4) {
    int v[4] = {0, 0, 0, 0};
    size_t have = n - i < 4 ? n - i : 4;
    for (size_t k = 0; k < have; k++) {
      v[k] = b64v(text[i + k]);
      if (v[k] < 0) return false;
    }
    uint32_t w = ((uint32_t)v[0] << 18) | ((uint32_t)v[1] << 12) | ((uint32_t)v[2] << 6) | (uint32_t)v[3];
    size_t bytes = have - 1;
    if (len + bytes > cap) return false;
    out[len++] = (uint8_t)(w >> 16);
    if (bytes > 1) out[len++] = (uint8_t)(w >> 8);
    if (bytes > 2) out[len++] = (uint8_t)w;
  }
  return true;
}

String withCrc(const String& s_) {
  uint16_t c = crc16((const uint8_t*)s_.c_str(), s_.length());
  String out = s_;
  out += '*';
  out += HEXU[(c >> 12) & 15];
  out += HEXU[(c >> 8) & 15];
  out += HEXU[(c >> 4) & 15];
  out += HEXU[c & 15];
  out += '\n';
  return out;
}

const String* Msg::get(const char* k) const {
  for (int i = 0; i < n; i++)
    if (strcmp(key[i], k) == 0) return &val[i];
  return nullptr;
}

long Msg::getInt(const char* k, long dflt) const {
  const String* v = get(k);
  if (!v || v->length() == 0) return dflt;
  return v->toInt();
}

bool parseBody(const char* body, size_t len, Msg& out) {
  out.n = 0;
  size_t colon = 0;
  while (colon < len && body[colon] != ':') colon++;
  if (colon == len || colon < 2 || colon > 6) return false;
  for (size_t i = 0; i < colon; i++) {
    if (body[i] < 'A' || body[i] > 'Z') return false;
    out.type[i] = body[i];
  }
  out.type[colon] = 0;
  size_t p = colon + 1;
  while (p < len) {
    size_t end = p;
    while (end < len && body[end] != ',') end++;
    size_t eq = p;
    while (eq < end && body[eq] != '=') eq++;
    if (eq == end || eq == p || eq - p >= sizeof(out.key[0])) return false;
    if (out.n >= MAX_FIELDS) return false;
    memcpy(out.key[out.n], body + p, eq - p);
    out.key[out.n][eq - p] = 0;
    if (!pctDecode(body + eq + 1, end - eq - 1, out.val[out.n])) return false;
    out.n++;
    p = end + 1;
  }
  return true;
}

bool parseLine(const char* line, size_t len, Msg& out, size_t maxBytes) {
  while (len && (line[len - 1] == '\r' || line[len - 1] == '\n')) len--;
  if (len + 1 > (maxBytes ? maxBytes : (size_t)bp::LINE_MAX_BYTES) || len < 6) return false;
  if (line[len - 5] != '*') return false;
  int h[4];
  for (int i = 0; i < 4; i++) {
    h[i] = hexv(line[len - 4 + i]);
    if (h[i] < 0) return false;
  }
  uint16_t want = (h[0] << 12) | (h[1] << 8) | (h[2] << 4) | h[3];
  if (crc16((const uint8_t*)line, len - 5) != want) return false;
  return parseBody(line, len - 5, out);
}

}  // namespace codec
