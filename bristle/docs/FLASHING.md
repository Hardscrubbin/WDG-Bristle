# Flashing Bristle nodes

Images are in `firmware/releases/<release>/<board>/`. Pick the folder for your board:

| Board | Folder |
|---|---|
| Seeed XIAO ESP32-C5 | `xiao_c5` |
| ESP32-C5 board with native USB (shows up as "USB JTAG/serial debug unit") | `devkit_c5` |
| ESP32-C5 board whose only USB port is a CH343/UART chip (e.g. Waveshare kit's UART port) | `devkit_c5_uart` |

8 MB flash minimum. Every first flash must **erase** the board and use the **FULL** image: that is
what puts the Bristle flash layout (two update slots and the find queue) on it.

Use `v1-build5-dev` or later. Earlier builds are superseded: build 4 lets a replayed ESP-NOW frame
cut a node off from its bridge for 20 s (fixed in 5; a board on build 4 can be updated over the
air), build 3 has only the USB link and no updates over the air, build 2's images carry folder names of the PC that built them, and build 1
has an older flash layout. Don't flash them.

The images contain third-party software (Espressif's Arduino core and ESP-IDF, NimBLE and others).
`THIRD_PARTY_NOTICES.md` next to the images lists it, with the licence texts in `licenses/`.

There are three ways to get firmware onto a board. Nothing here has been done on a real board yet:
see `BENCH_TESTS.md` for the steps with their result tables.

| | When | Erases the board? |
|---|---|---|
| A. From the uConsole, in the game | a blank board, or a node on USB | first flash: yes. A node you already adopted: no. |
| B. From a PC | a blank board, before it ever meets the uConsole | yes |
| C. Over the air, from the Nodes screen | a node that is linked (USB, bridge or Bluetooth) | no |

## A. From the uConsole (PLUGINS > Bristle: Nodes)

1. Plug the board into the powered hub with a USB-C **data** cable. It shows up as a `NEW PORT`
   row (a blank board) or as one of your nodes.
2. Select the row and press **W**. A box shows the port, the board type and what will happen.
3. **LEFT/RIGHT** picks the board type. Check it: the wrong type leaves the board without a
   working USB console (flash it again with the right one to fix that).
4. **ENTER** writes it. The line above the messages shows esptool's progress. Don't unplug.
   - A `NEW PORT` row: the whole flash is erased, then the FULL image goes on at 0x0.
   - One of your nodes: only the app is written. Owner, node number, settings and stored finds
     stay.
5. The board restarts, announces itself and shows up as a `NEW` row. **A** adopts it.

The game takes the newest release in `bristle/firmware/releases/` of its install folder (or the
folder named by `firmware_dir` in `bristle.conf`), and uses an image only if its SHA-256 matches
`SHA256SUMS`. It refuses a port that is excluded, marked "not a node" or held by the game's own
ESP32, and it never offers the GPS dongle: only ESP32 USB chips are listed at all.

If esptool cannot connect: hold BOOT, tap RESET, release BOOT, press **W** again.

## B. From a PC

### In a browser

Seeed XIAO web flasher (or any ESP Web Tools / esptool-js page), Chrome or Edge:
1. Plug the board in with a USB-C **data** cable.
2. Connect, pick the port.
3. Choose `bristle-<board>-FULL.bin`, address **0x0**, tick **erase**.
4. Flash, then press RESET (or unplug and replug).

If it won't connect: hold BOOT, tap RESET, release BOOT, try again.

### With esptool

On the PC, from the Bristle folder (replace COM5 with your port, and the board folder):
```
& "$env:USERPROFILE\PigletBuild\data\packages\esp32\tools\esptool_py\5.3.0\esptool.exe" --chip esp32c5 --port COM5 erase-flash
& "$env:USERPROFILE\PigletBuild\data\packages\esp32\tools\esptool_py\5.3.0\esptool.exe" --chip esp32c5 --port COM5 --baud 921600 write-flash 0x0 "firmware\releases\v1-build4-dev\xiao_c5\bristle-xiao_c5-FULL.bin"
```

On the uConsole, by hand:
```
esptool --chip esp32c5 --port /dev/ttyACM0 erase-flash
esptool --chip esp32c5 --port /dev/ttyACM0 --baud 921600 write-flash 0x0 bristle-xiao_c5-FULL.bin
```
The port is `/dev/ttyACM…` for native USB and for CH343 boards. Never point esptool at the GPS
dongle or at another board: check which is which first with `ls -l /dev/serial/by-id/`.

### Updating by hand, keeping settings

App image only. Keeps owner, node number, plan and stored finds. The first command clears the two
sectors that say which update slot boots, so the board starts the app you are about to write (after
an update over the air it may be running from the other slot):
```
esptool --chip esp32c5 --port <PORT> erase-region 0xe000 0x2000
esptool --chip esp32c5 --port <PORT> --baud 921600 write-flash 0x10000 bristle-<board>-app.bin
```

## C. Over the air (PLUGINS > Bristle: Nodes)

1. Select a node that is online. Press **U**, then **U** again to confirm.
2. The STATE column shows `upd 37%`; the line above the messages shows the link and the progress.
   One node at a time. The node stops scanning during its update.
3. At 100% the node checks the image it stored (SHA-256, read back from flash), switches to it and
   restarts. The messages say `Node 3 now runs build 5`, or that it is back on its old build.

What protects the node:
- Nothing changes what it boots until the whole image has arrived and checked out. Pull the cable,
  drive out of range or press **U** twice to stop: it stays on the firmware it had.
- The new image starts on trial. If it does not pass its own self-test and meet your core within 5
  minutes, or it resets before that, the node goes back to the previous firmware by itself.

Expected time for one node (not measured yet): about 1 minute over a native-USB port, 3 to 4
minutes over a UART port, 3 to 5 minutes through the bridge or over Bluetooth.

## Check it

Open a serial terminal at 115200 (Arduino Serial Monitor, PuTTY, or on the uConsole:
`python3 -m serial.tools.miniterm /dev/ttyACM0 115200`). Press RESET. You should see:
```
# Bristle node build 5 (xiao_c5), hw a0b1c2d3e4f5, no owner yet
```
and then, once a second while nothing has spoken to the board, a line starting `BCN:`. That is the
board saying "a Bristle board is here"; it stops for a minute when you type a command.

Type `selftest`: every line must say PASS (15 of them), ending with `SELFTEST PASS`.
Type `status`: it must say `flash layout ok`. If it says `flash layout WRONG`, the FULL image did
not go on at 0x0: flash it again with erase.
Type `links` for the ESP-NOW and Bluetooth links, `update` for the state of a firmware update,
`queue` for the find queue.
