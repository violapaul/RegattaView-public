# Calibration

Local-only tooling and artifacts for dual-fisheye stitch calibration.

## Purpose

Keep calibration workflows separate from the production viewer while sharing the same render model.

## Why OpenCV + Charuco First

This repo uses a hybrid process:

- automated geometric initialization with OpenCV (`aruco`/`charuco` + `fisheye`)
- manual seam finishing in `calibrate.html`

For this project, OpenCV is the best first solver because it is practical, well-supported, and easy to
validate in a repeatable local pipeline. If edge accuracy later becomes the limiting factor, this structure
still allows adding an alternative omnidirectional solver without changing the viewer preset schema.

## Folder Layout

- `presets/` -- working/auto-generated calibration presets (e.g. `auto_charuco.json` from `fit_dual_fisheye.py`)
- `targets/` -- printable Charuco target guidance
- `scripts/` -- offline detection and fit scripts

The canonical **default** preset used by the viewer is `Src/src/assets/calibration/default.json`.
Copy a finished auto-generated preset there (and optionally to `presets/`) to make it the active viewer calibration.

## Preset Schema (`schemaVersion: 1`)

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

## Capture Instructions

1. Print a Charuco board at 100% scale (see `targets/README.md`).
2. Record 2-4 minutes while moving the board:
   - center, corners, and edges of both fisheye circles
   - multiple distances (near, medium, far)
   - multiple tilts/rotations
3. Avoid motion blur and glare.
4. Include some frames where the board appears near each lens overlap zone.

### Practical Capture Recipe (What To Do In The Field)

- **Clip length:** target `2-4 minutes` per calibration clip. If detections are weak, capture a second clip.
- **Who stands where:** keep the camera fixed on the boat in its normal position; one person holds/moves the board around the boat.
- **Distance bands:** include board distances around `0.6-1.0 m` (near), `1.5-2.5 m` (mid), and `3-6 m` (far).
- **Coverage goal:** spend time with the board in center, near image edges, and near seam overlap for both lenses.
- **Board orientation:** rotate and tilt the board through many angles (yaw/pitch/roll), not just fronto-parallel.
- **Motion speed:** move slowly enough to keep corners sharp; pause briefly at each pose.

### Suggested 3-Minute Script

1. **Minute 1:** front lens coverage (center -> edges -> corners), mostly near and mid distances.
2. **Minute 2:** back lens coverage (same pattern).
3. **Minute 3:** seam/overlap emphasis with board crossing overlap regions at multiple tilts and distances.

### Camera/Capture Settings

- Prefer highest quality mode you use for analysis (same resolution/fps pipeline as normal footage).
- Use daylight or strong diffuse light.
- Avoid heavy spray on lens, condensation, and direct sun flare.
- If available, use faster shutter / brighter light to reduce motion blur.

### Quality Checks Before Processing

- Board fully visible in many frames, but partial-board frames are also fine.
- At least hundreds of usable detections across both sides.
- Distances and orientations are diverse; not all frames from one viewpoint.
- Minimal blur and clipping on board squares/markers.

## Data Processing Pipeline

Two workflows are available depending on what needs changing:

| Situation | Workflow |
|---|---|
| New camera, new rig geometry, or intrinsics are unknown | **Full calibration** (`fit_dual_fisheye.py`) |
| Existing calibration is close but the seam/overlap region needs adjustment | **Simplified overlap refinement** (`refine_overlap.py`) |

---

### Full calibration workflow

Use this when you need to solve for all intrinsics (focal length, principal
point, distortion) from scratch, or when the current preset is very far from
reality.  Requires 2-4 minutes of video with broad coverage of the full
fisheye circles.

#### 1) Generate target (one-time)

```bash
conda run -n calibration python Calibration/scripts/generate_charuco_target.py \
  --squares-x 10 --squares-y 7 \
  --square-length-mm 40 --marker-length-mm 28 \
  --output Calibration/targets/charuco_10x7_40mm_28mm.png
```

#### 2) Detect observations from dual-fisheye video

```bash
conda run -n calibration python Calibration/scripts/detect_charuco.py \
  --input /path/to/dual_fisheye.mp4 \
  --layout dual_half \
  --sample-every 15 \
  --output Calibration/tmp/charuco_observations.json
```

#### 3) Fit initial calibration and export preset

```bash
conda run -n calibration python Calibration/scripts/fit_dual_fisheye.py \
  --observations Calibration/tmp/charuco_observations.json \
  --output Calibration/presets/auto_charuco.json \
  --seed-preset Src/src/assets/calibration/legacy_guess.json
```

The `--seed-preset` flag seeds the solver with an existing preset, which
improves convergence on footage that lacks full pose diversity.

#### 4) Finish seam tuning in browser

```bash
cd Src && npm run start
```

Open:

- `http://localhost:9000/calibrate.html` (load `auto_charuco.json`, refine seam controls)
- then test in viewer:
  `http://localhost:9000/sailview.html?calibration=./assets/calibration/default.json`

---

### Simplified overlap refinement workflow

Use this when the existing calibration is visually good but the seam overlap
region shows a left/right shift or one lens appears rotated relative to the
other.  All lens intrinsics (focal length, principal point, distortion) are
held fixed.  Only `back.euler` and `seam.centerYaw` are updated.

Requires only a short clip (~60 s) with the Charuco board visible in the
**overlap region** of both lenses at varied tilts and distances.  Full-lens
coverage is not needed.

#### 1) Capture an overlap-focus clip

Keep the camera in its normal mount.  Hold the board in front of the seam
overlap zone at distances 0.5-2 m, slowly tilting and rotating through varied
orientations.  30-100 usable paired frames (board visible in both lenses
simultaneously) is enough.

#### 2) Detect observations

```bash
conda run -n calibration python Calibration/scripts/detect_charuco.py \
  --input /path/to/overlap_clip.mp4 \
  --layout dual_half \
  --sample-every 7 \
  --output Calibration/tmp/overlap_observations.json
```

#### 3) Run the simplified refinement

```bash
conda run -n calibration python Calibration/scripts/refine_overlap.py \
  --observations Calibration/tmp/overlap_observations.json \
  --seed-preset Src/src/assets/calibration/legacy_guess.json \
  --output Calibration/presets/overlap_refined.json
```

The script prints a diagnostic table showing how many pairs were accepted
vs. dropped and what the estimated corrections are.  Typical output:

```
Pair filtering: 48 accepted, 12 dropped
Refined back.euler: [0.3°, -0.6°, 0.2°]  (was [0.0°, 0.0°, 0.0°])
Seam yaw shift: 0.0000 deg -> new seam.centerYaw 0.0000 deg
```

All fixed fields (center, scale, aperture, distortion) are copied unchanged
from the seed preset into the output.

#### 4) Inspect and promote the refined preset

Load `overlap_refined.json` in `calibrate.html` to verify the seam looks
better.  If satisfied, copy it to the viewer:

```bash
cp Calibration/presets/overlap_refined.json Src/src/assets/calibration/default.json
```

#### When to fall back to full calibration

If `refine_overlap.py` still fails to converge (too few pairs, bad
conditioning), or if the visual result still shows obvious distortion errors
(not just seam shift), the intrinsics have probably drifted significantly and
a full calibration run is needed.

---

### Automated seam optimization (no targets needed)

When you do not have a calibration target, two scripts can refine the seam
using only regular dual-fisheye footage.  Both hold all intrinsics fixed and
adjust only `back.euler` and (optionally) `seam.centerYaw`.

| Script | How it works | Best for |
|---|---|---|
| `optimize_seam.py` | Photometric: minimises pixel-level colour discontinuity in the overlap band | Most situations; robust, uses `scipy.optimize` |
| `match_overlap_features.py` | Feature-based: ORB matching on cylindrical overlap strip | Quick geometric sanity check; cross-validates photometric result |

Both accept either an image directory (`--input-dir`) or a video file
(`--input`).

#### Photometric seam optimizer

```bash
conda run -n calibration python Calibration/scripts/optimize_seam.py \
  --input-dir Calibration/tmp/frames_20260330_131521/ \
  --seed-preset Src/src/assets/calibration/legacy_guess.json \
  --output Calibration/presets/seam_optimized.json \
  --num-frames 15
```

The optimizer samples a dense grid of rays in the overlap band (the ~14-degree
belt where both fisheye circles meet), computes the front and back pixel
values for each ray, and uses Powell's method to find the `back.euler` +
`seam.centerYaw` that minimises the normalised intensity difference.  Typical
run time is 30-60 seconds on 15 frames.

#### Feature-based overlap matcher

```bash
conda run -n calibration python Calibration/scripts/match_overlap_features.py \
  --input-dir Calibration/tmp/frames_20260330_131521/ \
  --seed-preset Src/src/assets/calibration/legacy_guess.json \
  --output Calibration/presets/feature_matched.json \
  --num-frames 20
```

Reprojects both lens halves into a common cylindrical strip, runs ORB
detection + brute-force matching with RANSAC, and converts the median pixel
displacement into an euler correction.  Run time is 5-10 seconds.

#### Recommended workflow

1. Run the photometric optimizer first (more robust, optimises all 4 params).
2. Run the feature matcher as a sanity check (should agree on yaw direction).
3. Load the photometric result in `calibrate.html` and verify visually.
4. If satisfied, promote to default:

```bash
cp Calibration/presets/seam_optimized.json Src/src/assets/calibration/default.json
```

---

### Legacy preset

`Src/src/assets/calibration/legacy_guess.json` is the manually-guessed
calibration preserved as a safe fallback.  All seam scripts accept it as a
seed via `--seed-preset`.  If you produce a better calibration, rename the old
`default.json` to `legacy_guess.json` before replacing `default.json`.

## Testing

The calibration pipeline has a synthetic test suite that exercises both detection
and fitting without requiring real camera footage.

The tests generate their synthetic PNG/JSON dataset into pytest's temporary
directory at runtime, so rebuildable fixture artifacts do not need to live in
the repository.

### Setup

Create a dedicated environment with all dependencies:

```bash
conda create -n calibration python=3.11 -y
conda activate calibration
pip install -r Calibration/requirements.txt
```

### Run the tests

From the repo root (the `calibration` conda env is required):

```bash
conda run -n calibration python -m pytest Calibration/tests/ -v
```

### What the tests validate

**Stage 1 (`test_detection.py`)** -- detection on synthetic images:

| Test | What it checks |
|---|---|
| `test_detection_count` | >= 80 % of GT-visible (side, image) pairs yield a detection with >= 14 corners |
| `test_detection_reprojection_error` | Detected corners match GT projected positions (median < 1.5 px, p95 < 3.0 px) |
| `test_split_dual_half` | `split_dual_half` correctly splits a side-by-side dual frame |
| `test_detection_on_blank_image` | `detect_charuco` returns `None` on a uniform gray image (no false detections) |

**Stage 2 (`test_fitting.py`)** -- calibration fitting accuracy:

| Test | What it checks |
|---|---|
| `test_calibrate_side_focal_length` | Fitted fx within 5 % of GT (475 front, 473 back) |
| `test_fitted_aperture` | Fitted aperture = f/half_width within 3 % of GT (0.4948 / 0.4927) |
| `test_fitted_center` | Principal point within 0.01 UV units (~10 px) of GT |
| `test_fitted_distortion_sign` | dominant k1 coefficient keeps the correct positive sign |
| `test_back_euler_estimate` | Residual back-lens euler within 0.02 rad (~1.1°) per component |
| `test_min_observations_guard` | Raises `RuntimeError` when fewer than `min_observations` are provided |

**Stage 3 (`test_refine_overlap.py`)** -- simplified overlap refinement:

| Test | What it checks |
|---|---|
| `test_preset_to_camera_matrices_roundtrip` | Normalized preset values correctly convert to pixel-space K and D |
| `test_filter_accepts_good_pair` | High-quality paired observations are accepted |
| `test_filter_drops_few_corners` | Pairs with too few corners are rejected with a descriptive reason |
| `test_filter_drops_missing_side` | Pairs with only one side are rejected |
| `test_estimate_back_euler_accuracy` | Overlap euler estimator recovers GT within 0.02 rad using GT intrinsics |
| `test_extract_seam_yaw_small_delta` | Small yaw deltas stay entirely in `back.euler`; seam shift is zero |
| `test_extract_seam_yaw_large_delta` | Large yaw deltas are split: lens gets the cap, seam absorbs the excess |
| `test_end_to_end_preserves_fixed_fields` | Running `main()` preserves all fixed fields and updates only `back.euler` and `seam.centerYaw` |

**Stage 4 (`test_fisheye_projection.py`)** -- projection module:

| Test | What it checks |
|---|---|
| `test_rotate_euler_identity` | Zero euler returns the input ray unchanged |
| `test_rotate_euler_90_yaw` | 90-degree yaw rotates X axis to -Z |
| `test_corrected_theta_identity` | Zero distortion leaves theta unchanged |
| `test_corrected_theta_positive_k1` | Positive k1 increases theta_d |
| `test_front_axis_uv` | Front optical axis (-Z) maps to front center UV (0.25, 0.5) |
| `test_back_axis_uv` | Back optical axis (+Z) maps to back center UV (0.75, 0.5) |
| `test_overlap_ray_sampling` | All sampled overlap rays project inside both fisheye circles |
| `test_overlap_ray_count_scales` | Higher grid resolution produces more overlap rays |
| `test_sample_image_bilinear` | Bilinear interpolation at image centre returns correct value |
| `test_sample_image_oob_nan` | Out-of-bounds UVs return NaN |
| `test_seam_blend_weight_extremes` | Front-facing ray has weight ~0, back-facing has weight ~1 |
| `test_front_back_uv_symmetry` | Equatorial ray has symmetric front/back u values around 0.5 |

**Stage 5 (`test_optimize_seam.py`)** -- seam optimizer and feature matcher:

| Test | What it checks |
|---|---|
| `test_photometric_zero_offset` | Already-aligned frame has lower loss than a perturbed one |
| `test_photometric_recovers_yaw` | Optimizer reduces loss and finds the correct yaw sign for a known offset |
| `test_feature_matching_on_synthetic` | ORB matching detects a known pixel shift in a synthetic overlap strip |
| `test_end_to_end_preserves_fixed_fields` | `optimize_seam.main()` preserves all non-overlap preset fields |
| `test_displacement_to_euler` | Pixel displacement converts to the correct yaw angle |

### Synthetic image generator as documentation

`Calibration/scripts/generate_synthetic_dual_fisheye.py` also serves as
executable documentation of the camera model and coordinate conventions.
Run it standalone to inspect what the synthetic images look like:

```bash
python Calibration/scripts/generate_synthetic_dual_fisheye.py \
  --output-dir /tmp/synth_preview \
  --num-images 10
```

The generated `ground_truth.json` documents the exact GT camera parameters
used, including `aperture_gt` for each lens. If you write it under
`Calibration/tests/fixtures/`, it is treated as a local preview artifact and
is ignored by git.

## Local UX Notes

`calibrate.html` supports:

- mouse drag for view orientation
- mouse wheel zoom
- `R` reset view/FOV
- `F` flip view 180 deg
- seam debug toggle and preset import/export
