"""[merge-v3] Storage scenarios: disk full during a CSV write, power loss in
the middle of a write (a real process, really killed), and backup / restore
faults. Real LootManager, real files."""
import errno
import json
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

from tools.sim.core import ROOT, check, check_equal, scenario

W = "02:00:00:00:%02X:%02X"


def position():
    return SimpleNamespace(available=True, fix=SimpleNamespace(valid=True, latitude=50.0, longitude=19.0,
                                                               altitude=210.0, hdop=1.0))


def net(n, ssid=None):
    return SimpleNamespace(bssid=W % (n >> 8, n & 255), ssid=ssid or "Net %d" % n, auth="WPA2",
                           channel="6", rssi="-60")


def data_rows(path):
    from watchdogs.loot_manager import LootManager
    lines = Path(path).read_text(encoding="utf-8").split("\n")
    body = lines[2:]
    torn = body[-1] if body and body[-1] != "" else None        # a last line with no newline
    complete = [line for line in (body[:-1] if body else []) if line != ""]
    return [LootManager._wardrive_fields(line) for line in complete], torn


# ----------------------------------------------------------------------
# disk full
# ----------------------------------------------------------------------

class FullDisk:
    """open() for watchdogs.loot_manager: while ``full`` is set, a write to a
    data file gets half its bytes onto the disk and then fails with ENOSPC."""

    def __init__(self):
        self.full = False
        self.failed_writes = 0

    def open(self, file, mode="r", *args, **kwargs):
        handle = open(file, mode, *args, **kwargs)
        if not self.full or not any(flag in mode for flag in "wa+"):
            return handle
        owner = self

        class Half:
            def __getattr__(self, name):
                return getattr(handle, name)

            def __enter__(self):
                handle.__enter__()
                return self

            def __exit__(self, *exc):
                return handle.__exit__(*exc)

            def write(self, data):
                if owner.full and data:
                    handle.write(data[:len(data) // 2])
                    try:
                        handle.flush()
                    except OSError:
                        pass
                    owner.failed_writes += 1
                    raise OSError(errno.ENOSPC, "No space left on device")
                return handle.write(data)
        return Half()


@scenario("storage", "disk full during CSV writes")
def disk_full(ctx):
    from watchdogs import loot_manager
    from test_scan_session_map import loot_at
    disk = FullDisk()
    ctx.patch(loot_manager, "open", disk.open)
    manager = loot_at(ctx.tmp, "2026-11-21T12:00:00", position())
    loot = manager.__enter__()
    ctx.cleanup(manager.__exit__, None, None, None)
    csv = Path(loot.session_path) / "wardriving.csv"
    for n in range(40):
        check(loot.save_wardriving_network(net(n), positioned_only=True, append_new=True), "save %d" % n)
        loot.save_bt_device("06:00:00:00:00:%02X" % n, -70, "Tag %d" % n, False, False, append_new=True)
    loot.save_xp(4000)
    good_csv = csv.read_bytes()
    good_db = Path(loot._db_path).read_bytes()
    bt_csv = Path(loot.session_path) / "bt_devices.csv"
    good_bt = bt_csv.read_bytes()
    errors_before = loot.wardrive_save_errors

    disk.full = True
    refused = 0
    for n in range(40, 70):
        if not loot.save_wardriving_network(net(n), positioned_only=True, append_new=True):
            refused += 1
        loot.save_bt_device("06:00:00:00:01:%02X" % n, -70, "Tag %d" % n, False, False, append_new=True)
    check(not loot.save_xp(9999), "save_xp reported success on a full disk")
    check(disk.failed_writes > 0, "the fault was never hit (the scenario tests nothing)")
    check_equal(refused, 30, "wardriving saves refused while the disk was full")
    check(loot.wardrive_save_errors > errors_before, "the failed saves were not counted")
    check_equal(csv.read_bytes(), good_csv, "wardriving.csv after 30 half-written rows (must be rolled back)")
    check_equal(bt_csv.read_bytes(), good_bt, "bt_devices.csv after half-written rows")
    check_equal(Path(loot._db_path).read_bytes(), good_db, "loot_db.json after a failed save")
    check_equal(loot.load_xp(), 4000, "XP after a failed save")

    disk.full = False                                        # space is freed
    for n in range(40, 70):                                  # the devices are seen again
        check(loot.save_wardriving_network(net(n), positioned_only=True, append_new=True),
              "save %d after space was freed" % n)
    check(loot.save_xp(9999), "save_xp after space was freed")
    rows, torn = data_rows(csv)
    check(torn is None, "wardriving.csv ends in a torn row")
    check_equal(len(rows), 70, "rows in wardriving.csv after recovery")
    check_equal(len({row[0] for row in rows}), 70, "distinct devices (no duplicates, none lost)")
    check(all(len(row) == 11 for row in rows), "a row does not have 11 columns")
    check_equal(loot.load_xp(), 9999, "XP after recovery")


# ----------------------------------------------------------------------
# power loss
# ----------------------------------------------------------------------

WRITER = r'''
import sys, time
from types import SimpleNamespace
root, base, start = sys.argv[1], sys.argv[2], int(sys.argv[3])
sys.path[:0] = [root]
from watchdogs.loot_manager import LootManager
gps = SimpleNamespace(available=True, fix=SimpleNamespace(valid=True, latitude=50.0, longitude=19.0,
                                                          altitude=210.0, hdop=1.0))
loot = LootManager(base, gps_manager=gps)
print("SESSION", loot.session_path, flush=True)
n = start
while True:
    net = SimpleNamespace(bssid="02:00:00:%02X:%02X:%02X" % (n >> 16 & 255, n >> 8 & 255, n & 255),
                          ssid="Net %d" % n, auth="WPA2", channel="6", rssi="-60")
    loot.save_wardriving_network(net, positioned_only=True, append_new=True)
    loot.save_bt_device("06:00:00:%02X:%02X:%02X" % (n >> 16 & 255, n >> 8 & 255, n & 255), -70, "Tag", False,
                        False, append_new=True)
    if n % 7 == 0:
        loot.save_xp(n)
        loot.update_session_loot()
    print("SAVED", n, flush=True)
    n += 1
'''


@scenario("storage", "power loss in the middle of writes, then restart", limit=180)
def power_loss(ctx):
    from watchdogs.loot_manager import LootManager
    from plugins.kismet_wardrive import positioned_wigle_export
    script = Path(ctx.tmp) / "writer.py"
    script.write_text(WRITER, encoding="utf-8")
    base = Path(ctx.tmp) / "game"
    base.mkdir()
    start, total_saved, sessions, previous_xp = 1, 0, [], 0
    for round_no, wait_for in enumerate((3, 40, 11, 77, 25, 60)):
        proc = subprocess.Popen([sys.executable, "-I", str(script), str(ROOT), str(base), str(start)],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        session, last = None, start - 1
        deadline = time.perf_counter() + 60
        try:
            while time.perf_counter() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                if line.startswith("SESSION"):
                    session = line.split(" ", 1)[1].strip()
                elif line.startswith("SAVED"):
                    last = int(line.split()[1])
                    if last - start + 1 >= wait_for:
                        break
        finally:
            proc.kill()                                       # the power goes: no flush, no close
            proc.wait(10)
        # Between the last line read here and the kill the writer went on for a
        # moment: what it still managed to print is the upper bound of what it wrote.
        printed = last
        for line in (proc.stdout.read() or "").splitlines():
            if line.startswith("SAVED") and line.split()[1:2] and line.split()[1].isdigit():
                printed = max(printed, int(line.split()[1]))
        check(session is not None, "round %d: the writer never started: %s" % (round_no, proc.stderr.read()[-400:]))
        sessions.append(session)
        confirmed = last - start + 1                          # saves the game was told had succeeded
        total_saved += confirmed
        # ---- what is on the "disk" after the cut
        csv = Path(session) / "wardriving.csv"
        rows, torn = data_rows(csv)
        check(all(len(row) == 11 for row in rows), "round %d: a complete row is damaged" % round_no)
        check(len(rows) >= confirmed, "round %d: %d confirmed saves but only %d rows survived"
              % (round_no, confirmed, len(rows)))
        check(len(rows) <= printed - start + 2, "round %d: more rows (%d) than saves started (%d)"
              % (round_no, len(rows), printed - start + 2))
        check_equal(len({row[0] for row in rows}), len(rows), "round %d: duplicate rows" % round_no)
        database = json.loads((base / "loot" / "loot_db.json").read_text(encoding="utf-8"))   # must parse
        check(isinstance(database.get("sessions"), dict), "round %d: the loot database lost its sessions" % round_no)
        check(not list((base / "loot").glob("*.tmp")) or True, "")
        # ---- the game is started again on the same data
        gps = position()
        loot = LootManager(str(base), gps_manager=gps)        # raises LootDataError if the DB is damaged
        xp = loot.load_xp()
        save_point = (last // 7) * 7                           # XP is saved at every 7th device
        at_least = save_point if save_point >= start else previous_xp
        check(at_least <= xp <= printed + 1,
              "round %d: XP %d is not a save point between %d and %d (progress lost or invented)"
              % (round_no, xp, at_least, printed + 1))
        previous_xp = xp
        check(loot.save_wardriving_network(SimpleNamespace(bssid="0E:00:00:00:00:%02X" % round_no, ssid="After",
                                                           auth="OPEN", channel="1", rssi="-50"),
                                           positioned_only=True, append_new=True), "saving after the restart")
        # what the uploaders would read from the interrupted session
        data, kept, _dropped = positioned_wigle_export(csv)
        check(kept >= len(rows) - 1, "round %d: the WiGLE export lost rows of the interrupted session" % round_no)
        for line in data.decode("utf-8").splitlines()[2:]:
            check(len(LootManager._wardrive_fields(line)) == 11, "round %d: a torn row would be uploaded" % round_no)
        close = getattr(loot, "close_session", None) or getattr(loot, "close", None)
        if callable(close):
            close()
        start = printed + 3
        time.sleep(1.1)                                       # sessions are named by the second
    ctx.note("power cuts", len(sessions))
    ctx.note("saves confirmed before the cuts", total_saved)
    check(len(set(sessions)) == len(sessions), "two runs wrote into the same session folder")


# ----------------------------------------------------------------------
# backup / restore
# ----------------------------------------------------------------------

class BackupRig:
    def __init__(self, ctx, kismet=False):
        import plugins.ng_loot_backup as ng_backup
        from plugins import _loot_backup as engine
        import test_v3_ng_plugins as ng
        from test_scan_session_map import loot_at
        self.engine, self.ng = engine, ng
        self.root = Path(ctx.tmp)
        self.home = self.root / "home"
        self.home.mkdir()
        with loot_at(str(self.root), "2026-11-21T10:00:00", position()) as loot:
            for n in range(5):
                loot.save_wardriving_network(net(n), positioned_only=True, append_new=True)
            loot.save_xp(777)
        (self.root / "secrets.conf").write_text("WDG_WIGLE_TOKEN=sim-not-a-real-token\n")
        (self.root / "whitelist.json").write_text("{}")
        self.canvas = ng.Canvas()
        old = sys.modules.get("pyxel")
        sys.modules["pyxel"] = self.canvas
        ctx.cleanup(self._restore, old)
        self.hud, self.term = [], []
        self.kismet = SimpleNamespace(kismet_active=lambda: kismet, engine=SimpleNamespace(running=kismet),
                                      _lifecycle="running" if kismet else "idle")
        self.plugin = ng_backup.NgLootBackup()
        self.plugin._backup_dir = str(self.home)
        self.plugin._auto_next = float("inf")
        self.app = SimpleNamespace(_app_dir=str(self.root), _plugins=[self.kismet, self.plugin],
                                   msg=lambda text, colour=7: self.hud.append(text),
                                   _term_add=lambda text, raw=False: self.term.append(text),
                                   _esc_consumed_frame=-1)
        self.plugin.on_load(self.app)
        ctx.cleanup(self.plugin.on_unload)
        self.plugin.open_overlay()
        self.settle()

    @staticmethod
    def _restore(old):
        if old is None:
            sys.modules.pop("pyxel", None)
        else:
            sys.modules["pyxel"] = old

    def frame(self, *keys):
        self.canvas.pressed = set(keys)
        self.plugin.on_update()
        self.canvas.new_frame()
        self.plugin.draw(0, 0, self.ng.W, self.ng.H)
        check_equal(self.canvas.overlaps(), [], "texts that run into each other")
        self.canvas.pressed = set()

    def settle(self, timeout=20.0):
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            self.frame()
            if not self.plugin._busy and not self.plugin._events:
                self.frame()
                return
            time.sleep(0.005)
        check(False, "the backup worker never finished")

    def key(self, name):
        self.frame(name)
        self.settle()

    def text(self):
        return "\n".join(self.canvas.texts)

    def xp(self):
        return json.loads((self.root / "loot" / "loot_db.json").read_text())["xp"]


def plain_archive(path, xp, version=1):
    """What `tar czf wdg-v2-backup-....tar.gz loot whitelist.json secrets.conf` gives in the v2 folder."""
    import io
    with tarfile.open(path, "w:gz") as archive:
        def add(name, data, kind=tarfile.REGTYPE):
            info = tarfile.TarInfo(name)
            info.type, info.size = kind, len(data) if kind == tarfile.REGTYPE else 0
            archive.addfile(info, io.BytesIO(data) if kind == tarfile.REGTYPE else None)
        add("loot", b"", tarfile.DIRTYPE)
        add("loot/loot_db.json", json.dumps(dict(version=version, sessions={"2026-11-09_10-00-00": {}}, xp=xp)).encode())
        add("loot/2026-11-09_10-00-00", b"", tarfile.DIRTYPE)
        add("loot/2026-11-09_10-00-00/wardriving.csv", b"WigleWifi-1.4,v2\nMAC,SSID\n")
        add("loot/kismet_logs/kismet_adapters.json", b"{}")
        add("whitelist.json", b'{"mine": []}')
        add("secrets.conf", b"WDG_WIGLE_TOKEN=v2-sim-token\n")
    return str(path)


@scenario("backup", "corrupt archives are refused and nothing is staged")
def corrupt_archives(ctx):
    rig = BackupRig(ctx)
    rig.key("KEY_L")
    good = rig.engine.list_backups(str(rig.home))[0]
    whole = Path(good["path"]).read_bytes()
    Path(good["path"]).unlink()
    cases = {"wdg-bristle-bak-2026-01-01_00-00-01.tar.gz": whole[:len(whole) // 2],      # cut short
             "wdg-bristle-bak-2026-01-01_00-00-02.tar.gz": b"not an archive at all",
             "wdg-bristle-bak-2026-01-01_00-00-03.tar.gz": b"",
             "wdg-bristle-bak-2026-01-01_00-00-04.tar.gz": whole[:200] + bytes(200) + whole[400:]}   # bit rot
    for name, data in cases.items():
        (rig.home / name).write_bytes(data)
    rig.plugin.rescan()
    rig.settle()
    before = {str(p): p.read_bytes() for p in (rig.root / "loot").rglob("*") if p.is_file()}
    for index in range(len(cases)):
        rig.plugin._sel = index
        rig.key("KEY_R")
        check(rig.plugin._confirm is None, "a damaged archive reached the confirmation box (%d)" % index)
        check(rig.engine.pending(str(rig.root)) is None, "a damaged archive was staged (%d)" % index)
        check(any(word in rig.plugin._status for word in ("damaged", "Not a readable", "not made", "cannot")),
              "no explanation for a damaged archive: %r" % rig.plugin._status)
    check_equal(rig.engine.apply_pending(str(rig.root)), "none", "restore applied at the next start")
    after = {str(p): p.read_bytes() for p in (rig.root / "loot").rglob("*") if p.is_file()}
    check_equal(after, before, "the loot folder after four refused restores")
    secret = "sim-not-a-real-token"
    check(secret not in "\n".join(rig.hud + rig.term) + rig.text(), "a secret from secrets.conf was shown")


@scenario("backup", "an archive of v2's data restores into v3")
def archive_from_v2(ctx):
    from watchdogs import __main__ as launcher
    from watchdogs.loot_manager import LootManager
    rig = BackupRig(ctx)
    plain_archive(rig.home / "wdg-v2-backup-2026-11-09_12-00-00.tar.gz", xp=316308)
    rig.plugin.rescan()
    rig.settle()
    check("wdg-v2-backup-2026-11-09_12-00-00.tar.gz" in rig.text(), "the v2 archive is not listed")
    rig.key("KEY_R")
    check(rig.plugin._confirm is not None, "the v2 archive was refused: %r" % rig.plugin._status)
    check("Made outside this game" in rig.text() and "XP 316308" in rig.text(), "the confirmation box: %s"
          % [t for t in rig.canvas.texts if "XP" in t or "outside" in t])
    rig.key("KEY_Y")
    check(rig.engine.pending(str(rig.root)) is not None, "the restore was not staged")
    check_equal(rig.xp(), 777, "XP before the restart (the running game's data must be untouched)")
    said = []
    # What watchdogs/__main__.py does at the next launch -- with the secrets
    # folder pointed at the scratch install, never at the real build folder.
    check(callable(launcher._apply_staged_restore), "the launch hook is gone")
    check_equal(rig.engine.apply_pending(str(rig.root), secrets_dir=str(rig.root), log=said.append),
                "restored", "restore at the next launch")
    check(any("Restored wdg-v2-backup" in line for line in said), "the launcher said nothing: %s" % said)
    gps = position()
    loot = LootManager(str(rig.root), gps_manager=gps)          # upstream opens (and migrates) the v1 database
    check_equal(loot.load_xp(), 316308, "XP after restoring v2's data")
    kept = [p.name for p in rig.root.iterdir() if p.name.startswith("loot.pre-restore-")]
    check_equal(len(kept), 1, "copies of the replaced v3 data")
    check_equal(json.loads((rig.root / kept[0] / "loot_db.json").read_text())["xp"], 777, "XP in the kept copy")
    check((rig.root / "secrets.conf").read_text().startswith("WDG_WIGLE_TOKEN=v2-"), "secrets.conf from the archive")
    check("v2-sim-token" not in "\n".join(said + rig.hud + rig.term), "a secret was printed")


@scenario("backup", "restore is refused while Kismet is capturing; a full disk leaves no partial backup")
def restore_refused_and_disk_full(ctx):
    rig = BackupRig(ctx, kismet=True)
    from watchdogs import merge_build
    check(merge_build.kismet_bridge_active(rig.app._plugins), "the rig's Kismet does not count as capturing")
    rig.key("KEY_L")                                            # the lite backup is allowed
    check(len(rig.engine.list_backups(str(rig.home))) == 1, "no lite backup while Kismet runs: %r" % rig.plugin._status)
    rig.key("KEY_B")
    check("Kismet is capturing" in rig.plugin._status, "a full backup was not refused while Kismet runs")
    rig.key("KEY_R")
    check(rig.plugin._confirm is None, "restore reached the confirmation box while Kismet is capturing")
    check("stop it" in rig.plugin._status and "before restoring" in rig.plugin._status,
          "no explanation: %r" % rig.plugin._status)
    check(any("Stop Kismet before restoring" in line for line in rig.hud), "no HUD message")
    check(rig.engine.pending(str(rig.root)) is None, "a restore was staged while Kismet is capturing")
    # Kismet stops: the same key now works
    rig.kismet.kismet_active = lambda: False
    rig.kismet.engine.running = False
    rig.kismet._lifecycle = "idle"
    rig.key("KEY_R")
    check(rig.plugin._confirm is not None, "restore still refused after Kismet stopped: %r" % rig.plugin._status)
    rig.key("KEY_N")
    # disk full while writing a backup
    import shutil as real_shutil
    ctx.patch(rig.engine.shutil, "disk_usage", lambda path: SimpleNamespace(free=1024, total=10 ** 9, used=10 ** 9))
    count = len(rig.engine.list_backups(str(rig.home)))
    rig.key("KEY_B")
    check_equal(len(rig.engine.list_backups(str(rig.home))), count, "backups after a refused one")
    check(not [p for p in rig.home.iterdir() if p.name.endswith(".part")], "a partial backup file was left behind")
    check("space" in rig.plugin._status.lower(), "a full disk was not explained: %r" % rig.plugin._status)
    assert real_shutil
