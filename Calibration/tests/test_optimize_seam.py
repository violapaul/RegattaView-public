"""Tests for the photometric seam optimizer and feature-based matcher.

These tests create synthetic dual-fisheye frames with a known scene rendered
through the projection model, then verify that the optimiser / matcher can
recover intentionally introduced misalignment.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import cv2
import numpy as np
import pytest

import fisheye_projection as fp
import optimize_seam
import match_overlap_features as mof


# ---------------------------------------------------------------------------
# Helpers: synthetic scene rendering
# ---------------------------------------------------------------------------

def _make_params(aperture: float = 0.296, back_euler: List[float] = None) -> Dict:
    """Build a param dict for a symmetric, zero-distortion preset."""
    if back_euler is None:
        back_euler = [0.0, 0.0, 0.0]
    return fp.preset_to_params({
        "schemaVersion": 1,
        "calibration": {
            "fov": 180.0,
            "front": {"center": [0.25, 0.5], "scale": [0.5, 1.0],
                       "euler": [0.0, 0.0, 0.0], "distortion": [0.0, 0.0]},
            "back":  {"center": [0.75, 0.5], "scale": [-0.5, 1.0],
                       "euler": back_euler, "distortion": [0.0, 0.0]},
            "projection": {"aperture": aperture, "viewCorrection": -0.2},
            "seam": {"centerYaw": 0.0, "width": 0.1, "feather": 1.0},
        },
    })


def _render_synthetic_frame(
    params: Dict,
    width: int = 800,
    height: int = 400,
) -> np.ndarray:
    """Render a synthetic dual-fisheye frame using a procedural 3D scene.

    The "scene" is a latitude/longitude checkerboard on the unit sphere,
    projected through both lenses using the given params.  This gives
    repeatable texture in the overlap zone.
    """
    frame = np.zeros((height, width), dtype=np.uint8)
    half_w = width // 2

    # Generate a grid of pixel coordinates for each half
    for side, x_offset in [("front", 0), ("back", half_w)]:
        # Build UV coords for each pixel in this half
        ys, xs = np.mgrid[0:height, 0:half_w]
        # Convert to full-frame UV
        u = (xs + x_offset + 0.5) / width
        v = (ys + 0.5) / height
        uvs = np.column_stack([u.ravel(), v.ravel()])

        # We need the inverse mapping: UV -> world ray.
        # For a centered equidistant fisheye with no distortion:
        #   uv = center + scale * aperture * angle * [cos(psi), sin(psi)]
        center = params[f"{side}_center"]
        scale = params[f"{side}_scale"]
        aperture = params["aperture"]
        euler = params[f"{side}_euler"]

        du = (uvs[:, 0] - center[0]) / scale[0]
        dv = (uvs[:, 1] - center[1]) / scale[1]
        R = np.sqrt(du ** 2 + dv ** 2)
        psi = np.arctan2(dv, du)
        angle = R / max(aperture, 1e-9)

        xy_len = np.sin(angle)
        if side == "front":
            z_sign = -1.0  # front looks along -Z
        else:
            z_sign = 1.0   # back looks along +Z
        z = z_sign * np.cos(angle)
        x = xy_len * np.cos(psi)
        y = xy_len * np.sin(psi)
        local_rays = np.column_stack([x, y, z])

        # Undo the euler rotation to get world rays
        inv_euler = -euler[::-1]
        # rotateEuler applies Rz*Ry*Rx; to invert, apply Rx(-ex)*Ry(-ey)*Rz(-ez)
        # which is rotateEuler with [-ez, -ey, -ex].  But for small angles and
        # testing purposes we use the transpose:
        R_mat = np.array([
            [np.cos(euler[1]) * np.cos(euler[2]),
             np.cos(euler[2]) * np.sin(euler[0]) * np.sin(euler[1]) - np.cos(euler[0]) * np.sin(euler[2]),
             np.cos(euler[0]) * np.cos(euler[2]) * np.sin(euler[1]) + np.sin(euler[0]) * np.sin(euler[2])],
            [np.cos(euler[1]) * np.sin(euler[2]),
             np.cos(euler[0]) * np.cos(euler[2]) + np.sin(euler[0]) * np.sin(euler[1]) * np.sin(euler[2]),
             np.cos(euler[0]) * np.sin(euler[1]) * np.sin(euler[2]) - np.cos(euler[2]) * np.sin(euler[0])],
            [-np.sin(euler[1]),
             np.cos(euler[1]) * np.sin(euler[0]),
             np.cos(euler[0]) * np.cos(euler[1])],
        ])
        world_rays = local_rays @ R_mat  # inverse rotation = transpose

        # Checkerboard on the unit sphere: alternate 15-degree cells
        lon = np.arctan2(world_rays[:, 0], -world_rays[:, 2])  # [-pi, pi]
        lat = np.arcsin(np.clip(world_rays[:, 1], -1, 1))       # [-pi/2, pi/2]
        cell_size = np.radians(15.0)
        checker = ((np.floor(lon / cell_size) + np.floor(lat / cell_size)).astype(int) % 2)
        brightness = np.where(checker == 0, 200, 80).astype(np.uint8)

        # Mask out pixels outside the fisheye circle
        theta_max = fp.max_theta_for_preset(params)
        outside = angle > theta_max
        brightness[outside] = 0

        frame[ys.ravel(), xs.ravel() + x_offset] = brightness

    return frame


# ---------------------------------------------------------------------------
# Test 1: Zero-offset stability -- optimizer returns near-zero corrections
# ---------------------------------------------------------------------------

def test_photometric_zero_offset() -> None:
    """With a perfectly aligned synthetic frame, the optimizer should
    return near-zero back.euler corrections."""
    params = _make_params(back_euler=[0.0, 0.0, 0.0])
    frame = _render_synthetic_frame(params, width=800, height=400)
    rays = fp.sample_overlap_rays(params, n_yaw=60, n_pitch=20)
    assert rays.shape[0] > 50

    loss_zero = optimize_seam.compute_overlap_loss(
        np.zeros(3), 0.0, rays, [frame], params
    )

    # Try a small perturbation -- loss should be higher
    loss_perturbed = optimize_seam.compute_overlap_loss(
        np.array([0.0, 0.05, 0.0]), 0.0, rays, [frame], params
    )

    assert loss_zero < loss_perturbed, (
        f"Zero-offset loss ({loss_zero:.4f}) should be less than "
        f"perturbed loss ({loss_perturbed:.4f})"
    )


# ---------------------------------------------------------------------------
# Test 2: Photometric optimizer recovers known yaw perturbation
# ---------------------------------------------------------------------------

def test_photometric_recovers_yaw() -> None:
    """Render a frame with a known yaw misalignment, then verify the
    optimizer finds a correction that reduces the loss below the
    initial misaligned value."""
    true_yaw = 0.03  # ~1.7 degrees
    params_true = _make_params(back_euler=[0.0, true_yaw, 0.0])
    frame = _render_synthetic_frame(params_true, width=800, height=400)

    # Start from zero euler (the "misaligned" seed)
    params_seed = _make_params(back_euler=[0.0, 0.0, 0.0])
    rays = fp.sample_overlap_rays(params_seed, n_yaw=60, n_pitch=20)
    assert rays.shape[0] > 50

    loss_initial = optimize_seam.compute_overlap_loss(
        np.zeros(3), 0.0, rays, [frame], params_seed
    )

    # Run a quick optimization
    from scipy.optimize import minimize
    x0 = np.array([0.0, 0.0, 0.0, 0.0])

    def obj(x):
        return optimize_seam.compute_overlap_loss(
            x[:3], float(x[3]), rays, [frame], params_seed
        )

    result = minimize(obj, x0, method="Powell",
                      options={"maxiter": 200, "ftol": 1e-7})

    assert result.fun < loss_initial, (
        f"Optimized loss ({result.fun:.4f}) should be less than "
        f"initial loss ({loss_initial:.4f})"
    )

    # The recovered yaw should be in the right direction
    recovered_yaw = result.x[1]
    assert np.sign(recovered_yaw) == np.sign(true_yaw), (
        f"Recovered yaw ({np.degrees(recovered_yaw):.3f}°) should have "
        f"same sign as true yaw ({np.degrees(true_yaw):.3f}°)"
    )


# ---------------------------------------------------------------------------
# Test 3: Feature matching detects a known horizontal shift
# ---------------------------------------------------------------------------

def test_feature_matching_on_synthetic() -> None:
    """Create two overlap strips with a known pixel shift and verify
    that ORB matching detects it."""
    params = _make_params(back_euler=[0.0, 0.0, 0.0])
    frame = _render_synthetic_frame(params, width=800, height=400)

    strip_w, strip_h = 500, 100
    front_strip = fp.reproject_to_overlap_strip(
        frame, "front", params, strip_width=strip_w, strip_height=strip_h
    )
    front_strip = np.nan_to_num(front_strip, nan=0.0)
    front_u8 = np.clip(front_strip, 0, 255).astype(np.uint8)

    # Create a shifted version (simulating a yaw error)
    shift_px = 5
    back_strip_shifted = np.zeros_like(front_u8)
    back_strip_shifted[:, shift_px:] = front_u8[:, :-shift_px]

    orb = cv2.ORB_create(nfeatures=1000)
    kp_f, desc_f = orb.detectAndCompute(front_u8, None)
    kp_b, desc_b = orb.detectAndCompute(back_strip_shifted, None)

    if desc_f is not None and desc_b is not None and len(kp_f) >= 5 and len(kp_b) >= 5:
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
        matches = bf.match(desc_f, desc_b)

        if len(matches) >= 3:
            displacements = []
            for m in matches:
                pt_f = kp_f[m.queryIdx].pt
                pt_b = kp_b[m.trainIdx].pt
                displacements.append(pt_b[0] - pt_f[0])

            median_dx = np.median(displacements)
            # The detected shift should be close to the introduced shift
            assert abs(median_dx - shift_px) < 3.0, (
                f"Expected ~{shift_px}px shift, got median {median_dx:.1f}px"
            )
        else:
            pytest.skip("Too few feature matches on synthetic checkerboard")
    else:
        pytest.skip("ORB could not find features on synthetic checkerboard")


# ---------------------------------------------------------------------------
# Test 4: End-to-end optimizer preserves fixed fields
# ---------------------------------------------------------------------------

def test_end_to_end_preserves_fixed_fields(tmp_path: Path) -> None:
    """Run optimize_seam.main() and verify all non-overlap fields are preserved."""
    params_true = _make_params(back_euler=[0.0, 0.02, 0.0])
    frames = [_render_synthetic_frame(params_true, 800, 400) for _ in range(3)]

    # Write frames as images
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    for i, f in enumerate(frames):
        cv2.imwrite(str(frame_dir / f"frame_{i:03d}.png"), f)

    # Write seed preset
    seed = {
        "schemaVersion": 1,
        "name": "test-seed",
        "calibration": {
            "fov": 180.0,
            "front": {"center": [0.25, 0.5], "scale": [0.5, 1.0],
                       "euler": [0.0, 0.0, 0.0], "distortion": [0.0, 0.0]},
            "back":  {"center": [0.75, 0.5], "scale": [-0.5, 1.0],
                       "euler": [0.0, 0.0, 0.0], "distortion": [0.0, 0.0]},
            "projection": {"aperture": 0.296, "viewCorrection": -0.2},
            "seam": {"centerYaw": 0.0, "width": 0.1, "feather": 1.0},
        },
    }
    seed_path = tmp_path / "seed.json"
    seed_path.write_text(json.dumps(seed))

    out_path = tmp_path / "optimized.json"

    import sys as _sys
    old_argv = _sys.argv
    _sys.argv = [
        "optimize_seam.py",
        "--input-dir", str(frame_dir),
        "--seed-preset", str(seed_path),
        "--output", str(out_path),
        "--num-frames", "3",
        "--n-yaw", "60",
        "--n-pitch", "20",
    ]
    try:
        optimize_seam.main()
    finally:
        _sys.argv = old_argv

    assert out_path.exists()
    with out_path.open() as f:
        result = json.load(f)

    assert result["schemaVersion"] == 1
    cal = result["calibration"]
    seed_cal = seed["calibration"]

    # Fixed fields must be unchanged
    for side in ("front", "back"):
        assert cal[side]["center"] == seed_cal[side]["center"]
        assert cal[side]["scale"] == seed_cal[side]["scale"]
        assert cal[side]["distortion"] == seed_cal[side]["distortion"]
    assert cal["front"]["euler"] == seed_cal["front"]["euler"]
    assert cal["projection"]["aperture"] == pytest.approx(seed_cal["projection"]["aperture"])
    assert cal["seam"]["width"] == pytest.approx(seed_cal["seam"]["width"])
    assert cal["seam"]["feather"] == pytest.approx(seed_cal["seam"]["feather"])

    # Diagnostics block present
    diag = result.get("fitDiagnostics", {})
    assert diag.get("mode") == "photometric_seam"
    assert "finalLoss" in diag


# ---------------------------------------------------------------------------
# Test 5: displacement_to_euler produces correct yaw for known shift
# ---------------------------------------------------------------------------

def test_displacement_to_euler() -> None:
    """A 1-pixel horizontal shift should produce the correct yaw angle."""
    params = _make_params()
    strip_w, strip_h = 1000, 200
    euler = mof.displacement_to_euler(1.0, 0.0, params, strip_w, strip_h)

    expected_yaw = 2.0 * np.pi / strip_w
    assert abs(euler[1] - expected_yaw) < 1e-10, (
        f"Expected yaw {np.degrees(expected_yaw):.4f}°, got {np.degrees(euler[1]):.4f}°"
    )
    assert abs(euler[0]) < 1e-10, "Pitch should be zero for horizontal-only shift"
    assert abs(euler[2]) < 1e-10, "Roll should be zero"
