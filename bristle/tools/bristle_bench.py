"""Bristle bench tool: drive real nodes over USB from a terminal, before WDG integration.

    python3 -I tools/bristle_bench.py [--exclude /dev/ttyACM0] [--auto]

Uses the same core library WDG will use. Keeps its own owner key and Node Memory in ~/.bristle-bench/
(separate from WDG, so bench testing never touches WDG data). Finds are printed and saved to
~/.bristle-bench/finds-<time>.csv. No GPS placement here (that's WDG's job); times are node timestamps.

It never writes to a serial port on its own unless that port belongs to a node it already adopted.
A new ESP32 port is only listed: you type `probe N` to say hello to it, or `notnode N` to mark it
"not a node" for good (your JanOS board, for example). --exclude PORT keeps a port out for this run.
--auto says hello to every new ESP32 port without asking (never to excluded or marked ones).

Commands:
  ports            every ESP32 serial port, what the tool decided about it, and why
  probe N          say hello to port N (a new board)
  notnode N        never speak to the device on port N again     isnode N   undo that
  adopt N          adopt the new node on port N                  block N    put it on the Block list
  ident NODE       blink node NODE's LED for 10 s
  start | stop     start / stop scanning on every node
  status           fleet totals and one line per node
  key              the owner id (never the key)
  quit             stop scanning on every node, then leave
  detach           leave WITHOUT stopping the nodes (they keep scanning and queueing on their own)
Needs pyserial:  sudo apt install python3-serial   (or pip install pyserial)
"""
from __future__ import annotations

import argparse
import csv
import os
import pathlib
import sys
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "core"))

from bristle import crypto, proto  # noqa: E402
from bristle.fleet import Fleet  # noqa: E402
from bristle.link_usb import PROBE, PortInfo, PortPolicy, UsbLink, UsbScanner  # noqa: E402
from bristle.registry import Registry  # noqa: E402

HOME = pathlib.Path.home() / ".bristle-bench"
KEY_PATH = HOME / "owner.key"
HELP = ("commands: ports | probe N | notnode N | isnode N | adopt N | block N | ident NODE | start | stop | "
        "status | key | quit | detach")


def load_key() -> bytes:
    if KEY_PATH.exists():
        return bytes.fromhex(KEY_PATH.read_text().strip())
    key = crypto.new_key()
    KEY_PATH.write_text(key.hex())
    os.chmod(KEY_PATH, 0o600)
    return key


def main() -> None:
    ap = argparse.ArgumentParser(description="Bristle bench tool (real nodes over USB)")
    ap.add_argument("--exclude", action="append", default=[], metavar="PORT",
                    help="never touch this port in this run (repeat for more)")
    ap.add_argument("--auto", action="store_true", help="say hello to new ESP32 ports without asking")
    args = ap.parse_args()

    HOME.mkdir(mode=0o700, exist_ok=True)
    key = load_key()
    reg = Registry(HOME / "nodes.json")
    out_path = HOME / f"finds-{time.strftime('%Y%m%d-%H%M%S')}.csv"
    out_f = open(out_path, "w", newline="")
    writer = csv.writer(out_f)
    writer.writerow(["node", "seq", "kind", "time_utc", "mac_or_bssid", "ssid_or_name", "channel", "rssi",
                     "security_or_company"])
    lock = threading.Lock()
    counts = {"AP": 0, "BT": 0}

    def on_find(f):
        with lock:
            counts[f.kind] += 1
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(f.t))
            if f.kind == "AP":
                sec = proto.SECURITY_NAMES.get(int(f.fields.get("k", 15)), "?")
                row = [f.node, f.seq, "AP", ts, f.fields["b"], f.fields.get("e", ""), f.fields.get("c"),
                       f.fields.get("r"), sec]
            else:
                row = [f.node, f.seq, "BT", ts, f.fields["m"], f.fields.get("nm", ""), "", f.fields.get("r"),
                       f.fields.get("mf", "")]
            writer.writerow(row)
            out_f.flush()
            print(f"  [{f.node}] {f.kind} {row[4]} {row[5]!r} ch{row[6]} {row[7]} dBm")

    links: dict[str, UsbLink] = {}          # device path -> open link
    ports: list[PortInfo] = []              # every ESP32 port seen this run, in the order it showed up
    policy = PortPolicy(reg, exclude=args.exclude, auto=args.auto)

    def number_of(port: PortInfo) -> int:
        for i, p in enumerate(ports):
            if p.key == port.key:
                ports[i] = port                 # same device, maybe on a new path
                return i + 1
        ports.append(port)
        return len(ports)

    def link_of(session):
        return next((l for l in links.values() if l.session is session), None)

    def on_port(port, action, reason):
        n = number_of(port)
        if action == PROBE:
            return                              # open_link prints its own line
        hint = f"   -> `probe {n}` to say hello, `notnode {n}` if it is not a node" if action == "ask" else ""
        print(f"\n  port {n}: {port.device} ({port.chip}, serial {port.serial_number or '-'}): {reason}{hint}")

    def on_event(kind, s, info):
        link = link_of(s)
        where = f"port {number_of(link.port)} ({link.device})" if link else "?"
        if kind == "console":
            print(f"  node console: {info['text']}")
        elif kind in ("new_node", "forgotten"):
            print(f"\n*** {kind}: hw {s.hwid} ({s.board}, build {s.build}) on {where}. "
                  f"Type `adopt {number_of(link.port)}` to adopt it.")
        elif kind == "authed":
            print(f"\n*** node {info['number']} online (hw {s.hwid}, {s.board}, build {s.build}) on {where}")
        elif kind == "no_answer":
            print(f"\n*** {where}: nothing answered {info['tries']} hellos. Not a Bristle node (or not flashed "
                  f"yet). Left alone until it is unplugged and plugged in again.")
            if link:
                policy.went_quiet(link.port.key)
                link.close()
        elif kind in ("not_ours", "blocked_seen", "silent", "reauth"):
            print(f"\n*** {kind}: {s.hwid or where} {info}")
        elif kind == "rsp" and not info.get("ok"):
            print(f"  node refused {info.get('c')}: {info.get('e')}")

    fleet = Fleet(reg, key, region="U", on_find=on_find, on_event=on_event)

    def open_link(port: PortInfo):
        link = UsbLink(port, lambda send: fleet.new_session(send, proto.LINK_USB), on_closed=closed)
        links[port.device] = link
        print(f"\n  port {number_of(port)}: {port.device} ({port.chip}): saying hello ...")
        try:
            link.open()
        except Exception as exc:
            links.pop(port.device, None)
            print(f"  could not open {port.device}: {exc}")
            raise

    def closed(link):
        links.pop(link.device, None)
        scanner.released(link.device)
        if link.session is not None:
            fleet.drop_session(link.session)
        print(f"\n  {link.device} closed")

    scanner = UsbScanner(open_link, policy, on_port=on_port)
    scanner.start()

    def ticker():
        while True:
            fleet.tick()
            time.sleep(0.1)
    threading.Thread(target=ticker, daemon=True).start()

    def port_arg(text: str) -> PortInfo:
        return ports[int(text) - 1]

    print(f"Bristle bench. Owner id {crypto.owner_id(key)}. Finds -> {out_path}")
    print("New ESP32 ports are listed, not touched: `probe N` says hello. " + HELP)
    while True:
        try:
            line = input("> ").strip().split()
        except (EOFError, KeyboardInterrupt):
            break
        if not line:
            continue
        cmd, rest = line[0], line[1:]
        try:
            if cmd in ("ports", "list"):
                if not ports:
                    print("  no ESP32 serial ports seen yet")
                for i, p in enumerate(ports, 1):
                    link = links.get(p.device)
                    if link is not None and link.session is not None:
                        s = link.session
                        what = f"open: {s.state} hw={s.hwid or '-'} node={s.number or '-'}"
                    else:
                        what = scanner.seen.get(p.key, (None, "", "unplugged"))[2]
                    print(f"  {i}. {p.device}  {p.chip}  serial {p.serial_number or '-'}  {what}")
            elif cmd == "probe":
                p = port_arg(rest[0])
                if reg.is_not_node(p.key):
                    print("  that device is marked not a node: `isnode N` first")
                else:
                    policy.approve(p.key)
                    scanner.rescan()
            elif cmd == "notnode":
                p = port_arg(rest[0])
                link = links.get(p.device)
                reg.mark_not_node(p.key, f"marked from the bench, was {p.device}")
                if link:
                    link.close()
                scanner.rescan()
                print(f"  {p.device} ({p.key}) is marked not a node: it will never be spoken to")
            elif cmd == "isnode":
                p = port_arg(rest[0])
                reg.unmark_not_node(p.key)
                scanner.rescan()
                print("  unmarked")
            elif cmd == "adopt":
                n = links[port_arg(rest[0]).device].session.adopt()
                print(f"  adopting as node {n} ...")
            elif cmd == "block":
                links[port_arg(rest[0]).device].session.block("blocked from bench")
                print("  blocked")
            elif cmd == "ident":
                s = next(s for s in fleet.authed() if s.number == int(rest[0]))
                s.identify(10)
            elif cmd == "start":
                print(f"  session {fleet.start()} started")
            elif cmd == "stop":
                fleet.stop()
                print("  stopped")
            elif cmd == "status":
                print(f"  {fleet.totals()}  finds: {counts}")
                for s in fleet.authed():
                    h = s.health
                    plan = s.desired.plan.channels_text() if s.desired.plan else "-"
                    d = fleet.delivery.node(s.hwid)
                    print(f"  node {s.number}: run={h.run_mask} backlog={h.backlog} dropped={h.dropped} "
                          f"fill={h.fill_pct}% temp={h.temp_c}C up={h.uptime_s}s got={d.received} "
                          f"repeats={d.duplicates} plan={plan[:50]}")
            elif cmd == "key":
                print(f"  owner id {crypto.owner_id(key)} (key file {KEY_PATH}, never share it)")
            elif cmd == "quit":
                break
            elif cmd == "detach":
                print("  leaving; nodes keep doing what they were doing")
                scanner.stop()
                out_f.close()
                return
            else:
                print("  " + HELP)
        except (IndexError, ValueError, StopIteration, KeyError) as exc:
            print(f"  ? {exc or 'bad argument'} (is that port open? try `ports`)")
        except (PermissionError, RuntimeError) as exc:
            print(f"  refused: {exc}")
    fleet.stop()
    time.sleep(0.5)
    scanner.stop()
    out_f.close()


if __name__ == "__main__":
    main()
