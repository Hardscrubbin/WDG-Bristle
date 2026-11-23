#!/usr/bin/env python3
"""[merge-v3] Self-test on the machine the game is installed on (selftest.sh).

    python3 -B tools/selftest.py [--quick] [--soak] [--screens] [--tests-only]

--tests-only [bristle]: the test suite alone, no simulation scenarios (step 2 is skipped and
says so). Use it while the scenarios are waiting for their own pass. A build without tools/sim
always runs this way.

1. The test suite, one file per process (a hang in one file cannot hide the
   others), grouped by feature.
2. Every simulation scenario of tools/sim, grouped by feature.
3. With --soak: the simulated 8-hour drive. With --screens: the screen renders.

Linux-only behaviour that the Windows build machine had to skip (file modes,
symbolic links, process groups, termios) runs here for the first time: that is
the point of running this on the uConsole.

Everything runs in temporary folders with a private HOME.

The result does not depend on what this machine has running. The tests state
the gpsd they are about themselves (tests/gps_env.py), and on top of that the
run pins the three things the game would otherwise read from the machine:
WDG_GPSD_DEFAULTS points at a file that does not exist (so /etc/default/gpsd is
not read), WDG_GPSD_HOST / WDG_GPSD_PORT point at a local port nothing listens
on (so a running gpsd is not contacted), and WDG_GPS_DEVICE is removed. The
real gpsd is neither used nor disturbed; whether one is running is printed in
the header, for the record only.

The log. A disk-full simulation makes the game say "No space left on device"
hundreds of times. Per test and per scenario the first 3 such lines are kept
and the rest are counted in one line, so a real failure is easy to find. The
summary table is at the very top of the log and again at the bottom.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TIMEOUT = 900

FEATURES = (            # (feature shown, test files whose name contains one of these)
    ("Bristle nodes", ("bristle",)),            # [bristle]
    ("GPS (gpsd, dual receivers)", ("gps",)),
    ("Kismet bridge", ("kismet",)),
    ("Uploads (WiGLE, WDGWars)", ("upload", "wigle", "sync", "portal", "auth", "retention", "pending")),
    ("ADS-B / 433 / SDR", ("adsb", "sdr", "433", "subghz", "ng_plugins", "ng_extras")),
    ("LoRa (MeshCore, Meshtastic)", ("mesh", "lora", "radio", "rns", "reticulum", "spi")),
    ("Loot, XP, backup", ("loot", "xp", "backup", "wardrive_csv", "csv", "scan_session")),
    ("Map, tiles, turf", ("map", "tile", "rabbit", "basemap", "turf")),
    ("Hardware Check, AIO rails", ("hardware", "aio", "power", "shutdown")),
    ("Updater and build identity", ("update", "merge", "release", "version", "ota")),
    ("Simulation harness", ("v3_sim",)),
)


NOISE = "No space left on device"
NOISE_KEEP = 3
CLOSED_PORT = "1"       # tcpmux: nothing listens there; a connect is refused at once
RULE = "=" * 78
_UNIT_START = re.compile(r"^(?:test\w* \([\w.]+\)|===== )")       # a new test, or a new file
_UNIT_END = re.compile(r"^\[(?:PASS|FAIL|CRASH|HANG)\] ")           # a scenario's result line


def squeeze_noise(text: str, keep: int = NOISE_KEEP, needle: str = NOISE) -> str:
    """Keep the first ``keep`` lines that contain ``needle`` per test / per
    scenario and replace the rest by one line with their number.

    A unit starts at a unittest line ("test_x (module.Class.test_x) ...") or a
    "===== file =====" header, and a scenario's output ends at its result line
    ("[PASS] feature: name"). Indented lines are never dropped: tracebacks and
    the detail under a failed scenario are indented, and those are the lines
    this exists to make easy to find."""
    out, seen = [], 0

    def flush():
        nonlocal seen
        if seen > keep:
            out.append('       (%d more "%s" lines suppressed)' % (seen - keep, needle))
        seen = 0

    for line in text.splitlines():
        result_line = _UNIT_END.match(line)
        if result_line or _UNIT_START.match(line):
            flush()
        if needle in line and not result_line and not line[:1].isspace():
            seen += 1
            if seen > keep:
                continue
        out.append(line)
    flush()
    return "\n".join(out) + ("\n" if text.endswith("\n") or out else "")


def gpsd_on_this_machine() -> str:
    """For the log header only. Nothing in the run uses the answer."""
    try:
        socket.create_connection(("127.0.0.1", 2947), timeout=0.5).close()
        return "running on 127.0.0.1:2947 (not used by the tests)"
    except OSError:
        return "not running (not used by the tests)"


def failure_blocks(output: str) -> list:
    """unittest's own report of every failed test in one file's output: the
    "FAIL: name" / "ERROR: name" line with its traceback."""
    return [block.strip() for block in re.findall(
        r"^={70}\n((?:FAIL|ERROR): .*?)(?=^={70}$|^-{70}\nRan \d+ tests? in)", output,
        re.MULTILINE | re.DOTALL)]


def finish_log(log_path: str, summary: str, failures: str = "") -> None:
    """Put the summary at the very top of the log and again at the bottom.
    ``failures`` (every failed test and scenario with its traceback) goes
    right under the top summary, so nobody has to search the details for it.
    The details were written as the run went (so a run that is interrupted
    still leaves them); they are copied, not held in memory."""
    temporary = log_path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as out, open(log_path, encoding="utf-8") as body:
        out.write("%s\nSUMMARY (repeated at the end of this file)\n%s\n%s" % (RULE, RULE, summary))
        if failures:
            out.write("%s\nEVERY FAILURE IN FULL\n%s\n%s\n" % (RULE, RULE, failures.rstrip("\n")))
        out.write("%s\nDETAILS (every test file, then the simulations)\n%s\n" % (RULE, RULE))
        body.readline()                     # the "not finished" marker
        shutil.copyfileobj(body, out)
        out.write("\n%s\nSUMMARY (the same as at the top)\n%s\n%s" % (RULE, RULE, summary))
    os.replace(temporary, log_path)


def feature_of(name: str) -> str:
    for feature, needles in FEATURES:
        if any(needle in name for needle in needles):
            return feature
    return "Other game tests"


def run_file(python, path, env) -> dict:
    """One unittest file in its own interpreter. Counts come from unittest's
    own summary lines."""
    relative = os.path.relpath(path, ROOT)
    in_tests = os.path.basename(os.path.dirname(path)) == "tests"
    module = os.path.splitext(os.path.basename(path))[0]
    code = ("import sys, unittest; sys.path[:0] = %r; "
            "unittest.main(module=%r, argv=['x', '-v'], exit=True)"
            % (([os.path.join(ROOT, "tests"), ROOT] if in_tests else [ROOT]), module))
    started = time.time()
    try:
        done = subprocess.run([python, "-B", "-c", code], cwd=ROOT, env=env, stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=TIMEOUT)
        output, returncode = done.stdout or "", done.returncode
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        output += "\nTIMEOUT after %d s\n" % TIMEOUT
        returncode = -1
    ran = re.search(r"^Ran (\d+) tests? in", output, re.MULTILINE)
    counts = dict(failures=0, errors=0, skipped=0, expected=0)
    summary = re.search(r"^(OK|FAILED)(?: \((.*)\))?\s*$", output, re.MULTILINE)
    if summary and summary.group(2):
        for key, label in (("failures", "failures"), ("errors", "errors"), ("skipped", "skipped"),
                           ("expected", "expected failures")):
            match = re.search(r"%s=(\d+)" % re.escape(label), summary.group(2))
            if match:
                counts[key] = int(match.group(1))
    total = int(ran.group(1)) if ran else 0
    broken = returncode != 0 and not (counts["failures"] or counts["errors"])
    return dict(file=relative, ran=total, passed=total - sum(counts.values()), failed=counts["failures"],
                errors=counts["errors"] + (1 if broken else 0), skipped=counts["skipped"],
                seconds=round(time.time() - started, 1), output=output, ok=returncode == 0,
                failing=["%s: %s" % pair for pair in re.findall(r"^(FAIL|ERROR): (.+?)\s*$", output, re.MULTILINE)])


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    quick, soak, screens = "--quick" in args, "--soak" in args, "--screens" in args
    tests_only = "--tests-only" in args          # [bristle] no simulation scenarios in this run
    python = sys.executable
    home = os.path.expanduser("~")
    log_dir = os.path.join(home, ".watchdogs", "selftest")
    os.makedirs(log_dir, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    log_path = os.path.join(log_dir, "selftest-%s.log" % stamp)
    scratch_home = tempfile.mkdtemp(prefix="wdg-selftest-home-")
    env = dict(os.environ, HOME=scratch_home, USERPROFILE=scratch_home, PYTHONDONTWRITEBYTECODE="1",
               WDG_GPSD_DEFAULTS=os.path.join(scratch_home, "no-gpsd-defaults"), SDL_AUDIODRIVER="dummy",
               WDG_GPSD_HOST="127.0.0.1", WDG_GPSD_PORT=CLOSED_PORT)
    for name in ("WDG_GPS_DEVICE", "JANOS_GPS_DEVICE", "JANOS_GPSD_HOST", "JANOS_GPSD_PORT",
                 "WDG_GPS_ANTENNA_WATCH", "SUDO_USER"):
        env.pop(name, None)

    # [bristle] a build without tools/sim runs the test suite alone, and says so below
    has_sim = os.path.isfile(os.path.join(ROOT, "tools", "sim", "run.py"))
    if not has_sim:
        tests_only, soak, screens = True, False, False
    files = sorted(os.path.join(ROOT, "tests", name) for name in os.listdir(os.path.join(ROOT, "tests"))
                   if name.startswith("test_") and name.endswith(".py"))
    files += sorted(os.path.join(ROOT, name) for name in os.listdir(ROOT)
                    if name.startswith("test_") and name.endswith(".py"))
    if quick:
        files = [path for path in files if os.path.basename(path).startswith("test_v3_")]
    # the scenarios are run once, by tools/sim/run.py below, with per-scenario output
    files = [path for path in files if os.path.basename(path) != "test_v3_sim.py"]
    if tests_only:
        files = [path for path in files if os.path.basename(path) != "test_v3_sim_harness.py"]

    print("WDG-Bristle - self-test on this machine")
    print("  folder : %s" % ROOT)
    print("  python : %s" % sys.version.split()[0])
    print("  log    : %s" % log_path)
    gpsd_state = gpsd_on_this_machine()
    print("  gpsd   : %s" % gpsd_state)
    print("")
    results = []
    with open(log_path, "w", encoding="utf-8") as log:
        log.write("SUMMARY: this run did not finish (the summary is written here when it does)\n")
        log.write("folder %s | python %s | started %s | gpsd on this machine: %s\n"
                  % (ROOT, sys.version.split()[0], stamp, gpsd_state))
        print("[1/2] Test suite: %d file(s)%s" % (len(files), " (quick: this build's own tests only)" if quick else ""))
        for index, path in enumerate(files, 1):
            result = run_file(python, path, env)
            results.append(result)
            log.write("\n===== %s =====\n%s\n" % (result["file"], squeeze_noise(result["output"])))
            log.flush()
            bad = result["failed"] + result["errors"]
            sys.stdout.write("\r  %3d/%d  %-52s %s   " % (index, len(files), result["file"][-52:],
                                                          "ok" if not bad else "FAIL"))
            sys.stdout.flush()
            if bad:
                sys.stdout.write("\n")
        print("")

        scenarios = []
        if tests_only:
            why = "--tests-only" if has_sim else "not part of this build"
            print("[2/2] Simulations: SKIPPED (%s). Only the test suite ran." % why)
            log.write("\n===== simulations =====\nskipped: %s\n" % why)
        else:
            print("[2/2] Simulations (tools/sim)%s" % (" + 8-hour soak" if soak else ""))
            sim_json = os.path.join(scratch_home, "sim.json")
            command = [python, "-B", os.path.join(ROOT, "tools", "sim", "run.py"), "--json", sim_json]
            if soak:
                command.append("--soak")
            sim = subprocess.run(command, cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, timeout=3 * 3600)
            log.write("\n===== simulations =====\n%s\n" % squeeze_noise(sim.stdout or ""))
            try:
                with open(sim_json, encoding="utf-8") as fh:
                    scenarios = json.load(fh).get("results", [])
            except (OSError, ValueError):
                print("  the simulation run produced no result file:\n" + (sim.stdout or "")[-1500:])
        screens_ok = None
        if screens:
            render = subprocess.run([python, "-B", os.path.join(ROOT, "tools", "sim", "render_screens.py")],
                                    cwd=ROOT, env=dict(env, HOME=home), stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600)
            log.write("\n===== screens =====\n%s\n" % squeeze_noise(render.stdout or ""))
            screens_ok = render.returncode == 0

    # ---- per feature
    summary = []

    def say(line=""):
        summary.append(line)
        print(line)

    table = {}
    for result in results:
        row = table.setdefault(feature_of(os.path.basename(result["file"])), dict(passed=0, failed=0, skipped=0))
        row["passed"] += result["passed"]
        row["failed"] += result["failed"] + result["errors"]
        row["skipped"] += result["skipped"]
    say("")
    say("TEST SUITE              %-34s %7s %7s %7s" % ("feature", "passed", "failed", "skipped"))
    for feature in sorted(table):
        row = table[feature]
        say("  %-6s                %-34s %7d %7d %7d" % ("PASS" if not row["failed"] else "FAIL", feature,
                                                         row["passed"], row["failed"], row["skipped"]))
    total_passed = sum(row["passed"] for row in table.values())
    total_failed = sum(row["failed"] for row in table.values())
    total_skipped = sum(row["skipped"] for row in table.values())
    say("  %-6s                %-34s %7d %7d %7d" % ("", "TOTAL", total_passed, total_failed, total_skipped))

    by_feature = {}
    for scenario in scenarios:
        row = by_feature.setdefault(scenario["feature"], [0, 0])
        row[0 if scenario["status"] == "pass" else 1] += 1
    say("")
    say("SIMULATIONS             %-34s %7s %7s" % ("feature", "passed", "failed"))
    for feature in sorted(by_feature):
        passed, failed = by_feature[feature]
        say("  %-6s                %-34s %7d %7d" % ("PASS" if not failed else "FAIL", feature, passed, failed))
    for scenario in scenarios:
        if scenario["status"] != "pass":
            say("      %s: %s -> %s: %s" % (scenario["feature"], scenario["name"], scenario["status"].upper(),
                                              str(scenario.get("detail", "")).strip().splitlines()[-1][:160]))
        if scenario["feature"] == "soak" and scenario.get("metrics"):
            metrics = scenario["metrics"]
            say("      soak: frame ms p50 %s / p95 %s / max %s; traced MB per hour %s; process MB %s"
                  % (metrics.get("frame ms p50"), metrics.get("frame ms p95"), metrics.get("frame ms max"),
                     metrics.get("traced MB per hour"), metrics.get("process MB start/end")))
    sim_failed = sum(row[1] for row in by_feature.values()) + (0 if scenarios or tests_only else 1)
    if screens_ok is not None:
        say("")
        say("SCREENS                 %s" % ("rendered into docs/screens/, text checks pass" if screens_ok
                                              else "FAILED (see the log)"))
    failures = [result for result in results if result["failed"] or result["errors"]]
    if failures:
        say("")
        say("Failing test files (details in the log):")
        for result in failures:
            say("  %s  (%d failed, %d errors)" % (result["file"], result["failed"], result["errors"]))
            for name in result.get("failing", [])[:20]:
                say("      " + name[:150])
    ok = not total_failed and not sim_failed and screens_ok is not False
    say("")
    say("RESULT: %s" % ("PASS - every feature passed on this machine" if ok else
                        "FAIL - send me the log: " + log_path))
    in_full = []
    for result in failures:
        in_full.append("----- %s -----" % result["file"])
        blocks = failure_blocks(squeeze_noise(result["output"]))
        in_full.extend(blocks or ["(no test report: the file crashed, hung or could not be imported; "
                                  "its last lines:)", squeeze_noise(result["output"])[-2000:].strip()])
        in_full.append("")
    for scenario in scenarios:
        if scenario["status"] != "pass":
            in_full.append("----- simulation [%s] %s: %s -----" % (scenario["status"].upper(), scenario["feature"],
                                                                 scenario["name"]))
            in_full.extend([str(scenario.get("detail", "")).strip(), ""])
    try:
        finish_log(log_path, "\n".join(
            ["WDG-Bristle - self-test, started %s" % stamp,
             "folder %s | python %s | gpsd on this machine: %s" % (ROOT, sys.version.split()[0], gpsd_state)]
            + summary) + "\n", "\n".join(in_full))
    except OSError as exc:
        print("(the summary could not be added to the log: %s)" % exc)
    print("Log: %s" % log_path)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
