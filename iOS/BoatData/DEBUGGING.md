# BoatData iOS App Debugging Guide

## Live Console Logs

### Stream logs from connected device

```bash
# All logs from BoatData
idevicesyslog | grep "BoatData"

# Filter for connection events
idevicesyslog | grep -E "BoatData.*(hello|UDP|reconnect|WiFi|SSID)"

# Filter out noise
idevicesyslog | grep "BoatData" | grep -v "UIAccessibility\|Saved agent"
```

**Note**: idevicesyslog only shows live logs. No history when disconnected.

### Log visibility requirements

1. **OSLog privacy**: Messages must use `privacy: .public`:
   ```swift
   logger.info("\(message, privacy: .public)")
   ```

2. **Logging level**: Check `LOGGING_LEVEL` in `LightLog.swift`. If set to `.error`, info/warn won't appear.

## Persistent Logs (survives disconnect)

The app writes logs to `Documents/logs/app.log`.

### Pull logs from device

```bash
# Copy logs to Mac
xcrun devicectl device copy from \
    --device dadPhone \
    --domain-type appDataContainer \
    --domain-identifier foobar.BoatData \
    --source "Documents/logs" \
    --destination /tmp/boatdata_logs

# Read them
cat /tmp/boatdata_logs/app.log
```

### View logs in app

Swipe to the Log View page (after Connection History).

### View logs in Finder

1. Connect iPhone via USB
2. Open Finder → select iPhone
3. Click "Files" tab
4. Expand "BoatData" → `logs` folder

## Device Management

```bash
# List connected devices
xcrun devicectl list devices

# Install app
xcrun devicectl device install app --device dadPhone "/path/to/BoatData.app"

# Launch app
xcrun devicectl device process launch --device dadPhone foobar.BoatData

# Check if app is running
xcrun devicectl device info processes --device dadPhone | grep -i boat
```

## Common Issues

### App won't launch after install
Settings → General → VPN & Device Management → Trust developer certificate

### Logs not appearing
1. Check `privacy: .public` in OSLog calls
2. Check `LOGGING_LEVEL` isn't filtering them
3. Reconnect device and try again

### WiFi join fails with "internal error"
Ensure `com.apple.developer.networking.HotspotConfiguration` entitlement is in `BoatData.entitlements`

### NEHotspotNetwork.fetchCurrent returns nil
- Location permission must be granted
- `com.apple.developer.networking.wifi-info` entitlement required
- Paid Apple Developer account required (not personal team)

## Log Format

```
[HH:mm:ss.SSS] LEVEL: message
```

Levels: D=debug, I=info, W=warn, E=error, C=critical

## Key Log Messages

| Message | Meaning |
|---------|---------|
| `UDP ready to X:101` | UDP connection established |
| `UDP hello #N sent OK` | Hello packet sent to register with server |
| `UDP received N bytes` | Data received from BoatBrain |
| `WiFi SSID: X` | Current WiFi network detected |
| `Attempting to reconnect to BoatBrain` | Trying to join BoatBrain WiFi |
| `Successfully joined BoatBrain` | WiFi connected |
| `Failed to join BoatBrain` | WiFi join failed |
| `Watchdog: No data for Xs` | Connection health check failed |
| `Not on BoatBrain WiFi` | Wrong network, waiting to switch |
