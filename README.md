# RegattaView

A complete sailing instrumentation and race replay system for a J/105 sailing out of Seattle. Includes custom hardware, mobile apps, and a web-based replay viewer.

**GitHub**: https://github.com/violapaul/RegattaView-public

**Not in this repository:** race data, video, race notes, research write-ups,
ops docs, and test fixtures (scraped results pages) are kept in a private S3
bucket (`repo_data.manifest` lists the paths; `Scripts/repo_data.py pull`
restores them for the owner). The code is complete, but some docs it links to
and the `Results/` test suite need that data. The frontend builds without private
assets and uses a solid-color placeholder until a video frame is available.
Licensed MIT (see `LICENSE`).

## System Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                           ON THE BOAT                                │
│                                                                      │
│  ┌──────────────┐    NMEA 2000     ┌──────────────┐                 │
│  │ Instruments  │◄────────────────►│  BoatBrain   │──► SD Card      │
│  │ GPS, Wind,   │                  │  (ESP32)     │    (logging)    │
│  │ Speed, Depth │                  └──────┬───────┘                 │
│  └──────────────┘                         │ WiFi AP                 │
│                                           ▼                         │
│  ┌──────────────┐                  ┌──────────────┐                 │
│  │   Insta360   │                  │   iPhone     │                 │
│  │   Camera     │                  │  BoatData    │                 │
│  │  (360° video)│                  │  (live view) │                 │
│  └──────────────┘                  └──────────────┘                 │
└─────────────────────────────────────────────────────────────────────┘
                    │                         
                    ▼ After sailing           
┌─────────────────────────────────────────────────────────────────────┐
│                        POST-PROCESSING                               │
│                                                                      │
│   SD Cards ──► race_upload.py ──► AWS S3 ──► RegattaView Web App    │
│                                                                      │
└─────────────────────────────────────────────────────────────────────┘
```

## Components

| Component | Location | Purpose |
|-----------|----------|---------|
| **BoatBrain** | `Arduino/BoatBrain/` | ESP32 firmware - logs NMEA 2000, broadcasts via WiFi |
| **BoatData** | `iOS/BoatData/` | iPhone app - live instrument display |
| **BoatStart** | `iOS/BoatStart/` | iPhone app - race start timer |
| **Web App** | `Src/` | Race replay with 360 video + data overlay |
| **Pipeline** | `Scripts/` | Post-race processing and upload |

## Quick Start

### Live Instruments (on the boat)

1. Power on BoatBrain → creates "BoatBrain" WiFi AP
2. Open BoatData app on iPhone → auto-connects, displays instruments

### Race Replay (after sailing)

```bash
# Process SD cards and upload to S3
python Scripts/race_upload.py

# View at https://your-s3-bucket/
```

## How It Works

**During a race**, two data sources are captured:

1. **Insta360 camera** -- dual-fisheye 360-degree video
2. **BoatBrain logger** -- GPS, wind, heading, speed, depth at 10 Hz

**After a race**, `Scripts/race_upload.py` ingests both from SD cards, processes video, parses sensor logs, time-synchronizes them, and uploads to AWS S3. The web frontend replays races with interactive 360 video, overlaid instruments, sparkline charts, and a live boat-track map.

## Directory Tree

```
RegattaView/
├── README.md                     <- this file
├── CLAUDE.md                     <- AI assistant guidance
├── columns.json                  <- sensor data column definitions (21 channels)
├── aws_sync.sh                   <- deploys Src/dist + data dirs to S3
│
├── Arduino/                      <- ESP32 firmware
│   └── BoatBrain/                <- main logger (M5Stack CoreS3)
│       ├── src/main.cpp          <- application entry point
│       ├── platformio.ini        <- build configuration
│       └── data/                 <- PWA files (LittleFS)
│
├── iOS/                          <- iPhone apps (Xcode project)
│   ├── BoatData/                 <- live instrument display
│   ├── BoatStart/                <- race start timer
│   ├── Shared/                   <- common code (BoatState, logging)
│   ├── UIShared/                 <- common UI components
│   └── BoatStart.xcodeproj
│
├── Src/                          <- frontend web app (see Src/README.md)
│   ├── src/
│   │   ├── js/                   <- JavaScript source (6 entry points)
│   │   ├── html/                 <- HTML templates
│   │   ├── css/                  <- stylesheets
│   │   └── assets/               <- fonts, images
│   ├── package.json
│   └── webpack.config.js
│
├── Libraries/                    <- Python processing libraries
│   ├── bb_log.py                 <- BoatBrain log parsing
│   ├── insta360.py               <- Insta360 video processing
│   ├── metadata.py               <- race metadata handling
│   └── global_variables.py       <- location-specific config (Seattle)
│
├── Scripts/                      <- data pipeline scripts
│   ├── race_upload.py            <- main pipeline: SD card -> S3
│   ├── extract_ais.py            <- extract AIS data from BB logs
│   └── ...
│
├── docs/                         <- detailed documentation
│   └── BOATBRAIN_SYSTEM.md       <- full system documentation
│
└── Notebooks/                    <- Jupyter notebooks for exploration
```

## BoatBrain Firmware

ESP32-based NMEA 2000 logger on M5Stack CoreS3. Creates WiFi AP, broadcasts data via UDP (10Hz) and WebSocket (4Hz).

```bash
cd Arduino/BoatBrain
pio run -e m5stack-cores3              # Build
pio run -e m5stack-cores3 -t upload    # Flash to device
```

See [`Arduino/BoatBrain/README.md`](Arduino/BoatBrain/README.md) for details.

## iOS Apps

Two iPhone apps share common code:

- **BoatData**: Live instrument display, connects to BoatBrain via UDP
- **BoatStart**: Race start timer, connects to RaceBox via BLE

```bash
cd iOS
xcodebuild -scheme BoatData -destination 'platform=iOS,name=YourPhone' build
xcodebuild -scheme BoatStart -destination 'platform=iOS,name=YourPhone' build
```

See [`iOS/BoatData/README.md`](iOS/BoatData/README.md) and [`iOS/BoatData/DEBUGGING.md`](iOS/BoatData/DEBUGGING.md).

## Data Pipeline

Runs in the `sailing` conda environment (miniforge3).

```bash
# Full pipeline: copy from SD cards, process, sync, upload
python Scripts/race_upload.py

# Common options
python Scripts/race_upload.py --no-video        # skip video processing
python Scripts/race_upload.py --no-logs         # skip log processing
python Scripts/race_upload.py --upload-only     # just re-upload existing data
python Scripts/race_upload.py --dry-run         # print what would happen
python Scripts/race_upload.py --recompute-pose  # force pose recomputation

# Regenerate .poses files (v2 format) for a date / year / glob without a
# full race_upload run. Writes raw to VIDEO_DIR (brotli compression now happens at upload time).
python Scripts/regen_poses.py --date 2025-10-11
python Scripts/regen_poses.py --year 2025
```

### AIS Data Extraction and Tracks

The BoatBrain firmware (updated 2026-04-13) now logs AIS data from nearby boats.
AIS is stored separately from the main 10 Hz parquet files because it has a
different structure (multiple boats, ~0.03 Hz per boat).

```bash
# Extract raw AIS data for a UTC BBLog date
conda run --no-capture-output -n sailing python Scripts/extract_ais.py --date 2026-05-09

# Generate local sailing-day web payloads for tracks.html
conda run --no-capture-output -n sailing python Analyze/slice_ais_day.py --days 30
```

Outputs:

- Raw per-UTC-date extracts:
  `/Users/viola/Data/SailingData/AISLogs/YYYY-MM-DD_ais.json`
- Review event slices:
  `Analyze/AIS/<event_id>.json`
- Day-level track payloads:
  `Analyze/AISDays/index.json` and `Analyze/AISDays/YYYY-MM-DD.json`

`race_upload.py` runs the recent AIS extraction, per-start AIS slicer, and
per-day tracks slicer automatically during the Analyze stage. Open
`tracks.html` for the AIS day index or `tracks.html?date=YYYY-MM-DD` for a
single sailing day. See `AIS_IMPLEMENTATION.md` for full documentation.

### External Tool Requirements

- **HandBrakeCLI** / **FFmpeg** -- video compression
- **s5cmd** -- fast AWS S3 sync (`s5cmd sync Insta360 s3://regattaanalysis/www/`)
- **canboat** -- NMEA 2000 parsing (`actisense-serial`, `analyzer`)
- **Conda env**: `sailing`

## Web Frontend

See [`Src/README.md`](Src/README.md) for full frontend documentation.

```bash
cd Src
npm install
npm run start   # dev server at http://localhost:9000
npm run build   # production build to Src/dist/
```

### Race Event Review

The review UI lives at `review.html` and is used to inspect, confirm, discard,
and annotate automatically detected race events (starts, tacks, gybes, mark
roundings). It combines the 360 video viewer with an event-focused map,
charts, and a gun-relative scrubber.

#### Notes & Critique (per-event)

Each event in the review UI has a `Notes & Critique` text box. Notes, status
(confirmed / pending / discarded), and trim markers live in a small editable
overlay at `Analyze/events_edits.json`; the generated read-only artifacts
(`events_index.json` + `Events/<id>.json`) are regenerated by the race
pipeline.

- `Analyze/find_starts.py` seeds each event's `notes` with a detector summary
  inside its `Events/<id>.json` payload.
- In `review.html`, editing notes/status updates `events_edits.json`, which is
  merged on top of the generated event records at load time.
- The browser autosaves after ~1.5 seconds of inactivity, and also saves on
  textarea blur.
- Saving happens by `PUT /Analyze/events_edits.json`, handled in dev by the
  webpack middleware and in prod by a signed-PUT to S3.

#### User comments (per-moment / per-start)

Separate from the detector notes, the review UI has a **Comments** tab and a
per-start comment composer. Sailview has a `K` shortcut to pin a comment to the
current video moment.

- Identity is the free-form alias chosen on first comment (cookie
  `comment_alias`, restricted to `[A-Za-z0-9_-]{1,30}`). Auth username is a
  shared login and not an identity.
- Storage: `Comments/index.json` (alias registry) and
  `Comments/<alias>/comments.json` (one file per alias).
- Both files are client-write-only. They live only on S3 in prod and under
  `Analyze/Comments/` in dev; `aws_sync.sh` never touches them.
- Sailview-targeted comments carry `{kind:"sailview", video, t}`; review-start
  comments carry `{kind:"review", start_id}`. The Comments tab flattens both
  into one reverse-chrono list.

To inspect notes locally:

```bash
# Show event id + notes after merging events_index.json with events_edits.json
python - <<'PY'
import json
from pathlib import Path

events = json.loads(Path("Analyze/events_index.json").read_text())["events"]
edits = json.loads(Path("Analyze/events_edits.json").read_text()).get("edits", {})
for ev in events:
    e = edits.get(ev["id"], {})
    note = (e.get("notes") or ev.get("notes") or "").strip()
    if note:
        status = e.get("status", ev.get("status", "pending"))
        print(f'{ev["id"]}\t{status}\t{note}')
PY
```

The review workflow depends on three generated artifacts under `Analyze/`,
all produced by `find_starts.py` in a single pass:

- `events_index.json` — small per-event summaries (eager fetch)
- `Events/<id>.json` — heavy per-event payload, lazy-fetched on selection
- `Sessions/<basename>.json` — decimated session-wide GPS track per parquet,
  used as the detail-map full-session backdrop and the wind-up overlay
  source. Lazy-fetched on selection.

### AIS Tracks

The day-level AIS viewer lives at `tracks.html`. It lists generated AIS sailing
days from `Analyze/AISDays/index.json`; a selected day loads
`Analyze/AISDays/<date>.json`. The payloads are generated by
`Analyze/slice_ais_day.py`, ignored by git, staged by `aws_sync.sh`, and uploaded
under `s3://regattaanalysis/www/Analyze/AISDays/`.

J/105 matching is configured in `Analyze/ais_fleets.yaml`. Matches are tags used
by the UI filter, not a hard data-generation filter, so nearby non-J/105 AIS
boats remain inspectable.

Typical local workflow:

```bash
# Regenerate review data (example: last year)
conda run -n sailing python Analyze/find_starts.py --days 365

# Start the dev server (single process on port 9000)
cd Src
npm run start
```

Then open `http://localhost:9000/review.html` or
`http://localhost:9000/tracks.html`. The webpack dev server mirrors
the prod S3 URL layout: `/Analyze/...` and `/Comments/...` are served from
`Analyze/` and `Analyze/Comments/` respectively, and PUTs to
`/Analyze/events_edits.json`, `/Comments/index.json`, and
`/Comments/<alias>/comments.json` persist straight to disk. There is no
separate backing process.

## Deployment

```bash
cd Src && npm run build
cd .. && ./aws_sync.sh
```

The site is hosted as static files on S3 at `s3://regattaanalysis/www/`. Authentication is handled by an AWS Lambda function that validates credentials and returns signed S3 URLs for data access.

## Data Formats

| Format | Description | Sampling |
|--------|-------------|----------|
| **Pose files** (v2) | 32-byte `'PS02'` header + delta-encoded int16 quaternions; brotli on the wire. See [POSES_FORMAT.md](POSES_FORMAT.md). | 33 ms (30 Hz) |
| **Log files** | Binary: header + column-major Uint16, min/max scaled | 100 ms |
| **`videos.json`** | Maps video filenames to pose files, log files, creation dates | -- |
| **`columns.json`** | Names the 21 sensor channels | -- |

### Sensor Channels (`columns.json`)

`row_seconds`, `spd`, `sog`, `latitude`, `longitude`, `turn_rate`, `rudder`, `depth`, `altitude`, `zg100_pitch`, `zg100_roll`, `awa`, `aws`, `twd`, `tws`, `twa`, `hdg`, `cog`, `map_lon`, `map_lat`, `row_times`
