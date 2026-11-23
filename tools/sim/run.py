"""[merge-v3] Run the simulation scenarios. See tools/sim/core.py.

    python -I tools/sim/run.py [feature ...] [--soak] [--json FILE] [--quiet] [--list]
                               [--only TEXT] [--repeat N]

    --only TEXT    only the scenarios whose name contains TEXT
    --repeat N     run each selected scenario N times (a fault that shows one
                   run in several); every run counts in the totals

    python -I tools/sim/run.py 433 --only "no captures" --repeat 50
"""
import os
import sys

# `python -I tools/sim/run.py` (isolated mode adds neither the script's folder nor
# the current one to sys.path): find the build folder from this file.
_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from tools.sim import core  # noqa: E402


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    soak = "--soak" in args
    quiet = "--quiet" in args
    json_out = only = None
    repeat = 1
    if "--json" in args:
        index = args.index("--json")
        json_out = args[index + 1]
        del args[index:index + 2]
    if "--only" in args:
        index = args.index("--only")
        only = args[index + 1]
        del args[index:index + 2]
    if "--repeat" in args:
        index = args.index("--repeat")
        repeat = int(args[index + 1])
        del args[index:index + 2]
    if "--list" in args:
        core.load_all()
        for feature, name, _function, _limit, is_soak in core.SCENARIOS:
            print("%-12s %s%s" % (feature, name, "   (soak)" if is_soak else ""))
        return 0
    features = [arg for arg in args if not arg.startswith("--")]
    return core.run(features=features or None, soak=soak, json_out=json_out, quiet=quiet, only=only, repeat=repeat)


if __name__ == "__main__":
    sys.exit(main())
