"""Python port of the dual-fisheye projection math from fisheye_shader.js.

This module provides the exact same world-ray-to-UV mapping used by the WebGL
viewer, enabling Python-side overlap analysis without needing a GPU.

All public functions are vectorised over ``(N, 3)`` ray arrays for batch
efficiency.  Coordinate conventions match the GLSL shader:

    Front lens optical axis: -Z  (worldRay.z < 0 is "forward")
    Back  lens optical axis: +Z  (worldRay.z > 0 is "backward")
    UV origin: top-left of the full dual-fisheye frame
    UV range:  [0, 1] x [0, 1]
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Preset helpers
# ---------------------------------------------------------------------------

def preset_to_params(preset: Dict) -> Dict:
    """Flatten a schemaVersion-1 preset into a dict of NumPy-friendly values."""
    cal = preset.get("calibration", {})
    proj = cal.get("projection", {})
    front = cal.get("front", {})
    back = cal.get("back", {})
    seam = cal.get("seam", {})
    return {
        "front_center": np.array(front.get("center", [0.25, 0.5]), dtype=np.float64),
        "front_scale": np.array(front.get("scale", [0.5, 1.0]), dtype=np.float64),
        "front_euler": np.array(front.get("euler", [0.0, 0.0, 0.0]), dtype=np.float64),
        "front_distortion": np.array(front.get("distortion", [0.0, 0.0]), dtype=np.float64),
        "back_center": np.array(back.get("center", [0.75, 0.5]), dtype=np.float64),
        "back_scale": np.array(back.get("scale", [-0.5, 1.0]), dtype=np.float64),
        "back_euler": np.array(back.get("euler", [0.0, 0.0, 0.0]), dtype=np.float64),
        "back_distortion": np.array(back.get("distortion", [0.0, 0.0]), dtype=np.float64),
        "aperture": float(proj.get("aperture", 0.296)),
        "seam_center_yaw": float(seam.get("centerYaw", 0.0)),
        "seam_width": float(seam.get("width", 0.1)),
        "seam_feather": float(seam.get("feather", 1.0)),
    }


# ---------------------------------------------------------------------------
# Rotation
# ---------------------------------------------------------------------------

def rotate_euler(rays: np.ndarray, euler: np.ndarray) -> np.ndarray:
    """Apply Rz * Ry * Rx rotation to an (N, 3) array of rays.

    ``euler`` is [x, y, z] in radians -- matches the shader's convention.
    """
    cx, sx = np.cos(euler[0]), np.sin(euler[0])
    cy, sy = np.cos(euler[1]), np.sin(euler[1])
    cz, sz = np.cos(euler[2]), np.sin(euler[2])

    # GLSL mat3 columns are written column-major; the shader multiplies
    # rz * ry * rx * v.  Build the combined 3x3 matrix directly.
    R = np.array([
        [cy * cz, cz * sx * sy - cx * sz, cx * cz * sy + sx * sz],
        [cy * sz, cx * cz + sx * sy * sz, cx * sy * sz - cz * sx],
        [-sy,     cy * sx,                cx * cy],
    ], dtype=np.float64)

    return rays @ R.T  # (N, 3) @ (3, 3)


# ---------------------------------------------------------------------------
# Distortion
# ---------------------------------------------------------------------------

def corrected_theta(theta: np.ndarray, distortion: np.ndarray) -> np.ndarray:
    """theta_d = theta * (1 + k1*theta^2 + k2*theta^4)"""
    t2 = theta * theta
    t4 = t2 * t2
    return theta * (1.0 + distortion[0] * t2 + distortion[1] * t4)


# ---------------------------------------------------------------------------
# World ray -> UV
# ---------------------------------------------------------------------------

def world_ray_to_uv(
    rays: np.ndarray,
    side: str,
    params: Dict,
) -> np.ndarray:
    """Map (N, 3) world-space unit rays to (N, 2) texture UV coordinates.

    ``side`` must be ``"front"`` or ``"back"``.
    ``params`` comes from :func:`preset_to_params`.

    Returns UV in [0, 1] range for in-frame pixels.  Out-of-circle rays
    produce UVs outside [0, 1] -- the caller should mask them.
    """
    euler = params[f"{side}_euler"]
    center = params[f"{side}_center"]
    scale = params[f"{side}_scale"]
    distortion = params[f"{side}_distortion"]
    aperture = params["aperture"]

    rotated = rotate_euler(rays, euler)
    xy_len = np.sqrt(rotated[:, 0] ** 2 + rotated[:, 1] ** 2)
    psi = np.arctan2(rotated[:, 1], rotated[:, 0])

    if side == "front":
        angle = np.arctan2(xy_len, -rotated[:, 2])
    else:
        angle = np.arctan2(xy_len, rotated[:, 2])

    R = aperture * corrected_theta(angle, distortion)

    u = center[0] + scale[0] * R * np.cos(psi)
    v = center[1] + scale[1] * R * np.sin(psi)

    return np.column_stack([u, v])


# ---------------------------------------------------------------------------
# Overlap ray sampling
# ---------------------------------------------------------------------------

def max_theta_for_preset(params: Dict) -> float:
    """Maximum polar angle (rad) that fits inside the fisheye circle.

    The fisheye circle radius in UV is |scale[0]| * R_max.  The circle
    fills half the frame width when |scale[0]| * R_max = 0.5, giving
    R_max = 0.5 / |scale[0]|.  Since R = aperture * theta_d (ignoring
    distortion), theta_max ≈ R_max / aperture.
    """
    sx = abs(params["front_scale"][0])
    r_max = min(0.5, abs(params["front_center"][0])) / max(sx, 1e-9)
    return r_max / max(params["aperture"], 1e-9)


def sample_overlap_rays(
    params: Dict,
    n_yaw: int = 120,
    n_pitch: int = 40,
    margin_deg: float = 1.0,
) -> np.ndarray:
    """Sample a uniform grid of unit rays that fall inside BOTH fisheye circles.

    The overlap band is the set of world directions where the polar angle from
    both the front and back optical axes is less than ``theta_max - margin``.

    Returns an (M, 3) array of unit rays (M <= n_yaw * n_pitch after masking).
    """
    theta_max = max_theta_for_preset(params)
    margin = np.radians(margin_deg)

    # The front optical axis is -Z, back is +Z.  A ray with angle alpha from
    # -Z has angle (pi - alpha) from +Z.  Both must be < theta_max:
    #   alpha < theta_max   AND   pi - alpha < theta_max
    #   => pi - theta_max < alpha < theta_max
    # alpha is measured from -Z, so alpha = arccos(-worldRay.z).
    alpha_min = np.pi - theta_max + margin
    alpha_max = theta_max - margin
    if alpha_min >= alpha_max:
        return np.zeros((0, 3), dtype=np.float64)

    # alpha = angle from -Z = arccos(-z), so z = -cos(alpha).
    # Sample alpha uniformly; for each alpha, sample the azimuth.
    alphas = np.linspace(alpha_min, alpha_max, n_pitch)
    azimuths = np.linspace(0, 2 * np.pi, n_yaw, endpoint=False)
    aa, az = np.meshgrid(alphas, azimuths, indexing="ij")
    aa = aa.ravel()
    az = az.ravel()

    x = np.sin(aa) * np.cos(az)
    y = np.sin(aa) * np.sin(az)
    z = -np.cos(aa)
    rays = np.column_stack([x, y, z])

    # Double-check: both UVs must be inside [0, 1]
    front_uv = world_ray_to_uv(rays, "front", params)
    back_uv = world_ray_to_uv(rays, "back", params)
    valid = (
        (front_uv[:, 0] >= 0) & (front_uv[:, 0] <= 1)
        & (front_uv[:, 1] >= 0) & (front_uv[:, 1] <= 1)
        & (back_uv[:, 0] >= 0) & (back_uv[:, 0] <= 1)
        & (back_uv[:, 1] >= 0) & (back_uv[:, 1] <= 1)
    )
    return rays[valid]


# ---------------------------------------------------------------------------
# Image sampling
# ---------------------------------------------------------------------------

def sample_image_at_uvs(
    image: np.ndarray,
    uvs: np.ndarray,
) -> np.ndarray:
    """Bilinear-sample an image at (N, 2) UV coordinates.

    ``image`` shape: (H, W) for grayscale or (H, W, C) for colour.
    ``uvs`` are in [0, 1] range (OpenGL convention: u right, v up in shader
    but images have row 0 at top).  The shader's ``texture2D`` maps v=0 to
    the top of the image, so v maps directly to row index.

    Returns (N,) or (N, C) float64 array.  Out-of-bounds samples are NaN.
    """
    H, W = image.shape[:2]
    # Convert UV to pixel coordinates (continuous).  u -> column, v -> row.
    px = uvs[:, 0] * W - 0.5
    py = uvs[:, 1] * H - 0.5

    x0 = np.floor(px).astype(np.int64)
    y0 = np.floor(py).astype(np.int64)
    x1 = x0 + 1
    y1 = y0 + 1

    wx = (px - x0).astype(np.float64)
    wy = (py - y0).astype(np.float64)

    oob = (x0 < 0) | (y0 < 0) | (x1 >= W) | (y1 >= H)

    x0c = np.clip(x0, 0, W - 1)
    y0c = np.clip(y0, 0, H - 1)
    x1c = np.clip(x1, 0, W - 1)
    y1c = np.clip(y1, 0, H - 1)

    img = image.astype(np.float64)
    if img.ndim == 2:
        v00 = img[y0c, x0c]
        v10 = img[y0c, x1c]
        v01 = img[y1c, x0c]
        v11 = img[y1c, x1c]
        result = v00 * (1 - wx) * (1 - wy) + v10 * wx * (1 - wy) + v01 * (1 - wx) * wy + v11 * wx * wy
        result[oob] = np.nan
    else:
        v00 = img[y0c, x0c]  # (N, C)
        v10 = img[y0c, x1c]
        v01 = img[y1c, x0c]
        v11 = img[y1c, x1c]
        wx = wx[:, None]
        wy = wy[:, None]
        result = v00 * (1 - wx) * (1 - wy) + v10 * wx * (1 - wy) + v01 * (1 - wx) * wy + v11 * wx * wy
        result[oob] = np.nan

    return result


# ---------------------------------------------------------------------------
# Seam blend weight (for diagnostics / visualisation)
# ---------------------------------------------------------------------------

def seam_blend_weight(
    rays: np.ndarray,
    params: Dict,
) -> np.ndarray:
    """Compute the front/back blend weight for (N, 3) world rays.

    Returns (N,) array in [0, 1] where 0 = pure front, 1 = pure back.
    Matches the shader's smoothstep + pow formula.
    """
    seam_euler = np.array([0.0, params["seam_center_yaw"], 0.0])
    seam_ray = rotate_euler(rays, seam_euler)
    w = max(0.001, params["seam_width"])
    # smoothstep(-w, w, z)
    t = np.clip((seam_ray[:, 2] + w) / (2.0 * w), 0.0, 1.0)
    blend = t * t * (3.0 - 2.0 * t)  # smoothstep polynomial
    feather = max(0.05, params["seam_feather"])
    return np.power(np.clip(blend, 0.0, 1.0), feather)


# ---------------------------------------------------------------------------
# Cylindrical reprojection (for feature matching)
# ---------------------------------------------------------------------------

def reproject_to_overlap_strip(
    image: np.ndarray,
    side: str,
    params: Dict,
    strip_width: int = 1000,
    strip_height: int = 200,
    margin_deg: float = 1.0,
) -> np.ndarray:
    """Reproject one lens half into a cylindrical strip covering the overlap band.

    The strip's horizontal axis is azimuth (0 to 2*pi) and the vertical axis
    is the polar angle from -Z within the overlap range.

    Returns a (strip_height, strip_width) or (strip_height, strip_width, C)
    float64 array (NaN for unmapped pixels).
    """
    theta_max = max_theta_for_preset(params)
    margin = np.radians(margin_deg)
    alpha_min = np.pi - theta_max + margin
    alpha_max = theta_max - margin
    if alpha_min >= alpha_max:
        shape = (strip_height, strip_width) if image.ndim == 2 else (strip_height, strip_width, image.shape[2])
        out = np.full(shape, np.nan, dtype=np.float64)
        return out

    azimuths = np.linspace(0, 2 * np.pi, strip_width, endpoint=False)
    alphas = np.linspace(alpha_min, alpha_max, strip_height)
    aa, az = np.meshgrid(alphas, azimuths, indexing="ij")
    aa = aa.ravel()
    az = az.ravel()

    x = np.sin(aa) * np.cos(az)
    y = np.sin(aa) * np.sin(az)
    z = -np.cos(aa)
    rays = np.column_stack([x, y, z])

    uvs = world_ray_to_uv(rays, side, params)
    sampled = sample_image_at_uvs(image, uvs)

    if image.ndim == 2:
        return sampled.reshape(strip_height, strip_width)
    else:
        return sampled.reshape(strip_height, strip_width, -1)
