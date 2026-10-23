# BoatBrain Firmware

ESP32-based NMEA 2000 data logger and wireless broadcaster for sailboat instrumentation.

## Hardware

- **MCU**: M5Stack CoreS3 (ESP32-S3)
- **NMEA 2000**: CAN bus via TWAI interface (TX: GPIO6, RX: GPIO14)
- **Storage**: MicroSD card for data logging
- **Display**: Built-in 320x240 LCD
- **Power**: PwrCAN 13.2 (boat 9–24 V → isolated 5 V on the M-Bus), with an
  **optional** M5GO Bottom3 LiPo battery at the bottom for automatic failover

## Features

- NMEA 2000 message parsing and logging
- WiFi Access Point for client connections
- UDP broadcast of boat state (10Hz) for iOS app
- SD card logging in Actisense format
- Optional battery failover with on-screen power-source status

## Power & Battery Failover

The logger runs off **boat power** (PwrCAN isolated 5 V) and, if an **M5GO
Bottom3** LiPo battery is fitted at the bottom of the stack, falls back to it
**automatically** when boat power drops — no reset, no firmware in the switching
loop. The AXP2101 PMIC does the switching; `include/power.hpp` (`power::setup()`,
called once at boot) just configures the PMIC so this works on this hardware and
reports which source is live.

The bottom row of the instrument screen shows the power source on the left and a
voltage (or, on battery, the shutdown countdown) right-justified on the right:

| Left | Right | Meaning |
|------|-------|---------|
| `BOAT 87% chg` (green) | `4.10V in4.9V` | On boat power; battery 87 %, charging; cell 4.10 V, boat-input 4.9 V |
| `BOAT (no bat)` (green) | `in 4.9V` | On boat power; no battery installed (fine) |
| `BOAT 14% lo` (orange) | `3.62V in4.9V` | On boat power but reserve battery is low |
| `BATT 64%` (**red**) | `off 9:58` (**red**) | **Boat power lost** — on reserve; time left until auto-shutdown |

A boat-power loss / restore is also edge-logged to the trace as
`*** POWER -> BATTERY (...)` / `*** POWER -> BOAT (...)`, so a session that ended
on reserve is visible after the fact.

### Auto-shutdown on sustained battery operation

Boat power is the reliable source, so a long continuous run on the reserve
battery means the boat is powered down or the logger was unplugged. After
**10 minutes** continuously on battery (`power::BATTERY_SHUTDOWN_MS`), the logger
closes its SD log cleanly, shows *"BOAT POWER LOST / Logs saved / Shutting
down…"*, and powers the AXP2101 off to preserve the cell. Re-applying boat power
(VBUS) boots the stack again and a fresh log starts. The bottom-row `off M:SS`
countdown shows the time remaining; it resets the instant boat power returns, so
brief PwrCAN dropouts don't trigger a shutdown.

**The battery is optional** — every PMIC setting is safe with no cell installed;
the screen just reads `BOAT (no bat)`. Notes on this specific stack (distilled
from the standalone `Arduino/BatteryMonitor` bench work, 2026-06-09):

- The PwrCAN's isolation is a 2 W module (~400 mA @ 5 V for the whole stack).
  BoatBrain (WiFi AP + CAN read + SD logging) draws most of that on its own, so
  charge current is capped to 125 mA and the Bottom3 acts mainly as a **reserve +
  buffer cap**; it may charge only slowly, or briefly lean on the cell under load.
- `power::setup()` puts the M-Bus in **input** mode (`setExtOutput(false)`) — the
  single most important line. Without it the CoreS3 back-drives 5 V onto the bus
  and the stack silently runs off battery while ignoring boat power.
- Battery **current** (mA in/out) is not measurable on the AXP2101/CoreS3; only
  source, voltage, %, and charge/discharge **direction** are shown.

See `Arduino/BatteryMonitor/README.md` for the full bench write-up.

## Quick Start

### Build & Flash

```bash
cd Arduino/BoatBrain
pio run -e m5stack-cores3           # Build
pio run -e m5stack-cores3 -t upload # Flash firmware
```

### Boot Behavior

On power-up, the screen shows "Tap=AP, Hold=Test" for 3 seconds:

| Input | Behavior |
|-------|----------|
| No touch | Creates "BoatBrain" WiFi AP |
| Tap | Creates "BoatBrain" WiFi AP (skips network join attempt) |
| Long hold (2+ sec) | Test mode + AP (generates fake NMEA data) |

## Network Configuration

### WiFi Access Point

Before building each physical device, run `python Scripts/provision_wifi.py`
from the repository root. It writes a random AP password to the ignored
`Arduino/include/wifi_credentials.local.hpp` with owner-only permissions.
It refuses to overwrite an existing configuration. Keep each device's file
privately and provision a different one before building another device.
Use `--station-ssid NAME` only if station mode is needed; the password is prompted
without echo. An unprovisioned build leaves Wi-Fi access disabled.

After flashing, enter that device password in BoatData's **BoatBrain Wi-Fi**
page (swipe past the log page). Removing old credentials from source does not
rotate deployed devices or a router's password: rotate any network that used
the previously published station credentials and flash each boat device.

| Setting | Value |
|---------|-------|
| SSID | `BoatBrain` |
| Password | `<provisioned-device-password>` |
| IP Address | `192.168.4.1` |

### Ports & Protocols

| Port | Protocol | Purpose | Rate |
|------|----------|---------|------|
| 101 | UDP | iOS app binary data | 10Hz |

## Data Protocol

### UDP Binary Format (Port 101)

Clients send periodic "hello" packets to register. BoatBrain then sends binary boat state:

```
[header: 1 byte] [data: variable] [terminator: 0xff 0xff 0x0a]
```

Data fields (16-bit integers, scaled):
- SOG, COG, Heading, Speed, Depth
- AWA, AWS, TWA, TWS, TWD
- Latitude, Longitude (split into high/low words)
- Rudder angle
- Packet counter, message counter, error counter

## NMEA 2000 Messages

Supported PGNs (see `nmea.hpp` for full list):

| PGN | Description |
|-----|-------------|
| 127250 | Vessel Heading |
| 128259 | Speed (Water) |
| 128267 | Water Depth |
| 129025 | Position (Rapid) |
| 129026 | COG/SOG (Rapid) |
| 130306 | Wind Data |
| 127245 | Rudder Angle |

## SD Card Logging

Logs are written in Actisense binary format:
- Default logs: `000001.log`, `000002.log`, etc. (before GPS time sync)
- Timestamped logs: `YYYYMMDD_HHMMSS.log` (after GPS time sync)
- Logs rotate every 20 minutes

## File Structure

```
Arduino/BoatBrain/
├── src/main.cpp          # Main application, tasks, boot logic
├── platformio.ini        # Build configuration

Arduino/include/
├── nmea.hpp              # NMEA 2000 parsing, PGN handlers
├── webServer.hpp         # WiFi setup utilities
├── sdcard.hpp            # SD card operations
└── display.hpp           # LCD display utilities
```

## Test Mode

Long-hold on boot enables test mode, which generates synthetic NMEA data:
- Simulated position (cycling around a point)
- Random wind angles and speeds
- Cycling heading and speed values

Useful for testing the iOS app without actual NMEA 2000 hardware.

## Troubleshooting

### No WiFi AP appears
- Check serial monitor for boot errors
- Verify firmware was flashed correctly
- Try power cycling the device

### Clients can't connect
- Verify phone is connected to "BoatBrain" WiFi
- Check that UDP port 101 is being used
- Look for "UDP client added" in serial logs

### No NMEA data
- Verify CAN bus connections (TX: GPIO6, RX: GPIO14)
- Check that NMEA 2000 network has power
- Use test mode to verify app connectivity
