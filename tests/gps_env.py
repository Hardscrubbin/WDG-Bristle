"""[merge-v3] Put a test in a KNOWN gpsd situation, whatever machine it runs on.

GpsManager looks at the machine in four places:

    GpsManager._connect_gpsd()        is a gpsd answering on 127.0.0.1:2947?
    GpsManager._raw_watch_worker()    (the same address, for antenna status)
    gpsd_configured_devices()         does /etc/default/gpsd name receivers?
    shutil.which("gpsd")              is gpsd installed?
    GpsManager._user_configured       was WDG_GPS_DEVICE set when Python started?

The build PC has no gpsd; the uConsole has one, running and configured. A test
that leaves any of these to the machine gives one result on the PC and another
on the uConsole. upstream's tests/test_gps_uart_power.py did: 5 of its tests expect a
direct open of /dev/ttyAMA0 and failed on the uConsole, where the game
(correctly) found gpsd first.

    no_gpsd(case)                 this machine has no gpsd, and none is set up
    gpsd = Gpsd(case, ...)        this machine has one: running or not,
                                  configured for these receivers or not
    gpsd.send(report, ...)        gpsd sends JSON reports to the game
    gpsd.close()                  gpsd dies

Both undo themselves when the test ends (case.addCleanup). Neither opens a
network port: a "running" gpsd is one end of a local socket pair.
"""
import json
import shutil
import socket
import unittest
from unittest.mock import patch

from watchdogs import gps_manager


class _ModuleScope:
    """addCleanup for a whole test module (see no_gpsd_for_module)."""
    addCleanup = staticmethod(unittest.addModuleCleanup)


def _apply(case, connect, devices, installed, pinned):
    real_which = shutil.which

    def which(name, *args, **kwargs):
        if name == "gpsd":
            return "/usr/sbin/gpsd" if installed else None
        return real_which(name, *args, **kwargs)

    def no_antenna_watch(self, generation, path):
        self._raw_pending.discard(path)          # as when gpsd refuses the connection

    for target, name, value in (
            (gps_manager.GpsManager, "_connect_gpsd", staticmethod(connect)),
            (gps_manager.GpsManager, "_raw_watch_worker", no_antenna_watch),
            (gps_manager.GpsManager, "_user_configured", bool(pinned)),
            (gps_manager, "gpsd_configured_devices", lambda path=None: list(devices)),
            (gps_manager, "GPSD_HOST", "127.0.0.1"),
            # /dev/ttyAMA0 means /dev/ttyAMA0: no symbolic links of the machine,
            # and on Windows no "C:\dev\ttyAMA0"
            (gps_manager.os.path, "realpath", lambda path, **_kwargs: path),
            (gps_manager.shutil, "which", which)):
        patcher = patch.object(target, name, value)
        patcher.start()
        case.addCleanup(patcher.stop)


def no_gpsd(case, pinned=False):
    """No gpsd answers, none is installed, /etc/default/gpsd names nothing.
    ``pinned``: as if WDG_GPS_DEVICE had been set."""
    _apply(case, connect=lambda: None, devices=(), installed=False, pinned=pinned)


def no_gpsd_for_module():
    """no_gpsd() for every test of a module: call it from setUpModule().
    For test files that hand a GpsManager its gpsd link themselves and must
    not find a second, real one on the machine."""
    _apply(_ModuleScope, connect=lambda: None, devices=(), installed=False, pinned=False)


class Gpsd:
    """A gpsd for one test.

    ``devices``    the receivers /etc/default/gpsd names ((): not configured)
    ``running``    False: installed and configured, but not answering (yet);
                   set ``gpsd.running = True`` to let it come up
    ``pinned``     as if WDG_GPS_DEVICE had been set (the player's override)
    """

    def __init__(self, case, devices=("/dev/ttyAMA0", "/dev/ttyACM0"), running=True, pinned=False):
        self.running = running
        self.connects = 0                 # how often the game asked for gpsd
        self.links = []                   # gpsd's end of every connection it accepted
        _apply(case, connect=self._connect, devices=devices, installed=True, pinned=pinned)
        case.addCleanup(self.close)

    def _connect(self):
        self.connects += 1
        if not self.running:
            return None
        ours, theirs = socket.socketpair()
        theirs.setblocking(False)         # what the real _connect_gpsd() hands back
        self.links.append(ours)
        return theirs

    def send(self, *reports):
        data = "".join(json.dumps(report) + "\n" for report in reports).encode("ascii")
        for link in self.links:
            try:
                link.sendall(data)
            except OSError:
                pass

    def close(self):
        """gpsd dies: every connection ends."""
        self.running = False
        links, self.links = self.links, []
        for link in links:
            try:
                link.close()
            except OSError:
                pass


def tpv(device, lat=50.0, lon=19.0, mode=3, second=1):
    return {"class": "TPV", "device": device, "mode": mode, "lat": lat, "lon": lon, "altMSL": 210.0,
            "time": "2026-11-21T12:00:%02d.000Z" % second}


def sky(device, used=8, seen=11, hdop=1.0):
    return {"class": "SKY", "device": device, "hdop": hdop,
            "satellites": [{"PRN": n + 1, "ss": 30.0, "used": n < used} for n in range(seen)]}
