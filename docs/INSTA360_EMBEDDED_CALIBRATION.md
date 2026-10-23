# Insta360 Embedded Lens Calibration → Viewer

> **Superseded for producing the shipped preset (2026-06-09).** The embedded
> `offset_v3` distortion polynomial proved undecodable and the v1-only mapping
> below (equidistant, zero distortion) cannot match both center and rim. The
> production calibration now comes from the **factory-transfer + distant-seam
> refinement** procedure in `docs/FISHEYE_CALIBRATION.md`. This doc remains the
> reference for what calibration data lives *inside* the `.insv`.

The viewer renders **raw dual-fisheye** video (no upstream stitching — `Libraries/insta360.py`
just `hstack`s the two lens circles into a 3840×1920 mp4). The fisheye→view dewarp is done
at render time in `Src/src/js/fisheye_shader.js`, driven by a per-lens calibration preset
fetched from `Src/src/assets/calibration/default.json` (see `viewer360.js::loadCalibrationPreset`).

Until now that preset was a **geometric guess** (lens circles assumed perfectly centered at
[0.25,0.5] / [0.75,0.5], aperture hand-tuned). But every Insta360 `.insv` carries the
**factory per-lens calibration** for that specific body. This doc records how we extract it and
fold it into the viewer preset.

## Where the calibration lives in the file

`.insv` is an MP4 with a proprietary Insta360 trailer appended after `mdat`, terminated by the
32-byte ASCII magic `8db42d694ccc418790edff439fe026bf` at EOF. A `u32` at EOF−40 gives the
trailer length; records are a backward-linked list. exiftool (≥12.5) parses it with `-ee`.

The lens calibration is trailer record `0x101` (INSV maker notes), tag `0x2a`, surfaced by
exiftool as **`Insta360:Parameters`**. The pipeline also dumps it into the per-video
`.telemetry` sidecar as `offset` (and richer `offset_v2` / `offset_v3`).

For the X3 the `offset` string is:

```
2  cx0 cy0 R0 d0a d0b fov0   cx1 cy1 R1 d1a d1b fov1   STACK ACROSS X
```

- `2` = lens count.
- Per lens: `(c_across, c_stack, R, d1, d2, halfFOV°)` in the camera's **native dual-fisheye
  frame** — `ACROSS` (5952) wide × `STACK` (11904) tall, two ~5952-px circles stacked along the
  STACK axis. `R` is the image-circle radius in native px; `halfFOV` ≈ 91°/88° (≈182° each lens).
- Trailing `STACK ACROSS X` = `11904 5952 …` native dimensions.

`d1`/`d2` are an Insta360-internal distortion convention (≈0.5), **not** the viewer's
OpenCV-θ-polynomial `k1`/`k2`; we do not copy them. `offset_v2`/`v3` additionally carry a
per-lens rotation quaternion (~3° rear-lens deviation) — also not mapped yet (see below).

## Mapping into the viewer preset

The served video is an `hstack` at `dimension {x:long=3840, y:short=1920}`, so the native STACK
axis → video-x and native ACROSS axis → video-y under a single uniform scale (3840/11904 =
1920/5952 = 0.3226). The fisheye shader samples the texture as

```
UV = center + (scaleX · aperture · θ · cosφ,  scaleY · aperture · θ · sinφ)
```

so a rim ray (θ = halfFOV) lands at UV-radius `scaleY·aperture·halfFOV`, which must equal
`R / ACROSS` (circle radius as a fraction of the short video axis). Therefore:

- `center = [c_stack / STACK, c_across / ACROSS]`  → front ≈ [0.250, 0.488], back ≈ [0.750, 0.490]
- `aperture = (R_front / ACROSS) / radians(halfFOV_front)`  with `scaleY_front = 1.0`  → **0.3162** (derivation)
- `scaleY_back = (R_back/ACROSS)/radians(halfFOV_back) / aperture`  → ≈ 1.031
- `scaleX = ±0.5·scaleY` (the 2:1 aspect correction; back negative — rear fisheye is mirrored)

The key correction vs the old guess is the **measured per-lens optical centers** (~1.3% above
geometric center) and per-lens radius ratio.

> **STATUS (2026-06-08): reverted to the pre-session baseline pending verification.**
> The session shipped embedded centers + per-lens back-scale asymmetry + aperture 0.285, but a
> mid-distance boat showed missing bits at the seam, so production was reverted to the symmetric
> baseline (centers 0.25/0.5 & 0.75/0.5, scale ±0.5/1.0, aperture 0.296). **Unresolved:** whether
> the seam artifact was a regression from the back-scale asymmetry / aperture, or pre-existing
> parallax (a 2-lens seam can't perfectly stitch finite-distance objects under *any* static
> calibration). The derivation below assumes the embedded `fov` (91.0°/88.3°) is the incidence
> angle *at the image-circle radius* — **this assumption is the prime suspect and is not yet
> verified.** Re-apply changes only one at a time, each verified against the seam.

The derived aperture is `0.3162`; an eyeball pass suggested `0.285` was tighter/better on the
forward view, but neither is currently shipped. The equidistant model assumes a rim ray at
`halfFOV` maps linearly to the image-circle radius; the lens is non-equidistant near 90° and we
don't model distortion, so a single linear aperture is at best a compromise.

### Validation

Decoded centers/radii were checked against a frame measured directly from the `.insv`: predicted
circle centers landed within ~6 px in x and the radius within ~3% of the vignette boundary — the
transpose+scale model is correct.

## Extracting it for a new camera / video

The factory calibration is **per-body** (keyed on serial). If the camera is ever swapped, re-run:

```bash
# from a .telemetry sidecar (already produced by race_upload.py):
conda run -n base python Calibration/scripts/insv_to_viewer_calibration.py \
    --telemetry /Users/viola/Data/Insta360/VID/raw/<basename>.telemetry \
    -o Src/src/assets/calibration/default.json

# or straight from a .insv (runs exiftool -ee):
conda run -n base python Calibration/scripts/insv_to_viewer_calibration.py \
    --insv /Volumes/Big/Insta360/VID/raw/<basename>.insv -o /tmp/preset.json

# or paste the raw offset/Parameters string:
conda run -n base python Calibration/scripts/insv_to_viewer_calibration.py \
    --offset "2 2903.95 2976.12 2990.38 ... 11904 5952 2887"
```

The script prints diagnostics (native frame, radii, half-FOVs, derived vs default aperture) to
stderr. After regenerating, rebuild/redeploy the frontend (`cd Src && npm run deploy`).

## Inspecting/tuning interactively

`http://localhost:9000/calibrate.html` (unauthed dev tool) loads any image/video as the fisheye
source and **Import Preset** loads a JSON like the one above; per-lens sliders + the seam-debug
overlay let you eyeball the seam. This is how the embedded preset was A/B'd against the old guess.

## Known limitations / next steps

- **Geometry only.** euler and distortion are left at viewer defaults. The previous
  `seam_optimized.json` photometric back-euler (~0.7°) was fit on top of the *wrong* geometry and
  no longer applies; a fresh photometric seam pass (`Calibration/scripts/fit_dual_fisheye.py`
  family) on top of this corrected geometry could squeeze the seam further.
- **Single global preset.** The design assumes one fixed camera/rig (one preset for all videos).
  Per-video calibration would require a `videos.json` field + a per-load fetch in `viewer360.js`.
