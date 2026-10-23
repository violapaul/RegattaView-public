# `.poses` Binary Format

Per-video camera-orientation file served alongside Insta360 videos to drive
360-stabilized playback in sailview. Readers accept two formats — v2 (current,
delta-encoded + brotli-compressed) and v1 (legacy, uncompressed 100 Hz) — so
objects that pre-date the v2 rollout keep working without regeneration.

- **Writer:** `Libraries/insta360.py::write_pose_file` (v2 only)
- **Upload-time compression:** `Scripts/race_upload.py::_upload_poses` brotli-compresses to a tmpdir before `s5cmd cp --content-encoding br` (no on-disk staging copy after the WEB_DIR collapse).
- **Uploader:** `Scripts/race_upload.py::_upload_poses` (sets `Content-Encoding: br`)
- **Readers:** `Src/src/js/viewer360.js::parsePoseBuffer`, `Src/src/js/sailview.js::parsePoseBuffer`
- **Offline driver:** `Scripts/regen_poses.py --date / --year / --pattern`
- **Offline JS verification:** `Src/scripts/verify_poses.mjs`
- **Sister doc:** `SLOG_FORMAT.md` (parallel v1→v2 work for `.log`/`.slog`)
- **VIRB-era origin:** `docs/VIRB_MIGRATION.md` (the 2020-2023 corpus is now
  in this format too; uses `projection: 'equirect'` instead of fisheye)

## TL;DR

- `.poses` live at `s3://regattaanalysis/www/Insta360/VID_*.poses`. v2 files
  ship with `Content-Encoding: br`; the browser decompresses transparently.
- **Measured shrink on real IMU data: ~8.3× vs v1** (1.39 MB → 168 KB for a
  typical 30-min clip). Gains split: 3.3× from the 30 Hz downsample, 2.0×
  from delta encoding, 1.3× from brotli-over-delta vs brotli-over-raw.
- Readers branch on the first 4 bytes: `'PS02'` → v2 header, otherwise →
  legacy v1. Old S3 objects (pre-rollout) remain v1; new writes are always
  v2. The viewer's nearest-neighbor lookup (`Math.round(timeMS / msecs)`)
  respects the header-stored sample rate, so mixed corpus just works.
- Migration: `python Scripts/regen_poses.py --year 2025` re-creates v2
  files for a year's worth of clips in ~25 min per 200 clips, then
  `python Scripts/race_upload.py --upload-only` pushes the changed-size
  objects with the right Content-Encoding.

## Layer separation

Three distinct concerns live in the pipeline, and each handles its own
aspect of the stored bytes:

1. **Structural format** — how the quaternion stream is packed into bytes.
   v1 and v2 differ here.
2. **Transport compression** — how those bytes are compressed for transit.
   v2 ships brotli-compressed; v1 historically ships uncompressed.
3. **Integrity** — handled by the layers below the structural format:
   HTTPS (TLS MAC), brotli's own frame integrity, S3's object-level
   MD5/ETag. There is no format-level checksum; see "Integrity" below.

## Structural format

### v2 (current)

Header is 32 bytes. Payload is `rows × cols × 2` bytes of int16 little-endian
delta-encoded quaternion components.

| Offset | Size | Field             | Notes                                 |
| -----: | ---: | ----------------- | ------------------------------------- |
| 0      | 4    | magic             | ASCII `'PS02'` (`0x50 0x53 0x30 0x32`) |
| 4      | 4    | flags uint32 LE   | bit 0 = delta-encoded (always set by current writer) |
| 8      | 4    | rows uint32 LE    | number of pose samples                |
| 12     | 4    | cols uint32 LE    | always 4 (x, y, z, w)                 |
| 16     | 4    | msec_per_sample   | uint32 LE, typically 33 (≈30 Hz)      |
| 20     | 4    | multiplier uint32 | LE, typically 32767                   |
| 24     | 8    | reserved          | zero                                  |
| 32     | *    | payload int16 LE  | `rows × cols` int16 values, row-major |

**Column order** is `(x, y, z, w)` in the WebGL convention: x right, y up,
z back, right-handed. The writer permutes the input `(w, x, y, z)` and
flips the sign of x to match. See `insta360.py:write_pose_file` for the
exact permutation.

**Delta encoding** (when `flags & 1`): row 0 is stored as the absolute
int16 quaternion components. Rows 1..N−1 store `q[r] − q[r−1]` in int16
(per-column independent, modular arithmetic). The reader reverses with a
column-wise cumulative sum:

```js
for (let i = cols; i < data.length; i++) {
    data[i] = data[i] + data[i - cols];  // Int16Array wraps mod 2^16
}
```

Int16 modular arithmetic is its own inverse so no overflow special-casing
is needed at either end.

**Downsampling**: the writer decimates the 1000 Hz complementary-filter
output by the stride `msec_per_sample` — pure sample-and-hold, no
anti-alias filter. The upstream filter's `gyro_gain=0.9` at 1 ms gives a
≈16 Hz roll-off that provides most of the anti-aliasing; the remainder is
acceptable because:
- Boat attitude content lives well below 1 Hz.
- The viewer's render-time lookup (`Math.round(timeMS / msecs)`) is
  nearest-neighbor at video frame rate, so the effective aliasing floor
  was already at the ~30 Hz level under the old v1 100 Hz storage.

### v1 (legacy, read-only)

Header is 32 bytes of four `uint64` LE fields; payload is raw int16
quaternion components (no delta).

| Offset | Size | Field             |
| -----: | ---: | ----------------- |
| 0      | 8    | rows uint64 LE    |
| 8      | 8    | cols uint64 LE    |
| 16     | 8    | msec_per_sample   |
| 24     | 8    | multiplier uint64 |
| 32     | *    | payload int16 LE  |

v1 files on S3 are uncompressed (no `Content-Encoding`) at 100 Hz
(`msec_per_sample = 10`). Readers keep supporting v1 indefinitely so
historical videos don't need regeneration.

### Version detection

Readers branch on the first 4 bytes:

```
bytes[0..3] == 'PS02'  → v2
otherwise              → v1 (four uint64 LE header)
```

This works unambiguously because a v1 file's first 8 bytes are a uint64
row count — at any realistic length (< 2³²), the high 4 bytes are zero,
so they cannot collide with `'PS02'` (`0x32305350` as a little-endian
uint32 suffix).

## Transport compression

v2 files are **brotli-compressed** at staging time and shipped with
`Content-Encoding: br`. Browsers decompress transparently — the reader
sees the uncompressed bytes. Compression ratio on real IMU data is ~2.5×,
on top of the ~3.3× from the 30 Hz downsample.

| Format + transport                   | Typical size (30-min clip) | vs original |
| ------------------------------------ | -------------------------: | ----------: |
| v1, 100 Hz, uncompressed             | ≈1.40 MB                   | 1.00×       |
| v2, 30 Hz, raw                       | ≈420 KB                    | 3.3×        |
| v2, 30 Hz, gzip                      | ≈340 KB                    | 4.1×        |
| **v2, 30 Hz, delta, brotli**         | **≈165 KB**                | **8.5×**    |

The dev server (`Src/webpack.config.js`) sniffs magic bytes to set the
right `Content-Encoding` for mixed-format testing: gzip magic (`1f 8b`) →
`gzip`; `'PS02'` → no encoding (uncompressed v2); apparent uint64 rows
header → no encoding (uncompressed v1); otherwise → `br`.

## Integrity

No format-level checksum. The transport and storage stack already provides:

- **TLS** for HTTPS fetches from S3 (MAC on every record).
- **Brotli frame integrity**: invalid compressed bytes fail the browser's
  decode, not silently decode to garbage.
- **S3 ETag** (MD5) on upload + `aws s3` / `s5cmd` verify on write.

A writer-side truncation bug would escape all three. The readers guard
against that with a lightweight size-match assertion:

```js
const expected = rows * cols;
if (data.length !== expected) {
    console.error(`.poses payload size mismatch: got ${data.length}, expected ${expected}`);
}
```

If truncation ever actually bites, the right fix is to add a 4-byte
CRC32 to the reserved header region and validate on read — the slot is
already zeroed out for that purpose.

## Lookup semantics

The viewer samples at video time with:

```js
const row = Math.round(timeMS / msecs);
return [data[row*cols] / m, data[row*cols+1] / m,
        data[row*cols+2] / m, data[row*cols+3] / m];
```

Nearest-neighbor, no interpolation. At 30 Hz storage and 30 fps playback
this is a 1-to-1 mapping from frame to pose, so the lookup adds no
additional quantization beyond what's already in the stored int16.

## Verification

`Src/scripts/verify_poses.mjs` is an offline Node test that:

1. Reads two `.poses` files (any mix of v1/v2, uncompressed / gzip / brotli).
2. Runs the same parse + nearest-neighbor lookup the browser viewer uses.
3. Compares the quaternion streams frame-by-frame at 30 fps.
4. Prints unit-norm stats and per-frame angular difference.
5. Exits non-zero if max angular difference exceeds 5°.

Typical usage (comparing an archived v1 against the regenerated v2):

```bash
node Src/scripts/verify_poses.mjs \
    /tmp/poses-backup/staged/VID_20251011_103551_00_017.poses \
    /Users/viola/Data/Insta360/VID/VID_20251011_103551_00_017.poses
```

On valid files the max angular difference is ~1.4°, dominated by the
100 Hz↔30 Hz sampling-grid offset during fast boat rotation, not by
format error. Grid-aligned frames are bit-identical between v1 and v2.
