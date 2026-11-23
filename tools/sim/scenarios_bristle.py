# Copyright (c) 2026 Hardscrubbin. SPDX-License-Identifier: MIT
"""[bristle] Bristle Nodes scenarios. STAND-INS, NOT HARDWARE.

The game's real Bristle service is driven against scripted boards (tools/sim/fake_bristle.py): a
node and a bridge written from the protocol document and the firmware source, a cable and the air.
A scenario that passes says the game's side holds a whole conversation with something that follows
the protocol, also when lines are damaged, frames are forged or replayed, cables are pulled and
boards restart. It says nothing about a real board, a real radio or real timing: that is the bench
list (bristle/docs/BENCH_TESTS.md).
"""
from plugins._bristle import codec, crypto, ota, proto
from plugins._bristle import session as S
from tools.sim import fake_bristle as fb
from tools.sim.core import check, check_equal, scenario

FEATURE = "bristle"
OLD = fb.CURRENT_BUILD                                   # the build the stand-in boards start on
NEW = OLD + 1                                            # a made-up next build, for the update scenarios


def state(core, node) -> str:
    session = core.session(node)
    return session.state if session is not None else "(no session)"


def wait(world, seconds, what, condition) -> None:
    check(world.run(seconds, until=condition), "%s did not happen within %s s" % (what, seconds))


def adopt_on_usb(world, core, node):
    """Plug it in, wait until it is listed as new, adopt it by hand, wait until it is online."""
    cable = core.plug(node)
    wait(world, 6, "the new board being listed", lambda: state(core, node) == S.NEW)
    core.session(node).adopt()
    wait(world, 6, "the adopted node coming online", lambda: state(core, node) == S.AUTHED)
    return cable


def hear_networks(node, count, first=0) -> int:
    """The stand-in radio "hears" made-up networks and Bluetooth devices. Returns how many it queued."""
    queued = 0
    for index in range(first, first + count):
        if index % 4 == 3:
            queued += node.hear("BT", m="02aa%08x" % index, r=-70 - index % 20, at=1, nm="demo tag %d" % index)
        else:
            queued += node.hear("AP", b="02bb%08x" % index, c=(1, 6, 11, 36)[index % 4], r=-50 - index % 30, k=3,
                                e="DEMO NET, %d*" % index)
    return queued


def check_once(core, expected, what) -> None:
    """Every find arrived, and none twice."""
    seen = [(find.hwid, find.seq) for find in core.finds]
    check_equal(len(seen), len(set(seen)), "%s: finds handed to the game twice" % what)
    check_equal(len(seen), expected, "%s: finds handed to the game" % what)


# --------------------------------------------------------------------------- adoption and the owner lock
@scenario(FEATURE, "USB: a new board says what it is, waits to be adopted by hand, then scans and delivers once")
def usb_adopt_and_scan(ctx):
    world = fb.World(ctx)
    core, node = world.core(), world.node(1)
    core.plug(node)
    wait(world, 6, "the new board being listed", lambda: state(core, node) == S.NEW)
    check(node.sent["BCN"] >= 1, "the board never announced itself before the core spoke to it")
    check(core.said("New node on /dev/sim-"), "the game did not say a new node is there: %s" % core.messages())
    beacons = node.sent["BCN"]
    world.run(12)                                         # nobody presses Adopt: nothing may happen
    check_equal(node.sent["BCN"], beacons, "beacons in the 12 s after the core said HI (they must stop)")
    world.run(30)
    check_equal(state(core, node), S.NEW, "state of a board nobody adopted")
    check(not node.flash.owned, "a board was given an owner without anyone adopting it")
    check_equal(sorted(set(node.heard) - {"HI", "TIME"}), [], "commands sent to a board that is not adopted")
    # (the core says nothing to a board that is not adopted, so after 20 s of silence the board
    # takes the link as gone and announces itself again: that is what the firmware does)

    core.session(node).adopt()
    wait(world, 6, "the adopted node coming online", lambda: state(core, node) == S.AUTHED)
    check(node.flash.owned and node.flash.key == core.service.fleet.owner_key, "the node does not hold the owner key")
    check_equal(node.flash.number, 1, "node number")
    check(core.said("Node 1 online over USB"), "the game did not say the node is online: %s" % core.messages())

    core.service.set_running(True)
    session = core.session(node)
    wait(world, 8, "the node taking its plan and starting", lambda: node.flash.run_mask and node.flash.plan
         and session.health.plan_hash == session.desired.plan.hash())
    # the node worked the plan's hash out by itself, from the protocol document's rule
    check_equal(fb.plan_hash(*node.flash.plan), session.desired.plan.hash(), "plan hash on the node and on the core")
    check(node.clock_since_boot, "the node was never given the time")
    queued = hear_networks(node, 60)
    check_equal(queued, 60, "finds the scanning node queued")
    wait(world, 15, "every find being acknowledged", lambda: node.backlog() == 0)
    check_once(core, 60, "a plain drive")
    names = {find.fields.get("e") for find in core.finds if find.kind == "AP"}
    check("DEMO NET, 0*" in names, "a network name with a space, a comma and a star did not survive: %r" % sorted(names)[:3])
    late = [find for find in core.finds if abs(find.t - world.time.wall()) > 30]
    check(not late, "finds carry a time that is not the time they were heard: %r" % late[:1])
    ctx.note("lines core->node", core.cables[node.hwid][0].lines["to_node"])
    ctx.note("lines node->core", core.cables[node.hwid][0].lines["to_core"])


@scenario(FEATURE, "Owner lock: a board with another owner is left alone, and a core without the key cannot run ours")
def owner_lock(ctx):
    world = fb.World(ctx)
    mine, other = world.core("mine"), world.core("stranger")
    check(mine.service.fleet.owner_key != other.service.fleet.owner_key, "two fresh installs made the same key")
    node = world.node(1)
    adopt_on_usb(world, mine, node)
    mine.service.set_running(True)
    wait(world, 8, "the node scanning", lambda: node.flash.run_mask)
    plan_before, number_before = node.flash.plan, node.flash.number
    hear_networks(node, 10)
    world.run(4)
    mine.unplug(node)                                     # the board is carried to somebody else's rig

    other.plug(node)
    wait(world, 6, "the stranger's core seeing the board", lambda: state(other, node) == S.NOT_OURS)
    check(other.said("has another owner: ignored"), "the stranger's game did not say so: %s" % other.messages())
    world.run(30)
    check_equal(state(other, node), S.NOT_OURS, "state on the stranger's core after 30 s")
    check_equal(sorted(set(node.heard_from_usb_since_plug) - {"HI"}), [], "what the stranger's core sent it")

    # a core that tries anyway: every command is refused, adoption included
    session = other.session(node)
    hear_networks(node, 5, first=100)
    for attempt in (lambda: session._cmd("AUTH", {"cp": "00" * 16}),
                    lambda: session._cmd("ADOPT", {"k": other.service.fleet.owner_key.hex(), "n": 7}),
                    lambda: session._cmd("RUN", {"m": 0, "s": 99}),
                    lambda: session._cmd("PLAN", {"f": 1, "r": "U", "c": "1"}),
                    lambda: session._cmd("TIME", {"u": 1_600_000_100}),
                    lambda: session._cmd("ACK", {"q": 10 ** 9}),
                    lambda: session._cmd("FORGET")):
        attempt()
        world.run(0.5)
    check(node.flash.owned and node.flash.key == mine.service.fleet.owner_key, "the node changed owner")
    check_equal((node.flash.plan, node.flash.number), (plan_before, number_before), "the node's plan and number")
    check(node.flash.run_mask, "a core without the key stopped the node's scanning")
    check(node.backlog() >= 5, "a core without the key made the node forget finds (backlog %d)" % node.backlog())
    check(node.refused[proto.ERRORS["NOT_AUTHED"]] >= 4, "the node did not answer 'not authenticated': %r" % dict(node.refused))
    check_equal(node.refused[proto.ERRORS["OWNED"]], 1, "'already owned' answers to the second ADOPT")
    check(abs(node.unix_ms() / 1000.0 - world.time.wall()) < 5, "a core without the key moved the node's clock")
    check_equal(len(other.finds), 0, "finds the stranger's core got")
    other.unplug(node)

    # back home: the owner gets everything, including what was heard on the stranger's rig
    mine.plug(node)
    wait(world, 8, "the node coming back online at home", lambda: state(mine, node) == S.AUTHED)
    wait(world, 20, "the backlog arriving", lambda: node.backlog() == 0)
    check_once(mine, 15, "after the visit to a stranger")


@scenario(FEATURE, "Owner lock: a blocked board is never spoken to again, and a forgotten node must be adopted again")
def block_and_forget(ctx):
    world = fb.World(ctx)
    core, node, pest = world.core(), world.node(1), world.node(2)
    adopt_on_usb(world, core, node)
    core.plug(pest)
    wait(world, 6, "the second board being listed", lambda: state(core, pest) == S.NEW)
    core.session(pest).block("blocked in the scenario")
    heard = dict(pest.heard)
    world.run(30)
    check_equal(dict(pest.heard), heard, "lines sent to a blocked board")
    core.unplug(pest)
    core.plug(pest)                                       # plugged in again: still blocked
    world.run(10)
    check_equal(state(core, pest), S.BLOCKED, "a blocked board after replugging")
    check(not pest.flash.owned, "a blocked board was adopted")

    # the node is wiped by hand (BOOT held 5 s): it is nobody's, and the core does not take it back by itself
    node.boot_held = True
    wait(world, 30, "the core noticing the node no longer knows it", lambda: state(core, node) == S.NEW)
    world.run(15)
    check(not node.flash.owned, "a freed node was adopted again without anyone asking")
    # ... and the other way round: the core forgets a node it still holds the key for
    core.session(node).adopt()
    wait(world, 8, "the node online again", lambda: state(core, node) == S.AUTHED)
    core.service.registry.forget(node.hwid)
    core.unplug(node)
    core.plug(node)
    wait(world, 6, "the core seeing its own node that is not in Node Memory",
         lambda: state(core, node) == S.FORGOTTEN)
    world.run(15)
    check_equal(state(core, node), S.FORGOTTEN, "a node that is not in Node Memory after 15 s")
    check(core.said("not in Node Memory"), "the game did not say so: %s" % core.messages())


# --------------------------------------------------------------------------- USB lines
@scenario(FEATURE, "USB: damaged lines are dropped by their check sum, in pieces or whole, and nothing is lost or doubled")
def usb_damaged_lines(ctx):
    world = fb.World(ctx)
    core, node = world.core(), world.node(1)
    cable = adopt_on_usb(world, core, node)
    cable.chop = 7                                        # the port hands bytes over a few at a time
    core.service.set_running(True)
    wait(world, 8, "the node scanning", lambda: node.flash.run_mask)
    hear_networks(node, 40)
    cable.garble["to_core"] = 12                          # a bad cable: the next lines each get a flipped bit
    cable.garble["to_node"] = 4
    world.run(3)
    session = core.session(node)
    check(session.dropped_lines >= 8, "the core did not drop the damaged lines (dropped %d)" % session.dropped_lines)
    hear_networks(node, 40, first=40)
    wait(world, 60, "every find arriving after the bad stretch", lambda: node.backlog() == 0)
    check(node.bad_lines >= 4, "the node did not drop the damaged lines it got (dropped %d)" % node.bad_lines)
    check_once(core, 80, "a drive over a bad cable")
    check_equal(state(core, node), S.AUTHED, "the link after the bad stretch")


@scenario(FEATURE, "USB: a board slow to answer (two hellos waiting in its port) is not taken for another owner's")
def usb_slow_to_answer(ctx):
    world = fb.World(ctx)
    core, node = world.core(), world.node(1)
    adopt_on_usb(world, core, node)
    core.unplug(node)
    world.run(2)
    cable = core.plug(node)
    cable.hold_to_node = True                             # the board is busy: the port takes the bytes, it reads them later
    world.run(proto.USB_HELLO_WAIT_MS / 1000 + 0.6)
    check_equal(cable.lines["to_node"], 2, "hellos the core has sent while the board was not answering")
    cable.hold_to_node = False
    wait(world, 5, "the node coming online", lambda: state(core, node) == S.AUTHED and node.usb.authed)
    check(not core.said("has another owner"), "the game took its own node for a stranger's: %s" % core.messages())
    world.run(10)
    check_equal(state(core, node), S.AUTHED, "the link ten seconds later")


@scenario(FEATURE, "Queue: a lost ACK is said again; a quiet node does not go on resending finds the game has")
def queue_lost_ack(ctx):
    world = fb.World(ctx)
    core, node = world.core(), world.node(1)
    cable = adopt_on_usb(world, core, node)
    core.service.set_running(True)
    wait(world, 8, "the node scanning", lambda: node.flash.run_mask)
    hear_networks(node, 20)
    cable.lose["to_node"] = ["ACK:", 1]                   # the one line that says "I have them" never arrives
    world.run(3)
    check_once(core, 20, "before the ACK was missed")
    check_equal(cable.lose["to_node"][1], 0, "ACK lines the scenario removed")
    wait(world, 45, "the node being told again that its finds arrived", lambda: node.backlog() == 0)
    sent = node.sent["AP"] + node.sent["BT"]
    world.run(60)
    check_equal(node.sent["AP"] + node.sent["BT"], sent, "find lines sent in a minute with nothing new to send")
    check_once(core, 20, "after a lost ACK")

# --------------------------------------------------------------------------- ESP-NOW through the bridge
def esp_session(core, node):
    return next((s for s in core.service.fleet.sessions
                 if s.hwid == node.hwid and s.link_type == proto.LINK_ESPNOW), None)


def on_esp(core, node) -> bool:
    session = esp_session(core, node)
    return session is not None and session.state == S.AUTHED


def make_bridge(world, core, number=90):
    bridge = world.node(number)
    adopt_on_usb(world, core, bridge)
    core.service.set_bridge(core.session(bridge), True)   # the Nodes screen's [G]
    wait(world, 30, "the board coming back as the bridge",
         lambda: bridge.is_bridge and core.service.hub.bridge is not None and state(core, bridge) == S.AUTHED)
    check(core.said("Bridge online on"), "the game did not say the bridge is online: %s" % core.messages())
    return bridge


def node_over_the_air(world, core, number=1, **board):
    """Adopted on USB (the only way), then unplugged and left to find the bridge by itself."""
    node = world.node(number, **board)
    adopt_on_usb(world, core, node)
    core.unplug(node)
    wait(world, 60, "node %d linking through the bridge" % number, lambda: on_esp(core, node))
    return node


@scenario(FEATURE, "ESP-NOW: a node adopted on USB links through the bridge with signed frames and delivers once")
def espnow_link(ctx):
    world = fb.World(ctx)
    core = world.core()
    bridge = make_bridge(world, core)
    node = node_over_the_air(world, core)
    check(core.said("online over ESP-NOW"), "the game did not say the node is online over ESP-NOW: %s" % core.messages())
    check_equal(bridge.flash.run_mask, 0, "the bridge's run mask (a bridge never scans)")
    core.service.set_running(True)
    wait(world, 15, "the node scanning", lambda: node.flash.run_mask and node.flash.plan)
    check_equal(fb.plan_hash(*node.flash.plan), esp_session(core, node).desired.plan.hash(), "plan hash over ESP-NOW")
    hear_networks(node, 120)
    wait(world, 60, "every find being acknowledged over ESP-NOW", lambda: node.backlog() == 0)
    check_once(core, 120, "a drive over ESP-NOW")
    stats = core.service.hub.stats
    check_equal((stats["bad"], stats["strangers"]), (0, 0), "bad frames and strangers on a clean link")
    check(bridge.bridge_rx > 20 and bridge.bridge_tx > 5, "the bridge relayed nothing (%d in, %d out)"
          % (bridge.bridge_rx, bridge.bridge_tx))
    # a board with no owner cannot sign: it sends nothing over the air and is told so on screen only on USB
    blank = world.node(7)
    world.run(10)
    check_equal(len([f for f in world.air.heard if f[1] == blank.mac]), 0, "frames sent by a board with no owner")
    ctx.note("frames relayed up", bridge.bridge_rx)
    ctx.note("node visits", node.visits)


@scenario(FEATURE, "ESP-NOW: a forged frame, a damaged frame and a replayed frame are refused, in both directions")
def espnow_forged_and_replayed(ctx):
    world = fb.World(ctx)
    core = world.core()
    make_bridge(world, core)
    node = node_over_the_air(world, core)
    core.service.set_running(True)
    wait(world, 15, "the node scanning", lambda: node.flash.run_mask)
    hear_networks(node, 30)
    wait(world, 40, "the first finds arriving", lambda: node.backlog() == 0)
    stats = core.service.hub.stats
    key = core.service.fleet.owner_key

    # 1. a damaged frame (one bit flipped on the air)
    hear_networks(node, 6, first=30)
    world.air.tamper = 2
    world.run(4)
    check(stats["bad"] >= 1, "a frame with a wrong tag was not counted as bad: %r" % dict(stats))
    wait(world, 40, "the finds from the damaged frames arriving again", lambda: node.backlog() == 0)
    check_once(core, 36, "after damaged frames")

    # 2. a stranger: a frame signed with another key, from an address the core does not know
    stranger = crypto.frame_build(bytes(range(16, 32)), proto.FRAME_KIND_LINES, 5, 0x01,
                                  b"AP:q=1,t=1795400000,s=1,b=02cc00000001,c=6,r=-40,k=0,e=FORGED")
    sessions = len(core.service.fleet.sessions)
    world.air.inject(bytes.fromhex("02eeeeeeeeee"), stranger)
    world.run(2)
    check(stats["strangers"] >= 1, "a frame signed with another key was not counted as a stranger's")
    check_equal(len(core.service.fleet.sessions), sessions, "sessions after a stranger's frame")
    # ... and the same forgery claiming to come from our node's address
    bad = stats["bad"]
    world.air.inject(node.mac, stranger)
    world.run(2)
    check(stats["bad"] > bad, "a forged frame from a known address was not refused")

    # 3. a replay: somebody recorded a real frame with finds in it and sends it again later
    recorded = [frame for way, mac, frame in world.air.heard if way == "up" and mac == node.mac
                and crypto.frame_parse(key, frame).kind == proto.FRAME_KIND_LINES
                and b"AP:" in crypto.frame_parse(key, frame).payload]
    check(recorded, "no frame with finds was on the air to record")
    stale = stats["stale"]
    for _ in range(5):
        world.air.inject(node.mac, recorded[0])
        world.run(0.3)
    check(stats["stale"] >= stale + 5, "replayed frames were not refused as stale: %r" % dict(stats))
    check_once(core, 36, "after a replay towards the core")
    check_equal(state_esp(core, node), S.AUTHED, "the node's link after the replay")

    # 4. the other way: the frame in which the core said "scan" is recorded and played back to the node
    core.service.set_running(False)
    wait(world, 15, "the node stopping", lambda: not node.flash.run_mask)
    recorded = recorded_start_frame(world, core, node)
    stale_node, runs, bridge_before = node.frames_stale, node.heard["RUN"], node.bridge_mac
    world.air.inject_to_node(node, recorded)              # sent again by somebody else, from another address
    node.visit()
    check_equal(node.frames_stale, stale_node + 1, "stale frames counted by the node")
    check_equal(node.heard["RUN"], runs, "RUN commands the node took from a replayed frame")
    check(not node.flash.run_mask, "a replayed frame started the node again")
    check_equal(node.bridge_mac.hex(), bridge_before.hex(), "the address the node sends to after a replayed frame")
    # the node still reaches its own core, with no pause
    core.service.set_running(True)
    wait(world, 6, "the node scanning again right after the replay", lambda: node.flash.run_mask)
    hear_networks(node, 10, first=40)
    wait(world, 8, "finds arriving right after the replay", lambda: node.backlog() == 0)
    check_once(core, 46, "after a replay towards the node")
    check_equal(state_esp(core, node), S.AUTHED, "the node's link after the replay")
    check(not core.said("Node %d went quiet" % node.flash.number), "the replay cost the node its link: %s" % core.messages())


def recorded_start_frame(world, core, node) -> bytes:
    """What an eavesdropper would have: a frame from the core to this node with a RUN command in it."""
    key = core.service.fleet.owner_key
    for way, mac, frame in world.air.heard:
        if way == "down" and mac == node.mac:
            opened = crypto.frame_parse(key, frame)
            if opened.kind == proto.FRAME_KIND_LINES and b"RUN:" in opened.payload and b"m=0" not in opened.payload:
                return frame
    raise AssertionError("no frame with a start command was on the air to record")


@scenario(FEATURE, "ESP-NOW: a node still on build 4 is cut off by a replayed frame, comes back by itself, loses nothing")
def espnow_replay_build4(ctx):
    """Kept on record: what the fault fixed in build 5 costs a node that has not been updated yet.
    Up to build 4 the node took the sender of an old frame for the bridge and sent everything
    there until it gave the bridge up for lost (20 s)."""
    world = fb.World(ctx)
    core = world.core()
    bridge = make_bridge(world, core)
    node = node_over_the_air(world, core, build=4)
    core.service.set_running(True)
    wait(world, 15, "the node scanning", lambda: node.flash.run_mask)
    hear_networks(node, 10)
    wait(world, 30, "the first finds arriving", lambda: node.backlog() == 0)
    core.service.set_running(False)
    wait(world, 15, "the node stopping", lambda: not node.flash.run_mask)
    recorded = recorded_start_frame(world, core, node)
    world.air.inject_to_node(node, recorded)
    node.visit()
    check(not node.flash.run_mask, "a replayed frame started the node")
    check(node.bridge_mac != bridge.mac, "the build-4 stand-in did not show the fault this scenario is about")
    core.service.set_running(True)
    quick = world.run(10, until=lambda: node.flash.run_mask)
    check(not quick, "a build-4 node was reached within 10 s although its frames went to the replayer's address")
    wait(world, 60, "the node finding its bridge again by itself", lambda: node.flash.run_mask and on_esp(core, node))
    hear_networks(node, 10, first=10)
    wait(world, 30, "finds arriving after it came back", lambda: node.backlog() == 0)
    check_once(core, 20, "a build-4 node across a replay")

def state_esp(core, node) -> str:
    session = esp_session(core, node)
    return session.state if session is not None else "(no session)"


def hear_long(node, index) -> None:
    """A find whose line is a legal line (240 bytes at most) and still too long for one frame."""
    fields = dict(m="02ad%08x" % index, r=-60, at=1, nm="long demo %d" % index)
    stamp = node.unix_ms()
    short = codec.strip_crc(codec.build("BT", dict(q=node.next_seq, t=stamp // 1000, ms=stamp % 1000,
                                                   s=node.flash.session, x="p", **fields)))
    fields["x"] = "p" * (fb.PAYLOAD_MAX + 2 - len(short) + 1)
    check(node.hear("BT", **fields), "the node did not queue the long find")


@scenario(FEATURE, "ESP-NOW: a line too long for one frame travels in pieces; a lost piece costs only that line")
def espnow_fragments(ctx):
    world = fb.World(ctx)
    core = world.core()
    make_bridge(world, core)
    node = node_over_the_air(world, core)
    core.service.set_running(True)
    wait(world, 15, "the node scanning", lambda: node.flash.run_mask)
    # node -> core: finds whose lines are longer than a frame's 232 bytes and still legal lines (a
    # made-up extra field the core ignores pads them)
    for index in range(6):
        hear_long(node, index)
    session = esp_session(core, node)
    peer = core.service.hub.peers[node.hwid]
    wait(world, 30, "the long finds arriving", lambda: node.backlog() == 0)
    check_once(core, 6, "long lines in pieces")
    pieces = [frame for way, mac, frame in world.air.heard if way == "up" and mac == node.mac
              and (crypto.frame_parse(core.service.fleet.owner_key, frame).frag & 0x0F) > 1]
    check(len(pieces) >= 12, "the long lines did not travel in pieces (%d piece frames)" % len(pieces))

    # a piece is lost on the air: that one line is dropped, the rest is taken, the node sends it again
    for index in range(6, 10):
        hear_long(node, index)
    world.air.lose_to_bridge = 1
    world.run(4)
    wait(world, 40, "the line with the lost piece arriving after all", lambda: node.backlog() == 0)
    check(peer.endpoint.reassembler.dropped >= 1, "the core did not count the line with the lost piece")
    check_once(core, 10, "after a lost piece")

    # core -> node: a long plan (every channel with its own dwell) goes down in two frames
    entries = [(channel, 31 + index) for index, channel in enumerate(
        proto.REGIONS["U"]["ch24"] + proto.REGIONS["U"]["ch5"])]

    def body(items):
        return codec.strip_crc(codec.build("PLAN", {"f": 1, "r": "U", "c": "/".join("%d.%d" % i for i in items)},
                                           proto.LINE_RELAY_MAX_BYTES))
    while len(body(entries)) > fb.PAYLOAD_MAX + 2:
        entries.pop()
    for index in range(len(entries)):                     # a digit more here and there, to land just over a frame
        if len(body(entries)) > fb.PAYLOAD_MAX:
            break
        entries[index] = (entries[index][0], 100 + index)
    text = "/".join("%d.%d" % entry for entry in entries)
    check(fb.PAYLOAD_MAX < len(body(entries)) <= fb.PAYLOAD_MAX + 2, "the scenario's plan does not need two frames")
    bad = node.bad_lines
    session._cmd("PLAN", {"f": 1, "r": "U", "c": text})
    wait(world, 10, "the node taking the long plan", lambda: ("1", "U", entries) in node.plans_taken)
    check_equal(node.bad_lines, bad, "lines the node dropped while taking the long plan")
    wait(world, 20, "the core putting its own plan back", lambda: session.health.plan_hash == session.desired.plan.hash())


# --------------------------------------------------------------------------- the queue
@scenario(FEATURE, "Queue: the cable is pulled mid-drive; the node keeps scanning and everything arrives once after")
def queue_across_a_dropout(ctx):
    world = fb.World(ctx)
    core, node = world.core(), world.node(1)
    adopt_on_usb(world, core, node)
    core.service.set_running(True)
    wait(world, 8, "the node scanning", lambda: node.flash.run_mask)
    hear_networks(node, 50)
    world.run(0.3)                                        # some are on their way, none acknowledged yet
    core.unplug(node)
    check(node.backlog() > 0, "the scenario pulled the cable too late: nothing was left on the node")
    for burst in range(4):                                # two minutes of driving with no core
        hear_networks(node, 25, first=50 + burst * 25)
        world.run(30)
    check_equal(node.backlog(), 150 - len(core.finds) + sum(1 for f in core.finds if f.seq >= node.queue_ack()),
                "finds the node kept while the cable was out")
    check(any("core went quiet" in line for line in node.log), "the node never noticed the core was gone")
    check(node.flash.run_mask, "the node stopped scanning because the cable was pulled")
    core.plug(node)
    wait(world, 10, "the node coming back online", lambda: state(core, node) == S.AUTHED)
    wait(world, 60, "the backlog arriving", lambda: node.backlog() == 0)
    check_once(core, 150, "across a pulled cable")
    delivery = core.service.fleet.delivery.node(node.hwid)
    ctx.note("repeats the core dropped", delivery.duplicates)


@scenario(FEATURE, "Queue: the node is out of the bridge's range for minutes; its finds wait and arrive once")
def queue_out_of_range(ctx):
    world = fb.World(ctx)
    core = world.core()
    make_bridge(world, core)
    node = node_over_the_air(world, core)
    core.service.set_running(True)
    wait(world, 15, "the node scanning", lambda: node.flash.run_mask)
    hear_networks(node, 20)
    world.run(2)
    node.in_range = False
    for burst in range(5):
        hear_networks(node, 20, first=20 + burst * 20)
        world.run(40)
    check(node.backlog() >= 100, "the node did not keep its finds while out of range (%d held)" % node.backlog())
    check(state_esp(core, node) != S.AUTHED, "the core still shows the node online after 200 s of silence")
    node.in_range = True
    wait(world, 60, "the node linking again", lambda: on_esp(core, node))
    wait(world, 90, "the backlog arriving over ESP-NOW", lambda: node.backlog() == 0)
    check_once(core, 120, "across minutes out of range")
    check(not core.said("has another owner"), "the game took its own node for a stranger's: %s" % core.messages())


@scenario(FEATURE, "Queue: the node restarts mid-drive; its numbers skip ahead and the gap never holds the ack up")
def queue_across_a_restart(ctx):
    world = fb.World(ctx)
    core, node = world.core(), world.node(1)
    adopt_on_usb(world, core, node)
    core.service.set_running(True)
    wait(world, 8, "the node scanning", lambda: node.flash.run_mask)
    hear_networks(node, 30)
    world.run(0.4)
    before = node.next_seq
    node.restart()                                        # a reset: the queue is saved, the clock survives
    check(node.next_seq >= before + 1 and node.next_seq % proto.QUEUE_SEQ_BLOCK == 0,
          "after a restart the node's numbers did not skip to a fresh block (%d -> %d)" % (before, node.next_seq))
    check(node.flash.run_mask, "a node that restarted mid-drive did not keep scanning")
    hear_networks(node, 30, first=30)                     # heard before the core has noticed anything
    wait(world, 30, "the core shaking hands again", lambda: state(core, node) == S.AUTHED and node.usb.authed)
    wait(world, 60, "everything arriving", lambda: node.backlog() == 0)
    check_once(core, 60, "across a restart")
    delivery = core.service.fleet.delivery.node(node.hwid)
    check_equal(delivery.ack_value(), node.next_seq, "the core's ack point (it must be past the gap)")
    check(not core.said("refused"), "the game reported a refusal the player can do nothing about: %s" % core.messages())


@scenario(FEATURE, "Queue: finds from a power-up that never got a clock are counted as dropped, never sent, never block")
def queue_untimed_finds(ctx):
    world = fb.World(ctx)
    core, node = world.core(), world.node(1)
    adopt_on_usb(world, core, node)
    core.service.set_running(True)
    wait(world, 8, "the node scanning", lambda: node.flash.run_mask)
    hear_networks(node, 10)
    wait(world, 15, "the first finds arriving", lambda: node.backlog() == 0)
    core.unplug(node)
    node.restart(power_cut=True)                          # on a power bank in the car: no core, no clock
    check(not node.clock_valid, "a power cut did not take the node's clock away")
    check_equal(hear_networks(node, 7, first=10), 7, "finds queued with no clock (stamped by uptime)")
    world.run(20)
    node.save_queue()
    node.restart(power_cut=True)                          # and off again before any core gave it the time
    hear_networks(node, 5, first=17)                      # this power-up will get its clock: these are kept
    world.run(5)
    core.plug(node)
    wait(world, 10, "the node online again", lambda: state(core, node) == S.AUTHED)
    wait(world, 30, "the finds of this power-up arriving", lambda: node.backlog() == 0)
    check_once(core, 15, "with seven finds that could never be timed")
    check_equal(node.flash.dropped, 7, "finds the node counted as dropped (no clock)")
    wait(world, 8, "the core hearing the count", lambda: core.session(node).health.dropped == 7)
    check_equal(core.service.fleet.totals()["dropped"], 7, "dropped finds shown by the game")
    times = [find.t for find in core.finds[10:]]
    check(all(abs(t - world.time.wall()) < 60 for t in times),
          "finds stamped by uptime did not get their real time when the clock arrived: %r" % times[:2])


@scenario(FEATURE, "Queue: the game is quit and started again mid-drive; ack points come back from disk, nothing doubled")
def queue_across_a_game_restart(ctx):
    world = fb.World(ctx)
    core, node = world.core(), world.node(1)
    adopt_on_usb(world, core, node)
    core.service.set_running(True)
    wait(world, 8, "the node scanning", lambda: node.flash.run_mask)
    hear_networks(node, 40)
    world.run(1.0)
    got = len(core.finds)
    check(0 < got, "nothing had arrived before the game was quit")
    core.unplug(node)
    core.service._save()                                  # what stop() does when the game quits
    hear_networks(node, 40, first=40)
    world.run(45)
    core.boot()                                           # the game starts again: Node Memory, key and acks from disk
    check(core.service.registry.known(node.hwid) is not None, "Node Memory did not survive the restart")
    core.plug(node)
    wait(world, 10, "the node online after the restart, without adopting", lambda: state(core, node) == S.AUTHED)
    core.service.set_running(True)
    wait(world, 60, "the backlog arriving", lambda: node.backlog() == 0)
    check_once(core, 80, "across a restart of the game")


# --------------------------------------------------------------------------- firmware updates
def online_node(world, core):
    node = world.node(1)
    adopt_on_usb(world, core, node)
    core.service.set_running(True)
    wait(world, 8, "the node scanning", lambda: node.flash.run_mask)
    return node


@scenario(FEATURE, "Update over USB: sent, checked, committed, on trial, confirmed when it meets its owner")
def ota_over_usb(ctx):
    world = fb.World(ctx)
    core = world.core()
    node = online_node(world, core)
    image = core.release_folder(NEW)
    hear_networks(node, 10)
    core.start_update(node)
    job = core.service.ota
    wait(world, 60, "the node restarting into the new image", lambda: node.flash.build == NEW)
    check(node.flash.trial or any("confirmed" in line for line in node.log), "the new image did not start on trial")
    check_equal(job.sender.state, ota.DONE, "the update's state on the core")
    wait(world, 30, "the new image being confirmed", lambda: not node.flash.trial and state(core, node) == S.AUTHED)
    check(any("new firmware confirmed: build %d stays" % NEW in line for line in node.log), "the node never confirmed: %r" % node.log[-3:])
    check(core.said("Node 1 now runs build %d" % NEW), "the game did not say the update took: %s" % core.messages())
    check_equal(len(image), 6000, "size of the stand-in image")
    check(node.flash.run_mask and node.flash.owned and node.flash.number == 1, "the update cost the node its settings")
    wait(world, 30, "the finds from before the update arriving", lambda: node.backlog() == 0)
    hear_networks(node, 10, first=10)
    wait(world, 30, "finds after the update arriving", lambda: node.backlog() == 0)
    check_once(core, 20, "across an update")
    ctx.note("chunks resent", job.sender.resends)


@scenario(FEATURE, "Update interrupted: cable pulled, then power cut, mid-update; the node keeps its firmware; a retry works")
def ota_interrupted(ctx):
    world = fb.World(ctx)
    core = world.core()
    node = online_node(world, core)
    core.release_folder(NEW, size=40000)
    core.start_update(node)
    job = core.service.ota
    wait(world, 30, "a third of the image arriving", lambda: job.sender.acked > 13000)
    core.unplug(node)                                     # 1. the cable is pulled
    world.run(2)
    check_equal(job.sender.state, ota.FAILED, "the update's state after the cable was pulled")
    check("keeps its old firmware" in job.sender.error, "the reason shown: %r" % job.sender.error)
    check(core.said("update failed"), "the game did not say the update failed: %s" % core.messages())
    world.run(30)
    check_equal(node.ota_state, "idle", "the node's update state 30 s after its link closed")
    check_equal((node.flash.build, node.flash.trial, node.flash.slot), (OLD, False, None), "the node's firmware")
    core.plug(node)
    wait(world, 10, "the node online again on its old build", lambda: state(core, node) == S.AUTHED)
    check_equal(core.session(node).build, OLD, "build the node reports")

    core.start_update(node)                               # 2. this time the node loses power half way
    job = core.service.ota
    wait(world, 30, "half of the image arriving", lambda: job.sender.acked > 20000)
    node.restart(power_cut=True)
    wait(world, 60, "the core giving the update up", lambda: not job.sender.active)
    check_equal(job.sender.state, ota.FAILED, "the update's state after the node lost power")
    check_equal((node.flash.build, node.flash.trial), (OLD, False), "the node's firmware after the power cut")
    wait(world, 30, "the node online again", lambda: state(core, node) == S.AUTHED)

    core.start_update(node)                               # 3. left alone, it goes through
    wait(world, 120, "the third try going through", lambda: node.flash.build == NEW and not node.flash.trial)
    check(core.said("Node 1 now runs build %d" % NEW), "the game did not say the update took: %s" % core.messages())


@scenario(FEATURE, "Update rollback: an image that fails its self-test, and one that never meets its owner, go back")
def ota_rollback(ctx):
    world = fb.World(ctx)
    core = world.core()
    node = online_node(world, core)
    core.release_folder(NEW)
    node.selftest_ok = False                              # the new image will not pass its own self-test
    core.start_update(node)
    wait(world, 60, "the node going back to its old build",
         lambda: any("did not pass its self-test" in line for line in node.log))
    wait(world, 60, "the core shaking hands with the node again", lambda: node.usb.authed and state(core, node) == S.AUTHED)
    world.run(10)
    check_equal((node.flash.build, node.flash.trial), (OLD, False), "the node's firmware after the failed self-test")
    check(core.said("is back on build %d: the update did not take (it rolled back)" % OLD),
          "the game did not say the update rolled back: %s" % core.messages())
    check(not core.said("now runs build %d" % NEW), "the game said the update took, and it had not: %s" % core.messages())
    check(node.flash.owned and node.flash.run_mask, "the rollback cost the node its owner or its run state")

    node.selftest_ok = True                               # a good image this time, but its owner never shows up
    core.start_update(node)
    wait(world, 60, "the new image starting on trial", lambda: node.flash.build == NEW and node.flash.trial)
    core.unplug(node)
    world.run(proto.OTA_CONFIRM_MS / 1000 + 20)
    check_equal((node.flash.build, node.flash.trial), (OLD, False), "the node's firmware after 5 minutes without its owner")
    core.plug(node)
    wait(world, 10, "the node online again", lambda: state(core, node) == S.AUTHED)
    check_equal(core.session(node).build, OLD, "build the node reports")
    hear_networks(node, 10)
    wait(world, 30, "finds arriving after the rollback", lambda: node.backlog() == 0)
    check_once(core, 10, "after two rollbacks")


@scenario(FEATURE, "Update over ESP-NOW: frames lost on the air cost resends, never a damaged image")
def ota_over_espnow(ctx):
    world = fb.World(ctx)
    core = world.core()
    make_bridge(world, core)
    node = node_over_the_air(world, core)
    core.release_folder(NEW, size=12000)
    core.start_update(node)
    job = core.service.ota
    wait(world, 20, "the update starting", lambda: job.sender.acked > 0)
    for _ in range(6):                                    # a bad patch of air, six times
        world.air.lose_to_node = 2
        world.air.lose_to_bridge = 1
        world.run(3)
    wait(world, 240, "the node restarting into the new image", lambda: node.flash.build == NEW)
    wait(world, 120, "the new image meeting its owner over ESP-NOW", lambda: not node.flash.trial and on_esp(core, node))
    check(job.sender.resends > 0, "frames were lost but nothing was sent again")
    check(core.said("Node %d now runs build %d" % (node.flash.number, NEW)), "the game did not say the update took: %s" % core.messages())
    ctx.note("chunks resent", job.sender.resends)


# --------------------------------------------------------------------------- a second core
def second_install(world, first, name="second"):
    """A second install with the same owner key and Node Memory, moved the way a player moves
    them: export on one rig, import on the other, start the game again."""
    first.service.export_core("a long passphrase 12345")
    second = world.core(name)
    with open(first.service.export_path(), "rb") as source, open(second.service.export_path(), "wb") as target:
        target.write(source.read())
    second.service.import_core("a long passphrase 12345")
    second.boot()                                         # "RESTART THE GAME to use it"
    check(second.service.fleet.owner_key == first.service.fleet.owner_key, "the import did not carry the owner key")
    check(second.service.core_id != first.service.core_id, "two installs have the same core id")
    return second


def said_times(core, text) -> int:
    return sum(1 for message, _kind in core.service.messages if text in message)


@scenario(FEATURE, "Busy: a second core reaching a linked node over Bluetooth is told 'busy' and leaves it alone", limit=120)
def second_core_busy(ctx):
    world = fb.World(ctx)
    first = world.core("first")
    bridge = make_bridge(world, first, number=90)
    node = node_over_the_air(world, first)
    first.service.set_running(True)
    wait(world, 15, "the node scanning for the first core", lambda: node.flash.run_mask and node.flash.plan)
    plan = node.flash.plan
    second = second_install(world, first)
    session = second.bluetooth(node)                      # the node is in its Node Memory and advertises
    check(session is not None, "a node linked over ESP-NOW was not advertising")
    heard = 0
    for burst in range(6):                                # three minutes with both cores there
        heard += hear_networks(node, 15, first=burst * 15)
        world.run(30)
    busy = proto.ERRORS["BUSY"]
    check(node.refused[busy] >= 5, "the node did not answer 'busy' every time it was asked: %r" % dict(node.refused))
    check(node.refused[busy] <= 8, "the second core asked far more often than every 30 s: %d times" % node.refused[busy])
    check_equal(session.state, S.BUSY, "the second core's state for that node")
    check_equal(said_times(second, "is being run by another core: left alone"), 1, "times the second game said so")
    check_equal(sorted(set(node.heard_on[proto.LINK_BLE]) - {"HI"}), [], "what the second core sent besides its hello")
    check_equal(len(second.finds), 0, "finds the second core took from a node the first is running")
    check(on_esp(first, node), "the first core lost its node while the second was asking")
    wait(world, 60, "the first core getting every find", lambda: node.backlog() == 0)
    check_once(first, heard, "with a second core asking")
    check(node.flash.run_mask and node.flash.plan == plan, "the second core changed the node's plan or stopped it")

    # the first core goes home: at its next ask the second core gets through and takes the node over
    first.unplug(bridge)
    second.service.set_running(True)
    wait(world, 120, "the second core taking over once the first is gone", lambda: session.state == S.AUTHED)
    hear_networks(node, 10, first=1000)
    wait(world, 60, "finds going to the second core", lambda: node.backlog() == 0)
    check(len(second.finds) >= 10, "the second core got %d finds after taking over" % len(second.finds))
    seen = [(find.hwid, find.seq) for find in second.finds]
    check_equal(len(seen), len(set(seen)), "finds handed to the second game twice")


@scenario(FEATURE, "Busy: a node one core runs over Bluetooth answers 'busy' to another core's bridge; USB still gets through",
          limit=120)
def second_core_busy_espnow(ctx):
    world = fb.World(ctx)
    first, node = world.core("first"), world.node(1)
    adopt_on_usb(world, first, node)
    first.unplug(node)
    wait(world, 40, "the node advertising once its USB link has timed out", node.advertising)
    mine = first.bluetooth(node)
    wait(world, 15, "the first core linking the node over Bluetooth", lambda: mine.state == S.AUTHED)
    first.service.set_running(True)
    wait(world, 15, "the node scanning for the first core", lambda: node.flash.run_mask)
    second = second_install(world, first)
    make_bridge(world, second, number=91)                 # the node's polls are heard by this bridge
    heard = 0
    for burst in range(5):
        heard += hear_networks(node, 12, first=burst * 12)
        world.run(30)
    check(node.refused[proto.ERRORS["BUSY"]] >= 3, "the node never told the second core's bridge it is busy: %r"
          % dict(node.refused))
    check_equal(state_esp(second, node), S.BUSY, "the second core's state for that node")
    check_equal(said_times(second, "is being run by another core: left alone"), 1, "times the second game said so")
    check(not second.said("has another owner"), "the second game took its own node for a stranger's: %s" % second.messages())
    check_equal(len(second.finds), 0, "finds the second core took")
    check_equal(mine.state, S.AUTHED, "the first core's link while the second was asking")
    wait(world, 30, "the first core getting every find", lambda: node.backlog() == 0)
    check_once(first, heard, "with a second core's bridge in range")

    # on a cable a hello always gets through: whoever has the node in their hands has it
    second.plug(node)
    wait(world, 10, "the second core linking the node over USB", lambda: state(second, node) == S.AUTHED)
    wait(world, 15, "the node following the core that has it on the cable", lambda: not node.flash.run_mask)
