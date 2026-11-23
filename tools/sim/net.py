"""[merge-v3] Listening sockets for the fake servers: open, close, open again.

Why this file exists. On the uConsole the scenario "kismet: two GPS receivers"
crashed with ``OSError: [Errno 98] Address already in use`` when the fake gpsd
restarted on its port. The listening socket already had SO_REUSEADDR, and the
restart did close it first. The cause is how Linux closes a socket that
another thread is waiting on: the accept thread sits in poll() (accept with a
timeout), and the kernel keeps the socket alive -- still LISTENING on the port
-- until that poll() returns, up to the accept timeout later. SO_REUSEADDR
does not allow a second listener on a port that still has one, so a bind made
in that window fails. Windows ends the wait at once, which is why the build PC
never showed it.

The rule for every fake server here:

    close_listener(sock, accept_thread)   shutdown() wakes the accept thread
                                          at once, close() frees the handle,
                                          and the thread is JOINED: only when
                                          it has left accept() is the port free
    listen(port)                          SO_REUSEADDR, and when a given port
                                          is still busy the bind is retried
                                          for a few seconds instead of failing

Both halves are used: the join makes the retry unnecessary in practice, the
retry covers a port that something else holds for a moment.
"""
import errno
import socket
import time

BUSY = {errno.EADDRINUSE, 10048}         # Linux / macOS, and WSAEADDRINUSE on Windows
BIND_PATIENCE = 5.0                      # seconds a given port may stay busy


def is_busy(exc) -> bool:
    return isinstance(exc, OSError) and (exc.errno in BUSY or getattr(exc, "winerror", None) == 10048)


def retry_busy(make, port, patience=BIND_PATIENCE):
    """Call ``make()`` (which binds ``port``) until it stops failing with
    "address already in use". Port 0 (any free port) is never retried."""
    deadline = time.monotonic() + patience
    while True:
        try:
            return make()
        except OSError as exc:
            if not port or not is_busy(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def listen(port=0, backlog=8, timeout=0.2, host="127.0.0.1"):
    """A listening TCP socket on ``host:port`` (0 = any free port)."""
    def make():
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.listen(backlog)
        except OSError:
            sock.close()
            raise
        sock.settimeout(timeout)
        return sock
    return retry_busy(make, port)


def close_socket(sock) -> None:
    """shutdown() first: it ends a recv()/accept() another thread is waiting
    in right away (close() alone does not on Linux), then close()."""
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass                              # not connected (a listener on Windows), or already closed
    try:
        sock.close()
    except OSError:
        pass


def close_listener(sock, thread=None, wait=3.0) -> bool:
    """Close a listening socket and wait until its accept thread has ended.
    True when the port is free again (the thread is gone)."""
    close_socket(sock)
    if thread is not None and thread.is_alive():
        thread.join(wait)
        return not thread.is_alive()
    return True
