# BoatBrain bench testing — two-board NMEA 2000 rig

How to exercise the BoatBrain logger **off the boat**, by pairing it with a second
board that synthesizes NMEA 2000 traffic. Built 2026-06 while diagnosing a field
failure where a freshly-updated BoatBrain logged almost nothing (38 rotated
`000NNN.log` files, 37 of them 0 bytes, no GPS timestamp). The rig let us prove the
**firmware/logging path is healthy** and that the field failure was upstream
(no time PGN on the bus + a near-zero-capture hardware/power problem on the new unit).

## Roles

- **Receiver = board under test** = stock `BoatBrain` (`m5stack-cores3` env). Do **not**
  modify it for the bench — the whole point is to validate the shipping firmware.
- **Sender** = `../NMEAMessageSender` built for `m5stack-cores3`. Synthesizes
  SystemTime (126992), heading (127250), wind (130306), depth, rudder, rate-of-turn,
  position (129025), COG/SOG (129026) and GNSS (129029) and streams them onto the bus.
  See `Arduino/NMEAMessageSender/README.md`.

The original rig used an old ESP32-WROOM proto as the sender, but two identical
M5Stack CoreS3 + PwrCAN boards are far easier (same toolchain, same DIPs, same pins).

## Hardware

- 2× M5Stack CoreS3, each on an M5Stack **PwrCAN 13.2 (M139)** module, DIPs in the
  normal boat position (CAN routed to **GPIO6 = TX, GPIO14 = RX**).
- Both PwrCAN CAN ports joined on one NMEA 2000 bus (CAN-H/CAN-H, CAN-L/CAN-L, GND),
  with bus termination (~60 Ω across CAN-H/CAN-L ideal; one 120 Ω terminator works).
- **A 12 V supply into the PwrCAN power inputs of BOTH boards.** This is not optional —
  see below.

### ⚠️ The single most important gotcha: the PwrCAN CAN transceiver needs 12 V

The PwrCAN's CAN side is **galvanically isolated**, powered by a 2 W isolated DC-DC
(Mornsun F0505S-2WR3) fed from the M-Bus 5 V. On the boat that 5 V comes from the
PwrCAN's own 9–24 V → 5 V buck. **On a USB-only bench there is no bus power, so the
isolated CAN transceiver is dark** — the MCU and firmware run fine, the TWAI
controller initializes, but nothing reaches the wire. Symptom: the sender's CAN goes
bus-off and `TX fail` runs away while `TX ok` stays ~0. **Apply 12 V to both PwrCANs
and the bus comes alive.**

### Corollary: under 12 V the CoreS3 drops off USB

With 12 V applied, the PwrCAN drives M-Bus 5 V and the CoreS3's USB VBUS loses the
power arbitration — the **USB-CDC serial port disappears** (the AXP2101 takes PwrCAN
power; see `../BatteryMonitor/README.md`). So you **cannot** drive the serial CLI or
read serial while 12 V is applied. Two consequences:
- The sender must enable its PGNs **by default in firmware** (it does), so no CLI is
  needed at runtime.
- Observe everything on the **two displays** (and the receiver's **SD card**), not USB.

## Build & flash

```bash
# Receiver (from the MAIN checkout — BoatBrain/include is a git-ignored symlink that
# worktrees don't recreate, and lib/display is untracked, so it only builds in main):
cd Arduino/BoatBrain && pio run -e m5stack-cores3 -t upload --upload-port /dev/cu.usbmodemAAAA

# Sender:
cd Arduino/NMEAMessageSender && pio run -e m5stack-cores3 -t upload --upload-port /dev/cu.usbmodemBBBB
```

Flash with **12 V removed** (USB must be present and powering the board). `pio device
list` to find the two `usbmodem*` ports — both CoreS3s are VID `303A:1001`; tell them
apart by the MAC/serial in the listing.

**Native-USB flashing flakiness:** the ESP32-S3 USB-CDC sometimes fails the esptool
stub handshake ("No serial data received" / "Invalid head of packet"). Retry a couple
times; if it persists, flash just the app via the ROM loader (skips the stub):

```bash
cd Arduino/NMEAMessageSender/.pio/build/m5stack-cores3
python ~/.platformio/packages/tool-esptoolpy*/esptool.py --chip esp32s3 \
  --port /dev/cu.usbmodemBBBB --before default_reset --after hard_reset --no-stub \
  write_flash 0x10000 firmware.bin     # bootloader/partitions already on-chip
```

## Operating procedure (order matters)

1. Flash both boards (12 V off, USB on).
2. **Apply 12 V to both PwrCANs.** The sender auto-recovers from bus-off (it polls
   `twai_get_status_info` and calls `twai_initiate_recovery`/`twai_start`), so it does
   **not** need a reflash after a power glitch.
3. Watch the **sender display**: `Bus: RUNNING` (green), `PGNs on: 10/15`,
   `TX ok` climbing, `TX fail` low/flat, `TEC: 0`. A healthy run is ~21000 ok / ~28
   fail / TEC 0 (the few fails are the power-up transient).
4. Watch the **receiver display** (`main.cpp` draws it): instruments fill in, the
   **N2K msg rate** (right of the IP row) goes > 0, and — the headline — the **log
   filename flips from `000NNN.log` to a `2023-12-12T..Z.log` timestamp** once the
   SystemTime PGN sets the clock.
5. For the SD-card record (timestamped `.log` with real data), power the receiver from
   12 V, let it log a minute, drop 12 V, and read the card in a Mac reader.

## What to expect on the receiver (sender's hardcoded test values)

`HDG ≈ 227`-ish *(but see filter below)*, `COG ≈ 115`, `DEP ≈ 85`, `AWA ≈ 30`,
`AWS/TWS ≈ 1.5`, position ≈ 47.6 / -122.3. Two intentional zeros:

- **HDG reads 0** from the synthetic sender. `nmea.hpp` `Heading()` has
  `if (N2kMsg.Source != 4) return;` — the boat has two compasses and the firmware only
  trusts heading from **source address 4**. The synthetic sender claims another
  address, so its heading is **received and logged to SD but dropped from the display
  state**. (Brittle: if a real compass moves to a different source address, HDG and
  everything derived silently read 0.) Set the sender's node address to 4 if you need
  HDG live on the bench.
- **TWD/TWS read 0.** The display shows *received* true wind; the sender only emits
  *apparent* wind (130306). The board reports true wind from a PGN, it does not compute
  it for display, so with no true-wind PGN on the bus they stay 0.

## CAN mode notes (why a naive 2-node bus fails)

- `NMEA2000_esp32_twai` installs TWAI in **`TWAI_MODE_NO_ACK`**. Stock BoatBrain also
  never calls `SetMode`, so the NMEA2000 library defaults to `N2km_ListenOnly`.
- On the boat (many nodes) this is invisible. On a **2-node** bench it matters: with
  both boards in NO_ACK, frames complete without anyone ACKing and the receiver still
  receives — which is exactly the stock config, so **no firmware change is needed**.
- Do **not** "fix" this by making the receiver a transmitting node
  (`N2km_ListenAndNode` / `TWAI_MODE_NORMAL`): on a 2-node bus its TX attempts fail,
  it runs to bus-off, and a bus-off node injects error frames that wreck the bus for
  the sender too. Keep the receiver stock and passive.

## Probe: does GNSS alone set the clock?

Disable SystemTime (126992) on the sender, leave GNSS (129029) on, and watch the log
filename. Historically the `GNSS()` handler parsed the time but never called
`setTime()`, so the clock only ever came from SystemTime — a GPS that emits 129029 but
not 126992 would leave the logger in `000NNN.log` forever. This was fixed 2026-06
(`GNSS()` now sets the clock with the same validity gate as `SystemTime()`), so after
the fix the filename should timestamp from GNSS alone too.

## What the rig proved (field-failure context)

The stock receiver firmware correctly: receives PGNs → sets its clock from SystemTime
→ names the log with the timestamp → writes to SD. So the 2026-06-15 field failure was
**not** a firmware bug. It was upstream: no time PGN (126992/129029) reached the new
unit (the one non-empty field log had only 129025 position, no time), plus a near-zero
capture that smells like the same PwrCAN power-arbitration fragility we fought on the
bench. Check the new unit's bus power and which source address/PGNs its GPS actually
emits.
