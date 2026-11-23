// Board settings for Bristle nodes. One sketch, picked at build time by the FQBN (see
// tools/build_firmware.ps1 / build_firmware.sh).
//
//   XIAO ESP32-C5:   esp32:esp32:XIAO_ESP32C5:CDCOnBoot=cdc,FlashSize=8M      -> xiao_c5
//   Generic C5:      esp32:esp32:esp32c5:CDCOnBoot=cdc,FlashSize=8M           -> devkit_c5
//                    (boards that only have a CH343/UART port: CDCOnBoot=default -> devkit_c5_uart)
//
// Flash layout for every board: partitions_bristle_8mb.csv (two 2.5 MB OTA app slots, a 2.875 MB
// LittleFS find queue, NVS). 8 MB flash minimum, always. Build with the scripts, not from the IDE:
// they are what hands that layout to the compiler.
#pragma once
#include <Arduino.h>

#if defined(ARDUINO_XIAO_ESP32C5)
  #define BRISTLE_BOARD "xiao_c5"
  #define BRISTLE_LED_PIN 27          // LED_BUILTIN on the XIAO ESP32-C5 variant
  #define BRISTLE_LED_ACTIVE_LOW 1    // VERIFY at checkpoint A (console: `ident 5`)
  #define BRISTLE_LED_RGB 0
#else
  // The name is also the firmware folder an update is taken from, so the build for boards whose
  // only USB port is a CH343/UART chip has its own.
  #if ARDUINO_USB_CDC_ON_BOOT
    #define BRISTLE_BOARD "devkit_c5"
  #else
    #define BRISTLE_BOARD "devkit_c5_uart"
  #endif
  #define BRISTLE_LED_PIN RGB_BUILTIN // DevKit-style boards: addressable RGB LED on GPIO27
  #define BRISTLE_LED_ACTIVE_LOW 0
  #define BRISTLE_LED_RGB 1
#endif

// BOOT button (strapping pin). VERIFY at checkpoint A: console `boot` prints its live state.
#ifndef BRISTLE_BOOT_PIN
  #define BRISTLE_BOOT_PIN 28
#endif

// Firmware build number: bump on every release (reported in HELLO and HB as b=).
#define BRISTLE_BUILD 5
