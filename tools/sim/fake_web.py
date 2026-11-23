"""[merge-v3] A scriptable stand-in for wigle.net and the WDGWars portal.

One local HTTP server with both services' endpoints. What each request gets
is scripted per endpoint: queue behaviours with ``script(name, ...)``; when a
queue is empty the endpoint answers normally.

    "ok"                     the normal success answer
    ("status", code)         that HTTP status with a small JSON error body
    ("status", code, {hdr})  ... plus headers (Retry-After)
    ("hang", seconds)        say nothing for that long, then answer normally
    ("accept-hang", s)       take the upload, then hang (the receipt is lost)
    "html"                   200 with a login page instead of JSON
    "empty"                  200 with an empty body
    "redirect"               302 to /login (which serves the login page)

The server keeps what it accepted, so scenarios can check that nothing was
posted twice and nothing was lost.
"""
import base64
import collections
import email
import hashlib
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tools.sim import net

LOGIN_PAGE = b"<!DOCTYPE html><html><head><title>Log in</title></head><body>Please log in</body></html>"
PAIR_KEY = "ab" * 32                 # a well-formed 64-hex key; never a real one


class FakeWeb:
    def __init__(self):
        self.port = 0
        self._server = None
        self._lock = threading.Lock()
        self._scripts = collections.defaultdict(collections.deque)
        self.log = []                         # (method, path, endpoint)
        self.inflight = collections.Counter()
        self.max_inflight = collections.Counter()
        # wigle
        self.wigle_attempts = 0
        self.wigle_files = []                 # (file name, sha256, data rows) accepted
        self.keep_rows = True
        # wdgwars
        self.upload_attempts = 0
        self.accepted = []                    # one set of record keys per accepted upload
        self.known = set()                    # every record the "server" has
        self.imported_new = collections.Counter()   # record key -> times counted as NEW
        self.state = {"ok": True, "me": {"username": "sim-player", "level": 7,
                                         "team": {"name": "Blackout", "color": "#00e5ff"}},
                      "uconsole_rank": {"position": 12, "of": 340},
                      "contracts": [{"title": "Map the harbour", "description": "200 new networks"}]}
        self.state_requests = []              # query strings of game-state reads
        self.pair = dict(code="WDG-4821", poll="poll-secret", pending=2, expires_in=600,
                         username="sim-player", requests=0, polls=0)

    # -- lifecycle -------------------------------------------------------
    def up(self):
        if self._server is not None:
            return self
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *_args):
                pass

            def do_GET(self):
                owner._handle(self, "GET", b"")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                owner._handle(self, "POST", self.rfile.read(length) if length else b"")

        # "network back": the same port again. Free once down() has returned;
        # a bind that still finds it busy is retried, see tools/sim/net.py.
        ThreadingHTTPServer.allow_reuse_address = True
        self._server = net.retry_busy(lambda: ThreadingHTTPServer(("127.0.0.1", self.port), Handler), self.port)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, kwargs=dict(poll_interval=0.05),
                                        name="fake-web", daemon=True)
        self._thread.start()
        return self

    def down(self):
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()             # returns when serve_forever() has left select()
            server.server_close()         # only now is the listening socket closed
            thread = getattr(self, "_thread", None)
            if thread is not None:
                thread.join(3.0)

    @property
    def base(self):
        return "http://127.0.0.1:%d" % self.port

    @property
    def wigle_url(self):
        return self.base + "/api/v2/file/upload"

    @property
    def upload_url(self):
        return self.base + "/api/upload/"

    def script(self, endpoint, *behaviours):
        self._scripts[endpoint].extend(behaviours)

    # -- plumbing --------------------------------------------------------
    def _send(self, handler, status, body=b"{}", content="application/json", headers=None):
        try:
            handler.send_response(status)
            handler.send_header("Content-Type", content)
            handler.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                handler.send_header(name, str(value))
            handler.end_headers()
            handler.wfile.write(body)
        except OSError:
            pass

    def _json(self, handler, payload, status=200, headers=None):
        self._send(handler, status, json.dumps(payload).encode(), headers=headers)

    @staticmethod
    def _endpoint(method, path):
        if path == "/api/v2/file/upload":
            return "wigle"
        if path == "/api/upload/":
            return "upload"
        if path == "/api/game-state":
            return "state"
        if path == "/api/device-pair":
            return "pair"
        if path == "/login":
            return "login"
        return "other"

    def _handle(self, handler, method, body):
        split = urllib.parse.urlsplit(handler.path)
        endpoint = self._endpoint(method, split.path)
        with self._lock:
            self.log.append((method, split.path, endpoint))
            self.inflight[endpoint] += 1
            self.max_inflight[endpoint] = max(self.max_inflight[endpoint], self.inflight[endpoint])
            behaviour = self._scripts[endpoint].popleft() if self._scripts[endpoint] else "ok"
        try:
            self._respond(handler, endpoint, split, body, behaviour)
        finally:
            with self._lock:
                self.inflight[endpoint] -= 1

    def _respond(self, handler, endpoint, split, body, behaviour):
        kind = behaviour if isinstance(behaviour, str) else behaviour[0]
        if endpoint == "login":
            return self._send(handler, 200, LOGIN_PAGE, "text/html")
        if kind == "status":
            headers = behaviour[2] if len(behaviour) > 2 else None
            return self._json(handler, {"ok": False, "error": "scripted HTTP %d" % behaviour[1]},
                              behaviour[1], headers)
        if kind == "html":
            return self._send(handler, 200, LOGIN_PAGE, "text/html")
        if kind == "empty":
            return self._send(handler, 200, b"")
        if kind == "redirect":
            return self._send(handler, 302, b"", headers={"Location": "/login"})
        if kind == "hang":
            time.sleep(behaviour[1])
        if endpoint == "wigle":
            self.wigle_attempts += 1
            return self._wigle(handler, body)
        if endpoint == "upload":
            self.upload_attempts += 1
            reply = self._upload(body)
            if kind == "accept-hang":
                time.sleep(behaviour[1])
            return self._json(handler, reply)
        if endpoint == "state":
            self.state_requests.append(split.query)
            return self._json(handler, self.state)
        if endpoint == "pair":
            return self._json(handler, self._pair(body))
        if split.path == "/api/me":
            return self._json(handler, {"ok": True, "username": "sim-player", "badges": [], "gang": "Blackout"})
        if split.path == "/api/rabbits":
            return self._json(handler, {"ok": True, "rabbits": []})
        return self._json(handler, {"ok": True})

    # -- wigle -----------------------------------------------------------
    def _wigle(self, handler, body):
        content_type = handler.headers.get("Content-Type", "")
        message = email.message_from_bytes(b"Content-Type: " + content_type.encode() + b"\r\n\r\n" + body)
        data, name = b"", ""
        for part in message.walk():
            if part.get_filename():
                name, data = part.get_filename(), part.get_payload(decode=True) or b""
        rows = [line for line in data.decode("utf-8", "replace").splitlines()[2:] if line.strip()]
        # keep_rows False (the soak): remember only how many rows, or this
        # fake would hold megabytes per upload and look like a leak in the game
        self.wigle_files.append((name, hashlib.sha256(data).hexdigest(),
                                 rows if self.keep_rows else len(rows)))
        return self._json(handler, {"success": True, "results": {"transids": [{"transId": "sim"}]}})

    # -- wdgwars ---------------------------------------------------------
    def _upload(self, body) -> dict:
        try:
            envelope = json.loads(body.decode("utf-8"))
            payload = json.loads(base64.b64decode(envelope["data"]))
        except (ValueError, KeyError, TypeError):
            return {"ok": False, "error": "bad envelope"}
        networks = payload.get("networks") or []
        keys = [json.dumps(row, sort_keys=True) for row in networks]
        new = [key for key in keys if key not in self.known]
        with self._lock:
            for key in new:
                self.imported_new[key] += 1
            self.known.update(keys)
            self.accepted.append(set(keys))
        return {"ok": True, "imported": len(new), "updated": 0, "duplicates": len(keys) - len(new),
                "cooldown": 0, "cap_hits": 0, "captured": 0, "skipped": 0,
                "aircraft_imported": len(payload.get("aircraft") or []), "aircraft_already_seen": 0,
                "meshcore_imported": len(payload.get("meshcore_nodes") or []), "meshcore_already_seen": 0,
                "total": len(self.known)}

    def _pair(self, body) -> dict:
        try:
            request = json.loads(body.decode("utf-8"))
        except ValueError:
            return {"ok": False, "error": "bad_request"}
        pair = self.pair
        if request.get("action") == "request":
            pair["requests"] += 1
            return {"ok": True, "code": pair["code"], "poll": pair["poll"], "expires_in": pair["expires_in"]}
        if request.get("code") != pair["code"] or request.get("poll") != pair["poll"]:
            return {"ok": False, "error": "bad_request"}
        pair["polls"] += 1
        if pair["polls"] <= pair["pending"]:
            return {"ok": True, "pending": True}
        return {"ok": True, "api_key": PAIR_KEY, "username": pair["username"]}
