# Vakaros Atlas 2 — BLE reverse engineering

Goal: pull live HDG / heel / pitch / GPS from a Vakaros Atlas 2 into an ESP32
(M5Stack CoreS3) over BLE, for RegattaAnalysis.

## Toolkit (this dir; venv at `.venv`, bleak)
- `scan.py [secs]`            — scan, flag likely Vakaros
- `gatt_dump.py <addr>`       — full GATT profile + readable values
- `handles.py <addr>`         — ATT handle <-> UUID map (for reading sniffs)
- `notify_log.py <addr> [s]`  — subscribe to notify chars, log raw packets
- `parse_pklg.py <f.pklg>`    — decode a PacketLogger HCI capture to ATT ops

Atlas 2 on this Mac: BLE id `204E4874-3B0F-F4C3-91F2-3F2C0520E206`
(CoreBluetooth per-host UUID, not a MAC).

## Device facts
- Mfr "Vakaros Inc", Model "Atlas2,0", Serial 0228005BE3, FW 0.30.3, SW 6001001
- Advertises member service 0xFDA2, mfr id 0x069d
- Vendor service `ac510001-0000-5a11-0076-616b61726f73`  (UUID tail = ASCII "vakaros")

| UUID      | role            | ATT handle | CCCD   |
|-----------|-----------------|------------|--------|
| ac510002  | write (cmd A)   | 0x0019     | -      |
| ac510003  | notify(streamA) | 0x001b     | 0x001d |
| ac510004  | write (cmd B)   | 0x001e     | -      |
| ac510005  | notify(streamB) | 0x0020     | 0x0022 |

## Findings
- Subscribe-only (CCCD=0100) yields **0 notifications** -> telemetry is gated
  behind an app-level START command.

### Handshake (from PacketLogger capture, decoded by parse_pklg.py)
NOTE: real ATT handles from the HCI capture (macOS CoreBluetooth hides handles,
so bleak's numbers were synthetic). True handles:
  0x001a = ac510002 (write)    <- START here
  0x001c = ac510003 (notify, status, ~33B)        CCCD 0x001d
  0x0021 = ac510005 (notify, high-rate telemetry) CCCD 0x0022

Sequence the Connect app performs:
  1. WRITE_REQ CCCD 0x001d = 0100   (enable notify on ac510003)
  2. WRITE_REQ CCCD 0x0022 = 0100   (enable notify on ac510005)
  3. WRITE_CMD 0x001a (ac510002) = **0002**   <-- START STREAMING
  -> device immediately streams on ac510003 + ac510005

### Telemetry payload (ac510005 / handle 0x21)
Each NOTIFY = 1 header byte (0xff or 0xfe; meaning TBD, likely seq/continuation)
then a chunk of a continuous byte stream of 45-byte records that span notif
boundaries. Reassemble by stripping the header byte and concatenating.

45-byte record (type 0x02 = nav), little-endian [CORRECTED w/ live outdoor data]:
  off  0  u8   type (0x02 nav)
  off  1  u64  timestamp, Unix epoch MILLISECONDS
  off  9  i32  latitude  * 1e-7 deg
  off 13  i32  longitude * 1e-7 deg
  off 17  f32  SOG (m/s)         (0 when stationary; 2-4 on sailing track)
  off 21  f32  COG (RADIANS)     (0..2pi; NOT an accuracy field)
  off 25  f32  ? const 537086.7  (unknown; constant across sessions/locations)
  off 29  f32  quat w
  off 33  f32  quat x
  off 37  f32  quat y
  off 41  f32  quat z   (unit quaternion, norm=1.000 verified)
Validated: GPS 47.683,-122.404 (Shilshole) and 47.640,-122.304 (home) live.

### Live real-time record (ac510003 / handle 0x1c, type 0x0a, 35 bytes)  [CORRECTED]
  020a | u48 time(microseconds) | f32 lat | f32 lon | f32 HEADING(deg 0-360) |
        f32 ?(~305) | f32 ?(~358) | f32 SOG(m/s) | u8 satcount(~22-24) | 2B
f2 = live HEADING (swept full 0-360 on a stationary rotation test, not altitude).
f3(~305)/f4(~358) are rotation-INVARIANT (likely pitch/roll or a solution ref);
NOT line/DTL. No-fix: lat/lon zero + NaN (0x7FC00000). Lean low-latency feed:
lat/lon/heading/SOG directly, no quaternion math needed. byte[32]=satcount.

### DTL (distance-to-line) is NOT transmitted — CONFIRMED
Pinged a line on the device (independent mode), DTL shown on its screen (=-2m,
changes with rotation via a 9m bow offset). Rotation test: DTL swept ~18m but
NO streamed field followed; searched all offsets/scales for the -2 value -> none.
Setting a line added no record type and no line-end coords. => the Atlas computes
DTL/OCS internally (screen + edge alerts) but does not broadcast it on the app
GATT link. ESP32 must COMPUTE DTL itself from: streamed pos + heading (f2) +
bow offset (config) + self-pinged line ends. All inputs are available.
(One untested caveat: an active start-timer/countdown might emit a start-info
record; not exercised here.)

### Other record types in the ac510005 stream (interleaved between 0x02 navs)
  0x01 (12B)  x48  - TBD (counter/imu?)
  0x03 (34B)  x18  - has its own ts + lat/lon(i32 1e-7) + speed float
  0x0e (var)  rare - contains 0xffffffffffffffff invalid markers

### Poll seq (SOLVED empirically)
seq for 0203 poll must be near the device's LOG HEAD index = u16 at offset 27 of
the 33B state header. drive_live.py uses START_SEQ = head - 0x30 and increments;
this returns buffered-log records up to the head, then live records. Confirmed:
2221 type-0x02 records pulled live (mix of buffered Shilshole track + live home).

### CONFIRMED: full pipeline driveable from a BLE central (= ESP32 viable)
drive_live.py replicates the app handshake+poll from bleak and receives both the
live 020a feed AND the full type-0x02 nav (pos + SOG + COG + attitude quaternion).
No bonding/encryption required.

### STILL OPEN: explicit horizontal-accuracy / DOP estimate not located.
Candidates exhausted in 0x02 (pos/SOG/COG/quat/const). Proxy = satellite count
in 020a byte[32]. To get a real accuracy number: short OUTDOOR PacketLogger
capture WITH the app while it DISPLAYS an accuracy value, then match to a byte.

### Command protocol (ac510002 / handle 0x1a) — telemetry is POLLED, not pushed
All commands are `02 <opcode> [payload]`:
  0002          open session  -> device emits 33B state header on ac510003
  020b          request log/track history -> device sends 020b blobs (track hist)
  020901        subscribe (09=subscribe, 01=on)
  0203 <seq16LE> 0000   periodic POLL @ ~5Hz; seq increments by 1 each poll.
                NAV stream on ac510005 begins the instant polling starts.
Replaying 0002 alone yields only the status packet; must also poll.
(seq started 0x5b9e in capture; exact index semantics still fuzzy — see below.)

### GPS fix state
- 33B state header byte[7]: 0x03 = fix (outdoor capture), 0x08 = no-fix (indoor).
- No fix => device streams type-0x0a records on ac510003 instead of type-0x02
  nav on ac510005. 020a record (35B) carries a float = 0x7FC00000 (NaN) in the
  position/accuracy slot. Confirmed indoors: zeroed lat/lon + NaN.
- LIVE DRIVE WORKS from bleak (drive_live.py): our Mac central replicates the
  app handshake+poll and receives the live stream. Just need a real fix (sky
  view) to get type-0x02 nav records.

## Open questions
- quaternion convention: which axis = bow, sign of heel/pitch, mag vs true
  north. Needs a CONTROLLED motion run (level / point-north / known tilt).
- meaning of the two constant f32s (off21/off25) and u32 flags
- ac510003 status packet layout (33B + larger variants)
- 0xff vs 0xfe notif header; other record types (0x0e ...)
- can we DRIVE it ourselves: from bleak write 0002 to ac510002 and receive the
  stream (proves the ESP32 path). <-- NEXT STEP
- bonding/encryption: none required (capture shows plain writes, no pairing)
