"""[v2] System health monitor: CPU load, SoC temperature, Raspberry Pi
throttle / under-voltage flags, fan level, free memory.

Built for the CM5 uConsole running Kismet + ESP32 + SDR + LoRa at once. It
samples on its own daemon thread (never on the pyxel loop) and publishes an
immutable snapshot dict that the HUD reads every frame. On anything that
isn't a Pi (dev PC), each reading is just None and the HUD hides it.

Sources (all cheap, no extra packages):
  CPU %      /proc/stat aggregate line, delta between samples
  Temp       /sys/class/thermal/thermal_zone*/temp, preferring the zone whose
             type is "cpu-thermal" (Pi 4/5/CM4/CM5), else zone0
  Throttle   `vcgencmd get_throttled` (every few samples). Bits:
               0 under-voltage now      16 under-voltage has occurred
               1 ARM freq capped now    17 freq capping has occurred
               2 throttled now          18 throttling has occurred
               3 soft temp limit now    19 soft temp limit has occurred
  Fan        /sys/class/thermal/cooling_device*/cur_state of type "pwm-fan"
  Memory     /proc/meminfo MemAvailable

Thresholds: Pi 5 / CM5 firmware starts throttling the ARM cores at 80 C
and throttles hard at 85 C. Defaults are WARN 70 C and HOT 80 C, overridable
with WDG_TEMP_WARN_C / WDG_TEMP_HOT_C. Alerts fire on crossings only, with
3 C of hysteresis, so they never spam.
"""

import glob
import os
import shutil
import subprocess
import threading
import time


try:
    from .config import _secret as _cfg_secret
except Exception:  # standalone use / tests without the package config
    def _cfg_secret(*keys, default=""):
        return default


def _env_float(name, default):
    """secrets.conf first (like every other WDG setting), then env var."""
    raw = _cfg_secret(name, default="") or os.environ.get(name, "")
    try:
        return float(raw) if raw != "" else float(default)
    except (TypeError, ValueError):
        return float(default)


TEMP_WARN_C = _env_float("WDG_TEMP_WARN_C", 70)
TEMP_HOT_C = _env_float("WDG_TEMP_HOT_C", 80)
HYSTERESIS_C = 3.0

UV_NOW, CAP_NOW, THR_NOW, SOFT_NOW = 0x1, 0x2, 0x4, 0x8
UV_EVER, CAP_EVER, THR_EVER, SOFT_EVER = 0x10000, 0x20000, 0x40000, 0x80000


def parse_throttled(text):
    """'throttled=0x50005' -> int, or None if unparseable."""
    if not text:
        return None
    try:
        return int(text.strip().split("=", 1)[-1], 16)
    except (ValueError, IndexError):
        return None


class SystemMonitor:
    EMPTY = {"cpu_pct": None, "temp_c": None, "throttle_raw": None,
             "undervolt_now": False, "throttled_now": False,
             "undervolt_ever": False, "throttled_ever": False,
             "fan_state": None, "fan_max": None, "mem_avail_mb": None,
             "peak_temp_c": None, "ts": 0.0}

    def __init__(self, interval=2.0, log=None, root="/", runner=None,
                 throttle_every=3):
        self.interval = interval
        self.log = log or (lambda text: None)
        self._root = root
        self._runner = runner or self._run_vcgencmd
        self._throttle_every = max(1, int(throttle_every))
        self._prev_cpu = None
        self._temp_path = None
        self._fan_paths = None
        self._have_vcgencmd = None
        self._n = 0
        self._last_throttle = None
        self._temp_level = 0          # 0 ok, 1 warn, 2 hot
        self._was_uv = False
        self._was_thr = False
        self._peak = None
        self._stop = threading.Event()
        self._thread = None
        self.snapshot = dict(self.EMPTY)

    # ---- paths (root is injectable so tests can fake /proc and /sys) ----
    def _p(self, path):
        return os.path.join(self._root, path.lstrip("/"))

    def _read(self, path):
        with open(self._p(path), encoding="utf-8") as fh:
            return fh.read()

    # ---- individual readings ----
    def _cpu(self):
        try:
            fields = self._read("/proc/stat").splitlines()[0].split()[1:8]
            total = sum(int(x) for x in fields)
            idle = int(fields[3])
        except (OSError, ValueError, IndexError):
            return None
        prev, self._prev_cpu = self._prev_cpu, (total, idle)
        if prev is None:
            return None
        dt, di = total - prev[0], idle - prev[1]
        if dt <= 0:
            return None
        return round(max(0.0, min(100.0, 100.0 * (dt - di) / dt)), 1)

    def _find_temp_path(self):
        zones = sorted(glob.glob(self._p("/sys/class/thermal/thermal_zone*")))
        chosen = None
        for z in zones:
            try:
                with open(os.path.join(z, "type"), encoding="utf-8") as fh:
                    if "cpu" in fh.read().lower():
                        chosen = z
                        break
            except OSError:
                continue
        if chosen is None and zones:
            chosen = zones[0]
        return os.path.join(chosen, "temp") if chosen else ""

    def _temp(self):
        if self._temp_path is None:
            self._temp_path = self._find_temp_path()
        if not self._temp_path:
            return None
        try:
            with open(self._temp_path, encoding="utf-8") as fh:
                return round(int(fh.read().strip()) / 1000.0, 1)
        except (OSError, ValueError):
            return None

    def _fan(self):
        if self._fan_paths is None:
            self._fan_paths = ""
            for d in sorted(glob.glob(self._p("/sys/class/thermal/cooling_device*"))):
                try:
                    with open(os.path.join(d, "type"), encoding="utf-8") as fh:
                        if "fan" in fh.read().lower():
                            self._fan_paths = d
                            break
                except OSError:
                    continue
        if not self._fan_paths:
            return None, None
        try:
            with open(os.path.join(self._fan_paths, "cur_state"), encoding="utf-8") as fh:
                cur = int(fh.read().strip())
            with open(os.path.join(self._fan_paths, "max_state"), encoding="utf-8") as fh:
                mx = int(fh.read().strip())
            return cur, mx
        except (OSError, ValueError):
            return None, None

    def _mem(self):
        try:
            for line in self._read("/proc/meminfo").splitlines():
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
        except (OSError, ValueError, IndexError):
            pass
        return None

    def _run_vcgencmd(self):
        if self._have_vcgencmd is None:
            self._have_vcgencmd = shutil.which("vcgencmd") is not None
        if not self._have_vcgencmd:
            return None
        try:
            r = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True,
                               text=True, timeout=1.5)
            return r.stdout if r.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            return None

    # ---- one sample ----
    def sample(self):
        self._n += 1
        cpu = self._cpu()
        temp = self._temp()
        if self._n == 1 or self._n % self._throttle_every == 0:
            raw = parse_throttled(self._runner())
            if raw is not None or self._n == 1:
                self._last_throttle = raw
        raw = self._last_throttle
        fan, fan_max = self._fan()
        if temp is not None and (self._peak is None or temp > self._peak):
            self._peak = temp
        snap = {
            "cpu_pct": cpu, "temp_c": temp, "throttle_raw": raw,
            "undervolt_now": bool(raw is not None and raw & UV_NOW),
            "throttled_now": bool(raw is not None and raw & (CAP_NOW | THR_NOW | SOFT_NOW)),
            "undervolt_ever": bool(raw is not None and raw & UV_EVER),
            "throttled_ever": bool(raw is not None and raw & (CAP_EVER | THR_EVER | SOFT_EVER)),
            "fan_state": fan, "fan_max": fan_max, "mem_avail_mb": self._mem(),
            "peak_temp_c": self._peak, "ts": time.time(),
        }
        self._alerts(snap)
        self.snapshot = snap  # rebind: readers always see a complete dict
        return snap

    def _alerts(self, s):
        t = s["temp_c"]
        if t is not None:
            cur = self._temp_level
            if t >= TEMP_HOT_C:
                level = 2
            elif cur == 2 and t >= TEMP_HOT_C - HYSTERESIS_C:
                level = 2                      # hysteresis: stay hot
            elif t >= TEMP_WARN_C:
                level = 1
            elif cur >= 1 and t >= TEMP_WARN_C - HYSTERESIS_C:
                level = 1                      # hysteresis: stay warm
            else:
                level = 0
            if level != self._temp_level:
                if level == 2:
                    self.log(f"[SYS] HOT: SoC {t:.0f}C (>= {TEMP_HOT_C:.0f}C) -- "
                             f"CM5 throttles at 80C; check airflow/fan")
                elif level == 1 and self._temp_level == 0:
                    self.log(f"[SYS] Warm: SoC {t:.0f}C (>= {TEMP_WARN_C:.0f}C)")
                elif level == 1:
                    self.log(f"[SYS] Cooling: SoC {t:.0f}C")
                else:
                    self.log(f"[SYS] Temp back to normal: {t:.0f}C")
                self._temp_level = level
        if s["undervolt_now"] and not self._was_uv:
            self.log("[SYS] UNDER-VOLTAGE detected -- too many USB radios for the "
                     "supply? Data can corrupt; check power")
        if s["throttled_now"] and not self._was_thr:
            self.log("[SYS] CPU THROTTLED (freq capped / thermal limit)")
        if self._was_thr and not s["throttled_now"]:
            self.log("[SYS] Throttling cleared")
        self._was_uv, self._was_thr = s["undervolt_now"], s["throttled_now"]

    # ---- thread ----
    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="sys-monitor", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sample()
            except Exception:  # never let the monitor take anything down
                pass
            self._stop.wait(self.interval)
