# Video-to-BoatBrain Synchronization

## Overview

Insta360 video files and BoatBrain (BB) sensor logs are recorded independently
with separate clocks. To overlay instruments on the video, the two streams
must be aligned in time. This is done by cross-correlating the video's
built-in gyroscope with the BB compass heading -- both measure the boat's
rotation, so the correlation peak reveals the precise time offset.

## The Problem

Two issues can make synchronization fail silently:

### 1. Camera clock error (coarse)

The Insta360 camera clock can drift or be set incorrectly. A 2-hour error
was observed on 2026-02-28. Because the pipeline uses the camera's
`creation_time` metadata to decide *where* in the BB logs to search, a
large clock error means the search window doesn't overlap with the real
data at all.

### 2. Capped correlation search (fine)

The original sync code (`extract_bb_logs_for_video` in `insta360.py`) set
`max_delay=offset`, limiting the cross-correlation search to exactly the
number of seconds used to pad the BB window. If the true alignment falls
even slightly outside this range -- as happened when corrected metadata was
~9 seconds off -- the algorithm picks a weaker false peak without any
warning. On 2026-02-28, all 10 usable videos had the wrong alignment
(off by 10-190 seconds) because the true peak at ~209s was just beyond the
200s search cap.

## The Solution: `sync_videos.py`

A single script at `Scripts/sync_videos.py` handles the full pipeline:

```
sync_videos.py --date YYYYMMDD              # review report
sync_videos.py --date YYYYMMDD --apply --upload   # commit
```

### Stage 1: Coarse clock detection

The script checks whether BB data exists near the video's `creation_time`.
If not, it widens the search progressively (+-60s, +-30m, +-2h, +-6h) and
runs a correlation on one representative video to find the approximate clock
offset. No manual anchor time is required.

### Stage 2: Metadata correction (durable)

If the detected clock error exceeds 30 seconds, the script corrects the
`creation_time` in `.ffprobe` and `CreateDate` in `.exif` sidecar files.
Originals are backed up as `.bak`. Because the rest of the pipeline
(`race_upload.py`, `extract_date_from_video`, `create_videos_json`) reads
from these sidecar files, the correction is permanent and survives
reprocessing.

### Stage 3: Fine per-video alignment

For each video, the gyro-heading cross-correlation is run over the **full**
valid range -- no `max_delay` cap. The `--offset` parameter (default 30s)
only controls how much BB data padding to include, not the search limit.
This eliminates the false-peak problem entirely.

### Stage 4: Validation report

Each video gets a status:

| Status     | Meaning |
|------------|---------|
| **GOOD**     | Strong peak, well within the correlation range |
| **WEAK**     | Low peak/mean ratio -- insufficient heading changes for reliable sync |
| **BOUNDARY** | Peak at the edge of the correlation range -- BB window may be too narrow |

The script refuses to `--apply` if any video has BOUNDARY status.

### Stage 5: Apply and upload

With `--apply`, the script writes `.log` files (binary instrument data
aligned to video frames). With `--upload`, it copies to the web directory,
regenerates `videos.json`, and runs `s5cmd sync` to S3.

## How the correlation works

1. Compute heading rate-of-change from BB data (dhdg, at 10 Hz)
2. Downsample video gyroscope from 1000 Hz to 10 Hz (vertical axis)
3. Normalize both signals to zero mean, unit variance
4. `np.correlate(nhdg, ngg, mode='valid')` slides the shorter gyro signal
   across the longer BB signal
5. The peak position (in samples / 10 = seconds) is the time offset

The offset tells us: "the video starts this many seconds after the BB
search window begins." Combined with the known window start time, this
gives the precise real-world start time of the video.

## File layout

```
/Users/viola/Data/Insta360/VID/
    VID_20260228_085613_00_004.insv      # raw video
    VID_20260228_085613_00_004.ffprobe   # metadata (corrected creation_time)
    VID_20260228_085613_00_004.ffprobe.bak  # original backup
    VID_20260228_085613_00_004.exif      # EXIF metadata (corrected)
    VID_20260228_085613_00_004.exif.bak  # original backup
    VID_20260228_085613_00_004.imu       # raw gyroscope/accelerometer
    VID_20260228_085613_00_004.log       # aligned BB data (binary, for web viewer)
    VID_20260228_085613_00_004.poses     # camera orientation (from IMU)

/Users/viola/Data/Insta360/VID/
    VID_20260228_085613_00_004.mp4       # compressed video
    VID_20260228_085613_00_004.log       # copy of .log for web serving
    VID_20260228_085613_00_004.poses     # copy of .poses for web serving
    videos.json                          # index for the web viewer
```

## Typical workflow after a recording session

```bash
cd /Users/viola/Python/sailing

# 1. Run race_upload.py to copy/compress/process video and BB logs
Scripts/race_upload.py

# 2. Validate and apply sync (separate step for visibility)
Scripts/sync_videos.py --date YYYYMMDD
# Review the report, then:
Scripts/sync_videos.py --date YYYYMMDD --apply --upload
```

## Integration with race_upload.py

**DONE** (2026-03-01): The sync pipeline is now fully integrated into `race_upload.py`.

The sync functions have been moved to `Libraries/insta360.py`:
- `detect_clock_offset()` - Coarse clock detection
- `correct_metadata()` - Fix .ffprobe/.exif timestamps  
- `align_video()` - Fine per-video correlation
- `apply_sync_logs()` - Write .log files
- `sync_videos_to_bb()` - Full pipeline entry point

`race_upload.py` now calls `sync_videos_to_bb()` automatically, handling:
- Camera clock detection and correction
- Uncapped correlation search (no max_delay limit)
- Validation with GOOD/WEAK/BOUNDARY status

`sync_videos.py` remains as a standalone CLI tool for debugging or re-running
sync on specific dates/videos.

The old functions (`extract_all_bb_logs`, `extract_bb_logs_for_video`) are
deprecated and emit warnings if used.
