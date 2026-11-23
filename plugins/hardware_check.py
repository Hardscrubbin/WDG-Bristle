"""[merge-v3] PLUGINS > Hardware Check -- everything the game can see, live.

One screen for the uConsole rig: board (CM4/CM5), CPU temperature, power
(under-voltage is shown loudly), the four AIO v2 rails with whether the part
behind each one actually answers, the GPS receivers, who is using the
RTL-SDR, the ESP32, every Wi-Fi adapter (driver, monitor mode, used by
Kismet) and every Bluetooth controller.

Read-only. Every probe runs on a worker thread ("hw-check"), never on the
game's; the screen redraws from the last finished snapshot. Nothing is
switched unless a key is pressed:
    R  probe again now          T  RTL-SDR tuner test (rtl_test -t)
    G / S / U  toggle the GPS / SDR / internal-USB rail through the game's own
               SYSTEM toggles (the same thing as SYSTEM > GPS / SDR / USB)

What "answers" means per rail
    GPS    gpsd is reporting the UART receiver (or the game reads it itself)
    LoRa   a mesh protocol is running on it, or -- when nothing is using the
           radio -- the SX1262 returns a valid status byte on SPI
    SDR    the game is receiving with it, or the RTL2832U is on USB and
           rtl_test found its tuner. The tuner test is never run while the
           game (or anything else) is using the SDR.
    USB    the rail state only; what is plugged into the internal port shows
           up under Wi-Fi / ESP32 like any other USB device.
"""
import os
import re
import subprocess
import threading
import time

from plugins.plugin_base import PluginBase, PluginMenuItem

FONT_W = 5                      # upstream draws every pyxel.text() with Spleen 5x8 (app.py)
C_BG, C_BAR, C_TITLE, C_TEXT, C_DIM = 0, 1, 3, 7, 13
C_OK, C_WARN, C_ERR, C_BORDER = 11, 10, 8, 5
REFRESH_SECONDS = 3.0
COLUMN_CHARS = 62               # 310 px per column at 5 px per character
LEFT_X, RIGHT_X = 6, 326
RTL_SDR_IDS = ("0bda:2838", "0bda:2832")
# Realtek 8812AU / 88x2BU "AC1200" sticks, by USB id and by the names lsusb prints
AC1200_IDS = ("0bda:8812", "0bda:881a", "0bda:881b", "0bda:881c", "0bda:b812", "0bda:b82c",
              "2357:0101", "2357:0103", "2357:010d", "2357:0115", "2357:012d", "7392:a822",
              "0b05:17d2", "050d:1109", "2001:3315", "0846:9052", "2604:0012")
AC1200_NAME = re.compile(r"8812\s*au|88x2\s*bu|8822\s*bu|8812\s*bu|ac1200", re.IGNORECASE)
MONITOR_DRIVER = re.compile(r"8812au|88xxau|88x2bu|8822bu|rtw_?88", re.IGNORECASE)
RAILS = (("gps", "GPS", 27), ("lora", "LoRa", 16), ("sdr", "SDR", 7), ("usb", "USB", 23))


def board_name(model: str) -> str:
    """"CM5" / "CM4" (+ the model string) from the device-tree model."""
    model = (model or "").replace("\x00", "").strip()
    match = re.search(r"Compute Module (\d)", model)
    if match:
        return "CM%s" % match.group(1)
    match = re.search(r"Raspberry Pi (\d)", model)
    if match:
        return "Pi %s" % match.group(1)
    return "unknown board" if not model else model[:20]


def clip(text, limit=COLUMN_CHARS) -> str:
    text = str(text)
    return text if len(text) <= limit else text[:limit - 2] + ".."


class Probes:
    """Every look at the machine, one small method each, so tests and the
    simulations can replace them. All of them may block: worker thread only."""

    def model(self) -> str:
        for path in ("/proc/device-tree/model", "/sys/firmware/devicetree/base/model"):
            try:
                with open(path, "rb") as fh:
                    return fh.read(200).decode("utf-8", "ignore")
            except OSError:
                continue
        return ""

    def rails(self):
        """{feature: True/False/None} or None when this is not an AIO board."""
        from watchdogs.aio_manager import AioManager
        if not AioManager.is_installed():
            return None
        return {feature: AioManager.get_feature_state(feature) for feature, _name, _pin in RAILS}

    def usb_devices(self) -> dict:
        from plugins import _w5_hardware as hw
        return hw._lsusb_map()

    def wifi(self) -> list:
        from plugins import _w5_hardware as hw
        return hw.detect_wifi()

    def ble(self) -> list:
        from plugins import _w5_hardware as hw
        return hw.detect_ble()

    def serial_ports(self) -> list:
        from watchdogs.serial_manager import list_usb_serial_devices
        return list_usb_serial_devices()

    def rtl_test(self) -> tuple:
        """(state, text): "ok" with the tuner name, "busy", "none" or "error"."""
        try:
            done = subprocess.run(["rtl_test", "-t"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True, timeout=8)
            out = done.stdout or ""
        except FileNotFoundError:
            return "error", "rtl_test is not installed (apt install rtl-sdr)"
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout or ""
            if isinstance(out, bytes):
                out = out.decode("utf-8", "replace")
            if "tuner" not in out:
                return "error", "rtl_test did not answer within 8 s"
        except OSError as exc:
            return "error", "rtl_test: %s" % exc
        if "usb_claim_interface" in out or "Device or resource busy" in out:
            return "busy", "in use by another program"
        tuner = re.search(r"Found (.+?) tuner", out)
        if tuner:
            return "ok", "tuner test OK (%s)" % tuner.group(1).strip()[:28]
        if "No supported devices" in out:
            return "none", "no RTL-SDR found by rtl_test"
        return "error", "rtl_test gave no tuner line"

    def sx1262(self) -> tuple:
        """(state, text) from one GetStatus on /dev/spidev1.0. Only called
        while no mesh protocol is using the radio."""
        device = "/dev/spidev1.0"
        if not os.path.exists(device):
            return "error", "no %s (SPI1 overlay not enabled?)" % device
        try:
            users = subprocess.run(["fuser", device], stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=4)
            if users.stdout.strip():
                return "busy", "SPI in use by another program (meshtasticd?)"
        except (OSError, subprocess.TimeoutExpired):
            pass
        try:
            import spidev
        except ImportError:
            return "error", "cannot test: python spidev is not installed"
        bus = spidev.SpiDev()
        try:
            bus.open(1, 0)
            bus.max_speed_hz = 1_000_000
            bus.mode = 0
            reply = bus.xfer2([0xC0, 0x00])            # GetStatus
        except OSError as exc:
            return "error", "SPI error: %s" % exc
        finally:
            try:
                bus.close()
            except Exception:
                pass
        status = reply[1] if len(reply) > 1 else 0
        mode = (status >> 4) & 0x7
        if status not in (0x00, 0xFF) and 2 <= mode <= 6:
            return "ok", "SX1262 answers on SPI (status 0x%02X)" % status
        return "none", "no answer on SPI (status 0x%02X)" % status


class HardwareCheck(PluginBase):
    NAME = "Hardware Check"
    VERSION = "1.0"
    AUTHOR = "WDG-Bristle"

    def __init__(self):
        super().__init__()
        self.has_overlay = True
        self._overlay_active = False
        self.probes = Probes()
        self._snap = None                      # the last finished snapshot
        self._busy = False
        self._lock = threading.Lock()
        self._thread = None
        self._next = 0.0
        self._want_tuner_test = True           # once when the screen opens, then on T
        self._tuner = None                     # (state, text) of the last rtl_test
        self._lora_probe = None

    def menu_items(self) -> list[PluginMenuItem]:
        return [PluginMenuItem("h", "Hardware Check", "open_overlay")]

    @property
    def overlay_active(self) -> bool:
        return self._overlay_active

    def open_overlay(self):
        self._overlay_active = True
        self._want_tuner_test = True
        self._next = 0.0

    # ------------------------------------------------------------------
    # Snapshot (worker thread)
    # ------------------------------------------------------------------

    def _start_refresh(self) -> bool:
        with self._lock:
            if self._busy:
                return False
            self._busy = True
        thread = threading.Thread(target=self._refresh, name="hw-check", daemon=True)
        self._thread = thread
        try:
            thread.start()
        except RuntimeError:
            self._busy = False
            return False
        return True

    def _refresh(self) -> None:
        try:
            self._snap = self.collect()
        except Exception as exc:               # a probe bug must never kill the screen
            self._snap = dict(error="Hardware check failed: %s: %s" % (type(exc).__name__, exc))
        finally:
            self._busy = False

    def _safe(self, probe, default):
        try:
            return probe()
        except Exception:
            return default

    def collect(self) -> dict:
        """Run every probe once and return plain rows for draw()."""
        app, probes = self.app, self.probes
        snap = {}
        model = self._safe(probes.model, "")
        sysmon = dict(getattr(getattr(app, "_sysmon", None), "snapshot", None) or {})
        snap["board"] = board_name(model)
        snap["model"] = model.replace("\x00", "").strip()
        snap["temp_c"], snap["cpu_pct"] = sysmon.get("temp_c"), sysmon.get("cpu_pct")
        snap["undervolt_now"] = bool(sysmon.get("undervolt_now"))
        snap["undervolt_ever"] = bool(sysmon.get("undervolt_ever"))
        snap["throttled_now"] = bool(sysmon.get("throttled_now"))
        snap["throttled_ever"] = bool(sysmon.get("throttled_ever"))
        snap["power_known"] = sysmon.get("throttle_raw") is not None

        usb = self._safe(probes.usb_devices, {})
        wifi = self._safe(probes.wifi, [])
        ble = self._safe(probes.ble, [])
        gps_health = self._gps_health()
        rails = self._safe(probes.rails, None)
        snap["aio"] = rails is not None
        sdr = getattr(app, "_sdr", None)
        sdr_mode = str(getattr(sdr, "mode", "") or "") if getattr(sdr, "running", False) else ""
        if not sdr_mode:        # a plugin that drives the dongle itself (433 Hunter) says so
            for plugin in getattr(app, "_plugins", ()) or ():
                user = getattr(plugin, "sdr_user", None)
                if callable(user):
                    try:
                        sdr_mode = str(user() or "")
                    except Exception:
                        sdr_mode = ""
                    if sdr_mode:
                        break
        sdr_on_usb = next((usb[key] for key in RTL_SDR_IDS if key in usb), None)

        # -- the SDR tuner test: once per screen opening / on T, never while in use
        if self._want_tuner_test:
            self._want_tuner_test = False
            if sdr_mode:
                self._tuner = None
            elif rails is not None and rails.get("sdr") is False:
                self._tuner = None
            else:
                self._tuner = self._safe(probes.rtl_test, ("error", "tuner test failed"))
        lora_user = self._lora_user()
        if lora_user:
            self._lora_probe = None
        elif rails is not None and rails.get("lora") is True:
            self._lora_probe = self._safe(probes.sx1262, ("error", "SPI test failed"))
        else:
            self._lora_probe = None

        snap["rails"] = [self._rail_row(feature, name, pin, rails, gps_health, sdr_mode, sdr_on_usb, lora_user)
                         for feature, name, pin in RAILS] if rails is not None else []
        snap["gps"] = self._gps_rows(gps_health)
        snap["sdr"] = self._sdr_row(sdr_mode, sdr_on_usb, rails)
        snap["esp32"] = self._esp32_rows(self._safe(probes.serial_ports, []))
        bristle = self._bristle()                    # [bristle] one line: the node fleet
        if bristle is not None:
            totals = bristle.service.fleet.totals()
            waiting = len(bristle.service.new_ports)
            snap["esp32"].append((clip("Bristle nodes: %d online of %d%s%s" % (
                totals["online"], totals["known"], ", bridge on" if totals.get("bridges") else "",
                ", %d new port(s) waiting" % waiting if waiting else "")),
                C_OK if totals["online"] else C_DIM))
        kismet = self._kismet()
        snap["wifi"] = self._wifi_rows(wifi, usb, kismet)
        snap["ble"] = self._ble_rows(ble, kismet)
        snap["kismet"] = self._kismet_rows(kismet)
        snap["at"] = time.strftime("%H:%M:%S")
        return snap

    # -- pieces ----------------------------------------------------------
    def _gps_health(self):
        health = getattr(getattr(self.app, "gps", None), "health", None)
        try:
            return health() if callable(health) else None
        except Exception:
            return None

    def _lora_user(self) -> str:
        app = self.app
        mesh = getattr(app, "_mt", None)
        if getattr(mesh, "running", False):
            return "Meshtastic" + ("" if getattr(mesh, "ready", False) else " (starting)")
        lora = getattr(app, "_lora", None)
        if getattr(lora, "running", False):
            mode = str(getattr(lora, "mode", "") or "MeshCore")
            return ("MeshCore" if mode in ("", "sniffer") else mode) + (
                "" if getattr(lora, "ready", False) else " (starting)")
        reticulum = getattr(app, "_rns", None)
        if getattr(reticulum, "lora_active", False):
            return "WDGWars Chat (Reticulum)"
        return ""

    def _kismet(self):
        for plugin in getattr(self.app, "_plugins", ()) or ():
            if hasattr(plugin, "kismet_active") and hasattr(plugin, "_wifi_selected"):
                return plugin
        return None

    def _rail_row(self, feature, name, pin, rails, gps_health, sdr_mode, sdr_on_usb, lora_user):
        state = rails.get(feature)
        on = {True: "ON ", False: "OFF", None: "?  "}[state]
        colour = C_OK if state else C_DIM if state is False else C_WARN
        if feature == "gps":
            uart = None
            for row in (gps_health or {}).get("devices", ()):
                if row["label"].startswith("AIO"):
                    uart = row
            source = (gps_health or {}).get("source")
            if uart is not None and uart["reporting"]:
                answer, tone = "gpsd sees the GP-02 (%s, %d/%d sats)" % (
                    {3: "3D fix", 2: "2D fix"}.get(uart["mode"], "no fix yet"), uart["used"], uart["seen"]), C_OK
            elif source == "serial":
                answer, tone = "read directly by the game (no gpsd)", C_OK
            elif state:
                answer, tone = "NO DATA from the GP-02 yet", C_WARN
            else:
                answer, tone = "rail off: not tested", C_DIM
        elif feature == "lora":
            if lora_user:
                answer, tone = "in use by %s" % lora_user, C_OK
            elif self._lora_probe is not None:
                answer, tone = self._lora_probe[1], C_OK if self._lora_probe[0] == "ok" else C_WARN
            else:
                answer, tone = ("rail off: not tested" if state is False else "not tested"), C_DIM
        elif feature == "sdr":
            if sdr_mode:
                answer, tone = "receiving (%s)" % sdr_mode, C_OK
            elif state is False:
                answer, tone = "rail off: not tested", C_DIM
            elif self._tuner is not None and self._tuner[0] == "ok":
                answer, tone = self._tuner[1], C_OK
            elif self._tuner is not None:
                answer, tone = self._tuner[1], C_WARN
            elif sdr_on_usb:
                answer, tone = "RTL2832U on USB; press T to test the tuner", C_TEXT
            else:
                answer, tone = "NO RTL-SDR on USB", C_WARN
        else:
            answer, tone = "internal USB-C port (devices show under Wi-Fi / ESP32)", C_DIM
        return dict(name="%-4s GPIO%-2d" % (name, pin), state=on, state_colour=colour,
                    answer=clip(answer, COLUMN_CHARS - 17), answer_colour=tone)

    def _gps_rows(self, health) -> list:
        if health is None:
            return [("GPS manager has no health data", C_DIM)]
        rows = []
        for row in health.get("devices", ())[:3]:
            fix = {3: "3D", 2: "2D"}.get(row["mode"], "--") if row["reporting"] else "silent"
            text = "%s%s  %s  %d/%d  ant %s" % (">" if row["in_use"] else " ", row["label"], fix,
                                                 row["used"], row["seen"], row["antenna"])
            colour = C_ERR if row["antenna"] == "SHORT" else C_TEXT if row["reporting"] else C_WARN
            rows.append((clip(text), colour))
        if not rows:
            rows.append((clip("%s -- details on PLUGINS > GPS: Health" % health.get("source", "off")), C_DIM))
        return rows

    def _sdr_row(self, sdr_mode, sdr_on_usb, rails) -> tuple:
        if sdr_mode:
            names = {"adsb": "ADS-B", "433": "433 MHz", "adsb+433": "ADS-B + 433 MHz",
                     "emergency": "Emergency Radio"}
            return ("in use by: %s" % names.get(sdr_mode, sdr_mode), C_OK)
        if self._tuner is not None and self._tuner[0] == "busy":
            return ("in use by: another program (not the game)", C_WARN)
        present = "on USB" if sdr_on_usb else "NOT on USB"
        if rails is not None and rails.get("sdr") is False:
            present = "rail off"
        return ("in use by: none   (%s)" % present, C_TEXT if sdr_on_usb else C_DIM)

    def _esp32_rows(self, ports) -> list:
        app = self.app
        rows = []
        if getattr(app, "_esp32", False):
            port = getattr(getattr(app, "serial", None), "device", "") or "?"
            firmware = getattr(app, "_fw_version", "") or "version not read yet"
            rows.append((clip("connected on %s, firmware %s" % (port, firmware)), C_OK))
        else:
            rows.append(("not connected to the game", C_DIM))
        others = []
        for item in ports[:4]:
            try:
                others.append("%s %s" % (item[0], item[1]))
            except (TypeError, IndexError):
                continue
        if others:
            rows.append((clip("USB serial: " + "; ".join(others)), C_DIM))
        return rows

    def _wifi_rows(self, wifi, usb, kismet) -> list:
        selected = set(getattr(kismet, "_wifi_selected", ()) or ()) if kismet is not None else set()
        capturing = bool(kismet is not None and kismet.kismet_active())
        rows = []
        bound = {row.get("usbid") for row in wifi}
        for row in wifi[:4]:
            driver = row.get("driver") or "no driver"
            in_kismet = capturing and row["id"] in selected
            monitor = "yes" if row.get("can_monitor") else "NO"
            is_ac1200 = (row.get("usbid") in AC1200_IDS or AC1200_NAME.search(str(row.get("guess", "")))
                         or MONITOR_DRIVER.search(driver))
            line = "%-7s %-13s monitor:%-3s Kismet:%s" % (row["id"][:7], driver[:13], monitor,
                                                          "yes" if in_kismet else "no")
            detail = str(row.get("guess", ""))
            if is_ac1200:      # the flag first: a long chipset name is what gets cut
                detail = ("[monitor driver installed] " if row.get("can_monitor")
                          else "[MONITOR DRIVER NOT INSTALLED] ") + detail
            rows.append((clip(line), C_OK if in_kismet else C_TEXT if row.get("can_monitor") else C_WARN,
                         clip("  " + detail), C_DIM if row.get("can_monitor") or not is_ac1200 else C_WARN))
        for usb_id, name in usb.items():
            if usb_id in bound or not (usb_id in AC1200_IDS or AC1200_NAME.search(name)):
                continue
            rows.append((clip("AC1200 on USB (%s) but NO INTERFACE" % usb_id), C_ERR,
                         clip("  no driver bound: the monitor-mode driver is not installed"), C_WARN))
        if not rows:
            rows.append(("no Wi-Fi adapter found", C_WARN, "", C_DIM))
        return rows[:5]

    def _bristle(self):
        """[bristle] The Bristle: Nodes plugin, if it started."""
        for plugin in getattr(self.app, "_plugins", ()) or ():
            if getattr(plugin, "NAME", "") == "Bristle: Nodes" and getattr(plugin, "service", None) is not None:
                return plugin
        return None

    def _ble_rows(self, ble, kismet) -> list:
        selected = set(getattr(kismet, "_ble_selected", ()) or ()) if kismet is not None else set()
        capturing = bool(kismet is not None and kismet.kismet_active())
        # [bristle] one Bluetooth adapter is kept for Bristle; Kismet must not be given it
        bristle = self._bristle()
        reserved = str(bristle.service.conf.get("ble_adapter", "")) if bristle is not None else ""
        rows = []
        for row in ble[:3]:
            used = capturing and row["id"] in selected
            kind = "built-in" if row.get("likely_onboard") else str(row.get("guess", ""))[:34]
            if reserved and row["id"] == reserved:
                clash = row["id"] in selected
                rows.append((clip("%-5s %s  %s" % (row["id"], kind[:20], "KEPT FOR BRISTLE BUT KISMET IS SET TO USE IT: "
                                                   "untick it in Kismet" if clash else "kept for Bristle  Kismet:no")),
                             C_ERR if clash else C_TEXT))
                continue
            rows.append((clip("%-5s %s  Kismet:%s" % (row["id"], kind, "yes" if used else "no")),
                         C_OK if used else C_TEXT))
        return rows or [("no Bluetooth controller found", C_WARN)]

    def _kismet_rows(self, kismet) -> list:
        if kismet is None:
            return [("the Kismet plugin is not loaded", C_DIM)]
        state = getattr(kismet, "_lifecycle", "idle")
        rows = [("state: %s" % ("capturing" if kismet.kismet_active() else state),
                 C_OK if kismet.kismet_active() else C_DIM)]
        report = "channels: %s" % getattr(kismet, "_split_report", "not started")
        rows.append((report[:COLUMN_CHARS], C_TEXT))
        if len(report) > COLUMN_CHARS:
            rows.append((clip("  " + report[COLUMN_CHARS:]), C_TEXT))
        return rows

    # ------------------------------------------------------------------
    # Per-frame update
    # ------------------------------------------------------------------

    def on_update(self) -> None:
        if not self._overlay_active:
            return
        import pyxel
        if pyxel.btnp(pyxel.KEY_ESCAPE):
            self._overlay_active = False
            if self.app:
                self.app._esc_consumed_frame = pyxel.frame_count
            return
        now = time.monotonic()
        if pyxel.btnp(pyxel.KEY_R):
            self._next = 0.0
        if pyxel.btnp(pyxel.KEY_T):
            self._want_tuner_test = True
            self._next = 0.0
        for key, action, name in ((pyxel.KEY_G, "_toggle_gps", "GPS"), (pyxel.KEY_S, "_toggle_sdr", "SDR"),
                                  (pyxel.KEY_U, "_toggle_usb", "USB")):
            if pyxel.btnp(key):
                toggle = getattr(self.app, action, None)
                if callable(toggle) and self._snap and self._snap.get("aio"):
                    self.term("[HW] %s rail toggled from Hardware Check" % name)
                    toggle()
                    self._next = now + 0.5
                else:
                    self.msg("[HW] No AIO v2 rails on this machine", C_WARN)
        if now >= self._next and self._start_refresh():
            self._next = now + REFRESH_SECONDS

    # ------------------------------------------------------------------
    # Draw (640x360, pyxel 16 colours, upstream overlay style)
    # ------------------------------------------------------------------

    def draw(self, x: int, y: int, w: int, h: int) -> None:
        if not self._overlay_active:
            return
        import pyxel
        pyxel.rect(0, 0, w, h, C_BG)
        pyxel.rect(0, 0, w, 12, C_BAR)
        pyxel.text(4, 3, "HARDWARE CHECK", C_TITLE)
        snap = self._snap
        if snap is None:
            pyxel.text(LEFT_X, 22, "Looking at the hardware...", C_WARN)
            pyxel.text(4, h - 12, "[ESC] Back", C_DIM)
            return
        if "error" in snap:
            pyxel.text(LEFT_X, 22, clip(snap["error"], 124), C_ERR)
            pyxel.text(4, h - 12, "[R] Try again  [ESC] Back", C_DIM)
            return
        stamp = "checked %s%s" % (snap["at"], "  (checking...)" if self._busy else "")
        pyxel.text(w - 4 - FONT_W * len(stamp), 3, stamp, C_DIM)
        if snap["undervolt_now"]:
            blink = pyxel.frame_count % 20 < 12
            pyxel.rect(0, 13, w, 11, C_ERR if blink else C_BAR)
            pyxel.text(LEFT_X, 15, "UNDER-VOLTAGE NOW -- the supply is sagging: check battery / cable / power bank",
                       C_TEXT)

        # ---- left column ------------------------------------------------
        ty = 28
        ty = self._heading(pyxel, LEFT_X, ty, "SYSTEM")
        pyxel.text(LEFT_X, ty, clip("Board  %s  %s" % (snap["board"], snap["model"])), C_TEXT)
        ty += 9
        temp, cpu = snap["temp_c"], snap["cpu_pct"]
        temp_text = "CPU temp %s   CPU load %s" % ("%.1f C" % temp if temp is not None else "n/a",
                                                    "%d%%" % cpu if cpu is not None else "n/a")
        pyxel.text(LEFT_X, ty, temp_text, C_ERR if temp is not None and temp >= 80 else
                   C_WARN if temp is not None and temp >= 70 else C_TEXT)
        ty += 9
        if not snap["power_known"]:
            power, colour = "Power  not reported (no vcgencmd on this machine)", C_DIM
        elif snap["undervolt_now"]:
            power, colour = "Power  UNDER-VOLTAGE NOW", C_ERR
        elif snap["throttled_now"]:
            power, colour = "Power  ok, but the CPU is THROTTLED right now", C_WARN
        elif snap["undervolt_ever"] or snap["throttled_ever"]:
            power, colour = ("Power  ok now; %s since boot" % " and ".join(
                [name for name, flag in (("under-voltage", snap["undervolt_ever"]),
                                         ("throttling", snap["throttled_ever"])) if flag])), C_WARN
        else:
            power, colour = "Power  ok, no under-voltage or throttling since boot", C_OK
        pyxel.text(LEFT_X, ty, clip(power), colour)
        ty += 14

        ty = self._heading(pyxel, LEFT_X, ty, "AIO v2 RAILS          answers?")
        if not snap["rails"]:
            pyxel.text(LEFT_X, ty, "no AIO v2 board found (pinctrl missing)", C_DIM)
            ty += 9
        for rail in snap["rails"]:
            pyxel.text(LEFT_X, ty, rail["name"], C_TEXT)
            pyxel.text(LEFT_X + FONT_W * 12, ty, rail["state"], rail["state_colour"])
            pyxel.text(LEFT_X + FONT_W * 17, ty, rail["answer"], rail["answer_colour"])
            ty += 9
        ty += 5
        ty = self._heading(pyxel, LEFT_X, ty, "GPS RECEIVERS  (> = in use)")
        for text, colour in snap["gps"]:
            pyxel.text(LEFT_X, ty, text, colour)
            ty += 9
        ty += 5
        ty = self._heading(pyxel, LEFT_X, ty, "RTL-SDR")
        pyxel.text(LEFT_X, ty, clip(snap["sdr"][0]), snap["sdr"][1])
        ty += 14
        ty = self._heading(pyxel, LEFT_X, ty, "ESP32")
        for text, colour in snap["esp32"]:
            pyxel.text(LEFT_X, ty, text, colour)
            ty += 9

        # ---- right column -----------------------------------------------
        ty = 28
        ty = self._heading(pyxel, RIGHT_X, ty, "WI-FI ADAPTERS")
        for line, colour, detail, detail_colour in snap["wifi"]:
            pyxel.text(RIGHT_X, ty, line, colour)
            ty += 9
            if detail:
                pyxel.text(RIGHT_X, ty, detail, detail_colour)
                ty += 9
        ty += 5
        ty = self._heading(pyxel, RIGHT_X, ty, "BLUETOOTH")
        for text, colour in snap["ble"]:
            pyxel.text(RIGHT_X, ty, text, colour)
            ty += 9
        ty += 5
        ty = self._heading(pyxel, RIGHT_X, ty, "KISMET")
        for text, colour in snap["kismet"]:
            pyxel.text(RIGHT_X, ty, text, colour)
            ty += 9

        pyxel.text(4, h - 22, "Read-only. Rails change only with [G] GPS  [S] SDR  [U] internal USB "
                              "(same as SYSTEM). LoRa: SYSTEM > LoRa.", C_DIM)
        pyxel.text(4, h - 12, "[R] Check again now   [T] RTL-SDR tuner test   [ESC] Back", C_DIM)

    @staticmethod
    def _heading(pyxel, x, ty, title) -> int:
        pyxel.text(x, ty, title, C_DIM)
        pyxel.line(x, ty + 9, x + 306, ty + 9, C_BORDER)
        return ty + 13
