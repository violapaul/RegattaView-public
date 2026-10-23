# BoatData iOS App

BoatBrain passwords are provisioned per device, rather than shipped in the app.
Swipe past the log page to **BoatBrain Wi-Fi**, enter the provisioned password,
and choose **Save password and connect**. The app stores it in this device's
Keychain; it is not included in cloud backups or source control. Build the
corresponding firmware with `Scripts/provision_wifi.py` before flashing.

Real-time sailing instrument display app that connects to BoatBrain over WiFi.

## Features

- Automatic WiFi connection to BoatBrain AP
- UDP data reception at 10Hz
- Large, glanceable instrument displays
- Connection status monitoring and auto-reconnect
- Debug view with detailed connection diagnostics

## Requirements

- iOS 16.4+
- iPhone with WiFi capability
- Apple Developer account (for WiFi entitlements)

## Setup

### Xcode Configuration

1. Open `BoatStart.xcodeproj` in Xcode
2. Select the **BoatData** target
3. Under Signing & Capabilities:
   - Select your development team
   - Ensure bundle ID is unique (e.g., `com.yourname.BoatData`)

### Required Entitlements

The app requires these entitlements (configured in `BoatData.entitlements`):

| Entitlement | Purpose |
|-------------|---------|
| `com.apple.developer.networking.wifi-info` | Read current WiFi SSID |
| `com.apple.developer.networking.HotspotConfiguration` | Programmatically join WiFi networks |

### Required Permissions

Add to `Info.plist`:
- **Location When In Use** - Required by iOS to access WiFi information
- **Local Network** - For UDP communication with BoatBrain

## Build & Deploy

```bash
# Build for device
xcodebuild -scheme BoatData -destination 'platform=iOS,name=YourPhone' build

# Or use Xcode: Product > Run (⌘R)
```

On first run, you'll need to trust the developer certificate:
**Settings > General > VPN & Device Management > [Your Certificate] > Trust**

## Usage

### Automatic Connection

1. Power on BoatBrain (creates "BoatBrain" WiFi AP)
2. Launch BoatData app
3. App automatically:
   - Detects current WiFi
   - Joins BoatBrain if not already connected
   - Establishes UDP connection
   - Displays instrument data

### First-Time WiFi Join

On first launch, iOS will prompt:
> "BoatData" Wants to Join Wi-Fi Network "BoatBrain"?

Tap **Join** to allow. Subsequent connections are automatic.

### Views

| View | Content |
|------|---------|
| Main | Large instrument tiles (configurable) |
| Debug | Detailed diagnostics: lat/lon, all instruments, connection stats |
| History | Connection event log with timestamps |

## Architecture

### Data Flow

```
BoatBrain (192.168.4.1:101)
    │
    │ UDP packets (10Hz)
    ▼
UdpConnection
    │ - Sends hello packets every 2s
    │ - Receives binary boat state
    ▼
DataManager
    │ - Decodes binary protocol
    │ - Updates BoatState
    │ - Monitors connection health
    ▼
SwiftUI Views
    - InstrumentView
    - DebugView
    - ConnectionHistoryView
```

### Key Files

| File | Purpose |
|------|---------|
| `ContentView.swift` | Main UI, view switching |
| `DataManager.swift` | Connection management, data decoding |
| `UdpConnection.swift` | UDP networking, hello packets |
| `WiFiHelper.swift` | WiFi SSID detection, auto-reconnect |
| `InstrumentView.swift` | Instrument tile display |
| `DebugView.swift` | Diagnostic information display |
| `ConnectionHistoryView.swift` | Connection event logging |

### Shared Code

Located in `../Shared/`:

| File | Purpose |
|------|---------|
| `BoatState.swift` | Instrument data model |
| `LightLog.swift` | OSLog-based logging |
| `DataPacker.swift` | Binary protocol encoding/decoding |
| `ExponentialFilter.swift` | Data smoothing |

## Connection Management

### WiFiHelper

Monitors WiFi status and auto-reconnects to BoatBrain:

```swift
// Checks every 5 seconds
// If not on BoatBrain, attempts to join
NEHotspotConfiguration(ssid: "BoatBrain", passphrase: "<provisioned-device-password>")
```

### UdpConnection

Maintains UDP connection with hello packets:

```swift
// Sends "hello" every 2 seconds to register with BoatBrain
// BoatBrain tracks clients and sends data to registered IPs
```

### DataManager

Monitors connection health:

```swift
// Watchdog: if no data for 5+ seconds, triggers reconnect
// Startup grace period: 10 seconds before watchdog activates
// Tracks packet/message counts and error rates
```

## Debug Information

The debug view shows:

| Field | Description |
|-------|-------------|
| WiFi | Current SSID (green=BoatBrain, yellow=other) |
| Status | Connection state (connected/connecting/failed) |
| Retry# | Reconnection attempt count |
| Reset# | Connection reset count |
| Pkt/Msg | Packets received / messages decoded |
| Errors | Decoding error count |
| Stale | Max/avg time since last message (seconds) |

## Logging

Uses OSLog for device-visible logging:

```swift
// View logs with:
idevicesyslog | grep BoatData

// Log levels: debug, info, warn, error, critical
// Default level: info (set in LightLog.swift)
```

## Troubleshooting

### App shows "No WiFi" or wrong network

1. Ensure Location permission is granted
2. Check WiFi entitlement is properly configured
3. Verify BoatBrain AP is powered on and in range

### Won't auto-join BoatBrain

1. First time: approve the WiFi join prompt
2. If iOS prefers home WiFi: the captive portal on BoatBrain should prevent this
3. Manual fix: Settings > WiFi > [home network] > disable Auto-Join

### Connected but no data

1. Check BoatBrain is running (not stuck at boot)
2. Verify UDP port 101 is correct in `ContentView.swift`
3. Check debug view for packet/message counts
4. Look for "Connection refused" in logs (BoatBrain not ready)

### Connection keeps cycling

1. Check "Stale" value in debug view
2. If startup: wait 10 seconds for grace period
3. Verify BoatBrain is sending data (check its serial output)

## Configuration

### Network Settings

In `ContentView.swift`:
```swift
static let defaultPort = 101      // UDP port
// IP is auto-discovered from gateway (192.168.4.1)
```

### WiFi Settings

In `WiFiHelper.swift`:
```swift
static let TARGET_SSID = "BoatBrain"
static let TARGET_PASSWORD = "<provisioned-device-password>"
```

### Timing

In `DataManager.swift`:
```swift
static let WATCHDOG_INTERVAL = 5.0    // Seconds without data before reconnect
static let STARTUP_GRACE_PERIOD = 10.0 // Seconds before watchdog activates
```

In `UdpConnection.swift`:
```swift
static let HELLO_INTERVAL = 2.0       // Seconds between hello packets
```
