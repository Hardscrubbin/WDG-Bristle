"""Copy the Bristle core library into a WDG build's plugins/_bristle folder.

    python -I tools/sync_wdg.py "<WDG folder>"          copy
    python -I tools/sync_wdg.py "<WDG folder>" --check  exit 1 if the WDG copy is out of date

The core library (core/bristle/*.py) is plain Python with no WDG imports. WDG carries a copy next
to its own Bristle files (settings, service, ingest, screen). Only the library files listed in
LIBRARY are copied; WDG's own files in that folder are never touched.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIBRARY = ("__init__.py", "codec.py", "crypto.py", "delivery.py", "fleet.py", "gpsplace.py", "link_usb.py",
           "plan.py", "proto.py", "registry.py", "session.py")


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__)
        return 2
    target = pathlib.Path(args[0]) / "plugins" / "_bristle"
    check = "--check" in sys.argv
    stale = []
    for name in LIBRARY:
        source = (ROOT / "core" / "bristle" / name).read_bytes()
        there = target / name
        if not there.exists() or there.read_bytes() != source:
            stale.append(name)
            if not check:
                target.mkdir(parents=True, exist_ok=True)
                there.write_bytes(source)
    # The library's own tests and test vectors travel with it, so WDG's suite (and the self-test on
    # the uConsole) checks the copy it actually runs.
    tests = pathlib.Path(args[0]) / "tests"
    source = (ROOT / "tests" / "test_core.py").read_text(encoding="utf-8")
    for old, new in (('sys.path.insert(0, str(ROOT / "core"))', "sys.path.insert(0, str(ROOT))"),
                     ("from bristle", "from plugins._bristle"),
                     ('ROOT / "tests" / "vectors" / "vectors.json"', 'ROOT / "tests" / "fixtures" / "bristle_vectors.json"'),
                     ("(run from the Bristle folder)", "(copied here by Bristle's tools/sync_wdg.py: edit it there)")):
        if old not in source:
            raise SystemExit("tests/test_core.py no longer contains %r: update tools/sync_wdg.py" % old)
        source = source.replace(old, new)
    for path, data in ((tests / "test_bristle_core.py", source.encode("utf-8")),
                       (tests / "fixtures" / "bristle_vectors.json",
                        (ROOT / "tests" / "vectors" / "vectors.json").read_bytes())):
        if not path.exists() or path.read_bytes() != data:
            stale.append("tests/" + path.name)
            if not check:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
    if check:
        for name in stale:
            print("OUT OF DATE: %s  (run tools/sync_wdg.py)" % name)
        return 1 if stale else 0
    print("copied %d file(s) to %s" % (len(stale), target) if stale else "already up to date: %s" % target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
