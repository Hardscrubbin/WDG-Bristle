"""Copy the Bristle core library into a WDG build's plugins/_bristle folder.

    python -I tools/sync_wdg.py "<WDG folder>"          copy
    python -I tools/sync_wdg.py "<WDG folder>" --check  exit 1 if the WDG copy is out of date

The core library (core/bristle/) is plain Python with no WDG imports. WDG carries a copy next
to its own Bristle files (settings, service, ingest, screens). Only the library files listed in
LIBRARY are copied; WDG's own files in that folder are never touched.
"""
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
LIBRARY = ("__init__.py", "alerts.py", "codec.py", "crypto.py", "delivery.py", "espnow.py", "fleet.py",
           "gpsplace.py", "link_ble.py", "link_espnow.py", "link_usb.py", "nearby.py", "ota.py", "plan.py",
           "proto.py", "registry.py", "second_core.py", "session.py", "signatures.yaml", "skimmer.py")
# What changes when a test file moves from the Bristle folder into a WDG build.
REWRITES = (('sys.path.insert(0, str(ROOT / "core"))', "sys.path.insert(0, str(ROOT))"),
            ("from bristle", "from plugins._bristle"),
            ('ROOT / "tests" / "vectors" / "vectors.json"', 'ROOT / "tests" / "fixtures" / "bristle_vectors.json"'),
            ('ROOT / "core" / "bristle" / "signatures.yaml"', 'ROOT / "plugins" / "_bristle" / "signatures.yaml"'),
            ("(run from the Bristle folder)", "(copied here by Bristle's tools/sync_wdg.py: edit it there)"))


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
    copies = []
    for path in sorted((ROOT / "tests").glob("test_*.py")):
        source = path.read_text(encoding="utf-8")
        for old, new in REWRITES[:2] + REWRITES[4:]:
            if old not in source:
                raise SystemExit("tests/%s no longer contains %r: update tools/sync_wdg.py" % (path.name, old))
        for old, new in REWRITES:
            source = source.replace(old, new)
        copies.append((tests / ("test_bristle_" + path.name[len("test_"):]), source.encode("utf-8")))
    copies.append((tests / "fixtures" / "bristle_vectors.json", (ROOT / "tests" / "vectors" / "vectors.json").read_bytes()))
    for path, data in copies:
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
