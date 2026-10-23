# Sailing Data Analysis Library

A comprehensive Python library for analyzing sailing performance data from NMEA 2000 marine instrumentation systems. This library processes data from the "Boat Brain" system and GPS devices, providing tools for race analysis, visualization, and performance optimization.

## Overview

This library collection provides a complete data pipeline for sailing analysis:

1. **Data Acquisition**: Reading NMEA 2000 data from USB drives (Boat Brain logs)
2. **Data Processing**: Converting raw sensor data to standardized formats
3. **Signal Processing**: Filtering, smoothing, and computing derived metrics (true wind, etc.)
4. **Visualization**: Creating interactive charts, plots, and race track overlays
5. **Race Analysis**: Performance metrics, sail trim analysis, and race comparison
6. **Video Integration**: Synchronizing action camera footage with sensor data

## Core Modules

### Data Ingestion & Processing

#### `bb_log.py` (437 lines)
Processes Boat Brain log files - the primary data source from onboard NMEA 2000 instrumentation.

**Key Features:**
- Combines multiple 20-minute log files into continuous sessions
- Time synchronization between GPS and Boat Brain clocks (corrects for drift)
- Converts NMEA 2000 messages to structured JSON records
- Exports to Pandas DataFrames with 0.1s time steps
- Handles PGN filtering and message validation

**Main Functions:**
- `find_log_file_sequences()` - Groups sequential log files
- `compute_pandas_log()` - Complete pipeline from raw logs to DataFrame
- `extract_time_messages()` - Extracts GPS and system time
- `find_time_correction()` - Computes clock drift parameters

**Usage:**
```python
from Libraries import bb_log
sequences = bb_log.find_log_file_sequences("/Volumes/BOATBRAIN3/")
df = bb_log.compute_pandas_log(sequences[0])
```

#### `canboat.py` (337 lines)
Low-level routines for processing Canboat JSON files from NMEA 2000 networks.

**Key Features:**
- Parses Actisense NMEA 2000 binary format (via canboat analyzer)
- Handles asynchronous sensor messages
- Field name deconfliction (e.g., true vs. apparent wind)
- Device-specific prefixing (Zeus GPS, ZG100 Compass, etc.)
- GPX file generation from GPS data

**Data Flow:**
```
Raw NMEA 2000 → Actisense Serial → Analyzer JSON → Structured Records → Pandas DataFrame
```

**Main PGNs Processed:**
- 126992: System Time
- 127250: Vessel Heading
- 128259: Speed
- 129029: GNSS Position Data
- 130306: Wind Data

#### `process.py` (611 lines)
Generic signal processing and data transformation tools for boat sensor data.

**Key Features:**
- Angular signal processing (handles 360° wraparound)
- Butterworth and Chebyshev filters
- Exponential smoothing with adaptive gain
- True wind calculation from apparent wind
- GPS coordinate transformations
- Log file management and compression

**Signal Processing:**
- `smooth()` - Apply IIR filters (causal or non-causal)
- `smooth_angle()` - Filter angular signals with unwrap
- `exponential_filter()` - Adaptive non-linear filter
- `unwrap_d()` - Remove 360° discontinuities

**Angle Utilities:**
- `sign_angle()` - Convert to ±180° format
- `compass_angle()` - Convert to 0-360° format
- `angle_diff_d()` - Signed difference between angles

### Race Data Analysis

#### `race_logs.py` (473 lines)
High-level interface for loading, processing, and analyzing race data.

**Key Features:**
- Reads pickled/parquet log files with metadata
- Sensor data smoothing and filtering
- True wind estimation from apparent wind + boat motion
- Heel-corrected wind measurements
- GPS outlier detection and correction
- Automatic time synchronization

**True Wind Calculation:**
The library implements a sophisticated true wind estimator that:
1. Corrects apparent wind for masthead motion due to heel
2. Uses a feedback loop to iteratively refine TWS/TWD
3. Accounts for leeway and course over ground
4. Applies exponential smoothing for stability

**Main Functions:**
- `read_log_file()` - Load and process a single race log
- `read_logs()` - Load multiple races with metadata
- `process_sensors()` - Apply filters and compute derived signals
- `estimate_true_wind()` - Compute true wind from apparent wind

#### `metadata.py` (502 lines)
Manages race metadata including conditions, crew, settings, and results.

**Key Features:**
- YAML-based metadata storage
- Google Sheets integration for data entry
- Automatic log file association
- Race summaries and reports
- Shroud tension tracking
- Sail settings documentation

**Metadata Fields:**
- Date, title, description
- Crew and conditions
- Wind/wave information
- Performance notes and learnings
- Links to RaceQs data
- Boat configuration (shrouds, sails)

#### `cloud_activity.py`
Builds the daily/weekly cloud activity report: usage + security review. Born as
the AWS cost-attribution helper after the 2026 S3 cost anomaly cleanup; now also
covers sign-ins, auth failures, signing-policy WOULD_DENY hits, allowlist
state, and the CloudFront distribution killswitch flag. Combines Cost Explorer,
S3 server access logs, CloudFront access logs, Lambda `PRESIGN` events, Lambda
auth/security warnings (`SIGNIN` / `AUTH_FAIL` / `WOULD_DENY` /
`Allowlist denied`), and a direct read of `s3://regattaanalysis/auth/allowlist.json`.

**Important semantics:**
- Report windows are `America/Los_Angeles` calendar days; the module fetches
  all required UTC log-object days and then filters/groups by local day.
- Human users exclude service identities in `SERVICE_IDENTITIES` (currently
  `peergynt`, the legacy iOS uploader credential).
- `CloudFront delivered` is the viewer-facing delivery metric.
- `S3 origin/cache-fill` is backend traffic for cache misses, direct signed
  fallback, poses/logs, and tooling; it should not be added to CloudFront bytes
  as additional viewing.
- `USW2-Retrieval-ZIA` and `REST.COPY.OBJECT` callouts generally indicate
  operational retrieval/copy work, not normal video watching.
- The on-disk parquet cache directory `~/Data/AwsUsage/` is unchanged across
  the module rename so historical cache hits keep working.

Command-line driver: `Scripts/cloud_activity_report.py`. SES email helper:
`cloud_activity_ses.py`.

### Visualization

#### `chart.py` (462 lines)
Creates georeferenced charts and interactive race track visualizations.

**Key Features:**
- NOAA chart integration via GDAL/MBTILES
- Transverse Mercator projection
- Interactive trim tools with sliders
- Synchronized plot/chart views
- Click-to-highlight functionality

**Workflow:**
1. Extract GPS bounds from race data
2. Generate georeferenced chart image
3. Project GPS track to map coordinates
4. Create interactive matplotlib figure
5. Link plots to chart display

**Main Functions:**
- `plot_chart()` - Quick chart with race track overlay
- `trim_track()` - Interactive track trimming UI
- `chart_and_plot()` - Synchronized chart + time-series plots

#### `quick_plot.py` (182 lines)
Fast plotting utilities for time-series sailing data.

**Key Features:**
- Optimized datetime axis handling
- Multiple traces on single axis
- Expression evaluation (plot strings like "df.sog, df.spd")
- Automatic legend generation
- Synchronized multi-axis plots

**Example:**
```python
from Libraries import quick_plot as qp
qp.plot(df.index, "df.sog, df.spd, df.aws", title="Speed vs Wind")
```

### Video Integration

#### `insta360.py` (625 lines)
Processes Insta360 camera videos and synchronizes with sailing data.

**Key Features:**
- Extracts telemetry from Insta360 files
- GPS synchronization with boat logs
- Video file management and organization
- Small file cleanup (failed recordings)
- Metadata extraction and processing
- Writes per-video `.poses` binary (v2: 30 Hz, delta-encoded int16
  quaternions) via `write_pose_file`; `_upload_poses` brotli-compresses
  the staged copy so `race_upload.py::_upload_poses` can push it to S3
  with `Content-Encoding: br`. See `../POSES_FORMAT.md` for the byte
  layout and backward-compat notes. Offline regen:
  `Scripts/regen_poses.py --year 2025`.

#### `sailview_log_v1.py`
Owns the column order, conversion helpers, and byte layout of v1 `.log` files
served under `s3://regattaanalysis/www/Insta360/VID_*.log` and consumed by
`Src/src/js/log_reader_v1.js`. See `../SLOG_FORMAT.md` for full format details.

**Functions:** `df_convert`, `column_dict`, `save_log_to_binary`,
`write_log_v1`, and `read_log_v1`.

#### `sailview_log_v2.py` + `slog_channel_config.py`
Writer/reader for the v2 `.slog` format — a smaller, zero-phase-smoothed
replacement for `.log` with per-session constants lifted out of the payload
and 9 columns retired or derived-on-read. Channel recipe is declarative in
`slog_channel_config.V2_COLUMNS`. Measured 88% corpus size reduction vs v1
(raw) on the 2026-04-19 session.

- Python: `write_slog`, `read_slog` (`sailview_log_v2.py`)
- JS: `Src/src/js/log_reader_v2.js`, dispatcher `log_load.js` (feature flag
  `?slog=1`)
- Offline driver: `Scripts/regen_slogs.py --date YYYY-MM-DD`
- Full format + concerns: `../SLOG_FORMAT.md`

### Supporting Modules

#### `boat_brain.py` (641 lines)
Communication with live Boat Brain system over UDP/TCP.

**Key Features:**
- Real-time data streaming from boat
- Binary protocol deserialization
- Message rate monitoring
- Latency measurement
- Data collection and storage

#### `geometry.py` (175 lines)
3D geometry utilities for camera pose estimation.

**Key Features:**
- Quaternion math
- Rotation matrices
- Pose estimation from IMU data
- Twist/axis-angle conversions

#### `latlonalt.py` (150 lines)
GPS coordinate utilities and conversions.

**Key Features:**
- Degrees/minutes/seconds conversions
- Garmin semicircle format
- Pretty printing of coordinates
- Array-compatible operations

#### `global_variables.py` (249 lines)
Configuration and boat-specific parameters.

**Configuration:**
- NMEA 2000 PGN definitions
- Boat dimensions (J105 specific)
- Location settings (Seattle/San Diego)
- Map projection parameters
- Race mark coordinates
- Directory paths

#### `timed_log.py` (279 lines)
Logging framework with time synchronization (like ROS bags).

**Key Features:**
- Tagged message logging
- Time-synchronized playback
- Save/load to pickle format
- Message filtering by tag

#### `utils.py` (169 lines)
General utilities for file operations, time handling, and system commands.

**Key Features:**
- File backup with versioning
- Time format conversions (Arrow library)
- Itertools helpers (pairwise, nwise)
- USB drive mounting/unmounting

#### `log_helper.py` (79 lines)
Standardized logging configuration.

**Key Features:**
- Console output with custom formatting
- Configurable log levels
- Global LOG instance

#### `analysis.py` (100 lines)
Specialized analysis functions for sailing performance.

**Key Features:**
- Signal delay estimation (COG vs HDG)
- Cross-correlation analysis
- Bandpass filtering for analysis

#### `compass.py` (86 lines)
Compass deviation table management and interpolation.

#### `signals.py` (280 lines)
Advanced signal processing for sailing data including IIR filter implementations.

#### `polars.py` (729 lines)
Polar diagram processing and boat performance prediction.

## Data Flow

```
┌─────────────────────┐
│  Boat Brain USB     │
│   (*.log files)     │
└──────────┬──────────┘
           │
           ↓ bb_log.py
┌─────────────────────┐
│  Actisense Serial   │
│  + Analyzer JSON    │
└──────────┬──────────┘
           │
           ↓ canboat.py
┌─────────────────────┐
│ Filtered JSON       │
│ (NMEA messages)     │
└──────────┬──────────┘
           │
           ↓ process.py
┌─────────────────────┐
│  Pandas DataFrame   │
│  (*.parquet files)  │
└──────────┬──────────┘
           │
           ↓ race_logs.py
┌─────────────────────┐
│ Processed Data      │
│ (smoothed, filtered)│
└──────────┬──────────┘
           │
           ├──→ chart.py ──→ Visualizations
           │
           ├──→ analysis.py ──→ Performance Metrics
           │
           └──→ metadata.py ──→ Race Reports
```

## Key Dependencies

**Core:**
- `pandas` - Data manipulation
- `numpy` - Numerical operations
- `scipy` - Signal processing

**Geospatial:**
- `pyproj` - Map projections
- `gpxpy` - GPX file handling
- `gdal` - Geospatial raster processing

**Time:**
- `arrow` - Better datetime handling
- `pytz` - Timezone support

**Visualization:**
- `matplotlib` - Plotting
- `PIL` - Image processing

**Other:**
- `yaml` - Metadata storage
- `ujson` - Fast JSON parsing
- `numba` - JIT compilation for performance
- `telemetry_parser` - Video telemetry extraction

## Installation

```bash
# Create conda environment
conda env create -f ../Envs/sailing.yml

# Or install dependencies manually
pip install pandas numpy scipy matplotlib
pip install pyproj gpxpy gdal
pip install arrow pytz
pip install pyyaml ujson numba
```

## Typical Workflow

### 1. Import Boat Brain Data
```python
from Libraries import bb_log
from Libraries.global_variables import G

G.init_seattle()  # Configure for Seattle location

# Find and process sequences
sequences = bb_log.find_log_file_sequences(bb_log.BBLOG_DIR)
for seq in sequences:
    bb_log.compute_pandas_log(seq)
```

### 2. Load and Analyze Race
```python
from Libraries import race_logs, metadata

# Read race by date
md = metadata.read_metadata()
df, race = race_logs.read_date("2024-10-15")

# Data is now processed with:
# - Smoothed sensors (aws, awa, hdg, etc.)
# - Computed true wind (tws, twd, twa)
# - GPS coordinates cleaned
# - Timestamps synchronized
```

### 3. Visualize Race
```python
from Libraries import chart

# Create interactive chart with race track
ch = chart.plot_chart(df, border=0.2, pixels=2000)

# Or create synchronized plot + chart
chart.chart_and_plot(df, df.index, 
                     [df.sog, df.spd],
                     [df.aws, df.tws])
```

### 4. Trim and Save
```python
# Interactive trimming to remove dock time
ch = chart.trim_track(df)
# Adjust sliders in UI...
begin, end = ch.begin, ch.end

# Save trim points
race_logs.trim_race(race, begin, end)
```

## File Organization

```
Data/
├── BBLog/              # Raw Boat Brain logs from USB
├── PandasLogs/         # Processed parquet files
├── GPXLogs/            # GPS tracks
├── MBTILES/            # Chart tiles
├── metadata.yml        # Race metadata
└── Backup/             # Automatic backups
```

## Boat Configuration

Currently configured for:
- **Boat:** J105 "Peer Gynt"
- **Location:** Seattle, WA (Puget Sound)
- **Sensors:**
  - B&G Zeus iGPS
  - B&G ZG100 Compass/GPS
  - Airmar DST200 Speed/Depth
  - B&G Wind Sensor
  - Various displays and autopilot

## Advanced Features

### True Wind Estimation
The library uses a sophisticated iterative algorithm:
1. Projects apparent wind to boat reference frame
2. Adds boat velocity vector (from COG/SOG)
3. Corrects for masthead motion during heel
4. Uses residual error to update estimate
5. Adaptive gain for quick response to changes

### Time Synchronization
Handles three time sources:
- **GPS time**: Accurate but 1Hz
- **Boat Brain time**: 10Hz but drifts
- **Raspberry Pi time**: No RTC, significant drift

The system:
1. Computes drift parameters via least squares
2. Interpolates between GPS time stamps
3. Applies correction to all messages

### Heel-Corrected Wind
Accounts for masthead motion:
1. Smooths roll angle (Butterworth filter)
2. Computes roll rate (derivative)
3. Calculates perpendicular velocity at masthead
4. Subtracts from apparent wind east component

## Development Notes

### Code Style
- Uses literate programming (some modules from Jupyter notebooks)
- Type hints in newer modules
- Extensive use of NumPy for vectorization
- JIT compilation with Numba for performance-critical loops

### Testing
Some modules include unit tests (e.g., `timed_log.py`, `time_utils.py`)

### Configuration
Edit `global_variables.py` for:
- Different boat types
- Other locations/race marks
- Data directory paths
- NMEA device addresses

## Related Files

See also:
- `../Envs/sailing.yml` - Conda environment specification
- Parent directory notebooks for analysis examples
- `pyrightconfig.json` - Type checking configuration

## Authors

Personal sailing data analysis system for J105 racing on Puget Sound.

## License

Personal use.

