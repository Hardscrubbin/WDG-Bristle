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

String LineOut::finish() const {
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

bool parseLine(const char* line, size_t len, Msg& out) {
  while (len && (line[len - 1] == '\r' || line[len - 1] == '\n')) len--;
  if (len + 1 > bp::LINE_MAX_BYTES || len < 6) return false;
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
