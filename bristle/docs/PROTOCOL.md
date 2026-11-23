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
| ESP-NOW via bridge | Binary frame carrying lines | HMAC-SHA256 tag + replay counter | Owned nodes only |

## Lines

```
TYPE:key=value,key=value*CRC\n
```

- Values are percent-encoded: every byte outside `A-Z a-z 0-9 - _ . ~ /` becomes `%HH`.
  An SSID `My Wi-Fi, 5G*` is sent as `My%20Wi-Fi%2C%205G%2A`. (`/` is left alone: it means nothing
  in a line and a channel plan is full of them. A decoder accepts `%2F` too.)
- CRC is CRC-16/CCITT-FALSE over everything before `*`, as 4 upper-case hex digits.
  `HB:n=3,f=31` has CRC `C888`, so the line is `HB:n=3,f=31*C888`.
- Longest line: 240 bytes. Unknown keys are ignored; unknown types and bad CRCs are dropped and counted.
- MACs are 12 lower-case hex digits with no separators.

## ESP-NOW frames

```
'B' 'R' | ver u8 | kind u8 | ctr u32 LE | frag u8 | len u8 | payload (lines, no CRC) | tag 8 bytes
tag = HMAC-SHA256(owner_key, "bristle-frame" || everything before the tag)[:8]
```

`kind` 0 = lines, 1 = poll (a node checking in). `frag` high nibble = index, low nibble = count.
Each sender's `ctr` must strictly increase; anything else is a replay and is dropped. Max 250 bytes.

Timing: a scanning node returns to the control channel (default 1) every 1500 ms for 40 ms, sends a
frame (or a poll), and picks up anything the bridge holds for it. These defaults are tuned at
checkpoint D.

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
| `OTA:op=begin\|chunk\|commit\|abort\|status,...` | Firmware update (see spec). |

Every command except `ACK` gets `RSP:c=<command>,ok=1|0[,e=<error>][,id=<id>]` and then a heartbeat.

## Node to core

| Line | Meaning |
|---|---|
| `HB:...` | Heartbeat every 3 s and after each command. Fields: node, flags, plan hash, run mask, session, link type, backlog, dropped, capacity, fill %, next seq, oldest seq, build, temp, uptime, free RAM, link RSSI. |
| `AP:q,t,s,b,c,r,k[,e,ms]` | Wi-Fi AP: seq, unix time, session, BSSID, channel, RSSI, security code, SSID. |
| `BT:q,t,s,m,r[,at,nm,mf,ms]` | BLE device: seq, time, session, MAC, RSSI, address type, name, manufacturer company id. |
| `TREQ:` | Asks for the time until it's set. |
| `OTAS:st=...` | Firmware update status. |

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

The ESP-NOW and Bluetooth LE sessions carry the same lines; their examples are added with those
steps (`PLAN.md`).

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
