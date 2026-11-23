"""[merge-v3] Simulation harness core: scenario registry, checks and the runner.

A scenario is a function taking one ``ctx`` argument. It drives REAL game
modules against fakes (tools/sim/fake_*.py) and raises ``Fail`` -- through
``check()`` -- when the game crashed, hung, lost data, told the player the
wrong thing or did not recover after the fault cleared. Every scenario runs
on its own thread under a time limit, so a hang is reported as a hang instead
of stopping the run.

    python -I -m tools.sim.run                 every scenario except the soak
    python -I -m tools.sim.run gpsd uploads    only these features
    python -I -m tools.sim.run --soak          also the 8-hour soak
    python -I -m tools.sim.run --json out.json machine-readable results
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _path in (os.path.join(ROOT, "tests"), ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

SCENARIOS = []            # (feature, name, function, limit seconds, soak?)
DEFAULT_LIMIT = 60.0


class Fail(AssertionError):
    """A scenario's expectation did not hold."""


def check(condition, message):
    if not condition:
        raise Fail(message)


def check_equal(actual, expected, what):
    if actual != expected:
        raise Fail("%s: expected %r, got %r" % (what, expected, actual))


def scenario(feature, name, limit=DEFAULT_LIMIT, soak=False):
    def register(function):
        SCENARIOS.append((feature, name, function, limit, soak))
        return function
    return register


class Context:
    """Per-scenario scratch space, metrics and cleanup."""

    def __init__(self, feature, name):
        self.feature, self.name = feature, name
        self.metrics = {}
        self._cleanups = []
        self._tmp = None

    @property
    def tmp(self) -> str:
        if self._tmp is None:
            self._tmp = tempfile.mkdtemp(prefix="wdg-sim-")
            self._cleanups.append(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        return self._tmp

    def cleanup(self, function, *args):
        self._cleanups.append(lambda: function(*args))

    def patch(self, target, name, value):
        """setattr for the length of the scenario (restored afterwards)."""
        missing = object()
        old = getattr(target, name, missing)
        setattr(target, name, value)

        def restore():
            if old is missing:
                try:
                    delattr(target, name)
                except AttributeError:
                    pass
            else:
                setattr(target, name, old)
        self._cleanups.append(restore)

    def note(self, key, value):
        self.metrics[key] = value

    def close(self):
        for function in reversed(self._cleanups):
            try:
                function()
            except Exception:
                pass
        self._cleanups = []


class SimClock:
    """A monotonic clock the scenario moves by hand. ``install(module)``
    swaps the module's ``time`` for a view of the real one with this clock's
    monotonic(), so only that module sees simulated time."""

    def __init__(self, start=1000.0):
        self.now = float(start)

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        return self.now

    def install(self, ctx, module):
        clock = self

        class _Time:
            def __getattr__(self, name):
                return getattr(time, name)

            @staticmethod
            def monotonic():
                return clock.now
        ctx.patch(module, "time", _Time())


def _run_one(feature, name, function, limit):
    ctx = Context(feature, name)
    outcome = {}

    def body():
        try:
            function(ctx)
            outcome["status"] = "pass"
        except Fail as exc:
            outcome["status"], outcome["detail"] = "fail", str(exc)
        except Exception:
            outcome["status"], outcome["detail"] = "crash", traceback.format_exc()[-1500:]

    started = time.perf_counter()
    thread = threading.Thread(target=body, name="sim-" + feature, daemon=True)
    thread.start()
    thread.join(limit)
    if thread.is_alive():
        outcome = dict(status="hang", detail="still running after %.0f s" % limit)
    try:
        ctx.close()
    except Exception:
        pass
    outcome.update(feature=feature, name=name, seconds=round(time.perf_counter() - started, 2),
                   metrics=ctx.metrics)
    return outcome


def load_all():
    """Import every scenario module (each registers its scenarios)."""
    import importlib
    folder = os.path.dirname(os.path.abspath(__file__))
    for entry in sorted(os.listdir(folder)):
        if entry.startswith("scenarios_") and entry.endswith(".py"):
            importlib.import_module("tools.sim." + entry[:-3])


PLAYER_FILES = ("secrets.conf", "whitelist.json", os.path.join("loot", "loot_db.json"),
                os.path.join("plugins", ".wardrive_state.json"))


def _player_files() -> dict:
    """Size + mtime of the player's own files in the build folder. Scenarios
    work in scratch folders only; this proves it after every run (selftest.sh
    runs the harness inside a real install, next to real keys and real loot)."""
    state = {}
    for name in PLAYER_FILES:
        try:
            info = os.stat(os.path.join(ROOT, name))
            state[name] = (info.st_size, info.st_mtime_ns)
        except OSError:
            state[name] = None
    return state


def run(features=None, soak=False, json_out=None, quiet=False, out=None, only=None, repeat=1) -> int:
    """``only``: run just the scenarios whose name contains this text.
    ``repeat``: run each selected scenario that many times (for a fault that
    shows one run in several); every run is counted in the totals."""
    out = out or sys.stdout
    load_all()
    results = []
    before = _player_files()
    repeat = max(1, int(repeat))
    for feature, name, function, limit, is_soak in SCENARIOS:
        if features and feature not in features:
            continue
        if only and only.lower() not in name.lower():
            continue
        if is_soak and not soak:
            continue
        for attempt in range(1, repeat + 1):
            result = _run_one(feature, name, function, limit)
            results.append(result)
            if not quiet or result["status"] != "pass":
                out.write("[%s] %s: %s (%.2f s)%s\n" % (result["status"].upper(), feature, name, result["seconds"],
                                                       "   run %d of %d" % (attempt, repeat) if repeat > 1 else ""))
                if result["status"] != "pass":
                    out.write("       " + result.get("detail", "").replace("\n", "\n       ") + "\n")
                out.flush()
    touched = [name for name, state in _player_files().items() if state != before[name]]
    if touched:
        results.append(dict(feature="harness", name="the player's own files are never touched", status="fail",
                            seconds=0.0, metrics={},
                            detail="changed in the build folder during the run: %s" % ", ".join(touched)))
        out.write("[FAIL] harness: a scenario touched %s in the build folder\n" % ", ".join(touched))
    by_feature = {}
    for result in results:
        entry = by_feature.setdefault(result["feature"], [0, 0])
        entry[0 if result["status"] == "pass" else 1] += 1
    out.write("\n%-14s %6s %6s\n" % ("feature", "passed", "failed"))
    for feature in sorted(by_feature):
        out.write("%-14s %6d %6d   %s\n" % (feature, by_feature[feature][0], by_feature[feature][1],
                                            "PASS" if not by_feature[feature][1] else "FAIL"))
    passed = sum(entry[0] for entry in by_feature.values())
    failed = sum(entry[1] for entry in by_feature.values())
    out.write("%-14s %6d %6d\n" % ("TOTAL", passed, failed))
    out.flush()
    if json_out:
        with open(json_out, "w", encoding="utf-8") as fh:
            json.dump(dict(results=results, passed=passed, failed=failed), fh, indent=1)
    return 1 if failed or not results else 0
