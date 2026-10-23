# `.slog` binary format (v2)

Per-video instrument-data file served alongside Insta360 videos to drive
sailview.html. Replaces the legacy `.log` (v1) format with a smaller, cleaner
schema, zero-phase smoothing, and per-session constants lifted out of the
payload.

- **Python writer / reader:** `Libraries/sailview_log_v2.py`
- **JavaScript reader:** `Src/src/js/log_reader_v2.js`
- **Channel config:** `Libraries/slog_channel_config.py`
- **Offline driver:** `Scripts/regen_slogs.py`
- **Frontend dispatcher / feature flag:** `Src/src/js/log_load.js`
- **Legacy v1 contract:** `SLOG_FORMAT.md#appendix-a--v1-log-format-for-comparison`

## TL;DR

- `.slog` is a parallel file served at `s3://regattaanalysis/www/Insta360/VID_*.slog`
  next to the existing `.log`. **Enabled by default** in sailview; `?slog=0`
  (or `localStorage.sailviewSlog='0'`) opts back to v1 for debugging.
- 16 stored columns (down from v1's 22); 9 retired columns are either recovered
  in the reader (e.g. `twa`, `map_lat`, `map_lon`, `turn_rate`), reconstructed
  from header scalars (`variation`, `row_times`, `row_seconds`), or dropped as
  unused (`altitude`, `zg100_pitch`). The three new `gt*` columns are explicit
  physically calibrated ground-reference wind and do not replace instrument
  `twd`/`tws`/`twa`.
- All smoothed channels use `scipy.signal.filtfilt` (zero phase) — replaces v1's
  causal exponential filter that was measured to lag raw by 2-3° on angles and
  ~0.1 kt on speed through a tack.
- Delta-encoded payload + transport gzip: measured **88.5% total size
  reduction** vs v1 raw on a 15-clip 4/19 sample (10.8 MB → 1.25 MB on the
  wire).

## Byte layout

Little-endian throughout.

### Fixed header — 30 bytes

| offset | size | type    | field             | notes |
|-------:|-----:|---------|-------------------|-------|
|    0   |  4   | uint32  | `magic`           | `0x474F4C53` = `'SLOG'` LE |
|    4   |  2   | uint16  | `version`         | currently `1` |
|    6   |  2   | uint16  | `num_cols`        | number of stored columns (16 today) |
|    8   |  4   | uint32  | `num_rows`        | per-column sample count |
|   12   |  4   | uint32  | `msec_per_sample` | typically `100` (10 Hz) |
|   16   |  8   | float64 | `start_unix_ms`   | absolute wall-clock anchor for sample 0, UTC |
|   24   |  4   | float32 | `variation_deg`   | session-constant magnetic variation (retires v1's per-sample `variation` column) |
|   28   |  2   | uint16  | `proj4_len`       | length of the UTF-8 proj4 string that follows |

Followed immediately by `proj4[proj4_len]` bytes — the PROJ4 string (e.g.
`+proj=tmerc +lat_0=47.6873070 +lon_0=-122.4386440 +k_0=0.99...`). Carried so
the JS reader can re-project `latitude`/`longitude` → `map_lat`/`map_lon`
without depending on a hard-coded venue projection.

### Per-column metadata — 12 bytes × `num_cols`

Immediately after the proj4 bytes, one block per column in write order:

| offset | size | type    | field           | notes |
|-------:|-----:|---------|-----------------|-------|
|    0   |  1   | uint8   | `col_id`        | See `Libraries/slog_channel_config.COL_ID`. Append-only: **never renumber**. |
|    1   |  1   | uint8   | `col_bits`      | 10, 12, or 16. Determines quantization resolution. |
|    2   |  1   | uint8   | `col_smoother`  | 0=raw, 1=filtfilt, 2=filtfilt_angle, 3=medfilt_then_filtfilt. Metadata only — not needed to decode. |
|    3   |  1   | uint8   | `col_source`    | 0=primary, 1=fallback. For `twd`/`tws`: primary=on-boat `rtwd`/`rtws`, fallback=`stwd`/`stws`. |
|    4   |  4   | float32 | `col_min`       | value at quantized 0 |
|    8   |  4   | float32 | `col_max`       | value at quantized `2^col_bits − 1` |

### Payload — column-major, in column-block order

For each column:

| offset within column | size | type   | content |
|---------------------:|-----:|--------|---------|
|    0                 |  2   | uint16 | first quantized sample |
|    2                 |  2 × (num_rows − 1) | int16 | delta-encoded remaining samples, in quant units |

Decode formula for sample `j`:

```
quantized[0]   = first
quantized[j]   = first + Σ deltas[0..j−1]           (j ≥ 1)
value[j]       = quantized[j] × (col_max − col_min) / (2^col_bits − 1) + col_min
```

For constant columns (writer path) where `col_max == col_min`, the decoded
`value` is `col_min` for every sample (range == 0 short-circuits in the reader).

## Channel set (stored)

From `Libraries/slog_channel_config.V2_COLUMNS`. All angles are degrees; all
speeds are knots (converted at write time — see §Writer behaviour).

| `col_id` | name          | kind   | source (primary ∣ fallback) | smoother             | bits |
|---------:|---------------|--------|-----------------------------|----------------------|-----:|
|    0     | `spd`         | scalar | `rspd`                      | filtfilt 0.2 Hz      |  12  |
|    1     | `sog`         | scalar | `rsog`                      | filtfilt 0.2 Hz      |  12  |
|    2     | `awa`         | angle  | `rawa`                      | filtfilt_angle 0.3 Hz|  12  |
|    3     | `aws`         | scalar | `raws`                      | filtfilt 0.2 Hz      |  12  |
|    4     | `hdg`         | angle  | `rhdg`                      | filtfilt_angle 0.2 Hz|  12  |
|    5     | `cog`         | angle  | `rcog`                      | filtfilt_angle 0.2 Hz|  12  |
|    6     | `twd`         | angle  | `rtwd` ∣ `stwd`             | filtfilt_angle 0.2 Hz|  12  |
|    7     | `tws`         | scalar | `rtws` ∣ `stws`             | filtfilt 0.2 Hz      |  12  |
|    8     | `zg100_roll`  | scalar | `zg100_roll`                | filtfilt 0.3 Hz      |  12  |
|    9     | `rudder`      | scalar | `rudder`                    | raw                  |  12  |
|   10     | `depth`       | scalar | `depth`                     | raw                  |  12  |
|   11     | `latitude`    | scalar | `latitude`                  | medfilt(5) + filtfilt 0.5 Hz |  16  |
|   12     | `longitude`   | scalar | `longitude`                 | medfilt(5) + filtfilt 0.5 Hz |  16  |
|   13     | `gtwd`        | angle  | `gtwd`                      | filtfilt_angle 0.2 Hz|  12  |
|   14     | `gtws`        | scalar | `gtws`                      | filtfilt 0.2 Hz      |  12  |
|   15     | `gtwa`        | angle  | `gtwa`                      | filtfilt_angle 0.2 Hz|  12  |

### Retired / synthesized from v1

| v1 column        | v2 handling |
|------------------|-------------|
| `row_seconds`    | reader derives: `i × msec_per_sample / 1000` |
| `row_times`      | reader derives: `start_unix_ms + i × msec_per_sample` |
| `variation`      | stored as header scalar `variation_deg` |
| `twa`            | reader derives: `wrap(twd − hdg − variation)`, (−180°, 180°] |
| `map_lat`, `map_lon` | reader derives from `latitude`/`longitude` via the header's proj4 string (small-angle approximation today — see §Concerns) |
| `turn_rate`      | reader derives from `d(hdg)/dt` with compass-wrap handling |
| `altitude`       | dropped — not consumed anywhere in production |
| `zg100_pitch`    | dropped — same |

## Writer behaviour (`write_slog`)

For each `ChannelSpec`:

1. **Source selection.** If `df[source_primary].isna().mean() ≤ primary_nan_frac_max`
   (default 20%), use `source_primary`; otherwise fall back to
   `source_fallback` (if set). Record the outcome in `col_source`. For 4/19
   data the primary was always used (autopilot was on the bus).
2. **NaN fill.** Forward-fill then back-fill so the smoothing and
   quantization steps have no holes. The format has no per-sample validity
   flag — see §Concerns.
3. **Variation correction** (angle channels flagged `variation_correction=True`,
   currently `twd` only): add
   `G.MAGNETIC_VARIATION − median(df.variation)` to the selected source. This
   retires the stale-boat-variation bias (boat publishes variation ~1° low
   per the `project_variation_authoritative_source` note).
4. **Knots conversion** (channels flagged `convert_to_knots=True`: `spd`,
   `sog`, `aws`, `tws`, `gtws`): multiply by `G.MS_2_KNOTS`. Parquet stores these in
   m/s; sailview expects knots (matches v1 behaviour via
   `sailview_log_v1.knots_convert`).
5. **Smoother.** Dispatch on `spec.smoother`:
   - `Raw()` → passthrough
   - `Filtfilt(cutoff_hz, order)` → `process.smooth(butterworth_filter(...))`
     with `causal=False`
   - `FiltfiltAngle(cutoff_hz, order)` → `process.smooth_angle(..., causal=False)`
   - `MedfiltThenFiltfilt(width, cutoff_hz, order)` → `scipy.signal.medfilt`
     followed by filtfilt
6. **Quantize.** `q = round((v − min) / (max − min) × (2^bits − 1))`, clip to
   `[0, 2^bits − 1]`. Uses round (not truncate) for minimum quantization error.
7. **Delta encode.** Write the first quantized sample as uint16, then
   `np.diff(quantized)` as int16. Writer raises `OverflowError` if any delta
   exceeds int16 range (not expected for sailing data at 10 Hz).

Resolution of header fields:

- `start_unix_ms`: `df.row_times.iloc[0].timestamp() × 1000` if tz-aware, else
  `0.0` (the reader treats `0` as "no anchor known").
- `msec_per_sample`: `round(median(diff(row_seconds)) × 1000)`, defaults to 100.
- `variation_deg`: explicit arg if passed, else `G.MAGNETIC_VARIATION`.
- `proj4`: caller-provided (typically `G.PROJ4`).

## Reader behaviour

### Python (`read_slog`)

Parses header + per-column block + payload. Inverse of the writer; returns
`{num_rows, num_cols, msec_per_sample, start_unix_ms, variation_deg, proj4,
 columns, data, source_flags}`. Used by tests and by the offline diagnostic
in `Analyze/slog_experiments.py`.

### JavaScript (`parseSlog` in `Src/src/js/log_reader_v2.js`)

1. Parse header + per-column block.
2. Decode each column's uint16 first + int16 deltas → cumulative sum →
   quantized uint → float via `(max − min) / (2^bits − 1)`.
3. Synthesize **derived columns** so downstream code in `sailview.js`,
   `review.js`, `graphs.js`, etc. can continue to access the same keys it did
   with v1:
   - `row_seconds[i] = i × msec / 1000`
   - `row_times[i]   = start_unix_ms + i × msec`
   - `variation[i]   = variation_deg` (constant)
   - `twa[i]         = wrap(twd[i] − hdg[i] − variation_deg)` into (−180, 180]
   - `turn_rate[i]   = (hdg[i] − hdg[i−1]) / (msec/1000)` with compass-wrap
     handling
   - `map_lat[i], map_lon[i]` via the PROJ4 origin — today a small-angle
     equirectangular projection (see §Concerns)

### Dispatcher (`loadLog` in `Src/src/js/log_load.js`)

- **Default on.** Fetches `…/VID_xxx.slog` first; on 404 (or any network
  error) falls back to `…/VID_xxx.log` and emits a single warning.
- **Explicit opt-out:** URL param `?slog=0` or `localStorage.sailviewSlog='0'`
  skips the `.slog` attempt entirely and fetches `.log` directly. Useful
  for debugging or A/B-ing against v1 behaviour.
- `?slog=1` / `localStorage.sailviewSlog='1'` are accepted for explicitness
  but equivalent to the default.
- Returns the same shape either way so callers in `sailview.js` don't need to
  branch.

## Data fidelity (measured on 4/19 clip `VID_20260419_114235_00_012`)

### Quantization error (decoded vs the float64 smoothed signal that was written)

| channel        | quantum      | RMS error   | max \|Δ\| | vs quantum |
|----------------|-------------:|------------:|-----------:|-----------:|
| `spd`, `sog`   | 0.0006 kt    | 0.0002 kt   | 0.0003 kt  | 0.5×       |
| `aws`          | 0.0024 kt    | 0.0007 kt   | 0.0012 kt  | 0.5×       |
| `tws`          | 0.0007 kt    | 0.0002 kt   | 0.0004 kt  | 0.5×       |
| `hdg`/`cog`/`awa`/`twd` | 0.09° | 0.03°       | 0.05°      | 0.5×       |
| `zg100_roll`   | 0.015°       | 0.004°      | 0.007°     | 0.5×       |
| `rudder`       | 0.026°       | 0.007°      | 0.013°     | 0.5×       |
| `depth`        | 0.045 m      | 0.012 m     | 0.023 m    | 0.5×       |
| `latitude`     | 2.7 × 10⁻⁷ ° | negligible  | ~0.2 m     | ~7×*       |
| `longitude`    | 1.8 × 10⁻⁷ ° | negligible  | ~0.3 m     | ~13×*      |

\* Lat/lon max |Δ| exceeds the quantum because `MedfiltThenFiltfilt` boundary
behaviour differs slightly between writer (applied to the full clip) and a
test re-slicing. Absolute ground error is ≤0.3 m, imperceptible at Leaflet
zoom 14–16.

### v2 vs v1 divergence (same clip, post-units-fix)

These are **smoother differences**, not losses:

| channel       | RMS v2−v1 | max \|Δ\| | interpretation |
|---------------|----------:|----------:|----------------|
| `spd`         | 0.12 kt   | 0.72 kt   | filtfilt 0.2 Hz vs v1's α=0.8 causal exp |
| `sog`         | 0.07 kt   | 0.55 kt   | same |
| `aws`         | 0.62 kt   | 3.20 kt   | same |
| `tws`         | 0.79 kt   | 1.83 kt   | v2 = filtfilt(rtws); v1 = `estimate_true_wind(ε=1e-4)` (rejected per project memory) |
| `hdg`         | 1.3°      | 7.6°      | v1 lags through tack transients; v2 is zero-phase |
| `cog`         | 2.4°      | 17.6°     | same |
| `awa`         | 3.4°      | 33.5°     | worst; v1's lag motivated the whole refactor |
| `twd`         | 2.7°      | 12.9°     | v2 = on-boat `rtwd` (helmsman's display); v1 = `estimate_true_wind(ε=1e-4)` |
| `zg100_roll`  | 0.9°      | 6.1°      | v1 was raw passthrough; v2 adds filtfilt 0.3 Hz (intentional) |
| `rudder`      | 0.008°    | 0.014°    | sub-quantum |
| `depth`       | 0.013 m   | 0.025 m   | sub-quantum |
| `lat`/`lon`   | ~0        | ~1×10⁻⁶ ° | essentially identical |

Max |Δ| values on angles cluster at tack transients — exactly the "old
signals during tacks" symptom the format change was designed to eliminate.

### Size (15 clips, 4 hr 51 min from 2026-04-19)

|                  | raw (KB) | gzip (KB) | vs v1 raw |
|------------------|---------:|----------:|----------:|
| v1 `.log`        | 10,823   | 7,218     | —         |
| v2 `.slog`       | 6,396    | **1,247** | **11.5%** |

So v2 `.slog` + gzip Content-Encoding is 88.5% smaller than v1 raw, and 82.7%
smaller than v1 gzipped. The 40% uncompressed savings come from schema cleanup
(8 columns retired / promoted to header scalars); the further 4× gzip benefit
comes from delta-encoded payloads compressing 3–4× better than raw uint16.

## Concerns and open issues

These are design choices or known edges. Numbers refer to real things we have
or will need to evaluate before defaulting the feature on.

1. **No CRC / checksum.** A truncated or corrupted `.slog` is parsed as best
   as possible and silently yields garbled tail samples. v1 has the same gap.
   Low probability over S3+HTTPS but worth a uint32 Adler32 footer in a
   hypothetical v2.1. Not a blocker for rollout — we'd detect via the
   sailview UI first.

2. **`col_min`/`col_max` as float32.** Saves 4 B × 16 cols = 64 B per file.
   Seattle PROJ4 origins fit exactly in float32; other venues with more
   digits might not. Not a concern for the current single-venue deployment.

3. **Small-angle map projection in JS.** `log_reader_v2.js` reconstructs
   `map_lat`/`map_lon` using an equirectangular approximation (cos(lat_0)
   longitude scaling, R × k_0 scaling on both axes). Error vs full tmerc is
   ≤0.5 m at 2 km from origin — imperceptible on our typical Leaflet map.
   If we ever need bit-equivalence with Python's `G.MAP` (pyproj), we'd
   either bundle `proj4` as a direct JS dep (~75 KB gzipped) or port the
   full tmerc forward formula. Today the approximation is documented inline
   in the reader.

4. **`turn_rate` reconstructed from quantized `hdg`.** With 12-bit `hdg`
   (0.09° quantum) at 10 Hz, reconstructed `turn_rate` has ±0.9°/s
   quantization noise on slow turns. v1's native `turn_rate` had similar
   fidelity (NMEA PGN 127251 + `process_sensors` coarse smoothing), but
   specifically a slow-turn readout built from v2 data may look grainier
   than v1's. If this shows up in use, we can either bump `hdg` to 16-bit
   or post-smooth `turn_rate` in JS.

5. **Angle filtfilt overshoot.** `hdg`, `twd`, `cog` can stray a few degrees
   past [0°, 360°); `awa` past ±180°. Caused by filtfilt ringing on a sharp
   step in the unwrapped angle (a tack). `col_min`/`col_max` are stored
   with the overshoot baked in, and the reader reproduces it exactly. Not a
   correctness issue, but a display that hard-clamps to [0, 360°) would
   see a jump. Historical v1 also has this (observed `hdg ∈ [−6°, 364°]`).

6. **No per-sample validity flag.** `_fill_nans` in the writer forward-fills
   missing samples before quantization. Fine for brief sensor dropouts
   (<1 s); can silently paper over a sustained GPS outage. Consumers today
   have no way to see "this 5-minute section was a GPS gap". A future v2.x
   could reserve a sentinel uint16 or attach a per-column 1-bit validity
   bitmap (~2 KB/column for a 30-min clip).

7. **`msec_per_sample` is a single uint32.** No support for variable-rate
   channels (e.g. depth at 1 Hz packed into a shorter column). We briefly
   sketched this but decided gzip already kills the redundancy and the
   reader simplicity is more valuable. Revisit only if a channel shows up
   that's genuinely sparse at the sample-rate level.

8. **Forward-fill is symmetric — no notion of "this sensor is down".** Closely
   related to (6). If `rudder` completely disappears for a session, today
   we ship all-zero or all-constant values (depending on the tail), and
   the UI has no signal to hide the dial.

9. **`COL_ID` is append-only.** Writer and reader both depend on
   `slog_channel_config.COL_ID` never renumbering entries. Documented in
   the config file; worth auto-checking in a test if the dict ever gets
   edited carelessly.

10. **`col_smoother` is currently informational.** The enum is written into
    the file but neither reader acts on it — both decode the same
    uint16-+-min/max-+-bits payload regardless. Useful for diagnostics
    (`read_slog` returns it in `columns[i].smoother_code`) and reserved in
    case a future format version switches on it.

11. **File not self-gzipped.** Relies on S3 serving with `Content-Encoding:
    gzip`. If a dev environment or CDN doesn't apply the transport gzip,
    the client gets ~6.4 MB per clip instead of ~1.25 MB. No correctness
    issue, just bandwidth. Upload scripts pre-gzip + set the encoding;
    this needs to stay consistent in any future upload path.

12. **Readers don't return `col_source` to downstream code today.** The
    fallback flag is preserved in `parseSlog`'s output (`sourceFlags`) but
    the sailview/review UI doesn't currently label a clip as "using stwd
    fallback". If we start seeing sessions with the autopilot off, we
    should surface this to the viewer.

## Migration / rollout notes

- Additive: `.slog` files live next to existing `.log` files; adding a
  `.slog` never overwrites a `.log`.
- **sailview defaults to v2** as of the default-flip. `?slog=0` (or
  `localStorage.sailviewSlog='0'`) is the explicit opt-out for debugging
  against v1. Missing `.slog` on S3 falls back to `.log` with a single
  console warning — no broken viewer experience.
- S3 cleanup: to revert a date, `aws s3 rm` the `VID_<date>_*.slog`
  objects. `aws_sync.sh` doesn't touch `Insta360/`; subsequent runs of
  `race_upload.py` won't resurrect deleted `.slog` files (they need to be
  regenerated via `Scripts/regen_slogs.py`).
- Tests: `Libraries/tests/test_sailview_log_v1.py` (5 tests) pins the v1
  contract; `test_sailview_log_v2.py` (5 tests) covers v2 writer/reader
  round-trip, fallback, and size regression vs v1.

## Appendix A — v1 `.log` format (for comparison)

The v1 format predates this doc and is owned by
`Libraries/sailview_log_v1.py` (extracted from
`Libraries/sailview_log_v1.save_log_to_binary`). Brief recap:

```
offset 0   int32   num_rows
offset 4   int32   num_cols
offset 8   int32   msec_per_sample   (typically 100)
offset 12  int32   multiplier        (typically 65535)
offset 16  float64 max_values[num_cols]
offset 16+8C  float64 min_values[num_cols]
offset 16+16C uint16 payload[num_rows * num_cols], column-major
```

Columns match `sailview_log_v1.COLUMN_LIST` (22 entries incl. `row_seconds`,
`row_times`, `variation`, `map_lat`, `map_lon`, `twa`, `turn_rate`,
`altitude`, `zg100_pitch` — all of which are retired, synthesized, or
dropped in v2). All values quantized at 16 bits via floor (not round), so a
read-decode-re-encode round-trip drifts ±1 ULP per sample.

Decode formula (matches `Src/src/js/sailview.js::readLog` pre-Stage-B, now
in `log_reader_v1.js`):

```
value[i][j] = uint16[j] × (max_values[i] − min_values[i]) / multiplier + min_values[i]
```

## References

- Project plan: `.claude/plans/did-you-run-a-steady-pike.md`
- Stage A experiments + plots: `Analyze/slog_experiments.py` + `Analyze/Plots/slog_experiments/20260419/`
- Project memory: `.claude/projects/-Users-viola-Resilio-Sync-Dropbox-Sailboat-Code-RegattaView/memory/project_estimate_true_wind_fallback.md`
- Reference for smoothers: `Libraries/process.py` (`smooth`, `smooth_angle`, `exponential_filter`, `exponential_filter_angle`)
