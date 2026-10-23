# NMEAMessageSender — NMEA 2000 bench sender

Synthetic NMEA 2000 source for **bench-testing the BoatBrain logger off the boat**.
Restored from git (deleted in `14cd4f76`, 2026-04-14) — the stock ttlappalainen
`MessageSender` example with Seattle coordinates, then extended for an M5Stack CoreS3 +
PwrCAN so it pairs cleanly with a second CoreS3 running BoatBrain.

**Full rig guide (hardware, power, procedure, gotchas): `../BoatBrain/BENCH_TESTING.md`.**
Read it — the PwrCAN power behavior is non-obvious and will waste your afternoon.

## Two build targets

| env             | board               | CAN driver / pins                | use                         |
|-----------------|---------------------|----------------------------------|-----------------------------|
| `m5stack-cores3`| M5Stack CoreS3+PwrCAN | TWAI, `TX=GPIO6 / RX=GPIO14`     | **preferred** — pairs with a 2nd CoreS3 |
| `node32s`       | bare ESP32-WROOM    | legacy SJA1000, `TX=17 / RX=16`  | original proto sender       |

```bash
pio run -e m5stack-cores3 -t upload --upload-port /dev/cu.usbmodemXXXX
```

Flash with **12 V removed** from the PwrCAN (USB must power the board). If the
native-USB stub handshake fails ("No serial data received"), flash the app via the ROM
loader: `esptool.py --chip esp32s3 --port ... --no-stub write_flash 0x10000 .pio/build/m5stack-cores3/firmware.bin`.

## CoreS3 behavior (what the firmware does for you)

- **Brings up M5 power** (`M5.begin()`) so the M-Bus 5 V / PwrCAN transceiver is fed.
- **Enables 10 data PGNs by default** (no CLI needed): SystemTime 126992, rudder
  127245, heading 127250, rate-of-turn 127251, boat speed 128259, depth 128267,
  position 129025, COG/SOG 129026, GNSS 129029, wind 130306. The CLI is still there
  but is unusable under 12 V (USB drops — see the guide), hence default-on.
- **On-screen dashboard**: `Bus: RUNNING/BUS-OFF`, `TX ok`, `TX fail`, `TEC/REC`,
  `PGNs on: N/15`. Your at-a-glance bus health.
- **Auto-recovers from TWAI bus-off** (`twai_initiate_recovery`/`twai_start`), so a
  12 V power glitch self-heals instead of needing a reflash.

`SendN2kSystemTime` sends a fixed **2023-12-12** date, so a healthy receiver names its
log `2023-12-12T..Z.log`. The date is arbitrary; timestamped vs. fallback
(`000NNN.log`) naming is the signal.

## Serial CLI (115200 baud, USB only — i.e. 12 V off)

Type `?` for help. `e` enable all, `d` disable all, `msg <x>` toggle one, `msgs` list,
`once <x>` send once, `stat` toggle TX stats, `p` pause. Index→PGN:

| idx | PGN | msg | idx | PGN | msg |
|--|--|--|--|--|--|
| 0 | 60928 | ISO addr claim | 8 | 128259 | boat speed |
| 1 | 126996 | product info | 9 | 128267 | water depth |
| 2 | 126992 | **system time** | 10 | 129025 | lat/lon rapid |
| 3 | 127245 | rudder | 11 | 129026 | COG/SOG |
| 4 | 127250 | heading | 12 | 129029 | **GNSS** |
| 5 | 127251 | rate of turn | 13 | 129539 | GNSS DOP |
| 6 | 127257 | attitude | 14 | 130306 | wind |
| 7 | 127258 | mag variation | | | |

## Receiver = stock BoatBrain

Do not modify the receiver — the point is to validate shipping firmware. Both nodes
end up in TWAI NO_ACK (sender default + stock BoatBrain `N2km_ListenOnly`), which is
exactly what makes a 2-node bus work without any ACK fiddling. See the guide for why,
and for the intentional `HDG = 0` (source-4 heading filter) and `TWD/TWS = 0`
(no true-wind PGN) on the receiver display.
