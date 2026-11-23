// Bristle line codec (node side). Must match core/bristle/codec.py byte for byte.
#pragma once
#include <Arduino.h>

namespace codec {

uint16_t crc16(const uint8_t* data, size_t len);
String pctEncode(const String& in);
bool pctDecode(const char* in, size_t len, String& out);

// Builds "TYPE:k=v,k=v" then finish() appends "*CRC\n".
class LineOut {
 public:
  explicit LineOut(const char* type);
  LineOut& add(const char* key, const String& value);   // value is percent-encoded here
  LineOut& add(const char* key, const char* value);
  LineOut& add(const char* key, long value);
  LineOut& addu(const char* key, unsigned long value);
  LineOut& addRaw(const char* key, const String& value); // value already safe (hex, digits)
  const String& body() const { return s_; }
  String finish() const;                                 // with *CRC and '\n'
 private:
  void sep();
  String s_;
  bool first_ = true;
};

constexpr int MAX_FIELDS = 16;

struct Msg {
  char type[8] = {0};
  int n = 0;
  char key[MAX_FIELDS][6];
  String val[MAX_FIELDS];
  const String* get(const char* k) const;
  bool has(const char* k) const { return get(k) != nullptr; }
  long getInt(const char* k, long dflt) const;
};

// Parses a full line (no trailing newline). Returns false on bad CRC / malformed / too long.
bool parseLine(const char* line, size_t len, Msg& out);
// Parses a body (no CRC), e.g. inside a signed ESP-NOW frame.
bool parseBody(const char* body, size_t len, Msg& out);

}  // namespace codec
