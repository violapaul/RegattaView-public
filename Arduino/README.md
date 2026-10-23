# Arduino / BoatBrain

ESP32-based boat instrument system that reads NMEA2000 data, logs to SD card, and streams over WiFi.

## BoatBrain

The main project. Runs on **M5Stack CoreS3** with CAN bus connection to the boat's NMEA2000 network.

### Features

- Reads NMEA2000 messages (GPS, wind, speed, heading, depth, etc.)
- Maintains live `boatState` with all sailing data
- Logs raw NMEA data to SD card with automatic file rotation
- Streams binary boat state over UDP (port 101)
- Creates WiFi AP or joins existing network
- Displays live data on CoreS3 screen

### Boat State Data

| Field | Description |
|-------|-------------|
| `lat`, `lon` | GPS position |
| `sog`, `cog` | Speed/course over ground |
| `hdg`, `spd` | Heading and boat speed (through water) |
| `aws`, `awa` | Apparent wind speed/angle |
| `tws`, `twa`, `twd` | True wind (calculated) |
| `depth` | Water depth |
| `rudder` | Rudder angle |

### Hardware

- M5Stack CoreS3
- CAN transceiver on GPIO 6 (TX) / GPIO 14 (RX)
- SD card for logging

### Build

```bash
cd BoatBrain
pio run -e m5stack-cores3
pio run -t upload
```

## Related Projects

| Project | Description |
|---------|-------------|
| `BatteryMonitor/` | CoreS3 boat-stack power/battery monitor; configures AXP2101 for PwrCAN-primary + battery failover ([README](BatteryMonitor/README.md)) |
| `MastHead/` | Mast-mounted sensor unit (AHRS/wind) |
| `ActisenseRelay/` | NMEA message relay |
| `CoreS3Can/` | CAN bus testing for CoreS3 |
| `CoreS3N2k/` | NMEA2000 testing for CoreS3 |
| `NMEADataDisplay/` | Simple NMEA data display |
| `NMEAMessageSender/` | Send test NMEA messages |
| `NMEATest/` | NMEA protocol testing |
| `AHRS-ICM_20948/` | Attitude/heading reference (ICM-20948 IMU) |
| `RotaryEncoder/` | Rotary encoder input |
| `WebFileServer/` | WiFi file server |

## Shared Code

### `include/`

| Header | Purpose |
|--------|---------|
| `nmea.hpp` | NMEA2000 parsing, boat state management |
| `board.hpp` | Board abstraction (CoreS3 vs generic ESP32) |
| `webServer.hpp` | WiFi setup utilities |
| `sdcard.hpp` | SD card operations |
| `msgEncode.hpp` | Binary message encoding for streaming |
| `trueWind.hpp` | True wind calculation from apparent wind |
| `ledS3.hpp` | LED control for CoreS3 |
| `loopTimer.hpp` | Timing utilities |

### `lib/`

| Library | Purpose |
|---------|---------|
| `display/` | LCD display abstraction |
| `sdcard/` | SD card wrapper |
| `utils/` | General utilities |

## Dependencies

Managed via PlatformIO `lib_deps`:

- `ttlappalainen/NMEA2000-library` - NMEA2000 protocol
- `NMEA2000_esp32_twai` - ESP32 TWAI (CAN) driver
- `m5stack/M5CoreS3` - M5Stack hardware support
- `m5stack/M5Unified` - Unified M5 API
- `hideakitai/DebugLog` - Logging
- `hideakitai/ArduinoEigen` - Linear algebra (for AHRS)
