"""Tests for the simplified overlap refinement script (refine_overlap.py).

These tests verify:
1. ``preset_to_camera_matrices`` correctly converts normalized preset values to
   pixel-space K and D matrices.
2. ``filter_paired_observations`` accepts quality pairs and rejects weak ones
   with descriptive reasons.
3. ``estimate_back_euler_from_pairs`` recovers the GT back-lens misalignment
   from noise-free projected observations within 0.02 rad.
4. ``extract_seam_yaw`` correctly routes large yaw deltas into seam.centerYaw
   and caps back.euler.
5. End-to-end: the output preset preserves all fixed fields and only updates
   ``back.euler`` and ``seam.centerYaw``.

The GT values used here are the same as in test_fitting.py (see
``generate_synthetic_dual_fisheye.py`` constants).
"""

from __future__ import annotations

import copy
import json
import tempfile
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

import generate_synthetic_dual_fisheye as gen
import refine_overlap


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_minimal_preset(
    aperture: float = 0.496,
    back_euler: List[float] = None,
    seam_center_yaw: float = 0.0,
) -> Dict:
    """Build a minimal valid preset matching the synthetic camera model."""
    if back_euler is None:
        back_euler = [0.0, 0.0, 0.0]
    half_w = gen.HALF_WIDTH
    half_h = gen.HALF_HEIGHT
    full_w = half_w * 2

    return {
        "schemaVersion": 1,
        "name": "test-preset",
        "calibration": {
            "fov": 180.0,
            "front": {
                "center": [gen.K_FRONT[0, 2] / full_w, gen.K_FRONT[1, 2] / half_h],
                "scale": [0.5, 1.0],
                "euler": [0.0, 0.0, 0.0],
                "distortion": [float(gen.D_FRONT[0]), float(gen.D_FRONT[1])],
            },
            "back": {
                "center": [(half_w + gen.K_BACK[0, 2]) / full_w, gen.K_BACK[1, 2] / half_h],
                "scale": [-0.5, 1.0],
                "euler": [float(v) for v in back_euler],
                "distortion": [float(gen.D_BACK[0]), float(gen.D_BACK[1])],
            },
            "projection": {
                "aperture": aperture,
                "viewCorrection": -0.2,
            },
            "seam": {
                "centerYaw": seam_center_yaw,
                "width": 0.1,
                "feather": 1.0,
            },
        },
    }


def _make_obs(
    side: str,
    pair_key: str,
    num_corners: int = 30,
    image_size: List[int] = None,
    pts3d: List = None,
    pts2d: List = None,
    edge_margin: float = 50.0,
) -> Dict:
    """Build a synthetic observation dict for filter/euler tests."""
    if image_size is None:
        image_size = [gen.HALF_WIDTH, gen.HALF_HEIGHT]
    w, h = image_size

    if pts3d is None:
        # Distribute corners in a 5x6 grid well inside the frame
        pts3d = []
        pts2d = []
        xs = np.linspace(edge_margin + 10, w - edge_margin - 10, 6)
        ys = np.linspace(edge_margin + 10, h - edge_margin - 10, 5)
        sq = gen.SQUARE_LENGTH_M
        for i, y in enumerate(ys):
            for j, x in enumerate(xs):
                pts3d.append([j * sq, i * sq, 0.0])
                pts2d.append([float(x), float(y)])
        pts3d = pts3d[:num_corners]
        pts2d = pts2d[:num_corners]

    return {
        "source_type": "synthetic",
        "source_path": "dummy",
        "pair_key": pair_key,
        "frame_index": 0,
        "side": side,
        "image_size": image_size,
        "num_corners": num_corners,
        "charuco_ids": list(range(num_corners)),
        "object_points": pts3d,
        "image_points": pts2d,
    }


# ---------------------------------------------------------------------------
# Test 1: preset_to_camera_matrices -- round-trip
# ---------------------------------------------------------------------------

def test_preset_to_camera_matrices_roundtrip() -> None:
    """K values produced from a GT-derived preset must match the GT K matrices."""
    front_size = (gen.HALF_WIDTH, gen.HALF_HEIGHT)
    back_size = (gen.HALF_WIDTH, gen.HALF_HEIGHT)

    # Use the exact GT aperture so the round-trip is exact.
    aperture = gen.APERTURE_FRONT_GT
    preset = _make_minimal_preset(aperture=aperture)

    Kf, Df = refine_overlap.preset_to_camera_matrices(preset, "front", front_size, back_size)
    Kb, Db = refine_overlap.preset_to_camera_matrices(preset, "back", front_size, back_size)

    # fx/fy
    assert abs(Kf[0, 0] - gen.K_FRONT[0, 0]) < 1.0, (
        f"Front fx: got {Kf[0, 0]:.2f}, expected ~{gen.K_FRONT[0, 0]:.2f}"
    )
    # Preset uses a single shared aperture (front's GT value), so back fx will
    # differ from K_BACK GT by the 2-pixel front/back focal-length difference.
    # Tolerance 3 px covers this expected systematic offset.
    assert abs(Kb[0, 0] - gen.K_BACK[0, 0]) < 3.0, (
        f"Back fx: got {Kb[0, 0]:.2f}, expected ~{gen.K_BACK[0, 0]:.2f}"
    )

    # cx/cy
    assert abs(Kf[0, 2] - gen.K_FRONT[0, 2]) < 1.0, (
        f"Front cx: got {Kf[0, 2]:.2f}, expected {gen.K_FRONT[0, 2]:.2f}"
    )
    assert abs(Kf[1, 2] - gen.K_FRONT[1, 2]) < 1.0, (
        f"Front cy: got {Kf[1, 2]:.2f}, expected {gen.K_FRONT[1, 2]:.2f}"
    )

    # Distortion
    assert abs(Df[0, 0] - gen.D_FRONT[0]) < 1e-6, f"Front k1 mismatch: {Df[0, 0]}"
    assert abs(Db[0, 0] - gen.D_BACK[0]) < 1e-6, f"Back k1 mismatch: {Db[0, 0]}"


# ---------------------------------------------------------------------------
# Test 2: filter_paired_observations -- accept a good pair
# ---------------------------------------------------------------------------

def test_filter_accepts_good_pair() -> None:
    """A pair with both sides having plenty of well-placed corners is accepted."""
    obs = [
        _make_obs("front", "pair_0", num_corners=30, edge_margin=60.0),
        _make_obs("back", "pair_0", num_corners=30, edge_margin=60.0),
    ]
    accepted, dropped = refine_overlap.filter_paired_observations(
        obs, min_corners=20, min_edge_margin=40.0, min_area_frac=0.001
    )
    assert "pair_0" in accepted, f"Expected pair_0 to be accepted; dropped={dropped}"
    assert len(dropped) == 0


# ---------------------------------------------------------------------------
# Test 3: filter_paired_observations -- drop a pair with too few corners
# ---------------------------------------------------------------------------

def test_filter_drops_few_corners() -> None:
    """A front observation with too few corners causes the pair to be dropped."""
    obs = [
        _make_obs("front", "pair_1", num_corners=8),   # below default min 20
        _make_obs("back", "pair_1", num_corners=30),
    ]
    accepted, dropped = refine_overlap.filter_paired_observations(
        obs, min_corners=20, min_edge_margin=10.0, min_area_frac=0.0
    )
    assert "pair_1" not in accepted
    assert "pair_1" in dropped
    assert "corners" in dropped["pair_1"].lower()


# ---------------------------------------------------------------------------
# Test 4: filter_paired_observations -- drop a pair missing one side
# ---------------------------------------------------------------------------

def test_filter_drops_missing_side() -> None:
    """A pair with only the front side is dropped."""
    obs = [
        _make_obs("front", "front_only"),
    ]
    accepted, dropped = refine_overlap.filter_paired_observations(
        obs, min_corners=10, min_edge_margin=5.0, min_area_frac=0.0
    )
    assert "front_only" not in accepted
    assert "front_only" in dropped


# ---------------------------------------------------------------------------
# Test 5: estimate_back_euler_from_pairs recovers GT within 0.02 rad
# ---------------------------------------------------------------------------

def test_estimate_back_euler_accuracy(projected_observations: List[Dict]) -> None:
    """Overlap refinement's euler estimator must recover GT within 0.02 rad.

    Uses the same noise-free projected observations as test_fitting.py.
    The intrinsics come directly from the GT constants so calibration noise
    is not a confound.
    """
    Kf = gen.K_FRONT.copy()
    Df = gen.D_FRONT.reshape(4, 1)
    Kb = gen.K_BACK.copy()
    Db = gen.D_BACK.reshape(4, 1)

    # Build accepted-pairs dict (both sides must be present)
    by_key: Dict[str, Dict] = {}
    for obs in projected_observations:
        by_key.setdefault(obs["pair_key"], {})[obs["side"]] = obs
    accepted = {k: v for k, v in by_key.items() if "front" in v and "back" in v}

    assert len(accepted) >= 5, f"Need >= 5 pairs for a meaningful test, got {len(accepted)}"

    fitted_euler, notes = refine_overlap.estimate_back_euler_from_pairs(
        accepted, Kf, Df, Kb, Db
    )
    gt_euler = gen.BACK_EULER_GT.tolist()

    tol = 0.02  # radians (~1.1 degrees)
    component_names = ["pitch (x)", "yaw (y)", "roll (z)"]
    for name, fitted, gt in zip(component_names, fitted_euler, gt_euler):
        err = abs(fitted - gt)
        assert err < tol, (
            f"Back euler {name}: fitted={np.degrees(fitted):.3f}°  "
            f"GT={np.degrees(gt):.3f}°  error={np.degrees(err):.3f}°  "
            f"(tolerance {np.degrees(tol):.1f}°)"
        )


# ---------------------------------------------------------------------------
# Test 6: extract_seam_yaw -- small delta stays in back.euler
# ---------------------------------------------------------------------------

def test_extract_seam_yaw_small_delta() -> None:
    """A yaw delta smaller than the cap stays in back.euler; seam shift is zero."""
    seed_euler = [0.0, 0.0, 0.0]
    raw_euler = [0.001, 0.005, 0.001]   # yaw delta = 0.005 < 0.015 cap

    capped, shift = refine_overlap.extract_seam_yaw(raw_euler, seed_euler)

    assert abs(capped[1] - 0.005) < 1e-9, f"Expected yaw unchanged, got {capped[1]}"
    assert abs(shift) < 1e-9, f"Expected no seam shift, got {shift}"


# ---------------------------------------------------------------------------
# Test 7: extract_seam_yaw -- large delta is split
# ---------------------------------------------------------------------------

def test_extract_seam_yaw_large_delta() -> None:
    """A yaw delta larger than the cap is split: lens gets the cap, seam gets excess."""
    LENS_YAW_CAP = 0.015
    seed_euler = [0.0, 0.0, 0.0]
    raw_euler = [0.0, 0.05, 0.0]   # yaw delta = 0.05 >> 0.015 cap

    capped, shift = refine_overlap.extract_seam_yaw(raw_euler, seed_euler)

    assert abs(capped[1] - LENS_YAW_CAP) < 1e-9, (
        f"Capped yaw should be {LENS_YAW_CAP}, got {capped[1]}"
    )
    expected_shift = 0.05 - LENS_YAW_CAP
    assert abs(shift - expected_shift) < 1e-9, (
        f"Seam shift should be {expected_shift:.4f}, got {shift:.4f}"
    )


# ---------------------------------------------------------------------------
# Test 8: end-to-end via main() -- fixed fields preserved, refined fields updated
# ---------------------------------------------------------------------------

def test_end_to_end_preserves_fixed_fields(
    tmp_path: Path,
    projected_observations: List[Dict],
) -> None:
    """Running refine_overlap.main() must:
    - keep front/back center, scale, distortion, projection.aperture unchanged
    - update back.euler to a value close to GT
    - write a valid schemaVersion=1 preset
    """
    # Write a seed preset based on the GT intrinsics
    seed_preset = _make_minimal_preset(
        aperture=gen.APERTURE_FRONT_GT,
        back_euler=[0.0, 0.0, 0.0],
        seam_center_yaw=0.0,
    )
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed_preset))

    # Wrap projected_observations in the detect_charuco.py schema
    obs_payload = {"schema_version": 1, "observations": projected_observations}
    obs_path = tmp_path / "obs.json"
    obs_path.write_text(json.dumps(obs_payload))

    out_path = tmp_path / "refined.json"

    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = [
        "refine_overlap.py",
        "--observations", str(obs_path),
        "--seed-preset", str(seed_path),
        "--output", str(out_path),
        "--min-corners", "10",
        "--min-edge-margin", "0",
        "--min-area-frac", "0",
        "--min-pairs", "5",
    ]
    try:
        refine_overlap.main()
    finally:
        _sys.argv = old_argv

    assert out_path.exists(), "Output preset was not written"

    with out_path.open() as f:
        result = json.load(f)

    assert result.get("schemaVersion") == 1

    cal = result["calibration"]
    seed_cal = seed_preset["calibration"]

    # Fixed fields must be identical to seed
    for side in ("front", "back"):
        assert cal[side]["center"] == seed_cal[side]["center"], (
            f"{side}.center changed unexpectedly"
        )
        assert cal[side]["scale"] == seed_cal[side]["scale"], (
            f"{side}.scale changed unexpectedly"
        )
        assert cal[side]["distortion"] == seed_cal[side]["distortion"], (
            f"{side}.distortion changed unexpectedly"
        )
    assert cal["projection"]["aperture"] == pytest.approx(seed_cal["projection"]["aperture"]), (
        "projection.aperture changed unexpectedly"
    )

    # back.euler must be updated and close to GT
    refined_euler = cal["back"]["euler"]
    gt_euler = gen.BACK_EULER_GT.tolist()
    tol = 0.02
    names = ["pitch (x)", "yaw (y)", "roll (z)"]
    for name, refined, gt in zip(names, refined_euler, gt_euler):
        err = abs(refined - gt)
        assert err < tol, (
            f"End-to-end back euler {name}: refined={np.degrees(refined):.3f}°  "
            f"GT={np.degrees(gt):.3f}°  error={np.degrees(err):.3f}°"
        )

    # Diagnostics block must be present
    diag = result.get("fitDiagnostics", {})
    assert diag.get("mode") == "overlap_refinement"
    assert "acceptedPairs" in diag
    assert diag["acceptedPairs"] >= 5
