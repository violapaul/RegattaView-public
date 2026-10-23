# RegattaView Technical Documentation

A comprehensive sailing race replay and analysis system that combines 360-degree video footage with synchronized boat sensor data, served through a web interface hosted on AWS S3.

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Data Sources](#data-sources)
3. [Data Processing Pipeline](#data-processing-pipeline)
4. [Algorithms and Signal Processing](#algorithms-and-signal-processing)
5. [Video Processing and Pose Estimation](#video-processing-and-pose-estimation)
6. [Video-Log Synchronization](#video-log-synchronization)
7. [Data Formats and Binary Encodings](#data-formats-and-binary-encodings)
8. [Frontend Architecture](#frontend-architecture)
9. [Calibration System](#calibration-system)
10. [Event Detection Framework](#event-detection-framework)
11. [AWS Infrastructure](#aws-infrastructure)
12. [Coordinate Systems and Conventions](#coordinate-systems-and-conventions)

---

## System Overview

RegattaView is a full-stack system for capturing, processing, and replaying sailing race data from a J/105 sailboat. The system integrates:

- **360-degree video** from an Insta360 camera (dual-fisheye lenses)
- **Sensor telemetry** from boat instruments via NMEA 2000 network
- **IMU-based camera stabilization** computed from embedded gyroscope/accelerometer data
- **Interactive web viewer** with WebGL-rendered 360 video and real-time data overlays

### High-Level Architecture

```
┌─────────────────┐     ┌─────────────────┐
│  Insta360 X3    │     │   BoatBrain     │
│  (360 Video)    │     │   (ESP32 MCU)   │
│  + IMU data     │     │   NMEA 2000     │
└────────┬────────┘     └────────┬────────┘
         │                       │
         │  SD Cards             │  SD Card
         ▼                       ▼
┌─────────────────────────────────────────┐
│         race_upload.py Pipeline         │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  │
│  │ Copy    │→ │ Process │→ │ Upload  │  │
│  │ Videos  │  │ & Sync  │  │ to S3   │  │
│  └─────────┘  └─────────┘  └─────────┘  │
└─────────────────────────────────────────┘
                    │
                    ▼
         ┌──────────────────┐
         │   AWS S3 Bucket  │
         │  regattaanalysis │
         └────────┬─────────┘
                  │
                  ▼
         ┌──────────────────┐
         │   Web Frontend   │
         │   (sailview.js)  │
         └──────────────────┘
```

---

## Data Sources

### 1. Insta360 Camera

**Hardware**: Insta360 X3 (or similar dual-fisheye 360 camera)

**Captured Data**:
- Dual-fisheye video (front/back hemispheres) at 4K/30fps
- Embedded IMU telemetry (1000 Hz):
  - 3-axis accelerometer
  - 3-axis gyroscope
- GPS coordinates (lower rate)

**File Formats**:
- `.insv` - Raw dual-fisheye video (proprietary)
- Single files for lower resolutions, paired `_00_` and `_10_` files for higher resolutions

**Timestamp Sources**:
- Filename: `VID_YYYYMMDD_HHMMSS_00_NNN.insv`
- MP4 metadata: `creation_time` tag
- Embedded telemetry: `first_frame_timestamp`

### 2. BoatBrain Logger

**Hardware**: ESP32-based microcontroller connected to NMEA 2000 network

**Network Devices** (J/105 "Peer Gynt"):
| Device ID | Name | Data Provided |
|-----------|------|---------------|
| 4 | RC42 Rate Compass | Heading, turn rate |
| 5 | Zeus iGPS | Position, COG, SOG |
| 14 | Wind Sensor | AWS, AWA |
| 35 | DST200 Transducer | Depth, water speed |
| 127 | ZG100 Antenna | GPS time, position |
| 128 | ZG100 Compass | Heading, pitch, roll |
| 99 | BoatBrain | System time (10 Hz) |

**NMEA 2000 PGNs Captured**:
```python
# Instrument PGNs (processed to 10Hz parquet)
PGN_NAME = {
    126992: 'System Time',
    127245: 'Rudder',
    127250: 'Vessel Heading',
    127251: 'Rate of Turn',
    127257: 'Attitude (pitch, roll)',
    127258: 'Magnetic Variation',
    128259: 'Speed',
    128267: 'Water Depth',
    129025: 'Position, Rapid Update',
    129026: 'COG & SOG, Rapid Update',
    129029: 'GNSS Position Data',
    130306: 'Wind Data'
}

# AIS PGNs (logged separately, extracted via extract_ais.py)
PGNS_AIS = {
    129039: 'AIS Class B Position Report',
    129809: 'AIS Class B static data (msg 24 Part A)',
    129810: 'AIS Class B static data (msg 24 Part B)',
}
```

**Note**: AIS data is logged to raw BB files but NOT included in the main parquet pipeline. It's extracted separately to `/Users/viola/Data/SailingData/AISLogs/` as JSON, grouped by MMSI. See `AIS_IMPLEMENTATION.md`.

Downstream web payloads are generated from those raw extracts:

- `Analyze/AIS/<event_id>.json` — small per-event AIS slices for `review.html`.
- `Analyze/AISDays/index.json` and `Analyze/AISDays/<YYYY-MM-DD>.json` —
  local sailing-day AIS tracks for `tracks.html`.

Both `Analyze/AIS/` and `Analyze/AISDays/` are generated, ignored by git, staged
by `aws_sync.sh`, and uploaded under `s3://regattaanalysis/www/Analyze/`.

**Log File Format**:
- Raw binary NMEA 2000 frames in Actisense format
- Files named: `YYYY-MM-DDTHH_MM_SSZ.log` (UTC)
- New file every 20 minutes
- Parsed via `canboat` toolchain (`actisense-serial` + `analyzer -json`)

---

## Data Processing Pipeline

### Main Entry Point: `race_upload.py`

```bash
python Scripts/race_upload.py [options]
```

**Pipeline Stages**:

#### Stage 1: Copy from SD Cards

```python
# Insta360 videos
insta360.copy_video_from_card()  # rsync from /Volumes/Insta360/DCIM/Camera01/

# BoatBrain logs  
bb_log.copy_bblogs_from_card()   # rsync from /Volumes/BOATBRAIN3/
```

#### Stage 2: Video Processing (Pipelined)

Three concurrent threads process videos:

```
┌──────────┐    ┌──────────┐    ┌──────────┐
│   Copy   │ -> │ Compress │ -> │  Upload  │
│  Thread  │    │  Thread  │    │  Thread  │
└──────────┘    └──────────┘    └──────────┘
     │              │               │
     │ Queue        │ Queue         │
     ▼              ▼               ▼
  SD Card     Local Storage        AWS S3
```

**Compression**:
- Solo files: HandBrakeCLI with H.265 VideoToolbox encoder
- Paired files: FFmpeg hstack filter + H.265

```bash
# Solo file compression
HandBrakeCLI --input {input} --output {output} --encoder vt_h265 --vb 10000

# Paired file compression (dual fisheye side-by-side)
ffmpeg -i left.insv -i right.insv \
  -filter_complex "[0:v]scale=-2:1920[v0];[1:v]scale=-2:1920[v1];[v0][v1]hstack[v]" \
  -map "[v]" -map 0:a -c:v hevc_videotoolbox -qmax 40 -b:v 10M output.mp4
```

#### Stage 3: Log Processing

```python
# Find log file sequences (20-min chunks)
sequences = bb_log.find_log_file_sequences(BBLOG_DIR)

# Process each sequence
for log_files in sequences:
    # 1. Convert to JSON via canboat
    nmea_messages = read_log_files(log_files)
    
    # 2. Correct time drift between GPS and BoatBrain clock
    time_messages = extract_time_messages(nmea_messages)
    params = find_time_correction(time_messages)
    correct_time_messages(time_messages, **params)
    
    # 3. Assign timestamps to all messages (interpolation)
    time_stamped = assign_time_to_messages(nmea_messages)
    
    # 4. Convert to DataFrame (10 Hz rows)
    df = messages_to_dataframe(time_stamped)
    
    # 5. Save as Parquet
    df.to_parquet(pandas_path)
```

#### Stage 4: Video-Log Synchronization

Correlates video gyroscope with log compass heading to find time offset:

```python
# For each video needing sync
videos = insta360.list_videos_for_sync()
big_df = insta360.read_parquet_files()

for video in videos:
    # Coarse offset from metadata
    video_df, video_start, duration = extract_video_logs(video, big_df)
    
    # Read IMU from video
    imu_times, accel, gyro = read_insta_imu_data(video)
    
    # Fine-grained correlation
    delay, _, _ = sync_video_to_bb(video_df, gyro)
    
    # Extract aligned log slice
    slice_start = video_start + pd.Timedelta(seconds=delay)
    aligned_df = log_extract_slice(big_df, slice_start, slice_start + duration)
    
    # Save binary log
    sailview_log_v1.save_log_to_binary(aligned_df, video + '.log')
```

#### Stage 5: Pose Computation

```python
# For each video with IMU data
for video in videos:
    imu_times, accel, gyro = read_insta_imu_data(video)
    
    # Complementary filter: fuse gyro integration with gravity vector
    q_poses = geo.estimate_pose(
        q_initial=q_identity(),
        accel=accel,
        gyro=gyro,
        accel_gain=0.0005,  # Slow gravity correction
        gyro_gain=0.9       # Fast rotation tracking
    )
    
    write_pose_file(video + '.poses', q_poses)
```

The on-disk and on-the-wire format of `.poses` files (v2 delta-encoded int16
quaternions, brotli transport) is specified separately — see
[POSES_FORMAT.md](POSES_FORMAT.md). Readers still accept the legacy v1
format (uncompressed 100 Hz), so historical videos on S3 do not need to be
regenerated.

---

## Algorithms and Signal Processing

### Time Synchronization: GPS to BoatBrain Clock

**Problem**: BoatBrain clock drifts from GPS time (no RTC). GPS time arrives 1x/second; BoatBrain timestamps at 10x/second.

**Solution**: Least-squares linear fit

```python
def find_time_correction(time_messages):
    # Extract paired GPS/BB timestamps
    gps_times = []
    bb_times = []
    for i in range(len(time_messages) - 2):
        tm1, tm2, tm3 = time_messages[i:i+3]
        if tm1['src'] == BB and tm2['src'] == GPS and tm3['src'] == BB:
            gps_times.extend([tm2['unixtime'], tm2['unixtime']])
            bb_times.extend([tm1['unixtime'], tm3['unixtime']])
    
    # Solve: GPS_time = alpha * BB_time + beta
    params = np.linalg.lstsq(
        np.column_stack((bb_times, np.ones(len(bb_times)))),
        gps_times
    )[0]
    return {'alpha': params[0], 'beta': params[1]}
```

### True Wind Estimation

**Algorithm**: Iterative Kalman-like filter combining apparent wind, boat speed, and heading.

```python
def estimate_true_wind_helper(epsilon, aws, awa, hdg, spd, cog, sog, ...):
    """
    State: [TWD, TWS] (true wind direction and speed)
    
    Forward model:
      AWA_predicted = f(TWD, TWS, HDG, SOG, COG)
    
    Update:
      residual = AWA_measured - AWA_predicted
      TWD += epsilon * d_residual/d_TWD
      TWS += epsilon * d_residual/d_TWS * tws_mult
    
    Adaptive epsilon: increases when residuals are large
    """
```

### Heel-Corrected Apparent Wind

Masthead motion during roll introduces apparent wind error:

```python
def heel_corrected_apparent_wind(df):
    # Smooth heel signal (Butterworth filter, 0.3 Hz cutoff)
    sheel = smooth_angle(butter_coeff, df.zg100_roll)
    
    # Derivative = roll rate
    heel_rate = SAMPLE_RATE * np.diff(sheel)
    
    # Masthead velocity = MAST_HEIGHT * radians(heel_rate)
    mast_vel = MAST_HEIGHT * np.radians(heel_rate)
    
    # Decompose apparent wind to boat coordinates
    aw_n = aws * cos(awa)  # Forward component
    aw_e = aws * sin(awa)  # Sideways component
    
    # Correct sideways component
    caw_e = aw_e - mast_vel
    
    # Reconstruct corrected AWA/AWS
    df['cawa'] = arctan2(caw_e, aw_n)
    df['caws'] = sqrt(aw_n**2 + caw_e**2)
```

### Exponential Filter (Non-linear Smoothing)

Adaptive filter that accelerates response when error is large:

```python
@jit(nopython=True)
def exponential_filter(sig, alpha, max_error):
    beta = beta0 = 1 - alpha
    res = np.zeros(len(sig))
    res[0] = sig[0]
    
    for i in range(1, len(sig)):
        res[i] = (1 - beta) * res[i-1] + beta * sig[i]
        
        if abs(res[i] - sig[i]) > max_error:
            beta = min(1.5 * beta, 0.5)  # Speed up
        else:
            beta = (beta + beta0) / 2    # Slow down
    
    return res
```

---

## Video Processing and Pose Estimation

### IMU Data Extraction

The Insta360 embeds IMU data in the video file. A custom `exiftool` plugin extracts binary data:

```python
def read_binary_imu_data(file_path):
    """
    Binary format (20 bytes per record):
    - uint64: timestamp (microseconds)
    - int16[6]: accel_x, accel_y, accel_z, gyro_x, gyro_y, gyro_z
    
    Scaling: (raw - 0x8000) / 1000.0
    """
    record_dtype = np.dtype([
        ('timestamp', 'u8'),
        ('data', '6H')  # 6 unsigned shorts
    ])
    data = np.fromfile(file_path, dtype=record_dtype)
    
    timestamps = data['timestamp'] / 1000.0  # to milliseconds
    scaled = (data['data'].astype(float) - 0x8000) / 1000.0
    
    accel = scaled[:, 0:3]
    gyro = scaled[:, 3:6]
    return timestamps, accel, gyro
```

### Complementary Filter for Pose Estimation

Fuses high-frequency gyroscope data with low-frequency gravity reference:

```python
def estimate_pose(initial_q, accel, gyro, accel_gain, gyro_gain):
    """
    Quaternion-based orientation estimation.
    
    1. Integrate gyroscope (fast response, drifts)
    2. Correct toward gravity vector (slow, stable)
    3. Correct toward camera forward (keeps view aligned with boat)
    """
    current_q = initial_q
    
    for i in range(len(accel)):
        # Gyro integration (pre-multiply = rotate in camera frame)
        gyro_twist = delta_t * gyro_gain * gyro[i]
        current_q = q_compose(current_q, twist_q(gyro_twist))
        
        # Gravity correction (post-multiply = rotate in world frame)
        down_measured = normalize(accel[i])
        down_estimated = q_mat(current_q) @ [1, 0, 0]
        gravity_error = accel_gain * cross(down_measured, down_estimated)
        current_q = q_compose(twist_q(gravity_error), current_q)
        
        # Forward alignment (keeps view facing boat direction)
        backward_estimated = q_mat(current_q) @ [0, 1, 0]
        bw_error = -0.5 * accel_gain * cross([0, 1, 0], backward_estimated)
        bw_angle = dot(bw_error, down_estimated)
        current_q = q_compose(axis_angle_q(down_estimated, bw_angle), current_q)
        
        poses[i] = current_q
    
    return poses
```

### Pose File Format

```python
def write_pose_file(filename, poses, msec_per_sample=10, multiplier=32767):
    """
    Binary format:
    - Header: 4 x uint64 (rows, cols, msec_per_sample, multiplier)
    - Data: int16[rows, 4] quaternions (scaled by multiplier)
    
    Quaternion ordering: [y, -x, z, w] (WebGL convention)
    """
    poses_scaled = (poses * multiplier).astype(np.int16)
    # Permute to WebGL coordinate system
    poses_webgl = poses_scaled[:, [2, 0, 1, 3]] * [1, -1, 1, 1]
    
    with open(filename, 'wb') as f:
        header = np.array([len(poses), 4, msec_per_sample, multiplier], dtype=np.uint64)
        f.write(header.tobytes())
        f.write(poses_webgl.tobytes())
```

---

## Video-Log Synchronization

### The Synchronization Problem

- Video timestamp comes from camera clock (can be wrong by minutes/hours)
- Log timestamp comes from GPS-corrected BoatBrain time
- Need sub-second alignment for meaningful data overlay

### Cross-Correlation Solution

Both video gyroscope and log compass measure rotation. Cross-correlate to find offset:

```python
def sync_video_to_bb(video_df, gyro, max_delay=None):
    """
    Correlate rate-of-change of compass heading with gyroscope data.
    
    1. Compute dHDG/dt from log data (10 Hz)
    2. Downsample gyro from 1000 Hz to 10 Hz
    3. Normalize both signals (zero mean, unit variance)
    4. Cross-correlate to find best alignment
    """
    # Derivative of heading (handle 360-degree wraparound)
    dhdg = np.diff(video_df.hdg)
    dhdg[dhdg > 180] -= 360
    dhdg[dhdg < -180] += 360
    
    # Downsample gyro (vertical axis rotation)
    sgg = gyro[::100, 0]  # Every 100th sample
    
    # Normalize
    nhdg = (dhdg - np.mean(dhdg)) / np.std(dhdg)
    ngg = (sgg - np.mean(sgg)) / np.std(sgg)
    
    # Cross-correlation
    match = np.correlate(nhdg, ngg, mode='valid')
    delay = np.argmax(match) / 10.0  # Convert samples to seconds
    
    return delay
```

### Staged Search Strategy

For large clock errors, search progressively wider windows:

```python
SEARCH_STAGES = [60, 1800, 7200, 21600]  # 1min, 30min, 2hr, 6hr

def align_video(video_name, all_videos, big_df):
    for max_offset in SEARCH_STAGES:
        result = attempt_align(video_name, big_df, max_offset)
        if result.success:
            return result
    return AlignResult(success=False)
```

---

## Data Formats and Binary Encodings

### Log File Format (`.log` v1 and `.slog` v2)

Two formats coexist during the v2 rollout. The v1 `.log` is the currently-
deployed format; the v2 `.slog` is a parallel, feature-flagged replacement
with a smaller schema, zero-phase smoothing, and per-session constants
lifted out of the payload. Full spec, concerns, and migration notes live in
**`SLOG_FORMAT.md`** (repo root). Below is a summary of the v1 layout for
anyone tracing the legacy path.

### Log File Format (`.log`, v1)

Column-major binary format for compact storage:

```
┌─────────────────────────────────────────┐
│ Header (16 bytes)                       │
│   int32: rows                           │
│   int32: cols (21)                      │
│   int32: msec_per_sample (100)          │
│   int32: multiplier (65535)             │
├─────────────────────────────────────────┤
│ Max Values (cols × 8 bytes)             │
│   float64[21]: column maxima            │
├─────────────────────────────────────────┤
│ Min Values (cols × 8 bytes)             │
│   float64[21]: column minima            │
├─────────────────────────────────────────┤
│ Data (rows × cols × 2 bytes)            │
│   uint16[cols][rows]: normalized data   │
│   Column-major order (Fortran)          │
│   Value = (raw / multiplier) * range    │
└─────────────────────────────────────────┘
```

**Column Order** (21 channels at 10 Hz):
```python
COLUMN_LIST = [
    'row_seconds',   # 0  - Time since video start
    'spd',           # 1  - Boat speed (knots)
    'sog',           # 2  - Speed over ground (knots)
    'latitude',      # 3  - GPS latitude
    'longitude',     # 4  - GPS longitude
    'turn_rate',     # 5  - Rate of turn (deg/s)
    'rudder',        # 6  - Rudder angle
    'depth',         # 7  - Water depth
    'altitude',      # 8  - GPS altitude
    'zg100_pitch',   # 9  - Pitch angle
    'zg100_roll',    # 10 - Heel angle
    'awa',           # 11 - Apparent wind angle
    'aws',           # 12 - Apparent wind speed (knots)
    'twd',           # 13 - True wind direction
    'tws',           # 14 - True wind speed (knots)
    'twa',           # 15 - True wind angle
    'hdg',           # 16 - Compass heading
    'cog',           # 17 - Course over ground
    'map_lon',       # 18 - Projected X (meters from origin)
    'map_lat',       # 19 - Projected Y (meters from origin)
    'row_times'      # 20 - Unix timestamp (ms)
]
```

### Pose File Format (`.poses`)

```
┌─────────────────────────────────────────┐
│ Header (32 bytes)                       │
│   uint64: rows                          │
│   uint64: cols (4)                      │
│   uint64: msec_per_sample (10)          │
│   uint64: multiplier (32767)            │
├─────────────────────────────────────────┤
│ Data (rows × 4 × 2 bytes)               │
│   int16[rows][4]: quaternions           │
│   Order: [y, -x, z, w] (WebGL)          │
│   Value = raw / multiplier              │
└─────────────────────────────────────────┘
```

### `videos.json` Index

```json
{
    "videos": [
        {
            "id": 0,
            "SourceFile": "V0090009.mp4",
            "PoseFile": "V0090009.poses",
            "LogFile": "V0090009.log",
            "CreateDate": "2020:04:04 10:01:10",
            "projection": "equirect"
        },
        {
            "id": 1292,
            "SourceFile": "VID_20260315_120000_00_001.mp4",
            "PoseFile": "VID_20260315_120000_00_001.poses",
            "LogFile": "VID_20260315_120000_00_001.log",
            "CreateDate": "2026/03/15 12:00:00"
        }
    ],
    "columns": {
        "row_seconds": 0,
        "spd": 1,
        ...
    }
}
```

Entries are sorted by `CreateDate` ascending so the dropdown lists the
oldest first. Two `CreateDate` formats coexist in the corpus: VIRB-era
clips use colons (`YYYY:MM:DD HH:MM:SS`), Insta360-era clips use slashes
(`YYYY/MM/DD HH:MM:SS`); both compare correctly when normalized to dashes
in the date portion.

The optional `projection` field is `"equirect"` for VIRB-era clips
(2020-2023) and absent (defaults to `fisheye`) for Insta360 clips. The
viewer compiles both shader programs at startup and switches per video —
see `docs/VIRB_MIGRATION.md`.

---

## Frontend Architecture

### Pages

| Page | Entry Point | Purpose |
|------|-------------|---------|
| `sailview.html` | `sailview.js` | 360 video player with instrument overlay |
| `review.html` | `review.js` | Race Start Review UI for validating detected starts |
| `tracks.html` | `tracks.js` | AIS sailing-day index and per-day track viewer |
| `index.html` | `index.js` | Race catalog with expandable cards |
| `graphs.html` | `graphs.js` | Bokeh time-series plots |
| `calibrate.html` | `calibrate.js` | Dual-fisheye calibration tuner |
| `login.html` | `login.js` | Authentication gate |

### 360 Video Rendering Pipeline

```
┌─────────────────────────────────────────────────────────┐
│                   WebGL Render Loop                      │
├─────────────────────────────────────────────────────────┤
│  1. Upload video frame as texture                       │
│     └─ twgl.setTextureFromElement(gl, videoTexture)     │
│                                                         │
│  2. Get current pose quaternion                         │
│     └─ pose = getPose(video.currentTime * 1000)         │
│                                                         │
│  3. Compute inverse for stabilization                   │
│     └─ viewQuat = state.stabilize ? quat.invert(pose)   │
│                                   : identity            │
│                                                         │
│  4. Set shader uniforms                                 │
│     └─ u_viewQuat, u_fov, u_calibration                 │
│                                                         │
│  5. Draw fullscreen quad                                │
│     └─ Fragment shader reprojects each pixel            │
└─────────────────────────────────────────────────────────┘
```

### GLSL Fisheye Shader (Simplified)

```glsl
// For each output pixel:
// 1. Compute view ray in camera space
vec3 ray = normalize(vec3(
    tan(u_fov/2) * (uv.x - 0.5) * 2.0,
    tan(u_fov/2) * (uv.y - 0.5) * 2.0,
    -1.0
));

// 2. Apply pose quaternion
ray = rotate_by_quat(ray, u_viewQuat);

// 3. Determine which fisheye lens sees this ray
float front_weight = seam_blend(ray);
float back_weight = 1.0 - front_weight;

// 4. Project ray to fisheye UV coordinates
vec2 front_uv = fisheye_project(ray, u_front_center, u_front_scale);
vec2 back_uv = fisheye_project(-ray, u_back_center, u_back_scale);

// 5. Sample and blend
vec4 front_color = texture2D(u_video, front_uv);
vec4 back_color = texture2D(u_video, back_uv);
gl_FragColor = front_weight * front_color + back_weight * back_color;
```

### Data Overlay System

```javascript
// Instrument readout update (every frame)
function updateDataFields(timeMS) {
    for (const field of dataFields) {
        const value = getLog(timeMS, field.channel);
        field.span.textContent = formatValue(field.channel, value);
        
        // Update sparkline (trailing window)
        const history = getLogHistory(timeMS, field.channel, state.sparkLength);
        sparkline.update(field.sparkline, history);
    }
}

// Boat track map (Leaflet-style SVG)
class BoatView {
    constructor(container, lat, lon, hdg) {
        this.track = createTrackPath(lat, lon);
        this.boat = createBoatOutline();  // J/105 hull shape
    }
    
    update(timeMS) {
        const row = Math.floor(timeMS / 100);
        const pos = [this.lat[row], this.lon[row]];
        const heading = this.hdg[row];
        
        this.boat.setPosition(pos);
        this.boat.setRotation(heading);
    }
}
```

### Review UI Architecture

The start-review flow is a second frontend on top of the same video/log stack.
It reuses `viewer360.js`, `shared.js`, and `sparkline.js`, but changes the UX
from freeform replay to a fixed start-analysis window:

- load `events_index.json` for detected event candidates and operator annotations
- on selection: lazy-load `Events/<id>.json` (heavy detail) and
  `Sessions/<basename>.json` (decimated session-wide track for the detail-map
  backdrop and wind-up overlay)
- render a detail pane with:
  - synchronized 360 video
  - start-relative scrubber (`-300s` to `+600s`)
  - Leaflet track map with pre-start and post-start highlighting
  - chart stack (`AWA`, `HDG`, `SPD`, `VMG`, `TWD`)
  - compact sailview-style overlay (`AWA`, `SPD`, `HDG`, `TWS`, wind-up mini-map)

In development, webpack serves `review.html` on port `9000` and mirrors the
prod S3 URL layout directly: `/Analyze/*` is served from the repo's `Analyze/`
directory, and `/Comments/*` is served from `Analyze/Comments/`. The dev
middleware accepts PUTs to `/Analyze/events_edits.json`, `/Comments/index.json`,
and `/Comments/<alias>/comments.json` and writes them to disk. There is no
separate backing process.

### Event Notes, Status, and Trim (editable overlay)

Detector output in `Analyze/events_index.json` and `Analyze/Events/<id>.json`
is treated as read-only. Per-event edits — notes, status (`confirmed` /
`pending` / `discarded`), and trim bounds — live in a small overlay at
`Analyze/events_edits.json` that is merged onto the event records at load time.

- UI element: `#detail-notes` textarea in `Src/src/html/review.html`
- Frontend logic: `renderNotes()` / `commitNotes()` / `upsertEdit()` in
  `Src/src/js/review.js`
- Stored path: `editsData.edits[event_id]` in `Analyze/events_edits.json`,
  written via `shared.putDataJSON('Analyze/events_edits.json', ...)` (dev:
  webpack middleware to disk; prod: signed S3 PUT).
- Merge: on boot, `review.js` fetches the index and edits, then
  `applyEditsToStarts()` projects status/notes/trim onto the event records
  as if they had been inlined.
- The race pipeline never overwrites `events_edits.json`; it only regenerates
  `events_index.json`, `Events/`, and `Sessions/`.

Behavior:

1. Selecting a start loads the merged view into the textarea.
2. Typing schedules an autosave after about 1.5 seconds.
3. Leaving the field triggers an immediate save.
4. Save writes only the small edits overlay, keyed by `start_id`.

Initial note values are seeded by `Analyze/find_starts.py`, which writes a
detector summary such as `AWA: pre=... post=..., hdg_std=...`. Reviewer input
overrides that via the overlay.

### User Comments (separate from start notes)

Free-form user comments, pinned either to a video moment (sailview) or to a
start (review). Identity is a free-form alias, not the shared login.

- Storage: `Comments/index.json` (alias registry) and
  `Comments/<alias>/comments.json` (per-alias file).
- Alias cookie: `comment_alias`. Allowed charset `[A-Za-z0-9_-]{1,30}`.
- Client API: `shared.loadAllComments`, `shared.ensureAliasRegistered`,
  `shared.appendMyComment` in `Src/src/js/shared.js`.
- Review UI: `Starts` / `Comments` tabs in `review.html`, plus a per-start
  composer in the detail pane (`#detail-comment-composer`).
- Sailview UI: `K` key → per-moment comment prompt
  (`postSailviewComment` in `Src/src/js/sailview.js`).
- `Comments/` is client-write-only. It lives only on S3 in prod; `aws_sync.sh`
  does NOT sync it. In dev it lives under `Analyze/Comments/` and the webpack
  PUT middleware persists writes to disk.

---

## Calibration System

### Purpose

The Insta360 produces dual-fisheye video that must be stitched. The calibration system determines:

- **Lens centers**: UV position of each fisheye optical axis
- **Lens scales**: Mapping from UV to fisheye angle
- **Euler rotations**: Fine alignment between lenses
- **Distortion**: Radial distortion coefficients
- **Seam parameters**: Blend zone width and position

### Calibration Preset Schema

```json
{
    "schemaVersion": 1,
    "name": "Human readable name",
    "calibration": {
        "fov": 180.0,
        "front": {
            "center": [0.25, 0.5],
            "scale": [0.5, 1.0],
            "euler": [0.0, 0.0, 0.0],
            "distortion": [0.0, 0.0]
        },
        "back": {
            "center": [0.75, 0.5],
            "scale": [-0.5, 1.0],
            "euler": [0.0, 0.0, 0.0],
            "distortion": [0.0, 0.0]
        },
        "projection": {
            "aperture": 0.296,
            "viewCorrection": -0.2
        },
        "seam": {
            "centerYaw": 0.0,
            "width": 0.1,
            "feather": 1.0
        }
    }
}
```

### Calibration Workflows

#### 1. Full Calibration (CharuCo Target)

```bash
# Generate target
python Calibration/scripts/generate_charuco_target.py --squares-x 10 --squares-y 7

# Detect corners in video
python Calibration/scripts/detect_charuco.py --input video.mp4 --layout dual_half

# Fit calibration
python Calibration/scripts/fit_dual_fisheye.py --observations observations.json --output preset.json
```

#### 2. Seam-Only Refinement (No Target)

```bash
# Photometric optimization (minimizes color discontinuity)
python Calibration/scripts/optimize_seam.py --input-dir frames/ --seed-preset current.json

# Feature-based verification (ORB matching)
python Calibration/scripts/match_overlap_features.py --input-dir frames/ --seed-preset current.json
```

---

## Event Detection Framework

### Architecture

Pluggable detector system for automated race analysis:

```python
class EventDetector(ABC):
    @abstractmethod
    def detect(self, video_name, log_data, columns, date) -> List[Dict]:
        """Returns list of detected events."""
        pass

class HeadingManeuverDetector(EventDetector):
    """Detects tacks and gybes from heading changes."""
    
    def detect(self, video_name, log_data, columns, date):
        hdg = log_data['hdg']
        
        # Find steady -> unsteady -> steady transitions
        maneuvers = self._find_maneuvers(hdg)
        
        events = []
        for sample_idx, heading_change in maneuvers:
            awa = log_data['awa'][sample_idx]
            event_type = 'tack' if abs(awa) < 90 else 'gybe'
            
            events.append({
                'id': f'auto-{video_name}-{sample_idx}',
                'video': video_name,
                'time': sample_idx / 10.0,
                'type': event_type,
                'comment': f'{heading_change:+.0f} deg'
            })
        
        return events
```

### Race Start Review Outputs

The review pipeline currently materializes generated event artifacts under
`Analyze/` plus one client-written overlay.

#### `events_index.json` and `Events/<id>.json`

Generated by `Analyze/find_starts.py`. The index contains small event summaries
for eager list rendering; each `Events/<id>.json` file contains the heavier
review payload: tracks, chart series, maneuver annotations, detector features,
and inferred start-line data.

Reviewer status, notes, and trim live separately in `Analyze/events_edits.json`
so detector regeneration does not overwrite human annotations.

To pull reviewer notes locally:

```bash
python - <<'PY'
import json
from pathlib import Path

events = json.loads(Path("Analyze/events_index.json").read_text())["events"]
edits_path = Path("Analyze/events_edits.json")
edits = (json.loads(edits_path.read_text()).get("edits", {})
         if edits_path.exists() else {})
for event in events:
    e = edits.get(event["id"], {})
    note = (e.get("notes") or event.get("notes") or "").strip()
    if note:
        status = e.get("status", event.get("status", "pending"))
        print(f'{event["id"]}\t{status}\t{note}')
PY
```

To pull user comments, iterate `Comments/index.json` and read each
`Comments/<alias>/comments.json` (via `aws s3 cp` in prod, or directly from
`Analyze/Comments/` in dev).

If you only want human-entered notes, exclude the default detector-generated
seeds that begin with `AWA: pre=`.

#### `Sessions/<basename>.json`

Generated by `Analyze/find_starts.py` alongside event detection. One file per
parquet log (basename = parquet stem, e.g. `2026-02-28T09_32.json`). Replaces
the previous monolithic `tracks.json`; the per-session split lets `review.js`
lazy-load only the session that contains the selected event instead of paying
for every session at boot.

Used by:

- the detail-map full-session backdrop (faded blue polyline)
- the wind-up overlay for the in-video mini-map (rotates `track_m` into a
  wind-relative frame around the start position)

Each record carries both geographic (`track`) and projected (`track_m`)
coordinates plus timestamps, so the frontend can align the track to event
times without reopening the source parquet file.

### Detection Algorithm: Maneuvers

```python
def _find_maneuvers(self, heading):
    # 1. Unwrap heading to handle 360-degree crossings
    unwrapped = np.degrees(np.unwrap(np.radians(heading)))
    
    # 2. Compute rolling standard deviation
    rolling_std = pd.Series(unwrapped).rolling(window=30*10).std()
    is_steady = rolling_std < 12  # degrees
    
    # 3. Compute smoothed heading change over window
    smoothed = pd.Series(unwrapped).rolling(window=30*10).mean()
    delta = smoothed[1.5*30*10:] - smoothed[:-1.5*30*10]
    
    # 4. Maneuver = was steady, now turning, will be steady, large delta
    is_maneuver = was_steady & ~is_steady & will_steady & (abs(delta) > 60)
    
    # 5. Cluster and return peak of each cluster
    return cluster_peaks(is_maneuver, delta)
```

---

## AWS Infrastructure

### S3 Bucket Structure

```
s3://regattaanalysis/
├── auth/
│   ├── allowlist.json            # Google-email allowlist (Lambda)
│   └── google-jwks.json          # cached Google JWKS for RS256 verify
└── www/
    ├── sailview/                 # webpack build output (live pages)
    │   ├── index.html, sailview.html, review.html, graphs.html,
    │   │   login.html, label.html, calibrate.html, playlists.html,
    │   │   boatbrain.html, favicon.svg
    │   ├── js/, css/, assets/    # bundles + static assets
    ├── Insta360/                 # canonical per-clip artifacts
    │   ├── videos.json           # live video registry
    │   ├── VID_*.mp4             # compressed clips
    │   ├── VID_*.poses           # PS02 v2 quaternions (brotli)
    │   ├── VID_*.log             # v1 uint16 instrument log
    │   └── VID_*.slog            # gzip'd structured log (Insta360-era only)
    ├── Analyze/                  # review-page data (find_starts.py output)
    │   ├── events_index.json
    │   ├── Events/<id>.json
    │   ├── Sessions/<basename>.json
    │   └── events_edits.json     # client-write-only, never overwritten
    ├── Comments/                 # client-write-only review comments
    │   ├── index.json
    │   └── <alias>/comments.json
    ├── FullLogs/                 # legacy `.bin` per-session logs (graphs.html)
    ├── Logs/
    │   └── video_poses_logs.json # legacy hand-curated race manifest
    │                             # (frozen 2023-10-31; index.html + graphs.html)
    └── SeattleTiles/             # NOAA chart tiles (chart.js)
```

The canonical bucket inventory and current-vs-deletable status of each
prefix lives in `AWS_SECURITY_STATUS.md` § 2 + § 7.8. As of 2026-05-02,
the migrated-out legacy prefixes (`Virb/Small/`, `Poses/`, `Logs/*.bin`)
have been deleted; the listing above is the post-cleanup layout.

### Lambda Function: `montlake_upload`

Mints S3 presigned URLs after auth. Pure-Python (no vendored deps),
running on ARM64 / 1024 MB. The full surface is in
`Lambda/src/lambda_function.py`; deploy and runtime config in
`Lambda/README.md` and `DEPLOY-AUTH.md`.

Auth has three accepted credentials, checked in this order:

1. **`session_token`** — HS256 JWT minted by the Lambda after the user
   signs in once with Google. 30-day TTL, verified with stdlib
   `hmac.compare_digest` (microseconds). This is what every Lambda call
   from a logged-in browser sends.
2. **`id_token`** — Google ID token. Used only on the initial sign-in
   handshake (login.js → TEST) or after the session token expires.
   Verified with ~30 lines of pure-Python RS256 against Google's JWKS,
   cached at `s3://regattaanalysis/auth/google-jwks.json`. On a verified
   token the Lambda mints a fresh `session_token` and returns it in the
   response so the frontend switches over.
3. **`username` + `password`** — legacy iOS Uploader credential. Disabled
   unless the Lambda env sets `LEGACY_UPLOADER_PASSWORD` (unset in prod);
   when enabled it may only PUT timestamped objects into `montlakeupload`.
   The old hardcoded password is retired and rejected.

Authorization is the same for all paths: the resolved email/identity is
checked against an allowlist in `s3://regattaanalysis/auth/allowlist.json`
(5-min cached). Removed users are locked out within that window
regardless of session-token TTL.

```python
# lambda_function.py (sketch)
def lambda_handler(event, context):
    body = parse_body(event)
    s3 = boto3.client('s3')
    identity, ttl, new_session, err = _authenticate(body, s3)
    if not identity:
        return {'statusCode': 401 or 403, 'body': {'reason': err}}
    url = utils.generate_presigned_url(s3, body['method'], ...)
    return {'statusCode': 200,
            'body': {'url': url,
                     # Migration: when client presented an id_token, hand
                     # back a session_token so future calls use HMAC.
                     'session_token': new_session if new_session else None}}
```

### Client-Side URL Signing

```javascript
// shared.js (sketch)
export function signAWSUrl(filepath) {
    const query = {method: 'GET', bucket: 'regattaanalysis',
                   filename: 'www/' + filepath};
    attachCredentials(query);   // prefers session_token, falls back to id_token
    const res = lambdaRequest(query);
    // Any 200 response may include a fresh session_token; harvestSessionToken
    // (called inside lambdaRequest) plucks it into localStorage and removes
    // the now-redundant Google ID token.
    return res.url;
}
```

`attachCredentials` looks for `localStorage['regatta_session_token']`
first (long-lived, HMAC-verified) and falls back to
`localStorage['google_id_token']` only on the initial sign-in handshake.
After the first successful Lambda call following sign-in, the Google
token is dropped and the session token is the only credential in play.

---

## Coordinate Systems and Conventions

### Geographic

- **Reference point**: 47.687307°N, -122.438644°W (Shilshole Bay, Seattle)
- **Projection**: Transverse Mercator centered on reference
- **Units**: Meters north/east from reference

```python
PROJ4 = f"+proj=tmerc +lat_0={LAT} +lon_0={LON} +k_0=0.9996 +datum=WGS84 +units=m"
```

### Camera/IMU

- **Insta360 IMU axes**: 
  - X: Right
  - Y: Down  
  - Z: Forward (out of front lens)

- **WebGL convention**:
  - X: Right
  - Y: Up
  - Z: Backward

- **Transform applied**:
  ```python
  accel_webgl = accel_imu * [1, -1, 1]
  gyro_webgl = gyro_imu * [1, -1, 1]
  ```

### Sailing Angles

- **Compass heading**: 0-360°, 0=North, 90=East
- **Apparent Wind Angle (AWA)**: -180 to +180°, positive = starboard
- **True Wind Angle (TWA)**: -180 to +180°, positive = starboard
- **Course Over Ground (COG)**: 0-360°, 0=North

### Race Marks (Seattle)

```python
STYC_MARKS = {
    'n': (47°41.064'N, 122°24.679'W),
    'b': (47°40.285'N, 122°25.342'W),
    'r': (47°44.387'N, 122°22.944'W),
    'u': (47°45.676'N, 122°23.833'W),
    'm': (47°41.783'N, 122°24.538'W),
    ...
}
```

---

## Dependencies

### Python (conda env: `sailing`)

| Package | Purpose |
|---------|---------|
| `pandas`, `numpy` | Data manipulation |
| `scipy` | Signal processing filters |
| `numba` | JIT compilation for hot loops |
| `pyproj` | Geographic projection |
| `pytz`, `arrow` | Timezone handling |
| `telemetry_parser` | Insta360 metadata extraction |
| `opencv-python` | Calibration target detection |

### JavaScript

| Package | Purpose |
|---------|---------|
| `twgl.js` | WebGL helper library |
| `gl-matrix` | Quaternion/matrix math |
| `mithril` | UI components |
| `@bokeh/bokehjs` | Interactive charts |

### External Tools

| Tool | Purpose |
|------|---------|
| `HandBrakeCLI` | Video compression (H.265) |
| `FFmpeg` | Video processing/stitching |
| `exiftool` | Video metadata extraction |
| `s5cmd` | Fast S3 sync |
| `canboat` | NMEA 2000 parsing |

---

## Quick Reference

### Run Full Pipeline
```bash
python Scripts/race_upload.py
```

### Process Logs Only
```bash
python Scripts/race_upload.py --no-video
```

### Local Development
```bash
cd Src && npm run start  # http://localhost:9000
```

### Deploy to AWS
```bash
cd Src && npm run build
cd .. && ./aws_sync.sh
```

### Calibrate Camera
```bash
# Full calibration with target
python Calibration/scripts/detect_charuco.py --input video.mp4 --output obs.json
python Calibration/scripts/fit_dual_fisheye.py --observations obs.json --output preset.json

# Seam refinement without target
python Calibration/scripts/optimize_seam.py --input video.mp4 --output preset.json
```
