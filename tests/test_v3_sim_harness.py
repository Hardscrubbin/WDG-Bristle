"""[merge-v3] The simulation harness's own plumbing (tools/sim): fake servers
that stop and come back on the same port, and the --only / --repeat options.

Why: on the uConsole the scenario "kismet: two GPS receivers" crashed with
"OSError: [Errno 98] Address already in use" when the fake gpsd restarted. The
build PC (Windows) cannot show that fault -- see tools/sim/net.py for the
reason -- so these tests check the property that prevents it on any system:
when stop() returns, no thread of the server is still waiting on its sockets.
On the uConsole they also exercise the real thing: 50 restarts on one port.

Unlike tests/test_v3_sim.py this file is run by ./selftest.sh.
"""
import errno
import io
import json
import socket
import sys
import threading
import time
import unittest
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from plugins import _gpsd_proxy  # noqa: E402
from tools.sim import core, net  # noqa: E402
from tools.sim.fake_gpsd import FakeGpsd  # noqa: E402
from tools.sim.fake_kismet import FakeKismet  # noqa: E402
from tools.sim.fake_web import FakeWeb  # noqa: E402

RESTARTS = 50


def accept_forever(listener):
    while True:
        try:
            sock, _ = listener.accept()
        except socket.timeout:
            continue
        except OSError:
            return
        sock.close()


class ListenerTests(unittest.TestCase):
    def test_a_busy_port_is_retried_until_it_is_free(self):
        for error in (OSError(errno.EADDRINUSE, "Address already in use"), OSError(10048, "WSAEADDRINUSE")):
            with self.subTest(error=error.errno):
                calls = []

                def make():
                    calls.append(1)
                    if len(calls) < 4:
                        raise error
                    return "bound"
                self.assertEqual(net.retry_busy(make, 2947, patience=5.0), "bound")
                self.assertEqual(len(calls), 4)

    def test_a_port_that_stays_busy_fails_after_the_wait_and_says_so(self):
        calls = []

        def make():
            calls.append(1)
            raise OSError(errno.EADDRINUSE, "Address already in use")
        began = time.monotonic()
        with self.assertRaises(OSError) as raised:
            net.retry_busy(make, 2947, patience=0.3)
        self.assertEqual(raised.exception.errno, errno.EADDRINUSE)
        self.assertGreaterEqual(time.monotonic() - began, 0.3)
        self.assertGreater(len(calls), 2)

    def test_only_a_busy_given_port_is_retried(self):
        for port, error in ((0, OSError(errno.EADDRINUSE, "busy")), (2947, OSError(errno.EACCES, "denied")),
                            (2947, ValueError("not a socket error"))):
            with self.subTest(port=port, error=type(error).__name__):
                calls = []

                def make():
                    calls.append(1)
                    raise error
                with self.assertRaises(type(error)):
                    net.retry_busy(make, port, patience=5.0)
                self.assertEqual(len(calls), 1)

    def test_closing_a_listener_waits_until_its_accept_thread_has_gone(self):
        listener = net.listen(0, timeout=2.0)                 # accept() would wait 2 s on its own
        thread = threading.Thread(target=accept_forever, args=(listener,), daemon=True)
        thread.start()
        time.sleep(0.1)                                      # the thread is inside accept()
        began = time.monotonic()
        self.assertTrue(net.close_listener(listener, thread))
        self.assertFalse(thread.is_alive())                  # only now is the port free on Linux
        self.assertLess(time.monotonic() - began, 1.5)       # shutdown() ended the wait early

    def test_one_port_can_be_listened_on_again_and_again(self):
        listener = net.listen(0, timeout=1.0)
        port = listener.getsockname()[1]
        for _ in range(RESTARTS):
            thread = threading.Thread(target=accept_forever, args=(listener,), daemon=True)
            thread.start()
            client = socket.create_connection(("127.0.0.1", port), timeout=3)
            client.close()
            self.assertTrue(net.close_listener(listener, thread))
            listener = net.listen(port, timeout=1.0)         # the same port, at once
            self.assertEqual(listener.getsockname()[1], port)
        listener.close()

    def test_record_what_this_system_does_with_a_listener_closed_under_accept(self):
        """Not a pass/fail check: one line for the log. "busy" is the Linux
        behaviour that crashed the scenario; the harness no longer depends on it."""
        listener = net.listen(0, timeout=1.0)
        port = listener.getsockname()[1]
        thread = threading.Thread(target=accept_forever, args=(listener,), daemon=True)
        thread.start()
        time.sleep(0.1)
        listener.close()                                     # the old way: close, no shutdown, no join
        again = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        again.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            again.bind(("127.0.0.1", port))
            again.listen(1)
            outcome = "free at once"
        except OSError as exc:
            outcome = "still BUSY (%s)" % exc
        finally:
            again.close()
        thread.join(3.0)
        sys.stderr.write("[info] %s: a port whose listener was closed while a thread waited in accept() was %s\n"
                         % (sys.platform, outcome))
        self.assertFalse(thread.is_alive())


class FakeGpsdRestartTests(unittest.TestCase):
    def watcher(self, fake):
        client = socket.create_connection(("127.0.0.1", fake.port), timeout=3)
        client.settimeout(3)
        self.addCleanup(client.close)
        self.assertIn(b'"VERSION"', client.recv(4096))
        client.sendall(b'?WATCH={"enable":true,"json":true};\n')
        return client

    def test_stop_returns_with_every_thread_of_the_server_gone(self):
        fake = FakeGpsd().add("/dev/ttyAMA0").add("/dev/ttyACM0").start()
        self.addCleanup(fake.stop)
        for _ in range(3):
            self.watcher(fake)
        self.assertTrue(fake.wait_clients(3))
        accept_thread, serving = fake._accept_thread, list(fake._serve_threads)
        self.assertEqual(len(serving), 3)
        fake.stop()
        self.assertFalse(accept_thread.is_alive())
        self.assertEqual([t.is_alive() for t in serving], [False] * 3)
        self.assertEqual((fake._listener, fake.clients("json")), (None, []))
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", fake.port), timeout=1).close()

    def test_restart_keeps_the_port_with_a_client_connected_every_time(self):
        fake = FakeGpsd().add("/dev/ttyAMA0").start()
        self.addCleanup(fake.stop)
        port = fake.port
        for round_number in range(RESTARTS):
            client = self.watcher(fake)
            self.assertTrue(fake.wait_clients(1), "round %d" % round_number)
            fake.emit()
            self.assertIn(b'"TPV"', self.read_until(client, b'"TPV"'))
            fake.restart()                                   # gpsd restarts under its client
            self.assertEqual(fake.port, port, "round %d" % round_number)
            self.assertEqual(self.read_to_end(client), b"")  # the old link is over
            client.close()
        self.assertEqual(fake.starts, RESTARTS + 1)
        self.watcher(fake)                                   # and it still serves
        self.assertTrue(fake.wait_clients(1))

    def test_starting_twice_keeps_one_listener(self):
        fake = FakeGpsd().start()
        self.addCleanup(fake.stop)
        listener, port = fake._listener, fake.port
        self.assertIs(fake.start()._listener, listener)
        self.assertEqual((fake.port, fake.starts), (port, 1))

    @staticmethod
    def read_until(client, needle):
        data = b""
        deadline = time.monotonic() + 3
        while needle not in data and time.monotonic() < deadline:
            try:
                data += client.recv(65536)
            except socket.timeout:
                break
        return data

    @staticmethod
    def read_to_end(client):
        """b"" when the server closed the connection (possibly after data)."""
        client.settimeout(3)
        try:
            while True:
                if not client.recv(65536):
                    return b""
        except ConnectionError:
            return b""                                       # a reset is "over" as well
        except socket.timeout:
            return b"still open"


class FakeHttpRestartTests(unittest.TestCase):
    def test_fake_kismet_comes_back_on_its_port(self):
        server = FakeKismet()
        self.addCleanup(server.down)
        server.up()
        port = server.port
        for _ in range(RESTARTS):
            with urllib.request.urlopen(server.url + "/system/status.json", timeout=3) as reply:
                self.assertEqual(json.load(reply), {"kismet.system.version": "sim"})
            server.down()                                    # Kismet crashes / is stopped
            self.assertFalse(server._thread.is_alive())
            server.up()                                      # ...and is started again
            self.assertEqual(server.port, port)

    def test_fake_web_comes_back_on_its_port(self):
        web = FakeWeb().up()
        self.addCleanup(web.down)
        port = web.port
        for _ in range(RESTARTS):
            with urllib.request.urlopen(web.base + "/api/me", timeout=3) as reply:
                self.assertTrue(json.load(reply)["ok"])
            web.down()                                       # network down
            self.assertFalse(web._thread.is_alive())
            web.up()                                         # network back
            self.assertEqual(web.port, port)


class GpsFilterRestartTests(unittest.TestCase):
    """The game's own GPS filter for Kismet (plugins/_gpsd_proxy.py) is
    stopped and started with every capture. It must come back whether or not
    its usual port is free again yet."""

    def test_the_filter_can_be_restarted_at_once(self):
        first = _gpsd_proxy.GpsdDeviceProxy(lambda: "/dev/ttyAMA0", upstream=("127.0.0.1", 1), port=0)
        wanted = first.start()
        first.stop()
        for _ in range(20):
            proxy = _gpsd_proxy.GpsdDeviceProxy(lambda: "/dev/ttyAMA0", upstream=("127.0.0.1", 1), port=wanted)
            port = proxy.start()                             # the wanted port, or any free one
            self.assertTrue(proxy.running)
            client = socket.create_connection(("127.0.0.1", port), timeout=3)
            client.settimeout(3)
            self.assertIn(b'"VERSION"', client.recv(4096))
            client.close()
            began = time.monotonic()
            proxy.stop()
            self.assertLess(time.monotonic() - began, 0.25)  # stop() runs on the game thread
            self.assertFalse(proxy.running)


class RunnerOptionTests(unittest.TestCase):
    def run_with(self, scenarios, **options):
        saved, load_all, files = core.SCENARIOS[:], core.load_all, core._player_files
        out = io.StringIO()
        try:
            core.SCENARIOS[:] = scenarios
            core.load_all = lambda: None
            core._player_files = lambda: {}
            code = core.run(out=out, **options)
        finally:
            core.SCENARIOS[:] = saved
            core.load_all, core._player_files = load_all, files
        return code, out.getvalue()

    def test_only_and_repeat_pick_one_scenario_and_run_it_n_times(self):
        ran = []
        scenarios = [("kismet", "two GPS receivers: Kismet reads only the one in use",
                      lambda ctx: ran.append("gps"), 5, False),
                     ("kismet", "normal capture and stop", lambda ctx: ran.append("normal"), 5, False),
                     ("433", "no captures, corrupt and odd files", lambda ctx: ran.append("433"), 5, False)]
        code, text = self.run_with(scenarios, only="two gps", repeat=20)
        self.assertEqual((code, ran), (0, ["gps"] * 20))
        self.assertIn("run 20 of 20", text)
        self.assertIn("kismet             20      0   PASS", text)

    def test_one_bad_run_in_many_fails_the_whole_run(self):
        ran = []

        def flaky(ctx):
            ran.append(1)
            core.check(len(ran) != 7, "the seventh run failed")
        code, text = self.run_with([("433", "sometimes", flaky, 5, False)], repeat=10, quiet=True)
        self.assertEqual((code, len(ran)), (1, 10))
        self.assertIn("[FAIL] 433: sometimes", text)
        self.assertIn("run 7 of 10", text)
        self.assertIn("433                 9      1   FAIL", text)


if __name__ == "__main__":
    unittest.main()
