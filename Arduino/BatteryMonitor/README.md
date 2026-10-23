# BatteryMonitor — CoreS3 boat-stack power monitor

Standalone PlatformIO sketch that displays power-source and battery status for a
boat-mounted M5Stack stack, and — more importantly — configures the CoreS3's
AXP2101 PMIC so the stack runs off **boat power normally and falls back to the
internal battery automatically** when boat power drops.

This is a self-contained utility, separate from the BoatBrain firmware.

## The stack

| Position | Module | Role |
|----------|--------|------|
| Bottom | **M5GO Bottom3** | LiPo battery → AXP2101 `BAT` |
| Middle | **PwrCAN 13.2** (M139) | Boat 9–24 V → isolated 5 V onto the M-Bus; isolated CAN / RS485 |
| Top | **M5Stack CoreS3** | ESP32-S3 + AXP2101 PMIC + LCD |

## What it shows

- **SRC: BOAT / BATTERY** — live power source (green = boat power, red = on battery)
- Battery **%** (voltage-based estimate) + bar, `NO BAT` when no cell is present
- **Vbat** + charge direction (charging / standby / discharging)
- **Boat … V in** — the M-Bus/VBUS input voltage (orange if sagging < 4.6 V)
- **Chg … mA / Vlim …** — active charge-current cap and VINDPM threshold
- **Temp** — AXP2101 die temperature
- Serial @ 115200: same line every 2 s, plus an edge-logged `*** SOURCE -> …`
  event on every boat-power loss / restore.

## How the failover works

Power switching is **automatic in the AXP2101 power path** — there is no firmware
in the switching loop. The sketch's job is only to (a) configure the PMIC so the
PwrCAN can actually act as the primary source, and (b) report which source is
live. Three PMIC settings, all applied in `setup()`, make this work on this
specific hardware.

## Hardware findings baked into this sketch

These were discovered on the bench (2026-06-09) and are the reason the sketch
does what it does. Read before changing power config.

1. **The CoreS3 fights the PwrCAN unless the bus is set to INPUT.**
   `M5.begin()` leaves the CoreS3 **driving 5 V out** onto the M-Bus (boost) when
   a battery is present, to power downstream modules. That ~5.1 V back-biases the
   PwrCAN's ~4.9 V bus feed out of the bus, so **with the battery installed the
   stack silently runs off battery and ignores boat power** (VBUS reads absent).
   Fix: `M5.Power.setExtOutput(false)` puts the bus in **input** mode so the
   PwrCAN powers the stack and the AXP2101 sees VBUS. This is the single most
   important line. Failover is unaffected: boat-power loss → no bus 5 V → AXP
   runs off battery.

2. **Default VINDPM blocks charging from the (sagging) bus rail.**
   The AXP2101's input-voltage limit (VINDPM, reg `0x15`) defaults high (~4.36 V+).
   The PwrCAN's isolated 5 V (Mornsun **F0505S-2WR3**, 2 W) sags under load, so the
   charger rejects it and won't charge on boat power even though it charges fine
   off a stiff USB 5 V. Fix: lower VINDPM to **4.04 V** (`0x15` low nibble = `0x02`,
   0.08 V steps from 3.88 V). Charge headroom still depends on how far Vbat sits
   below the rail, so charging tapers as the cell approaches full — fine for a
   reserve battery.

3. **Charge current is capped to protect the 2 W isolated supply.**
   F0505S-2WR3 = 2 W ≈ **~400 mA @ 5 V** total for the whole stack. Uncapped
   charging can brown out the input. `setChargeCurrent(125)` (the AXP2101 minimum
   step) keeps `run + charge` within budget. Raise `CHARGE_MA` only with a
   stiffer 5 V feed.

4. **Battery current (in/out) is NOT measurable on this hardware.**
   The AXP2101 has no battery-current ADC (unlike the AXP192 in Core2/StickC) and
   the CoreS3 has no shunt. M5Unified's `getBatteryChargeCurrent` /
   `getBatteryDischargeCurrent` / `getBatteryPower` / `getVBUSCurrent` all
   `return 0` — in both 0.1.x and 0.2.x. Direction (charge/discharge) comes from
   the charge-status register; magnitude in mA is unavailable. For true mA, add an
   inline USB power meter or an I²C INA226 / fuel-gauge breakout.

5. **Die-temp ADC must be enabled or it reads garbage (~385 °C).**
   M5Unified's init leaves the AXP2101 die-temperature ADC channel off, so
   reg `0x3C` returns nonsense. `bitOn(0x30, 1 << 4)` enables it.

6. **Battery % uses a voltage curve, not the AXP2101 gauge.**
   The AXP2101 coulomb gauge (reg `0xA4`) reads 0 unless it has run a full
   learning cycle (and a hot-inserted battery never converges), so SOC is
   estimated from a LiPo OCV→% curve instead. Good to a few % at rest; reads a
   bit low under heavy discharge load.

## Operating notes

- **Always run with the Bottom3 installed.** It is both the failover reserve and
  the buffer cap that keeps the stack stable through load spikes the stiff-but-
  limited 2 W PwrCAN can't follow. Running battery-less on the boat is the one
  configuration to avoid.
- **Feed the PwrCAN from a dedicated 12 V boat branch**, not raw NMEA 2000 bus
  power — a CoreS3 stack will blow the N2K LEN budget.

## Build & flash

```bash
cd Arduino/BatteryMonitor
pio run -e m5stack-cores3                 # build
pio run -e m5stack-cores3 -t upload       # flash (CoreS3 on USB-C)
pio device monitor -b 115200              # serial
```

`ARDUINO_USB_CDC_ON_BOOT=1` is set so the native-USB serial port persists after
boot — otherwise the `/dev/cu.usbmodem*` port vanishes once the app runs and the
next reflash needs a manual download-mode button press.

## Verifying failover (bench)

1. PwrCAN (12 V) + Bottom3 installed, USB-C unplugged → expect **SRC: BOAT**,
   **Boat ~4.9 V in**, **charging**, Vbat rising.
2. Pull the PwrCAN feed → flips to **SRC: BATTERY**, **discharging**, no reset.
3. Restore boat power → back to **SRC: BOAT**, charging resumes.
