# Dual-Fisheye Lens Calibration — Factory Transfer + Distant-Seam Refinement

How the viewer's dual-fisheye calibration (`Src/src/assets/calibration/default.json`)
is produced, why this method was chosen over the alternatives, and the exact
procedure to recalibrate for a future camera body. Calibration is **per camera
body** — redo this whenever the camera is swapped.

Status (2026-06-09): calibrated for X3 serial `IAQEF2403KW7YY`. Distant seam
registration measured at **< 0.1°** on both meridians (was ~1.3°), interior fit
sub-pixel against Insta360's own stitcher.

## The viewer's lens model

`Src/src/js/fisheye_shader.js` dewarps the raw side-by-side dual-fisheye frame at
render time with an OpenCV-fisheye (Kannala-Brandt) model per lens:

```
theta_d = theta * (1 + k1*theta^2 + k2*theta^4)        # theta = angle from lens axis
uv      = center + (scale.x * aperture * theta_d * cos(phi),
                    scale.y * aperture * theta_d * sin(phi))
```

Preset → pixels (for a 3840×1920 frame): `f_px = aperture * |scale.y| * 1920`.
The back lens has negative `scale.x` (mirrored) and a relative `euler` rotation;
`front.euler` is fixed to zero (the front lens *defines* the rig frame — global
orientation lives in the `.poses`, not the preset).

**GLSL trap (cost us a debugging round):** `mat3` constructors are column-major,
so the hand-written rotation matrices in `rotateEuler` are the transposes of what
they look like — the shader actually applies `Rz(-ez)·Ry(-ey)·Rx(-ex)`. Any tool
emitting `euler` for this shader must decompose **Eᵀ** as intrinsic XYZ
(`Rotation.from_matrix(E.T).as_euler('XYZ')`, see `fit_factory_transfer.py`).
Getting this wrong *doubles* the seam error instead of fixing it.

## Why this method (what didn't work)

1. **Embedded `offset_v3` decode — falsified.** The `.insv` telemetry carries a
   per-lens fx/fy/cx/cy + distortion polynomial + rotation, but the polynomial
   convention is proprietary. Forward Kannala-Brandt, inverse, and OcamCalib
   interpretations were all tested against the trusted v1 anchor (image-circle
   radius at half-FOV) and none reproduce it (`fit_insv_v3_distortion.py`
   documents the falsification). Only the crude v1 block (centers, radius,
   half-FOV) is decodable — that is what `insv_to_viewer_calibration.py` uses,
   and an equidistant model with zero distortion can match the lens at the
   center *or* the rim, not both.
2. **ChArUco board calibration — unstable, as expected.** A close-range board
   can't reach the rim (where fisheye distortion actually matters) with usable
   detections; bootstrap refits showed k1 CV 23% / k2 CV 79%
   (`charuco_calibrate.py` has the stability gate that caught this). A board big
   enough to fix that doesn't fit indoors.
3. **Factory transfer (chosen).** Insta360 Studio applies the factory
   calibration when it stitches; every stitched-equirect pixel has a known world
   ray, so feature matches between the raw fisheye and Studio's output are
   (ray ↔ pixel) correspondences over the *full* FOV — no target, no rim-coverage
   problem, massively overdetermined (≈10k matches/lens), and stable
   (disjoint-subset refits agree to ≤0.1% on f, ≤2% on k1).
4. **…plus a distance pass (required).** Studio's own stitch warps content near
   the seam (template/dynamic stitching closes the seam *visually*, overriding
   the static lens model), so transfer correspondences beyond ~85° are
   contaminated and the tail (85–91°) extrapolates badly: measured ~1.3° radial
   mismatch at the seam. `refine_seam_distance.py` fixes the tail using physics:
   a far-field feature matched between the two lenses must unproject to the same
   world direction. Two shared parameters (δk1, δk2), interior pinned by a
   regularization grid.

### Distance behavior (why a close-range fit is OK)

Lens distortion r(θ) is a property of the optics — range-independent, and the X3
is fixed-focus, so a calibration video shot indoors transfers to distant scenes.
What *is* range-dependent is the seam: parallax from the ~25 mm lens baseline.
The refinement pass deliberately registers the seam **at infinity** (race fleet,
shoreline); near-field objects crossing the seam ghost by their full parallax
(~0.7° at 2 m). That is physics, not calibration error — only depth-aware
stitching would remove it.

## Recalibrating for a new camera — procedure

Everything runs in the `calibration` conda env. Steps 1–3 need ~15 min plus one
manual Studio export.

**1. Shoot a calibration clip.** ~20 s, any feature-rich scene (a kitchen works
fine), camera still or slowly moving. No board or target needed. Copy the
`.insv` somewhere readable.

**2. Studio export.** Open the clip in Insta360 Studio → Export → **Export 360
video** (equirect, *not* reframed), same resolution as the source
(3840×1920 for X3 video), H.264. Stabilization off if the toggle exists
(a per-frame global rotation is absorbed by the fit anyway, since frames are
fitted pairwise). Default stitching settings are fine — seam contamination is
handled by the next pass.

**3. Transfer fit** — interior intrinsics from ~10k matches/lens:

```bash
conda run --no-capture-output -n calibration python Calibration/scripts/fit_factory_transfer.py \
    --raw <clip>.insv --stitched <studio_export>.mp4 --times 2,8,14,20 \
    --debug-dir /tmp/cal_debug -o /tmp/preset_candidate.json
```

Read the stderr report. Healthy run: per-θ-bin rms ≤ ~1.2 px out to 88°, both
lenses' k1/k2 within a few % of each other (same optics — a strong consistency
check), and `composite_t*.png` / `diff_t*.png` in the debug dir show a clean
stitch / near-black diff vs Studio. Stability check: rerun with two disjoint
`--times` subsets; f should agree to ≲0.1%.

Do **not** bother with `--telemetry/--anchor-weight`: the telemetry image-circle
radius is in photo-sensor pixels and video modes are *not* a pure rescale of the
sensor (measured disc ≈900 px vs naively-scaled 964 px), so the anchor is wrong
for video frames. Off by default.

**4. Distant-seam refinement** — fix the 85–91° tail on real water footage.
Pick a processed pipeline mp4 (same camera!) with distant content abeam
(shoreline, other boats — the seam looks out the sides of the boat):

```bash
conda run --no-capture-output -n calibration python Calibration/scripts/refine_seam_distance.py \
    --video /Users/viola/Data/Insta360/VID/<race>.mp4 \
    --preset /tmp/preset_candidate.json -o /tmp/preset_refined.json
```

Healthy run: dozens of far-field matches, δr ≈ 0 below 60° and a few px at 90°,
shared correction similar in spirit to 2026-06: `δr(90°) ≈ −6 px`. Fixed-point
check: rerun with the refined preset as `--preset` — δk should come back ≈ 0.
If dy (vertical) residuals are large, the transfer fit's euler is off — rerun
step 3 rather than reaching for `--fit-euler`.

**5. Validate visually, then ship.**

```bash
cp /tmp/preset_refined.json Src/src/assets/calibration/default.json
cd Src && npm run start     # eyeball sailview seam on a race video, then:
cd Src && npm run deploy
```

`calibrate.html` (unauthenticated dev tool) opens looking at a seam with the
seam-debug overlay on — use it for fine inspection; **Import Preset** loads any
candidate JSON.

## Validation numbers achieved (2026-06-09, X3 `IAQEF2403KW7YY`)

| check | result |
|---|---|
| transfer fit rms, 0–75° | 0.5–0.8 px per lens |
| transfer fit rms, 75–88° (rim, real matches) | 1.2 px |
| disjoint-subset stability | f Δ≤0.05%, k1 Δ≤2%, k2 Δ≤5% |
| independent ChArUco cross-check (center region) | f agrees to 0.2%, center to ~1.5 px |
| front-vs-back lens consistency | k1/k2 within 3% |
| distant seam dy (after euler fix) | ≤0.07° |
| distant seam dx before → after tail refinement | ±1.3° → ≤0.1° |

Final preset: front f=519.8 px, k=(+0.0775, −0.0175); back f=520.8 px,
k=(+0.0770, −0.0170); back euler z = +0.695° (shader convention); aperture
0.2707 (was 0.296 with zero distortion).

## Known residuals / future work

- **Exposure step at the seam.** The lenses auto-expose independently; when one
  faces the sun the seam shows a brightness jump. Fixable with a luminance-match
  term in the shader blend. Not done.
- **Near-field seam parallax** (~0.7° at 2 m): inherent, see above.
- **Single global preset** assumes one camera. On camera swap, re-run this
  procedure; on multi-camera, `videos.json` would need a per-video preset field.

## File inventory

- `Calibration/scripts/fit_factory_transfer.py` — stage 1: Studio-stitch
  transfer fit (bootstrap SIFT + Kabsch → render/rematch/refit rounds → preset).
- `Calibration/scripts/refine_seam_distance.py` — stage 2: distant-seam tail
  refinement (`preset_to_params` is also the reference for preset→pixel-model
  conversion, including the GLSL euler transpose).
- `Calibration/scripts/fit_insv_v3_distortion.py` — negative result, kept as
  documentation: offset_v3 polynomial decode attempts, all falsified.
- `Calibration/scripts/charuco_calibrate.py`, `draw_charuco_detections.py` —
  board-based path with bootstrap stability gate; superseded but useful for
  independent center-region cross-checks.
- `docs/INSTA360_EMBEDDED_CALIBRATION.md` — telemetry extraction + v1 block
  details (still the reference for what's *in* the `.insv`).
