# Race Start Analyzer

Automatically detects race starts in sailing log data and feeds the Race Start
Review UI (`Src/src/html/review.html`).

## Quick Start

```bash
# Detect events and emit per-event detail + per-session tracks
conda run -n sailing python Analyze/find_starts.py --days 365

# Start the frontend dev server (port 9000; serves review.html)
cd Src && npm run start
# → http://localhost:9000/review.html
```

The webpack dev server also serves `Analyze/` at `/Analyze/...` and
`Analyze/Comments/` at `/Comments/...`, mirroring the prod S3 layout, and
accepts PUTs to `/Analyze/events_edits.json`, `/Comments/index.json`, and
`/Comments/<alias>/comments.json`.

## Files

```
Analyze/
  find_starts.py       Detects events; writes events_index.json + Events/ + Sessions/
  slice_ais_starts.py  Writes per-event AIS overlays for review.html
  slice_ais_day.py     Writes per-local-day AIS payloads for tracks.html
  ais_fleets.yaml      Editable AIS alias config for J/105 tagging
  events_index.json    Small per-event summaries (eager fetch, all events)
  Events/<id>.json     Heavy per-event payload (lazy fetch on selection)
  Sessions/<base>.json Per-parquet decimated GPS track for the detail-map backdrop
  AIS/<event_id>.json  Nearby AIS boats sliced around an event (generated)
  AISDays/index.json   AIS sailing-day index for tracks.html (generated)
  AISDays/<date>.json  Per-day ego + nearby AIS tracks for tracks.html (generated)
  events_edits.json    Per-event status/notes/trim overlay (client-written, merged on load)
  Comments/            User comment files (dev mirror of S3 Comments/)
```

`events_index.json`, `Events/`, `Sessions/`, `AIS/`, and `AISDays/` are
generated and ignored by git. Regenerate them locally instead of committing the
payloads.

## Detection Algorithm

Race starts are detected by finding the **AWA transition** from pre-start maneuvering to sustained close-hauled sailing:

1. **Pre-start window** (2 min before): < 50% close-hauled (|AWA| < 40°)
2. **Post-start window** (4 min after): > 70% close-hauled
3. **Heading variance**: hdg_std > 50° (back-and-forth maneuvering)
4. **Minimum pre-start activity**: pre_frac > 5% (some close-hauled time)

The detector clusters nearby candidates and picks the best score (post_frac - pre_frac).

## Usage

### find_starts.py

Globs parquet files under `--logs-dir`, detects events, and in one pass writes:
- `events_index.json` — small per-event summaries (eager fetch by review.html)
- `Events/<id>.json` — heavy per-event payload (lazy fetch on selection)
- `Sessions/<basename>.json` — decimated session-wide GPS track per parquet,
  used as the detail-map full-session backdrop and the wind-up overlay path
  source. Replaces the old monolithic `tracks.json`.

```bash
# Files from last N days (recommended)
python Analyze/find_starts.py --days 365
```

Preserves existing events and sessions outside the current scope — re-running
with a smaller `--days` only updates files within scope.

### AIS day payloads

`slice_ais_day.py` turns raw AIS extracts into web payloads for `tracks.html`.

```bash
conda run --no-capture-output -n sailing python Analyze/slice_ais_day.py --days 30
conda run --no-capture-output -n sailing python Analyze/slice_ais_day.py --date 2026-05-09
```

It reads `/Users/viola/Data/SailingData/AISLogs/YYYY-MM-DD_ais.json` plus
processed ego logs from `/Users/viola/Data/SailingData/PandasLogs/`, keeps boats
whose closest approach to our track is under the configured threshold, and tags
known J/105s using `ais_fleets.yaml`.

## Review UI

- **List panel**: Filter by status, sort by date/confidence. Tab to switch
  between Starts and Comments.
- **Detail panel**: Full track map (blue=track, red=pre-start, green=post-start),
  time series charts, Notes & Critique textbox, and a per-start comment composer.
- **Browse mode**: Scrub through full tracks, manually mark starts.
- **Confirm/Discard**: Label starts, auto-saved to `events_edits.json`.

## Tuning

Constants in `find_starts.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `CLOSE_HAULED_AWA` | 40° | AWA threshold for upwind |
| `PRE_WINDOW_S` | 120 s | Pre-start analysis window |
| `POST_WINDOW_S` | 240 s | Post-start analysis window |
| `PRE_THRESHOLD` | 0.5 | Max pre-start close-hauled fraction |
| `POST_THRESHOLD` | 0.7 | Min post-start close-hauled fraction |
| `MIN_HDG_STD` | 50° | Min heading variance (back-and-forth) |
| `MIN_PRE_FRAC` | 0.05 | Min pre-start close-hauled time |
