# Sailing Data Processing Scripts

Command-line utilities for the current RegattaView pipeline: Insta360 video,
BoatBrain logs, AIS extraction, and deployment support.

Retired Raspberry Pi / VIRB-era scripts have been removed. Their durable lessons
are captured in [`../docs/LEGACY_LEARNINGS.md`](../docs/LEGACY_LEARNINGS.md).

## Current Scripts

```text
Scripts/
├── race_upload.py       Main pipeline: copy, process, sync, analyze, upload
├── extract_ais.py       Extract AIS tracks from raw BoatBrain logs
├── recompute_logs.py    Rebuild per-video `.log` slices for a date
├── regen_poses.py       Backfill v2 `.poses` files
├── regen_slogs.py       Backfill v2 `.slog` files
├── generate_events.py   Generate maneuver playlists from web `.log` files
├── cloud_activity_report.py  Daily/weekly AWS usage + security activity report
├── compute_sensor_calibration.py
│                         Refit display-time sensor calibration JSON
├── sync_videos.py       Video sync helper
├── manage_allowlist.py  Manage auth allowlist in S3
├── bb_data.py           Receive live BoatBrain data
├── bb_upload.py         Send test data to BoatBrain
├── udp_recv.py          UDP receiver debug utility
└── udp_send.py          UDP sender debug utility
```

## Main Pipeline

Run from the repo root:

```bash
conda run -n sailing python Scripts/race_upload.py
```

Common options:

```bash
conda run -n sailing python Scripts/race_upload.py --dry-run
conda run -n sailing python Scripts/race_upload.py --no-video
conda run -n sailing python Scripts/race_upload.py --no-logs
conda run -n sailing python Scripts/race_upload.py --upload-only
conda run -n sailing python Scripts/race_upload.py --no-analyze
```

The pipeline uses:

- Insta360 files from `/Users/viola/Data/Insta360/VID/`.
- BoatBrain raw logs from `/Users/viola/Data/BBLog/`.
- Processed parquet logs under `/Users/viola/Data/SailingData/PandasLogs/`.
- Web-ready video/log/pose output under `/Users/viola/Data/Insta360/VID/` (single source-of-truth dir after the WEB_DIR collapse).
- Review data under `Analyze/events_index.json`, `Analyze/Events/`, and
  `Analyze/Sessions/`.
- AIS web payloads under `Analyze/AIS/` and `Analyze/AISDays/`.

## AIS

AIS is extracted separately because it is a multi-boat side channel rather than
a single 10 Hz instrument row.

```bash
conda run -n sailing python Scripts/extract_ais.py --date 2026-04-15
```

`race_upload.py` runs recent AIS extraction plus both web slicers as part of
the Analyze stage:

- `Analyze/slice_ais_starts.py` writes per-event `Analyze/AIS/<event_id>.json`
  files used by `review.html`.
- `Analyze/slice_ais_day.py` writes `Analyze/AISDays/index.json` and
  `Analyze/AISDays/<YYYY-MM-DD>.json` files used by `tracks.html`.

The day slicer treats a day as the local date of processed ego parquet logs,
merges UTC AIS extracts when a local sailing day crosses UTC midnight, and tags
known J/105s from `Analyze/ais_fleets.yaml`.

Manual day-payload regeneration:

```bash
conda run --no-capture-output -n sailing python Analyze/slice_ais_day.py --days 30
conda run --no-capture-output -n sailing python Analyze/slice_ais_day.py --date 2026-05-09
```

## Backfills

Regenerate poses:

```bash
conda run -n sailing python Scripts/regen_poses.py --date 2025-10-11
conda run -n sailing python Scripts/regen_poses.py --year 2025
```

Regenerate v2 `.slog` files:

```bash
conda run -n sailing python Scripts/regen_slogs.py --date 2026-04-19
```

Recompute per-video v1 `.log` slices for a date:

```bash
conda run -n sailing python Scripts/recompute_logs.py 20241015
```

## Playlists

Generate maneuver playlists from staged web `.log` files:

```bash
conda run -n sailing python Scripts/generate_events.py
conda run -n sailing python Scripts/generate_events.py --upload
```

## AWS Usage Reports

Generate a local-day cloud activity report that separates human video watching
from CloudFront delivery, S3 origin/cache-fill reads, storage, request, and
operational retrieval/copy costs — and that surfaces a security review of
sign-ins, auth failures, off-allowlist attempts, and the current allowlist /
killswitch state:

```bash
conda run -n sailing python Scripts/cloud_activity_report.py
conda run -n sailing python Scripts/cloud_activity_report.py --day 2026-05-02
conda run -n sailing python Scripts/cloud_activity_report.py --since 14d --email
conda run -n sailing python Scripts/cloud_activity_report.py --since 7d --security-only --print
```

Dates are `America/Los_Angeles` calendar days. The script fetches the needed
UTC S3, CloudFront, and CloudWatch log-object days internally, then groups
access and attribution by local day. Default output goes to
`~/Resilio Sync/Dropbox/Sailboat/Reports/aws_usage_<from>_<to>.md` (the
`aws_usage_` filename prefix is preserved so previously synced reports stay
findable; it predates the rename from `aws_usage_report.py`).

The top of the report is intended to be the human-readable summary:

- Human users exclude service identities such as `peergynt`.
- `CloudFront delivered` is the best measure of what viewers received.
- `S3 origin/cache-fill` is backend traffic for cache misses, signed fallback,
  poses/logs, and tooling; do not add it to CloudFront bytes as extra viewers.
- `USW2-Retrieval-ZIA` and server-side copy callouts are operational retrieval
  costs, not normal video watching.

The security section covers: successful sign-ins (SIGNIN events), auth failures
(AUTH_FAIL), off-allowlist attempts, signing-policy WOULD_DENY hits, new
identities / new IPs per identity, anonymous direct-to-S3 fetches, the current
allowlist snapshot, and the CloudFront distribution enabled flag. Use
`--security-only` to skip the cost-attribution tables.

Implementation lives in `Libraries/cloud_activity.py`; SES email delivery is in
`Libraries/cloud_activity_ses.py`. More operational context is in
`../AWS_SECURITY_STATUS.md`.

## Sensor Calibration

`compute_sensor_calibration.py` is live maintenance code. It refits
`Src/src/assets/calibration/sensor_calibration.json`, which the shared 360
viewer loads at runtime to adjust displayed `spd`, `heel`, `awa`, and `depth`.

Run it when sensors are changed, remounted, or enough new data has accumulated:

```bash
conda run -n sailing python Scripts/compute_sensor_calibration.py --year 2026 --plot
```

The plots are diagnostic only; inspect the generated JSON before deploying it.

## BoatBrain Utilities

`bb_data.py`, `bb_upload.py`, `udp_recv.py`, and `udp_send.py` are development
and diagnostics tools for live BoatBrain/NMEA data paths. They are standalone by
design and are not imported by the main pipeline.
