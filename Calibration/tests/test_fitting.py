"""Stage 2: Calibration fitting tests using synthetic observations.

Tests verify that calibrate_side and estimate_residual_back_euler recover
intrinsic parameters close to the known ground truth used when generating
the synthetic images.

Ground truth (from generate_synthetic_dual_fisheye.py):
    K_FRONT:  fx=fy=475,  cx=480,  cy=480  (half_width=960, half_height=960)
    K_BACK:   fx=fy=473,  cx=482,  cy=481
    D_FRONT:  [0.05, -0.01, 0, 0]
    D_BACK:   [0.04, -0.008, 0, 0]
    back euler: [0.005, -0.01, 0.003] rad

Tolerance rationale:
    - Focal length / aperture: 5 % -- fisheye calibrator accuracy with ~25 views
    - Lens center UV: 0.01 (~10 px at half_width=960)
    - Distortion: sign only (magnitudes are noisy with limited data)
    - Back euler: 0.02 rad (~1.1 degrees) per component
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np
import pytest

import fit_dual_fisheye
import generate_synthetic_dual_fisheye as gen


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _split_obs(observations: List[Dict]):
    front = [o for o in observations if o["side"] == "front"]
    back = [o for o in observations if o["side"] == "back"]
    return front, back


def _calibrate_both(observations: List[Dict]):
    """Run calibrate_side for both lenses and return (Kf, Df, Kb, Db)."""
    front_obs, back_obs = _split_obs(observations)
    Kf, Df, _, _, _ = fit_dual_fisheye.calibrate_side(front_obs)
    Kb, Db, _, _, _ = fit_dual_fisheye.calibrate_side(back_obs)
    return Kf, Df, Kb, Db


# ---------------------------------------------------------------------------
# Test 1: Focal length recovery within 5 %
# ---------------------------------------------------------------------------

def test_calibrate_side_focal_length(synth_data: Dict, observations: List[Dict]) -> None:
    """Fitted focal lengths must be within 5 % of ground truth values."""
    Kf, _, Kb, _ = _calibrate_both(observations)

    gt_fx_front = gen.K_FRONT[0, 0]  # 475
    gt_fx_back = gen.K_BACK[0, 0]    # 473

    fitted_fx_front = Kf[0, 0]
    fitted_fx_back = Kb[0, 0]

    err_front = abs(fitted_fx_front - gt_fx_front) / gt_fx_front
    err_back = abs(fitted_fx_back - gt_fx_back) / gt_fx_back

    assert err_front < 0.05, (
        f"Front focal length: fitted={fitted_fx_front:.1f} "
        f"GT={gt_fx_front:.1f}, error={err_front:.1%} (tolerance 5 %)"
    )
    assert err_back < 0.05, (
        f"Back focal length: fitted={fitted_fx_back:.1f} "
        f"GT={gt_fx_back:.1f}, error={err_back:.1%} (tolerance 5 %)"
    )


# ---------------------------------------------------------------------------
# Test 2: Aperture within 3 %
# ---------------------------------------------------------------------------

def test_fitted_aperture(synth_data: Dict, observations: List[Dict]) -> None:
    """Aperture derived from fitted focal length must be within 3 % of GT."""
    Kf, _, Kb, _ = _calibrate_both(observations)

    half_w = gen.HALF_WIDTH
    fitted_ap_front = Kf[0, 0] / half_w
    fitted_ap_back = Kb[0, 0] / half_w

    gt_ap_front = gen.APERTURE_FRONT_GT   # 0.4948
    gt_ap_back = gen.APERTURE_BACK_GT     # 0.4927

    err_front = abs(fitted_ap_front - gt_ap_front) / gt_ap_front
    err_back = abs(fitted_ap_back - gt_ap_back) / gt_ap_back

    assert err_front < 0.03, (
        f"Front aperture: fitted={fitted_ap_front:.4f} "
        f"GT={gt_ap_front:.4f}, error={err_front:.1%} (tolerance 3 %)"
    )
    assert err_back < 0.03, (
        f"Back aperture: fitted={fitted_ap_back:.4f} "
        f"GT={gt_ap_back:.4f}, error={err_back:.1%} (tolerance 3 %)"
    )


# ---------------------------------------------------------------------------
# Test 3: Lens principal point within 0.01 UV
# ---------------------------------------------------------------------------

def test_fitted_center(synth_data: Dict, observations: List[Dict]) -> None:
    """Fitted principal point must be within 0.01 UV units of GT.

    GT front center UV: [480/1920, 480/960] = [0.25, 0.5]
    GT back  center UV: [(960+482)/1920, 481/960] = [0.7510, 0.5010]
    """
    Kf, _, Kb, _ = _calibrate_both(observations)

    half_w = gen.HALF_WIDTH
    half_h = gen.HALF_HEIGHT
    full_w = half_w * 2

    # GT values
    gt_front_cx_uv = gen.K_FRONT[0, 2] / full_w   # 480/1920 = 0.25
    gt_front_cy_uv = gen.K_FRONT[1, 2] / half_h   # 480/960 = 0.5
    gt_back_cx_uv = (half_w + gen.K_BACK[0, 2]) / full_w   # (960+482)/1920
    gt_back_cy_uv = gen.K_BACK[1, 2] / half_h              # 481/960

    # Fitted values
    fit_front_cx_uv = Kf[0, 2] / full_w
    fit_front_cy_uv = Kf[1, 2] / half_h
    fit_back_cx_uv = (half_w + Kb[0, 2]) / full_w
    fit_back_cy_uv = Kb[1, 2] / half_h

    tol = 0.01
    assert abs(fit_front_cx_uv - gt_front_cx_uv) < tol, (
        f"Front cx UV: fitted={fit_front_cx_uv:.4f} GT={gt_front_cx_uv:.4f}"
    )
    assert abs(fit_front_cy_uv - gt_front_cy_uv) < tol, (
        f"Front cy UV: fitted={fit_front_cy_uv:.4f} GT={gt_front_cy_uv:.4f}"
    )
    assert abs(fit_back_cx_uv - gt_back_cx_uv) < tol, (
        f"Back cx UV: fitted={fit_back_cx_uv:.4f} GT={gt_back_cx_uv:.4f}"
    )
    assert abs(fit_back_cy_uv - gt_back_cy_uv) < tol, (
        f"Back cy UV: fitted={fit_back_cy_uv:.4f} GT={gt_back_cy_uv:.4f}"
    )


# ---------------------------------------------------------------------------
# Test 4: Distortion coefficient sign correctness
# ---------------------------------------------------------------------------

def test_fitted_distortion_sign(synth_data: Dict, observations: List[Dict]) -> None:
    """The dominant distortion coefficient k1 must have the correct sign.

    GT: D_FRONT k1=+0.05, D_BACK k1=+0.04 (barrel distortion -- positive k1).
    k2 is a higher-order correction term (~1/5 the magnitude of k1) and
    is notoriously noisy with limited calibration data; its sign is not
    asserted here.
    """
    _, Df, _, Db = _calibrate_both(observations)

    assert Df[0, 0] > 0, f"Front k1 should be positive (GT +0.05), got {Df[0, 0]:.4f}"
    assert Db[0, 0] > 0, f"Back k1 should be positive (GT +0.04), got {Db[0, 0]:.4f}"


# ---------------------------------------------------------------------------
# Test 5: Back euler estimate within 0.02 rad per component
# ---------------------------------------------------------------------------

def test_back_euler_estimate(synth_data: Dict, projected_observations: List[Dict]) -> None:
    """estimate_residual_back_euler must recover the GT misalignment within 0.02 rad.

    Uses noise-free observations generated via direct projectPoints (no image
    rendering or aruco detection), providing controlled known-GT stereo pairs.
    See ``gen.generate_projected_observations`` for the construction details.

    GT back_euler = [0.005, -0.01, 0.003] radians
    Tolerance: 0.02 rad per component (~1.1 degrees)
    """
    # Use GT camera matrices directly -- this is a unit test of the rotation
    # estimation math, not of the calibration step.  Using exact intrinsics
    # removes calibration noise and tests whether estimate_residual_back_euler
    # correctly recovers BACK_EULER_GT from matched stereo observations.
    Kf = gen.K_FRONT.copy()
    Df = gen.D_FRONT.reshape(4, 1)
    Kb = gen.K_BACK.copy()
    Db = gen.D_BACK.reshape(4, 1)

    pair_map = fit_dual_fisheye.match_pairs(projected_observations)
    front_by_pair = {k: v["front"] for k, v in pair_map.items() if "front" in v}
    back_by_pair = {k: v["back"] for k, v in pair_map.items() if "back" in v}

    n_pairs = len(set(front_by_pair.keys()).intersection(back_by_pair.keys()))
    assert n_pairs >= 5, (
        f"Too few stereo pairs for euler estimation ({n_pairs}); "
        "check generate_projected_observations."
    )

    fitted_euler = fit_dual_fisheye.estimate_residual_back_euler(
        front_by_pair, back_by_pair, Kf, Df, Kb, Db
    )
    gt_euler = gen.BACK_EULER_GT.tolist()

    tol = 0.02  # radians (~1.1 degrees)
    names = ["pitch (x)", "yaw (y)", "roll (z)"]
    for name, fitted, gt in zip(names, fitted_euler, gt_euler):
        err = abs(fitted - gt)
        assert err < tol, (
            f"Back euler {name}: fitted={np.degrees(fitted):.3f}° "
            f"GT={np.degrees(gt):.3f}°, error={np.degrees(err):.3f}° "
            f"(tolerance {np.degrees(tol):.1f}°)"
        )


# ---------------------------------------------------------------------------
# Test 6: min_observations guard raises RuntimeError
# ---------------------------------------------------------------------------

def test_min_observations_guard() -> None:
    """fit_dual_fisheye.main should raise RuntimeError with too few observations."""
    import json
    import tempfile
    from pathlib import Path
    import argparse

    # Build a minimal valid observations payload with only 3 front + 3 back
    tiny_obs = []
    dummy_pts3d = [[[0.0, 0.0, 0.0]] * 14]  # not used, check fires before calibration
    for i in range(3):
        tiny_obs.append({
            "side": "front", "pair_key": f"f{i}", "frame_index": i,
            "source_type": "image", "source_path": "dummy",
            "image_size": [gen.HALF_WIDTH, gen.HALF_HEIGHT],
            "num_corners": 14, "charuco_ids": list(range(14)),
            "object_points": [[0.0, 0.0, 0.0]] * 14,
            "image_points": [[10.0 * j, 10.0 * j] for j in range(14)],
        })
    for i in range(3):
        tiny_obs.append({
            "side": "back", "pair_key": f"b{i}", "frame_index": i,
            "source_type": "image", "source_path": "dummy",
            "image_size": [gen.HALF_WIDTH, gen.HALF_HEIGHT],
            "num_corners": 14, "charuco_ids": list(range(14)),
            "object_points": [[0.0, 0.0, 0.0]] * 14,
            "image_points": [[10.0 * j, 10.0 * j] for j in range(14)],
        })

    payload = {"schema_version": 1, "observations": tiny_obs}

    with tempfile.TemporaryDirectory() as tmpdir:
        obs_path = Path(tmpdir) / "obs.json"
        out_path = Path(tmpdir) / "preset.json"
        obs_path.write_text(json.dumps(payload))

        # Patch sys.argv to call main() via argparse
        import sys as _sys
        old_argv = _sys.argv
        _sys.argv = [
            "fit_dual_fisheye.py",
            "--observations", str(obs_path),
            "--output", str(out_path),
            "--min-observations", "10",
        ]
        try:
            with pytest.raises(RuntimeError, match="Not enough observations"):
                fit_dual_fisheye.main()
        finally:
            _sys.argv = old_argv
