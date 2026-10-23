# VIRB Migration

One-time migration that brought 1,283 pre-Insta360 360-degree clips (Garmin
VIRB era, 2020-04 → 2023) into the modern `Insta360/` namespace so they play
through the current `sailview.html` and `review.html`. Done 2026-05-01/02.

This note documents what was done, the IMU/quaternion conventions decoded
along the way, and how to migrate further legacy clips if any reappear.

## What landed

- **MP4s**: server-side copied from `s3://regattaanalysis/www/Virb/Small/V*.MP4`
  (uppercase) to `s3://regattaanalysis/www/Insta360/V*.mp4` (lowercase). 1.1 TiB
  duplicated; legacy `Virb/Small/` left untouched (cleanup is a separate step).
- **`.poses`**: regenerated from `s3://.../Poses/<ts>.bin` (legacy 5-col
  Float64) into modern PS02 v2 (delta-encoded int16, brotli on upload). Sample
  rate fixed to honour the **4 ms (250 Hz)** legacy step instead of assuming
  1 ms; output `msec_per_sample=33` covers the full video duration now.
- **`.log`**: re-derived from local `/Users/viola/Data/SailingData/PandasLogs/
  <date>.pd.gz` parquets sliced to each clip's window, written via
  `Libraries/sailview_log_v1.write_log_v1`. Only ~1024 of the 1283 clips
  have matching parquets; the rest ship video-only.
- **`videos.json`**: each VIRB entry tagged with `"projection": "equirect"`.
  Entire array re-sorted by `CreateDate` ascending so VIRB (2020) lands at
  `id=0` and Insta360 (2024+) at the tail. CloudFront-invalidated after
  upload.
- **Short-clip drop**: clips with legacy pose duration < 60 s were skipped
  (78 of 1370 had poses; mostly camera hiccups). Two we'd already migrated
  before tightening the threshold (`V0070007`, `V2140945`) had their
  `.mp4` and `.poses` deleted from S3 and their entries removed from
  `videos.json`.

## Frontend changes

`viewer360.js` is rendering-mode aware. The Insta360 fisheye path is unchanged;
a parallel **equirectangular** path was added:

- `Src/src/js/equirect_shader.js` (NEW): standard rectilinear screen
  projection + `atan2(x, -z)` / `asin(y)` equirect lookup. FOV defaults to
  100° (vs fisheye's 180°) because pinhole projection blows up near 90°.
- `Src/src/js/fisheye_renderer.js`: compiles both programs, exposes
  `setProjection('fisheye'|'equirect')` and per-projection FOV defaults.
- `Src/src/js/viewer360.js`: `loadVideo({projection})` plumbs through.
- `Src/src/js/sailview.js`, `review.js`: read `projection` off the video
  record. Default `'fisheye'` so Insta360 playback is bitwise unchanged.

## Quaternion convention (the part that took the most digging)

The VIRB-era pose generator is a small C++ binary at
`/Users/viola/www/virb_pose/virb_pose.cpp` (not in this repo — out-of-tree
build). It's a complementary filter that:

- Treats body axes as `i_hat=(1,0,0)`, `j_hat=(0,1,0)`, `k_hat=(0,0,1)`
  with no physical labelling — the labelling comes from the constraints.
- Drives `k_est = qest · k_hat` toward the gravity direction (`down =
  -accel.normalized()`), so **world-Z is gravity-down**.
- Drives `j_est = qest · j_hat` toward `j_flat` (horizontal projection of
  world Y), so **world-Y is forward** (a fixed reference, not magnetic
  north — set when the FIT file's gyro starts).
- Right-hand rule then forces **world-X = LEFT** (`X = j × k = forward ×
  down = left`). The frame is `(X=left, Y=forward, Z=down)` — right-handed
  but axis-rearranged from NED.
- Output `qest` is **body-from-world** (`v_body = qest · v_world`).

The gain choices (`accel_gain=0.005`, `gyro_gain=0.5`) heavily damp the IMU.
A sustained 30° heel reaches the quat as only ~4° (≈13% of actual). The
on-disk quat is best understood as a *level-locked virtual camera*, not an
honest pose record. For instrument-driven heel/pitch overlays, use the
BoatBrain `.log` data instead.

### Encoding pitfall

`virb_pose.cpp` declares
`using EigenData = Eigen::Matrix<double, Dynamic, Dynamic, Eigen::RowMajor>`
and writes `.data()` raw. The `RowMajor` flag is essential — without it,
Eigen would dump column-major and a naïve Python `np.frombuffer().reshape(N, 5)`
would silently mix timestamps with quaternion components. The Python parser
in `Libraries/virb_migration.py::parse_legacy_pose_file` reshapes row-major,
matching the writer.

### Basis change to WebGL

Mapping legacy `(X=left, Y=forward, Z=down)` to WebGL `(X=right, Y=up, Z=back)`
is a **180° rotation about `(0, 1, -1)/√2`**. As an xyzw quaternion sandwich
this works out to:

```
q_webgl = (-qx, -qz, -qy, qw)
```

Implemented in `_virb_to_webgl()`. The viewer's `inv(q_webgl)` then
horizon-stabilizes correctly: applied to a `cameraRay = (0,0,-1)` (forward)
the result lands at the texture center (lon=0, lat=0) at all timestamps,
within a few degrees throughout a 30-min clip.

## Tooling

- **Library**: `Libraries/virb_migration.py` — pose transcode (legacy 5-col
  Float64 → PS02 v2), log derive (parquet slice → v1 `.log`), single-clip
  orchestration.
- **CLI**: `Scripts/migrate_virb.py` — drives `migrate_all`. Flags:
  `--execute` (default is dry-run), `--basenames`, `--limit`,
  `--no-mp4-copy` (sidecars only).
- **Bulk MP4 copy**: a single `s5cmd run` invocation with `--numworkers 256`
  and a batch file of `cp` commands moves the 1.1 TiB in ~3 minutes.
  Per-call `s5cmd cp` from inside the Python loop tops out at ~20 ops/min
  (S3's per-prefix CopyObject throttling); the batched form sustains ~500
  ops/min.

## To re-run on more legacy clips

```bash
# 1. Build the cp batch (skipping ones already in Insta360/).
python -c "
import subprocess
res = subprocess.run(['aws','s3','ls','s3://regattaanalysis/www/Virb/Small/'],
                    capture_output=True, text=True)
present = subprocess.run(['aws','s3','ls','s3://regattaanalysis/www/Insta360/'],
                        capture_output=True, text=True).stdout
existing = {ln.split()[-1] for ln in present.splitlines() if len(ln.split()) >= 4}
todo = []
for ln in res.stdout.splitlines():
    parts = ln.split()
    if len(parts) >= 4 and parts[-1].endswith('.MP4'):
        bn = parts[-1].rsplit('.', 1)[0]
        if f'{bn}.mp4' not in existing:
            todo.append(bn)
with open('/tmp/cp_batch.txt','w') as f:
    for bn in todo:
        f.write(f'cp s3://regattaanalysis/www/Virb/Small/{bn}.MP4 '
                f's3://regattaanalysis/www/Insta360/{bn}.mp4\n')
print(f'{len(todo)} cp commands')
"

# 2. Bulk MP4 copy.
AWS_PROFILE=regatta s5cmd --numworkers 256 run /tmp/cp_batch.txt

# 3. Sidecars only (parallel chunks, --no-mp4-copy skips the per-call cp).
python Scripts/migrate_virb.py --execute --no-mp4-copy --basenames V0090009 ...

# 4. Splice + sort + invalidate.
python /tmp/finalize_videos_json.py    # see git history for the script
aws cloudfront create-invalidation --distribution-id EPCTF7XCNPLK2 \
    --paths "/www/Insta360/videos.json"
```

## Cleanup

**Done 2026-05-02:** Three legacy prefixes deleted, driven by
confusion-reduction (their names collided with active data) more than
recovered cost.

- `s3://regattaanalysis/www/Virb/Small/` — 1.3 TiB, 1429 objects.
  Spot-checks confirmed byte-identical copies in `Insta360/V*.mp4`
  (`Content-Length` matched exactly on V0090009/10/11, V0150027,
  V1070447–9; ETags differ only because the server-side `s3 cp`
  re-chunked into different multipart sizes). 126 short-clip drops
  (44 GiB, < 60 s pose duration) were intentionally not migrated and
  were also removed.
- `s3://regattaanalysis/www/Poses/` — 21 GiB, 1437 objects (1436 legacy
  5-col Float64 `.bin` + the zero-byte `Poses/latest` sentinel).
  Superseded by `Insta360/<base>.poses` (PS02 v2). Last write 2023-10-31.
- `s3://regattaanalysis/www/Logs/*.bin` — 2.6 GiB, 1051 objects, deleted
  via `--exclude "*" --include "*.bin"`. **Preserved**:
  `Logs/video_poses_logs.json` (1.2 MiB), still consumed by
  `shared.getMetadata()` on `index.html` + `graphs.html`; manifest has
  had no writer since 2023-10-31.
- `aws_sync.sh:73` for-loop reduced to `for i in FullLogs SeattleTiles`
  so the next deploy doesn't re-upload the deleted prefixes from the
  local symlinks under `/Users/viola/BigData/wwwData/`.

Bucket size: 3.5 TiB → 2.1 TiB.

**Still to do** (lower priority; names don't collide with active data):

- `s3://regattaanalysis/www/Virb/Keep/` (758 MiB, 2 objects:
  `V1070459.MP4` + `output.webm`). Untouched by the migration; the name
  suggests intentional retention. Confirm before deleting.
- `s3://regattaanalysis/www/FullLogs/` (4.7 GiB) — only deletable in
  conjunction with retiring `graphs.html`, which is its sole reader,
  and the `Logs/video_poses_logs.json` manifest along with it.
- `s3://regattaanalysis/www/GMetrix/` (444 MiB), `pages/` (18 MiB),
  `oldsailview/` (53 MiB).
- `s3://regattaanalysis/www/Virb/.DS_Store` and other sub-MB bucket-root
  cruft.

Bucket versioning is **disabled**, so `aws s3 rm --recursive` actually
reclaims bytes (no delete markers). Most legacy data has already
transitioned to `ONEZONE_IA` (lifecycle rule, 30 days after creation), so
recovered cost is ~$0.01/GB·month per prefix. Do it one prefix at a time
with `aws s3 rm --recursive --dryrun` first.
