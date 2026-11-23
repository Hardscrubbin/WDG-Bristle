#include "Link_Usb.h"

#include "Codec.h"
#include "Console.h"
#include "Node.h"

UsbLink usbLink;

void UsbLink::begin() {
  // Buffer sizes have to be set before begin().
  Serial.setRxBufferSize(RX_BUFFER_BYTES);
  Serial.setTxBufferSize(TX_BUFFER_BYTES);
#if ARDUINO_USB_CDC_ON_BOOT
  // Native USB: never wait for a host that isn't reading (the default waits up to 2 s per write,
  // which would stall scanning whenever the port is plugged in but nobody has it open).
  Serial.setTxTimeoutMs(0);
#endif
  Serial.begin(bp::USB_BAUD);   // the baud rate only matters on boards with a CH343/UART port
  delay(200);
}

size_t UsbLink::room() {
  int n = Serial.availableForWrite();
  return n > 0 ? (size_t)n : 0;
}

bool UsbLink::send(const String& line) {
  if (room() < line.length()) {
    unsent_++;
    return false;
  }
  Serial.print(line);
  return true;
}

void UsbLink::poll() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c != '\n' && c != '\r') {
      if (len_ < bp::LINE_MAX_BYTES) buf_[len_++] = c;
      else overflow_ = true;
      continue;
    }
    if (overflow_) {
      node::badLine();
    } else if (len_) {
      buf_[len_] = 0;
      // A protocol line ends in '*' and four CRC digits. Anything else is someone typing.
      if (len_ > 6 && buf_[len_ - 5] == '*') {
        codec::Msg m;
        if (codec::parseLine(buf_, len_, m)) {
          lastRxMs = millis();
          node::onMessage(*this, m);
        } else {
          node::badLine();
        }
      } else {
        console::run(String(buf_));
      }
    }
    len_ = 0;
    overflow_ = false;
  }
}
