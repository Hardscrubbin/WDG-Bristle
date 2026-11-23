#!/bin/bash
# =============================================================================
# Watch Dogs Go - WDG-Bristle : self-test ON THIS MACHINE
#
#     ./selftest.sh              the test suite + every simulation scenario
#     ./selftest.sh --quick      only this build's own tests and the simulations (~3 min)
#     ./selftest.sh --soak       also the simulated 8-hour drive (several minutes more)
#     ./selftest.sh --screens    also redraw the screens into docs/screens/ (needs the desktop)
#     ./selftest.sh --tests-only the test suite alone (a build without tools/sim always runs this way)
#
# Run it as your normal user, with the game closed. It needs no radio, no GPS
# fix and no network: everything runs against local stand-ins, in temporary
# folders. Your loot, keys and gpsd settings are not touched (the run checks
# that itself and fails if they were).
#
# It prints pass / fail per feature and writes the full log to
# ~/.watchdogs/selftest/. Exit code 0 = everything passed.
#
# The result is the same whether or not gpsd is running on this machine: the
# tests bring their own stand-in gpsd and do not contact the real one.
#
# The log has the summary table at the very top and again at the bottom. The
# "No space left on device" lines of the disk-full simulations are cut to the
# first 3 per test / per scenario, with one line saying how many were left out.
#
# One scenario, many times (for a fault that shows only now and then):
#     .venv/bin/python3 -B tools/sim/run.py 433 --only "no captures" --repeat 50
#     .venv/bin/python3 -B tools/sim/run.py --list
# =============================================================================
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR" || exit 1
if [ "$(id -u)" -eq 0 ]; then
    echo "Run ./selftest.sh as your normal user, not with sudo."
    exit 1
fi
PY="$DIR/.venv/bin/python3"
if [ ! -x "$PY" ]; then
    echo "The game's Python environment is missing (.venv). Run ./install.sh first."
    exit 1
fi
export PYTHONDONTWRITEBYTECODE=1
exec "$PY" -B "$DIR/tools/selftest.py" "$@"
