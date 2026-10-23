"""Tests for the fisheye projection module (fisheye_projection.py).

Validates that the Python port of the viewer's shader math produces
correct UV coordinates for known camera configurations.
"""

from __future__ import annotations

import json
from typing import Dict

import numpy as np
import pytest

import fisheye_projection as fp


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def legacy_params() -> Dict:
    """Params derived from legacy_guess.json (aperture=0.296, centered lenses)."""
    return fp.preset_to_params({
        "schemaVersion": 1,
        "calibration": {
            "fov": 180.0,
            "front": {"center": [0.25, 0.5], "scale": [0.5, 1.0],
                       "euler": [0.0, 0.0, 0.0], "distortion": [0.0, 0.0]},
            "back":  {"center": [0.75, 0.5], "scale": [-0.5, 1.0],
                       "euler": [0.0, 0.0, 0.0], "distortion": [0.0, 0.0]},
            "projection": {"aperture": 0.296, "viewCorrection": -0.2},
            "seam": {"centerYaw": 0.0, "width": 0.1, "feather": 1.0},
        },
    })


# ---------------------------------------------------------------------------
# Test 1: rotate_euler with zero euler is identity
# ---------------------------------------------------------------------------

def test_rotate_euler_identity() -> None:
    rays = np.array([
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
        [0.0, 0.0, 1.0],
        [0.577, 0.577, 0.577],
    ])
    result = fp.rotate_euler(rays, np.zeros(3))
    np.testing.assert_allclose(result, rays, atol=1e-14)


# ---------------------------------------------------------------------------
# Test 2: rotate_euler 90-deg yaw rotates X to Z
# ---------------------------------------------------------------------------

def test_rotate_euler_90_yaw() -> None:
    rays = np.array([[1.0, 0.0, 0.0]])
    euler = np.array([0.0, np.pi / 2, 0.0])  # 90 deg yaw around Y
    result = fp.rotate_euler(rays, euler)
    # Ry(pi/2) * [1,0,0] = [0, 0, -1] (shader convention: Ry has +sy in row 0 col 2)
    np.testing.assert_allclose(result, [[0.0, 0.0, -1.0]], atol=1e-14)


# ---------------------------------------------------------------------------
# Test 3: corrected_theta with zero distortion is identity
# ---------------------------------------------------------------------------

def test_corrected_theta_identity() -> None:
    theta = np.linspace(0, np.pi / 2, 100)
    result = fp.corrected_theta(theta, np.array([0.0, 0.0]))
    np.testing.assert_allclose(result, theta, atol=1e-14)


# ---------------------------------------------------------------------------
# Test 4: corrected_theta with positive k1 increases theta
# ---------------------------------------------------------------------------

def test_corrected_theta_positive_k1() -> None:
    theta = np.array([0.5, 1.0, 1.5])
    result = fp.corrected_theta(theta, np.array([0.1, 0.0]))
    assert np.all(result > theta), "Positive k1 should increase theta_d"


# ---------------------------------------------------------------------------
# Test 5: Front optical axis maps to front center UV
# ---------------------------------------------------------------------------

def test_front_axis_uv(legacy_params: Dict) -> None:
    ray = np.array([[0.0, 0.0, -1.0]])  # front optical axis
    uv = fp.world_ray_to_uv(ray, "front", legacy_params)
    np.testing.assert_allclose(uv, [[0.25, 0.5]], atol=1e-10)


# ---------------------------------------------------------------------------
# Test 6: Back optical axis maps to back center UV
# ---------------------------------------------------------------------------

def test_back_axis_uv(legacy_params: Dict) -> None:
    ray = np.array([[0.0, 0.0, 1.0]])  # back optical axis
    uv = fp.world_ray_to_uv(ray, "back", legacy_params)
    np.testing.assert_allclose(uv, [[0.75, 0.5]], atol=1e-10)


# ---------------------------------------------------------------------------
# Test 7: Overlap rays are visible to both lenses
# ---------------------------------------------------------------------------

def test_overlap_ray_sampling(legacy_params: Dict) -> None:
    rays = fp.sample_overlap_rays(legacy_params, n_yaw=60, n_pitch=20)
    assert rays.shape[0] > 0, "Should produce some overlap rays"

    front_uv = fp.world_ray_to_uv(rays, "front", legacy_params)
    back_uv = fp.world_ray_to_uv(rays, "back", legacy_params)

    assert np.all(front_uv[:, 0] >= 0) and np.all(front_uv[:, 0] <= 1), \
        "All front UVs should be in [0, 1]"
    assert np.all(front_uv[:, 1] >= 0) and np.all(front_uv[:, 1] <= 1), \
        "All front UVs should be in [0, 1]"
    assert np.all(back_uv[:, 0] >= 0) and np.all(back_uv[:, 0] <= 1), \
        "All back UVs should be in [0, 1]"
    assert np.all(back_uv[:, 1] >= 0) and np.all(back_uv[:, 1] <= 1), \
        "All back UVs should be in [0, 1]"


# ---------------------------------------------------------------------------
# Test 8: Overlap ray count scales with grid resolution
# ---------------------------------------------------------------------------

def test_overlap_ray_count_scales(legacy_params: Dict) -> None:
    rays_small = fp.sample_overlap_rays(legacy_params, n_yaw=30, n_pitch=10)
    rays_large = fp.sample_overlap_rays(legacy_params, n_yaw=60, n_pitch=20)
    assert rays_large.shape[0] > rays_small.shape[0], \
        "Higher resolution grid should produce more overlap rays"


# ---------------------------------------------------------------------------
# Test 9: sample_image_at_uvs bilinear interpolation
# ---------------------------------------------------------------------------

def test_sample_image_bilinear() -> None:
    # 4x4 gradient image
    img = np.arange(16, dtype=np.float64).reshape(4, 4)
    # UV at the centre of the image
    uvs = np.array([[0.5, 0.5]])
    vals = fp.sample_image_at_uvs(img, uvs)
    # Centre of a 4x4 image: pixel (1.5, 1.5) -> average of 5, 6, 9, 10 = 7.5
    np.testing.assert_allclose(vals, [7.5], atol=1e-10)


# ---------------------------------------------------------------------------
# Test 10: sample_image_at_uvs out-of-bounds returns NaN
# ---------------------------------------------------------------------------

def test_sample_image_oob_nan() -> None:
    img = np.ones((10, 10), dtype=np.float64)
    uvs = np.array([[-0.1, 0.5], [1.1, 0.5], [0.5, -0.1]])
    vals = fp.sample_image_at_uvs(img, uvs)
    assert np.all(np.isnan(vals)), "Out-of-bounds UVs should produce NaN"


# ---------------------------------------------------------------------------
# Test 11: seam_blend_weight at extremes
# ---------------------------------------------------------------------------

def test_seam_blend_weight_extremes(legacy_params: Dict) -> None:
    # Pure front ray (z << 0) should have weight ~ 0
    # Pure back ray  (z >> 0) should have weight ~ 1
    rays = np.array([
        [0.0, 0.0, -1.0],   # front
        [0.0, 0.0, 1.0],    # back
    ])
    weights = fp.seam_blend_weight(rays, legacy_params)
    assert weights[0] < 0.01, f"Front ray should have near-zero blend weight, got {weights[0]}"
    assert weights[1] > 0.99, f"Back ray should have near-one blend weight, got {weights[1]}"


# ---------------------------------------------------------------------------
# Test 12: Front/back UVs are symmetric for symmetric preset
# ---------------------------------------------------------------------------

def test_front_back_uv_symmetry(legacy_params: Dict) -> None:
    """For an equatorial ray pointing along +X, front and back UVs should
    be symmetric across the image centre line (u=0.5)."""
    ray = np.array([[1.0, 0.0, 0.0]])
    front_uv = fp.world_ray_to_uv(ray, "front", legacy_params)
    back_uv = fp.world_ray_to_uv(ray, "back", legacy_params)

    # u values should be symmetric around 0.5
    np.testing.assert_allclose(front_uv[0, 0] + back_uv[0, 0], 1.0, atol=1e-10,
                               err_msg="Front and back u should sum to 1.0 for +X ray")
    # v values should both be 0.5 (equator)
    np.testing.assert_allclose(front_uv[0, 1], 0.5, atol=1e-10)
    np.testing.assert_allclose(back_uv[0, 1], 0.5, atol=1e-10)
