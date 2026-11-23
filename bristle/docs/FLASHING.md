# Flashing Bristle nodes

Images are in `firmware/releases/<release>/<board>/`. Pick the folder for your board:

| Board | Folder |
|---|---|
| Seeed XIAO ESP32-C5 | `xiao_c5` |
| ESP32-C5 board with native USB (shows up as "USB JTAG/serial debug unit") | `devkit_c5` |
| ESP32-C5 board whose only USB port is a CH343/UART chip (e.g. Waveshare kit's UART port) | `devkit_c5_uart` |

8 MB flash minimum. Every first flash must **erase** the board and use the **FULL** image: that is
what puts the Bristle flash layout (two update slots and the find queue) on it.

Use `v1-build3-dev` or later. `v1-build2-dev` is the same firmware, but its images carry folder
names of the PC that built them. `v1-build1-dev` has an older flash layout: don't flash it.

The images contain third-party software (Espressif's Arduino core and ESP-IDF, NimBLE and others).
`THIRD_PARTY_NOTICES.md` next to the images lists it, with the licence texts in `licenses/`.

## First flash from the PC (browser)

Seeed XIAO web flasher (or any ESP Web Tools / esptool-js page), Chrome or Edge:
1. Plug the board in with a USB-C **data** cable.
2. Connect, pick the port.
3. Choose `bristle-<board>-FULL.bin`, address **0x0**, tick **erase**.
4. Flash, then press RESET (or unplug and replug).

If it won't connect: hold BOOT, tap RESET, release BOOT, try again.

## First flash with esptool (PC or uConsole)

On the PC, from the Bristle folder (replace COM5 with your port, and the board folder):
```
& "$env:USERPROFILE\PigletBuild\data\packages\esp32\tools\esptool_py\5.3.0\esptool.exe" --chip esp32c5 --port COM5 erase-flash
& "$env:USERPROFILE\PigletBuild\data\packages\esp32\tools\esptool_py\5.3.0\esptool.exe" --chip esp32c5 --port COM5 --baud 921600 write-flash 0x0 "firmware\releases\v1-build3-dev\xiao_c5\bristle-xiao_c5-FULL.bin"
```

On the uConsole:
```
esptool --chip esp32c5 --port /dev/ttyACM0 erase-flash
esptool --chip esp32c5 --port /dev/ttyACM0 --baud 921600 write-flash 0x0 bristle-xiao_c5-FULL.bin
```
The port is `/dev/ttyACM…` for native USB and for CH343 boards. Never point esptool at the GPS
dongle or at another board: check which is which first with `ls -l /dev/serial/by-id/`.

## Updating a node that already runs Bristle build 2 or later

App image only, no erase. Keeps owner, node number, plan and stored finds:
```
esptool --chip esp32c5 --port <PORT> write-flash 0x10000 bristle-<board>-app.bin
```
(Over-the-air updates come later; see `PLAN.md`.)

## Check it

Open a serial terminal at 115200 (Arduino Serial Monitor, PuTTY, or on the uConsole:
`python3 -m serial.tools.miniterm /dev/ttyACM0 115200`). Press RESET. You should see:
```
# Bristle node build 3 (xiao_c5), hw a0b1c2d3e4f5, no owner yet
```
Type `selftest`: every line must say PASS, ending with `SELFTEST PASS`.
Type `status`: it must say `flash layout ok`. If it says `flash layout WRONG`, the FULL image did
not go on at 0x0: flash it again with erase.
