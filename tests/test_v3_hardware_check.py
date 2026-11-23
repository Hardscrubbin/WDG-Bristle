"""[merge-v3] Hardware Check: the real probes' parsing (board model, rtl_test
output, the SX1262 status byte) with subprocess and spidev replaced. What the
screen shows for each machine state is in tools/sim (test_v3_sim.py)."""
import subprocess
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from watchdogs import plugin_loader
import plugins.hardware_check as hc


def completed(text):
    return SimpleNamespace(stdout=text, returncode=0)


class BoardTests(unittest.TestCase):
    def test_board_names(self):
        cases = {"Raspberry Pi Compute Module 5 Rev 1.0\x00": "CM5",
                 "Raspberry Pi Compute Module 4 Rev 1.1": "CM4",
                 "Raspberry Pi 5 Model B Rev 1.0": "Pi 5", "": "unknown board", None: "unknown board",
                 "Some Other Board With A Very Long Name": "Some Other Board Wit"}
        for model, expected in cases.items():
            with self.subTest(model=model):
                self.assertEqual(hc.board_name(model), expected)

    def test_clip_never_exceeds_the_column(self):
        self.assertEqual(hc.clip("short"), "short")
        self.assertEqual(len(hc.clip("x" * 200)), hc.COLUMN_CHARS)
        self.assertTrue(hc.clip("x" * 200).endswith(".."))


class RtlTestProbeTests(unittest.TestCase):
    def run_with(self, result):
        with patch.object(hc.subprocess, "run", side_effect=result if isinstance(result, Exception)
                          else lambda *a, **k: result) as run:
            answer = hc.Probes().rtl_test()
        self.assertEqual(run.call_args.args[0], ["rtl_test", "-t"])
        self.assertEqual(run.call_args.kwargs["timeout"], 8)
        return answer

    def test_outputs_seen_in_the_field(self):
        found = ("Found 1 device(s):\n  0:  Realtek, RTL2838UHIDIR, SN: 00000001\n\n"
                 "Using device 0: Generic RTL2832U OEM\nFound Rafael Micro R820T tuner\n"
                 "Supported gain values (29): 0.0 0.9\nNo E4000 tuner found, aborting.\n")
        self.assertEqual(self.run_with(completed(found)), ("ok", "tuner test OK (Rafael Micro R820T)"))
        busy = "Found 1 device(s):\nusb_claim_interface error -6\nFailed to open rtlsdr device #0.\n"
        self.assertEqual(self.run_with(completed(busy))[0], "busy")
        self.assertEqual(self.run_with(completed("No supported devices found.\n"))[0], "none")
        self.assertEqual(self.run_with(completed("something else\n"))[0], "error")

    def test_missing_tool_and_hang_are_answers_not_exceptions(self):
        self.assertIn("not installed", self.run_with(FileNotFoundError("rtl_test"))[1])
        hang = subprocess.TimeoutExpired(["rtl_test", "-t"], 8, output=b"Found 1 device(s):\n")
        self.assertEqual(self.run_with(hang), ("error", "rtl_test did not answer within 8 s"))
        late = subprocess.TimeoutExpired(["rtl_test", "-t"], 8, output=b"Found Rafael Micro R828D tuner\n")
        self.assertEqual(self.run_with(late), ("ok", "tuner test OK (Rafael Micro R828D)"))
        self.assertEqual(self.run_with(PermissionError("denied"))[0], "error")


class Sx1262ProbeTests(unittest.TestCase):
    def probe(self, reply=None, users="", exists=True, spidev=True, error=None):
        opened = []

        class Bus:
            max_speed_hz = 0
            mode = 0

            def open(self, bus, chip):
                opened.append((bus, chip))
                if error:
                    raise error

            def xfer2(self, data):
                opened.append(tuple(data))
                return list(reply)

            def close(self):
                opened.append("closed")

        modules = {"spidev": SimpleNamespace(SpiDev=Bus) if spidev else None}
        with patch.object(hc.os.path, "exists", return_value=exists), \
                patch.object(hc.subprocess, "run", return_value=SimpleNamespace(stdout=users)), \
                patch.dict(sys.modules, modules):
            return hc.Probes().sx1262(), opened

    def test_a_standby_chip_answers(self):
        answer, opened = self.probe(reply=[0xA2, 0x22])
        self.assertEqual(answer, ("ok", "SX1262 answers on SPI (status 0x22)"))
        self.assertEqual(opened, [(1, 0), (0xC0, 0x00), "closed"])            # one GetStatus, bus closed

    def test_an_absent_chip_reads_all_zeros_or_all_ones(self):
        for reply in ([0, 0], [0xFF, 0xFF], [0x00, 0x10], [0xA2]):
            with self.subTest(reply=reply):
                self.assertEqual(self.probe(reply=reply)[0][0], "none")

    def test_the_bus_is_left_alone_when_it_cannot_or_must_not_be_used(self):
        answer, opened = self.probe(users=" 1234", reply=[0, 0x22])
        self.assertEqual(answer[0], "busy")                                   # meshtasticd has it
        self.assertEqual(opened, [])
        self.assertEqual(self.probe(exists=False)[0][0], "error")
        self.assertIn("spidev is not installed", self.probe(spidev=False, reply=[0, 0])[0][1])
        answer, opened = self.probe(error=OSError("no such device"), reply=[0, 0])
        self.assertEqual(answer[0], "error")
        self.assertEqual(opened[-1], "closed")


class PluginTests(unittest.TestCase):
    def test_loader_finds_it_and_it_never_runs_a_shell(self):
        kinds = [type(plugin).__name__ for plugin in plugin_loader.discover_plugins()]
        self.assertEqual(kinds.count("HardwareCheck"), 1)
        item, = hc.HardwareCheck().menu_items()
        self.assertEqual((item.key, item.label), ("h", "Hardware Check"))
        source = open(hc.__file__, encoding="utf-8").read()
        for forbidden in ("shell=True", "os.system", "pinctrl\", \"set", "eval("):
            self.assertNotIn(forbidden, source)

    def test_nothing_is_probed_until_the_screen_is_open(self):
        plugin = hc.HardwareCheck()
        plugin.probes = SimpleNamespace()                 # any probe call would raise AttributeError
        plugin.on_update()
        self.assertIsNone(plugin._snap)
        self.assertIsNone(plugin._thread)


if __name__ == "__main__":
    unittest.main()
