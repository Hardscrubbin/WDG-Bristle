"""[merge-v3] A stand-in machine for the Hardware Check screen: scripted probe
results (plugins/hardware_check.Probes) and the bits of the game it reads."""
import time
from types import SimpleNamespace

CM5 = "Raspberry Pi Compute Module 5 Rev 1.0\x00"
CM4 = "Raspberry Pi Compute Module 4 Rev 1.1\x00"
ALFA = dict(kind="wifi", id="wlan1", mac="00:c0:ca:00:00:01", driver="mt7921u", bus="usb",
            usbid="0e8d:7961", guess="MediaTek Inc. Wireless_Device", mode="managed",
            can_monitor=True, note="", likely_onboard=False, pinned=False)
AC1200 = dict(kind="wifi", id="wlan2", mac="00:e0:4c:00:00:02", driver="rtl8812au", bus="usb",
              usbid="0bda:8812", guess="Realtek Semiconductor Corp. RTL8812AU 802.11a/b/g/n/ac 2T2R",
              mode="managed", can_monitor=True, note="", likely_onboard=False, pinned=False)
ONBOARD = dict(kind="wifi", id="wlan0", mac="d8:3a:dd:00:00:03", driver="brcmfmac", bus="sdio",
               usbid=None, guess="driver: brcmfmac", mode="managed", can_monitor=False,
               note="can't do monitor mode", likely_onboard=True, pinned=False)
UB500 = dict(kind="ble", id="hci1", mac="00:1a:7d:00:00:01", bus="USB", usbid="2357:0604", up=True,
             guess="TP-Link UB500 Adapter", likely_onboard=False)
BUILTIN_BT = dict(kind="ble", id="hci0", mac="d8:3a:dd:00:00:04", bus="UART", usbid=None, up=True,
                  guess="onboard Bluetooth (UART)", likely_onboard=True)


class FakeProbes:
    """Scripted answers; every method counts its calls."""

    def __init__(self, **state):
        self.state = dict(
            model=CM5, rails=dict(gps=True, lora=True, sdr=True, usb=True),
            usb={"0bda:2838": "Realtek Semiconductor Corp. RTL2838 DVB-T", "0e8d:7961": "MediaTek Wireless",
                 "0bda:8812": "Realtek RTL8812AU", "2357:0604": "TP-Link UB500 Adapter"},
            wifi=[ALFA, AC1200, ONBOARD], ble=[UB500, BUILTIN_BT],
            serial=[("/dev/ttyACM0", "Espressif USB JTAG/serial debug unit", True)],
            rtl_test=("ok", "tuner test OK (Rafael Micro R820T)"),
            sx1262=("ok", "SX1262 answers on SPI (status 0x22)"), hang=0.0, raises=())
        self.state.update(state)
        self.calls = {}

    def _answer(self, name):
        self.calls[name] = self.calls.get(name, 0) + 1
        if name in self.state["raises"]:
            raise OSError("probe %s failed" % name)
        if self.state["hang"]:
            time.sleep(self.state["hang"])
        value = self.state[name]
        return [dict(row) for row in value] if name in ("wifi", "ble") else value

    def model(self):
        return self._answer("model")

    def rails(self):
        value = self._answer("rails")
        return dict(value) if value is not None else None

    def usb_devices(self):
        return dict(self._answer("usb"))

    def wifi(self):
        return self._answer("wifi")

    def ble(self):
        return self._answer("ble")

    def serial_ports(self):
        return list(self._answer("serial"))

    def rtl_test(self):
        return self._answer("rtl_test")

    def sx1262(self):
        return self._answer("sx1262")


def gps_health(*rows, source="gpsd"):
    devices = []
    for label, mode, used, seen, antenna, in_use in rows:
        devices.append(dict(label=label, path="/dev/x", mode=mode, used=used, seen=seen, antenna=antenna,
                            in_use=in_use, reporting=mode is not None, silent=mode is None,
                            has_fix=bool(mode and mode >= 2)))
    return dict(source=source, endpoint="gpsd://127.0.0.1:2947", devices=devices, switches=0,
                chosen=None, configured=[])


def game(**overrides):
    """The attributes of the game the screen reads, with recorders for the toggles."""
    toggles = []
    hud, term = [], []
    kismet = SimpleNamespace(kismet_active=lambda: True, _wifi_selected={"wlan1", "wlan2"},
                             _ble_selected={"hci1"}, _lifecycle="running",
                             _split_report="band split: wlan1: 28 channels (5/6 GHz), wlan2: 13 channels (2.4 GHz)")
    app = SimpleNamespace(
        gps=SimpleNamespace(health=lambda: gps_health(("AIO GP-02 (UART)", 3, 9, 12, "OK", True),
                                                      ("USB u-blox 7", 3, 8, 11, "not reported", False))),
        _sysmon=SimpleNamespace(snapshot=dict(temp_c=54.2, cpu_pct=23, throttle_raw=0, undervolt_now=False,
                                              undervolt_ever=False, throttled_now=False, throttled_ever=False)),
        _sdr=SimpleNamespace(running=False, mode=""), _lora=SimpleNamespace(running=False, ready=False, mode=""),
        _mt=SimpleNamespace(running=False, ready=False), _esp32=True, _fw_version="1.7.0",
        serial=SimpleNamespace(device="/dev/ttyACM0"), _plugins=[kismet],
        _toggle_gps=lambda: toggles.append("gps"), _toggle_sdr=lambda: toggles.append("sdr"),
        _toggle_usb=lambda: toggles.append("usb"),
        msg=lambda text, colour=7: hud.append(text), _term_add=lambda text, raw=False: term.append(text),
        _esc_consumed_frame=-1)
    app.toggles, app.hud, app.term, app.kismet = toggles, hud, term, kismet
    for name, value in overrides.items():
        setattr(app, name, value)
    return app
