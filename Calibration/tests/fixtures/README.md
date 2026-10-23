# Synthetic Calibration Fixtures

This directory is for optional, manually generated synthetic preview assets.
The pytest suite does not read fixtures from here anymore; it generates a
deterministic dataset into a temporary directory at runtime.

## What these images are

Each PNG is a synthetic dual-fisheye frame (`1920 x 960` pixels, 2:1 aspect ratio matching real Insta360 footage):

- **Left half (columns 0–959)**: view from the *front* fisheye lens
- **Right half (columns 960–1919)**: view from the *back* fisheye lens
- Background is neutral gray (128); the Charuco board is rendered in black/white
  using a reverse-map fisheye projection from the ground-truth camera model

The images were generated with known ground-truth camera parameters so that
the test suite can verify detection accuracy and calibration recovery without
real camera footage.

## Ground truth camera model

| Parameter | Front lens | Back lens |
|---|---|---|
| Focal length fx = fy (px) | 475 | 473 |
| Principal point cx, cy (px) | 480, 480 | 482, 481 |
| Distortion k1, k2 | +0.05, -0.01 | +0.04, -0.008 |
| Aperture (f / half_width) | 0.4948 | 0.4927 |
| Back residual euler (rad) | — | [0.005, -0.01, 0.003] |

See `generate_synthetic_dual_fisheye.py` for the full camera model and
coordinate convention documentation.

## Generating preview assets

If you want local preview images for inspection, generate them here with:

```bash
conda run -n calibration python Calibration/scripts/generate_synthetic_dual_fisheye.py \
  --output-dir Calibration/tests/fixtures \
  --num-images 40
```

These files are ignored by git. Re-run the tests separately to confirm the
generator and calibration pipeline still pass:

```bash
conda run -n calibration python -m pytest Calibration/tests/ -v
```
