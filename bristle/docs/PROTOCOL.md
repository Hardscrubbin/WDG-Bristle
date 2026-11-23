# Bristle protocol, version 1

The machine-readable source is `spec/bristle_proto.yaml`. This page explains it. If they disagree,
the YAML wins: fix this page.

## Roles

- **Core:** the uConsole running Watch Dogs Go. It decides everything: who's adopted, node
  numbers, channel plans, when to scan, and where each find goes on the map.
- **Node:** an ESP32-C5 board that scans Wi-Fi and BLE on its plan, queues finds with sequence
  numbers, and hands them over when the core acks.
- **Bridge:** a node in bridge role, plugged into the uConsole by USB, that relays ESP-NOW frames
  between the core and wireless nodes. It holds no fleet logic.

## Transports

| Transport | Framing | Integrity | Notes |
|---|---|---|---|
| USB serial | Text lines | CRC-16 per line | Discovery by sending `HI` and waiting for `HELLO` |
| Bluetooth LE | Text lines (notify / write) | CRC-16 per line | Own 128-bit UUIDs; one adapter reserved for Bristle |
| ESP-NOW via bridge | Binary frame carrying lines | HMAC-SHA256 tag + a counter that survives restarts | Owned nodes only |

## Lines

```
TYPE:key=value,key=value*CRC\n
```

- Values are percent-encoded: every byte outside `A-Z a-z 0-9 - _ . ~ /` becomes `%HH`.
  An SSID `My Wi-Fi, 5G*` is sent as `My%20Wi-Fi%2C%205G%2A`. (`/` is left alone: it means nothing
  in a line and a channel plan is full of them. A decoder accepts `%2F` too.)
- CRC is CRC-16/CCITT-FALSE over everything before `*`, as 4 upper-case hex digits.
  `HB:n=3,f=31` has CRC `C888`, so the line is `HB:n=3,f=31*C888`.
- Longest line: 240 bytes (400 for the two relay lines between core and bridge). Unknown keys are ignored; unknown types and bad CRCs are dropped and counted.
- MACs are 12 lower-case hex digits with no separators.

## ESP-NOW frames

```
'B' 'R' | ver u8 | kind u8 | ctr u32 LE | frag u8 | len u8 | payload (lines, no CRC) | tag 8 bytes
tag = HMAC-SHA256(owner_key, "bristle-frame" || everything before the tag)[:8]
```

`kind` 0 = lines, 1 = poll (a node checking in, no payload), 2 = sync. Max 250 bytes, so a frame
carries up to 232 bytes of lines. The lines are whole, without their `*CRC`, separated by `\n`: the
tag covers them.

This is Bristle's own format, written for it. It is not taken from, and not compatible with, any
other project's node or cluster protocol: every frame is signed with a key that never leaves your
core and your nodes, and there is no shared or published join secret.

**Fragments.** `frag` high nibble = index, low nibble = count; `0x01` is a whole frame. A line too
long for one frame travels alone, cut into `count` pieces (15 at most) with consecutive counters. A
missing or out-of-order piece drops that one line.

**The counter survives restarts.** Each sender's `ctr` must be higher than the last one the
receiver took from it; anything else is a replay and is dropped. Both ends keep that true across
restarts:

- A sender takes its counters from blocks of 4,096 that it reserves in permanent storage (the node:
  NVS; the core: `loot/bristle/espnow.json`), and starts a fresh block at every start. It never
  signs two frames with one counter.
- A receiver remembers the lowest counter it will still take. The first time it takes a counter in
  a block it writes down the end of that block, so after a restart it takes nothing it could have
  seen before.
- A frame that is too old is answered with a **sync** frame: its payload is 4 bytes, little-endian,
  "use a counter of at least this". The sender jumps there. A sync frame is checked for its tag but
  not for its counter: all it can do is move the other side's counter up, so replaying one does
  nothing. After a restart of either end, at most one exchange is lost before they agree again.

Counters never go back, also when a node is freed and adopted again.

**Who may send.** Only a node with an owner: a board with no owner cannot sign, sends nothing over
ESP-NOW, and is adopted over USB.

### The bridge

One Bristle board in the bridge role sits on the core's USB hub (`ROLE:r=1`, set from the Nodes
screen; the board restarts as a bridge and scans nothing). It relays:

```
node --ESP-NOW frame--> bridge --ERX:fr=<node MAC>,rs=<RSSI>,d=<frame, base64url>--> core
core --ETX:to=<node MAC>,d=<frame, base64url>--> bridge mailbox --ESP-NOW frame--> node
```

`ERX` and `ETX` are the only lines longer than 240 bytes (up to 400): they carry a whole frame.
The bridge checks that a frame has the shape of a Bristle frame and nothing else. It checks no
tags, holds no key logic and decides nothing; the core verifies every frame. A frame signed with
another key is counted and ignored by the core.

A scanning node listens on the control channel only for a few milliseconds per visit. So the
bridge keeps what the core sends in a **mailbox** per node (8 frames, 30 s) and sends it the
moment that node's next frame arrives. With each heartbeat the bridge reports `BST:rx,tx,tf,mb,md`
(frames in, out, failed, held, mail dropped).

### Timing plan, and the scan time it costs

Nothing here has been measured on a board yet. The numbers are worked out from the frame sizes and
ESP-NOW's 1 Mbit/s rate; checkpoint D measures them (the node's `links` console command prints how
long its last visit took).

| Item | Value | Where it is set |
|---|---|---|
| Control channel | 1 (2.4 GHz). Not 6: Piglet and Biscuit meshes use 6. | `control_channel` in bristle.conf; `ESPN:ch=` tells nodes and bridge |
| Visit interval | every 1,500 ms, between two channel dwells | `espnow_visit_interval_ms` |
| In a visit | tune to the control channel, send up to 8 frames (a poll if there is nothing to send), listen 12 ms; every frame that arrives restarts the 12 ms; answers go out in the same visit | `espnow_visit_frames`, `espnow_visit_window_ms` |
| Longest visit | 60 ms | `espnow_visit_max_ms` |
| Bridge lost | no valid frame for 20 s: the node polls by broadcast and tries channels 1, 6, 11 and the configured one in turn | `espnow_lost_ms` |
| No bridge at all | after a minute without one, the node looks only every 10 s | `espnow_search_ms` |
| On USB | a node the core has on USB makes no visits at all | |

Cost of one visit (one full 250-byte frame is about 2.9 ms on air with its acknowledgement, a poll
about 1 ms, a channel change about 2 ms):

| Visit | Time on the control channel | Share of scan time |
|---|---|---|
| Idle (poll out, nothing waiting) | about 15 ms | 1.0% |
| Normal driving (2 frames of finds out, an ack back) | about 33 ms | 2.2% |
| Worst case (8 frames out, more coming back) | 60 ms, capped | 4.0% |
| No bridge anywhere (after the first minute) | about 15 ms every 10 s | 0.15% |

**What a visit carries.** A find is 70 to 110 bytes, so a frame holds 2 or 3 and a visit 16 to 24:
10 to 16 finds per second per node. More than that stays in the node's queue for the next visit;
nothing is lost.

**How long things take.** A command waits for the node's next visit: up to 1.5 s. A node that
comes into range is online after about three visits (its poll, the core's hello, the proof):
3 to 5 s.

**40 nodes.** Nodes do not coordinate their visits; each visits between its own dwells, which
spreads them out, and ESP-NOW's own listen-before-talk and retries handle two that meet. Air time
on the control channel: about 3% with every node idle, about 20% with every node sending two
frames per visit. The bridge passes each frame up as a line of about 370 bytes: 40 nodes at two
frames per visit is about 20 KB/s, which a native-USB board (the XIAO) carries easily and a board
on a 115,200 baud UART port does not. **Use a native-USB board as the bridge.** ESP-NOW keeps at
most 20 peers; the bridge adds a node as a peer when it has mail for it and drops the one unused
longest. Past 40 nodes the next limit is the control channel's air time, and a second bridge on
another control channel is the way on (not built).

## Handshake and owner lock

```
core -> HI:v=1,cn=<core nonce>
node -> HELLO:v=1,hw=<hwid>,bd=<board>,b=<build>,cap=<flags>,nn=<node nonce>[,np=<node proof>,oid=<owner id>,n=<number>]
core -> AUTH:cp=<core proof>
```

- `node proof = HMAC(key, "bristle-node" || core nonce || hwid)[:16]`
- `core proof = HMAC(key, "bristle-core" || node nonce || hwid)[:16]`
- `owner id = HMAC(key, "bristle-owner-id")[:4]`

Both sides prove they hold the key, so a node can't be taken over and a fake node can't inject data.
Until the handshake completes, a node accepts only `HI`, `PING` and `IDENT`. A node with no owner
also accepts `TIME`. An owned node takes its clock only from its authenticated owner, so nobody
else on a link can shift its timestamps.

**Adoption is USB-only.** An unowned node accepts `ADOPT:k=<key>,n=<number>` only on its USB port.
Sending the key over the air would expose it; a future version may add an X25519 exchange with a
BOOT-button press. Holding BOOT for 5 s frees a node.

**Node Memory and the Block list live on the core.** The core only talks to devices that speak
Bristle, pass the handshake, and are in Node Memory. There is no generic-device mode.

**One core at a time.** `HI` may carry `ci`, the core's own id. A node that a core with another id
is running (a live, authenticated link) answers a hello that comes in over the air with
`RSP:c=HI,ok=0,e=busy` and changes nothing. That core leaves the node alone and asks again every
30 s. A hello over USB always gets through: whoever has the node on a cable has it in their hands.

**The beacon.** A board that no core has spoken to on its USB port prints
`BCN:hw=<hwid>,bd=<board>,b=<build>,o=<0|1>,ro=<role>` once a second. A program can so tell a
Bristle board from any other ESP32 by listening, without writing a byte to the port. The beacon
stops at the core's `HI`, and for a minute after someone types a console command.

## Commands (core to node)

| Line | Meaning |
|---|---|
| `TIME:u=<unix>[,ms=]` | Set the clock (UTC). The node holds finds until its clock is set. |
| `NUM:n=<number>` | Set the node number. |
| `PLAN:f=<flags>,r=<U\|E>[,d=<dwell>],c=<entry>/...` | Channel plan. Flags: 1 Wi-Fi, 2 BLE. An entry is `<ch>.<dwell ms>`, or just `<ch>` for the default dwell `d`. |
| `RUN:m=<mask>,s=<session>` | Start (mask 1 Wi-Fi, 2 BLE, 3 both) or stop (0). Kept across reboots. |
| `ACK:q=<seq>` | The core has every find before `seq`. No reply. |
| `PING:` | Liveness. |
| `IDENT:d=<seconds>` | Blink the LED. |
| `REPEAT:ap=<ms>,ble=<ms>` | Repeat suppression windows. |
| `FORGET:` | Free the node (owner, number, plan, queue cleared). |
| `ROLE:r=<0\|1>` | 0 node, 1 bridge. USB only. The board restarts in the new role. |
| `ESPN:ch=<1-13\|0>` | ESP-NOW control channel; 0 switches the ESP-NOW link off on that board. |
| `WATCH:m=<MAC>,d=<seconds>` | Hunt mode: listen for Bluetooth LE only and report every advertisement from that MAC as `SEEN`. 0 stops it; it ends by itself after 15 minutes. Nothing is sent to that device. |
| `ETX:to=<MAC>,d=<frame>` | Bridge only: hold this frame for that node. No reply. |
| `OTA:op=begin\|chunk\|verify\|commit\|abort\|status,...` | Firmware update (below). |

Every command except `ACK`, `ETX` and `OTA op=chunk` gets `RSP:c=<command>,ok=1|0[,e=<error>][,id=<id>]`,
and most are followed by a heartbeat.

## Node to core

| Line | Meaning |
|---|---|
| `HB:...` | Heartbeat every 3 s and after each command. Fields: node, flags, plan hash, run mask, session, link type, backlog, dropped, capacity, fill %, next seq, oldest seq, build, temp, uptime, free RAM, link RSSI. |
| `AP:q,t,s,b,c,r,k[,e,ms]` | Wi-Fi AP: seq, unix time, session, BSSID, channel, RSSI, security code, SSID. |
| `BT:q,t,s,m,r[,at,nm,mf,mt,sv,bh,bo,ms]` | BLE device: seq, time, session, MAC, RSSI, address type, name, manufacturer company id, first byte of the manufacturer data after the id (`mt`), up to three 16-bit service UUIDs (`sv`, 4 hex each, joined by `.`), and for another Bristle board its hwid (`bh`) and `bo=1` if it has an owner. |
| `SEEN:m,r` | Hunt mode: the watched MAC was heard just now at RSSI `r`. Not queued, not numbered. |
| `TREQ:` | Asks for the time until it's set. |
| `BCN:hw,bd,b[,o,ro]` | USB only, unasked: "a Bristle board is here" (above). |
| `ERX:fr,d[,rs]`, `BST:rx,tx,tf,mb,md` | Bridge only (above). |
| `OTAS:st=idle\|receiving\|verifying\|ready\|error[,o,e]` | Firmware update status; `o` is the next byte the node wants. |

Heartbeat flags added in build 4: 512 = this board is the bridge, 1024 = it runs a freshly updated
image that is not confirmed yet.

**Bluetooth scanning.** Plan flag 4 (`ble_active`) asks a node to also request scan responses,
which is where many device names are. The core sets it by default (`ble_scan = active`). Without
it (`ble_scan = passive`) a node only receives. Either way a node never connects to or pairs with
anything it hears, and while it watches one device for a hunt it only receives.

## Channel plans

The plan hash is FNV-1a 32 over `f=<f>;r=<r>;c=<c>` with the dwell written on every entry,
however the plan was sent. `PLAN:f=1,r=U,c=1.120/6.120/11.120` and `PLAN:f=1,r=U,d=120,c=1/6/11`
are the same plan and both hash to `d350013b`. The node reports the hash it holds; the core resends
the plan until they match.

The core sends the short form: the most common dwell once as `d=`, and a dwell only on the entries
that differ. A plan with every channel of the US region is 142 bytes that way; with a dwell on
every entry it would not fit the 240-byte line.

The core's planner (`core/bristle/plan.py`) keeps one node on BLE when there are 2+ nodes, gives 5
GHz only to 5 GHz-capable nodes, honours manual pins, pushes channels Kismet covers heavily to the end
of each list with the minimum dwell, and deals channels round-robin when there are more nodes than
channels.

## Delivery

Every find gets the next sequence number, never reused (blocks of numbers are reserved in flash, and a
reboot skips to the next block, so there are gaps). Finds stay queued in RAM and flash until acked.
After a reconnect, or if acks stall for 10 s, the node resends from the oldest unacked find. The
core drops duplicates by (node, seq). When flash fills, the oldest finds are dropped and counted (`qd`).

In the heartbeat, `qb` is the exact number of finds the node holds, `qo` is the sequence number of
the oldest one (`qn` when it holds none) and `qn` is the next number it will use. The core treats
everything below `qo` as gone, so a gap never holds the ack up.

**Time.** A find is stored with the time it was seen. If the node has no clock yet, it is stored with
the boot number and the uptime, and gets its real time when the core sets the clock during that
boot. A boot that ends without a clock can't be timed: those finds are never sent; they are dropped
and counted in `qd`. A node never sends a find without a valid time.

## Keeping a link alive

- The core sends `PING` when it has sent nothing else for 5 s. A node that hears nothing valid for
  20 s treats the link as gone: it stops sending and keeps its finds.
- A node that sends no heartbeat for 9 s, or answers a command with "not authenticated" (it
  restarted, or timed the link out), gets a fresh handshake from the core.
- The core sends `TIME` again every 10 minutes: node clocks drift.
- Discovery on USB: `HI`, wait 1.5 s, at most 3 tries. Then the core says nothing more to that port
  until it is unplugged and plugged in again.

## A session over USB

```
core -> HI:v=1,cn=00112233445566778899aabbccddeeff*....
node -> HELLO:v=1,hw=a0b1c2d3e4f5,bd=xiao_c5,b=2,cap=240,nn=ffeeddccbbaa99887766554433221100*....
          (no np: the node has no owner. It is listed as new. Nothing more happens until Adopt.)
core -> ADOPT:k=<32 hex owner key>,n=1*....
node -> RSP:c=ADOPT,ok=1*....
core -> HI:v=1,cn=<new nonce>*....
node -> HELLO:...,nn=<new nonce>,np=<node proof>,oid=6ca639c3,n=1*....
core -> AUTH:cp=<core proof>*....
node -> HB:n=1,f=246,ph=,m=0,s=0,lk=1,qb=0,qd=0,qc=2944,qf=1,qn=1000,qo=1000,b=2,t=41,up=12,ram=210*....
core -> TIME:u=1795300000,ms=250*....
core -> PLAN:f=3,r=U,d=120,c=1/2/3/.../165*....      node -> RSP:c=PLAN,ok=1*....   node -> HB:...
core -> RUN:m=3,s=7*....                             node -> RSP:c=RUN,ok=1*....    node -> HB:...
node -> AP:q=1000,t=1795300004,ms=120,s=7,b=a42bb0123456,c=6,r=-61,k=3,e=Home*....
node -> BT:q=1001,t=1795300004,ms=380,s=7,m=c0ffee123456,r=-70,at=1,mf=76*....
core -> ACK:q=1002*....
```
(`....` stands for the four CRC digits. The numbers in the heartbeat are an example.)

## A session over ESP-NOW

The same lines, inside signed frames, one visit at a time (`[n]` = the frame's counter):

```
visit 1   node   -> [0]    poll                                   bridge -> core: ERX:fr=a0b1c2d3e4f5,rs=-58,d=...
          core   -> ETX: [0] HI:v=1,cn=...,ci=c0ffee01            (waits in the bridge's mailbox)
visit 2   node   -> [1]    poll                                   bridge sends the mail at once
          bridge -> [0]    HI:v=1,cn=...,ci=c0ffee01
          node   -> [2]    HELLO:v=1,hw=...,nn=...,np=...,n=3     (same visit: the node is still listening)
          core   -> ETX: [1] AUTH:cp=...  TIME:u=...  PLAN:...  RUN:m=1,s=7   (one frame, four lines)
visit 3   node   -> [3]    poll
          bridge -> [1]    AUTH / TIME / PLAN / RUN
          node   -> [4]    HB:...  RSP:c=PLAN,ok=1  RSP:c=RUN,ok=1
visit 4+  node   -> [5]    AP:q=1000,...  AP:q=1001,...  BT:q=1002,...
          bridge -> [2]    ACK:q=1003
```

After a restart the node's next frame carries a counter from a fresh block (4096 here), and if the
core's frame is too old for the node, the node answers `[4097] sync 8192` and the core continues at
8192.

## Bluetooth LE link

- Service `7b1f2c3a-5d4e-4b6f-9a80-b1e5711e0001`; the core writes one line per write to the command
  characteristic (`...0002`); the node sends one line per notification on `...0003`. MTU 247.
- The node advertises the service UUID with 7 bytes of service data: its hwid and a flag byte
  (bit 0: it has an owner). Its name, `BRISTLE-<n>`, is in the scan response.
- The core links only nodes that are in Node Memory, on the one adapter kept for Bristle
  (`ble_adapter`), `ble_max_links` at a time (5; an adapter carries about 7), strongest first. A
  board with no owner is reported ("plug it into USB to adopt it"); a board with another owner is
  left alone. The Nodes screen says when more nodes are in range than the limit allows.
- The handshake and every line after it are the same as on USB.
- One radio, three users. While a core is connected over Bluetooth, the node's scan scheduler
  starts nothing for 150 ms every second, so the link's connection events are not starved by
  channel dwells: about 15% of scan time, only on nodes linked this way, to be tuned at checkpoint
  E. A node the core has on USB does not advertise.

## Firmware updates (OTA)

```
core -> OTA:op=begin,size=1490320,sha=<sha256 hex>     node -> RSP:c=OTA,ok=1   OTAS:st=receiving,o=0
core -> OTA:op=chunk,o=0,d=<144 bytes, base64url>      node -> OTAS:st=receiving,o=144
core -> OTA:op=chunk,o=144,d=...   (up to 4 ahead)     node -> OTAS:st=receiving,o=288
...
core -> OTA:op=verify                                  node -> OTAS:st=ready      (or st=error,e=...)
core -> OTA:op=commit                                  node -> RSP:c=OTA,ok=1, then it restarts
```

- The node writes a chunk only if its offset is the next byte it wants; any other chunk is answered
  with that offset and written nowhere. A lost line costs a resend from that offset.
- `verify` reads the whole slot back out of flash and compares its SHA-256 with the one given at
  `begin`. `commit` is refused before that. The ESP-IDF's own image check runs at commit as well.
- Nothing changes what the node boots until `commit`. An update that is cut off (cable pulled,
  out of range, power cut, the core's `abort`, 90 s without a chunk) leaves the node on the
  firmware it was running.
- **Rollback.** A committed image starts on trial. It is confirmed when its own self-test passes
  and its owner's core has authenticated on some link (a node with no owner: after one minute of
  running). If that has not happened 5 minutes after it started, or it resets before then, the
  bootloader starts the previous image again.
- One node at a time. Scanning pauses on that node during the update. Over ESP-NOW the node visits
  back to back instead of every 1.5 s.
- Expected time for a 1.5 MB image (not measured): about 1 minute on a native-USB port, 3 to 4
  minutes on a 115,200 baud UART port, 3 to 5 minutes over ESP-NOW or Bluetooth.

## Test vectors

All in the spec and in `tests/vectors/vectors.json`:

| What | Value |
|---|---|
| CRC of `HB:n=3,f=31` | `C888` |
| `My Wi-Fi, 5G*` encoded | `My%20Wi-Fi%2C%205G%2A` |
| `1/6.30/11 a=b` encoded | `1/6.30/11%20a%3Db` |
| Plan hash of `f=1,r=U,c=1.120/6.120/11.120` | `d350013b` |
| Owner id of key `000102...0f` | `6ca639c3` |
| Node proof (key above, cn `0011...ff`, hw `a0b1c2d3e4f5`) | `236f38ef09399a53c71aaa0c638debfe` |
| Core proof (key above, nn `ffee...00`, same hw) | `dd805ce85012e60994f4ff944df11325` |
| Frame (kind 0, ctr 7, frag 1, payload `PING:`) | `4252010007000000010550494e473acf7452ec86a86d6f` |
| Frame (kind 1 poll, ctr 4096, no payload) | see `vectors.json` |
| Frame (kind 0, ctr 70000, frag `0x12`: piece 1 of 2) | see `vectors.json` |
