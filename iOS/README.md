# BoatStart

iOS sailing/racing app for race starts, instrument display, and track logging.

## Features

- **Race Start Timer** - Countdown sequences for race starts
- **Starting Line** - Mark boat/pin ends, calculate distance and time to line
- **Instrument Display** - Speed, heading, wind data in multiple view formats
- **Tack Detection** - Monitors heading changes and wind shifts
- **GPS Tracking** - Logs position data with automatic upload to AWS

## Data Sources

| Source | Connection | Use Case |
|--------|------------|----------|
| Phone GPS | CoreLocation | Basic positioning |
| RaceBox Mini | BLE | High-precision racing GPS |
| NMEA 0183 | TCP | Standard marine instruments |
| BoatBrain | TCP/UDP | Custom instrument aggregator |

## Apps

### BoatStart (Main)
Tab-based UI with views for:
- Timer - Race countdown
- Velocitek - Speed/heading display
- Line - Starting line visualization
- Shift - Wind shift indicator
- Data - Raw values
- Instruments - Full panel

### BoatData
Standalone instrument display connecting to NMEA or BoatBrain devices. Shows GPS, wind, depth, rudder angle.

## Project Structure

```
BoatStart/
├── BoatStart/          # Main app views and app delegate
├── BoatData/           # Instrument display app
├── Shared/             # Core logic (BoatState, Mapping, RaceBox, etc.)
├── UIShared/           # Shared UI components and audio files
├── CLITest/            # Command-line test harness
├── TestCloudWrite/     # iCloud testing
├── UdpTest/            # UDP connection testing
└── BoatTimer/          # Standalone timer app
```

## Key Components

- **BoatState** - Central state management for position, heading, speed, line calculations
- **Mapping** - Geodetic math (LLA to NED conversion, line geometry)
- **RaceBoxManager** - BLE protocol implementation for RaceBox Mini GPS
- **DataManager** - NMEA/BoatBrain data parsing and connection management

## Requirements

- iOS 15.0+
- Xcode 14+
- Optional: RaceBox Mini GPS device

## Author

Paul Viola
