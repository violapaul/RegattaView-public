#!/usr/bin/env python3
"""Generate synthetic dual-fisheye calibration images with known ground truth.

This module serves two purposes:

  1. **Test fixture generator**: produces PNG images and a ``ground_truth.json``
     that the calibration test suite uses to verify detection and fitting accuracy
     without requiring real camera footage.

  2. **Executable documentation**: the code below precisely documents the camera
     model, coordinate conventions, and rendering pipeline used throughout the
     calibration scripts.

Camera model and coordinate conventions
----------------------------------------
Each half of a dual-fisheye frame is an independent fisheye lens rendered using
OpenCV's equidistant fisheye model (``cv2.fisheye``):

    r = f * theta_d
    theta_d = theta * (1 + k1*theta^2 + k2*theta^4 + k3*theta^6 + k4*theta^8)

where ``theta`` is the angle between the optical axis and the world ray (in
radians), and ``r`` is the image-plane radius in pixels. ``f`` is the focal
length (same units as pixels).

Coordinate frames:

- **Board frame**: Z=0 is the plane of the Charuco board, X points right along
  squares, Y points down along squares. The origin is the top-left inner corner
  of the board.

- **Camera frame (front lens)**: Z points out of the front of the camera (+Z is
  "forward"), X points right, Y points down. This is the standard OpenCV
  convention for a camera looking along +Z.

- **Camera frame (back lens)**: The back lens looks in the -Z direction of the
  front lens frame. The base rotation relating the two ideal lenses is a 180-deg
  rotation around Y, i.e.:
      R_base_flip = diag([-1, 1, -1])
  In practice, there is an additional small residual misalignment euler (pitch,
  yaw, roll), which ``fit_dual_fisheye.py`` estimates as ``back_euler``.

- **UV coordinates**: The dual-fisheye image has the front half on the left
  (u in [0, 0.5]) and the back half on the right (u in [0.5, 1.0]), where u
  is normalised to the full image width. Vertical v is normalised to image
  height.

Image layout (example: half_width=960, half_height=480):
    full image: 1920 x 480 pixels
    front half: columns [0, 960), back half: columns [960, 1920)

Aperture relationship:
    The viewer preset parameter ``aperture`` maps angle to UV radius:
        r_uv = aperture * theta_d
    Since r_pixels = f * theta_d, and r_uv = r_pixels / full_width,
    we get:
        aperture = f / half_width  (half_width, not full_width, because
                                    the scale uniform maps the half-width
                                    to 0.5 UV units)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

# ---------------------------------------------------------------------------
# Ground-truth camera parameters
# ---------------------------------------------------------------------------
# These are defined at module level so tests can import them directly.

HALF_WIDTH = 960
HALF_HEIGHT = 960  # Full frame is 2:1 (1920x960); each half is square (960x960)

# Front lens intrinsics (equidistant fisheye, OpenCV fisheye model)
K_FRONT = np.array(
    [[475.0, 0.0, 480.0], [0.0, 475.0, 480.0], [0.0, 0.0, 1.0]],
    dtype=np.float64,
)
# D is (k1, k2, k3, k4) in OpenCV fisheye convention
D_FRONT = np.array([0.05, -0.01, 0.0, 0.0], dtype=np.float64)

# Back lens intrinsics
K_BACK = np.array(
    [[473.0, 0.0, 482.0], [0.0, 473.0, 481.0], [0.0, 0.0, 1.0]],
    dtype=np.float64,
)
D_BACK = np.array([0.04, -0.008, 0.0, 0.0], dtype=np.float64)

# Residual rotation of the back lens relative to the ideal 180-deg flip,
# expressed as XYZ Euler angles in radians (the same convention as the preset).
BACK_EULER_GT = np.array([0.005, -0.01, 0.003], dtype=np.float64)

# Ground-truth aperture values (derived, not fitted):
#   aperture = focal_length / half_width
APERTURE_FRONT_GT = K_FRONT[0, 0] / HALF_WIDTH   # 475/960 ~ 0.4948
APERTURE_BACK_GT = K_BACK[0, 0] / HALF_WIDTH     # 473/960 ~ 0.4927

# ---------------------------------------------------------------------------
# Board parameters (must match the defaults in the other calibration scripts)
# ---------------------------------------------------------------------------
SQUARES_X = 10
SQUARES_Y = 7
SQUARE_LENGTH_M = 0.040   # 40 mm
MARKER_LENGTH_M = 0.028   # 28 mm
DICT_NAME = "DICT_5X5_1000"


# ---------------------------------------------------------------------------
# Rotation helpers
# ---------------------------------------------------------------------------

def euler_xyz_to_matrix(euler: np.ndarray) -> np.ndarray:
    """Convert XYZ Euler angles (radians) to a 3x3 rotation matrix.

    Applies rotations in order: Rz @ Ry @ Rx (extrinsic XYZ / intrinsic ZYX).
    This matches the convention in ``fit_dual_fisheye.matrix_to_euler_xyz``.
    """
    cx, cy, cz = np.cos(euler)
    sx, sy, sz = np.sin(euler)
    Rx = np.array([[1, 0, 0], [0, cx, -sx], [0, sx, cx]], dtype=np.float64)
    Ry = np.array([[cy, 0, sy], [0, 1, 0], [-sy, 0, cy]], dtype=np.float64)
    Rz = np.array([[cz, -sz, 0], [sz, cz, 0], [0, 0, 1]], dtype=np.float64)
    return Rz @ Ry @ Rx


def rvec_to_matrix(rvec: np.ndarray) -> np.ndarray:
    R, _ = cv2.Rodrigues(rvec.reshape(3, 1))
    return R


def matrix_to_rvec(R: np.ndarray) -> np.ndarray:
    rvec, _ = cv2.Rodrigues(R)
    return rvec.flatten()


# ---------------------------------------------------------------------------
# Pose generation
# ---------------------------------------------------------------------------
# Base-flip: the ideal relationship between front and back lens cameras.
BASE_FLIP = np.diag([-1.0, 1.0, -1.0])


def _back_rotation_from_front(R_front_world: np.ndarray) -> np.ndarray:
    """Given the front-lens pose R_front_world, derive the corresponding
    back-lens pose rotation using BASE_FLIP and BACK_EULER_GT misalignment."""
    R_residual = euler_xyz_to_matrix(BACK_EULER_GT)
    R_back_world = R_residual @ BASE_FLIP @ R_front_world
    return R_back_world


def generate_poses(seed: int = 42) -> List[Dict]:
    """Generate a deterministic set of board poses for synthetic rendering.

    Returns a list of dicts, each with:
        "rvec_front": 3-vector (Rodrigues) for the board relative to the front cam
        "tvec_front": 3-vector translation for the board relative to front cam
        "rvec_back":  corresponding rvec for back lens (derived)
        "tvec_back":  corresponding tvec for back lens (derived)
        "paired":     bool -- True if this pose is in the seam overlap region,
                      meaning it should be visible to both lenses simultaneously
        "side":       "front", "back", or "both"
    """
    rng = np.random.default_rng(seed)
    poses = []

    # -- Helper to build a pose dict from (R_front_world, t_front_board) ----
    def make_pose(R_fw: np.ndarray, t_fw: np.ndarray, side: str) -> Dict:
        rvec_f = matrix_to_rvec(R_fw)
        # Back-lens pose: same world point, transformed by the inter-lens rotation.
        R_bw = _back_rotation_from_front(R_fw)
        # The back lens is effectively the front lens rotated 180° around Y.
        # The translation in the back lens frame: R_bw_fw is the rotation from
        # the front-lens frame to the back-lens frame.
        R_bw_fw = R_bw @ R_fw.T          # = R_residual @ BASE_FLIP
        t_bw = R_bw_fw @ t_fw
        rvec_b = matrix_to_rvec(R_bw)
        return {
            "rvec_front": rvec_f.tolist(),
            "tvec_front": t_fw.tolist(),
            "rvec_back": rvec_b.tolist(),
            "tvec_back": t_bw.tolist(),
            "side": side,
        }

    # -- Utility: rotation from a tilt angle around an axis ------------------
    def tilt_rot(axis: np.ndarray, angle_deg: float) -> np.ndarray:
        axis = axis / np.linalg.norm(axis)
        a = np.deg2rad(angle_deg)
        return rvec_to_matrix(axis * a)

    def edge_pose_rotation(dx: float, dy: float, z: float, roll_deg: float) -> np.ndarray:
        """Construct a stable off-axis pose for edge-coverage samples.

        The previous implementation used random-axis tilts for these frames.
        With square 960x960 half-images, one of those random edge poses became
        ill-conditioned for ``cv2.fisheye.calibrate`` despite remaining
        detectable.  Build these poses deterministically instead: a modest yaw
        and pitch that depend on the board offset, plus a small roll for
        diversity.
        """
        yaw = -0.5 * np.arctan2(dx, z)
        pitch = 0.5 * np.arctan2(dy, z)
        roll = np.deg2rad(roll_deg)
        return euler_xyz_to_matrix(np.array([pitch, yaw, roll], dtype=np.float64))

    # With K_FRONT/K_BACK having fx=475 and image half_width=960, reliable aruco
    # detection (>= 3 px per cell) requires the 28mm marker to span >= 21px.
    # At distance d: px_per_marker = 475 * 0.028 / d => d <= 475*0.028/21 ≈ 0.63m.
    # All poses use distances <= 0.65m.

    # -- 1. Front-only poses -------------------------------------------------
    for z, angle_deg in [
        (0.35, 0.0), (0.35, 25.0),
        (0.45, 0.0), (0.45, 20.0),
        (0.55, 0.0), (0.55, 20.0),
        (0.50, 0.0), (0.50, 30.0),
    ]:
        t = np.array([0.0, 0.0, z])
        axis = rng.standard_normal(3)
        axis /= np.linalg.norm(axis)
        R = tilt_rot(axis, angle_deg) if angle_deg > 0 else np.eye(3, dtype=np.float64)
        poses.append(make_pose(R, t, "front"))

    # Front edge poses (board offset laterally but still within FOV at close range)
    for dx, dy, z, roll_deg in [
        (0.10, 0.0, 0.45, 8.0), (-0.10, 0.0, 0.45, -8.0),
        (0.0, 0.06, 0.40, 6.0), (0.0, -0.06, 0.40, -6.0),
        (0.08, 0.05, 0.50, 10.0), (-0.08, -0.05, 0.50, -10.0),
        (0.12, 0.0, 0.55, 12.0), (-0.12, 0.0, 0.55, -12.0),
    ]:
        t = np.array([dx, dy, z])
        R = edge_pose_rotation(dx, dy, z, roll_deg)
        poses.append(make_pose(R, t, "front"))

    # -- 2. Back-only poses --------------------------------------------------
    # Defined natively in the back lens frame, then converted to front convention.
    R_bw_fw = euler_xyz_to_matrix(BACK_EULER_GT) @ BASE_FLIP
    for z, angle_deg in [
        (0.35, 0.0), (0.35, 25.0),
        (0.45, 0.0), (0.45, 20.0),
        (0.55, 0.0), (0.55, 20.0),
        (0.50, 0.0), (0.50, 30.0),
    ]:
        t_back = np.array([0.0, 0.0, z])
        axis = rng.standard_normal(3)
        axis /= np.linalg.norm(axis)
        R_back = tilt_rot(axis, angle_deg) if angle_deg > 0 else np.eye(3, dtype=np.float64)
        R_fw = R_bw_fw.T @ R_back
        t_fw = R_bw_fw.T @ t_back
        poses.append(make_pose(R_fw, t_fw, "back"))

    for dx, dy, z, roll_deg in [
        (0.10, 0.0, 0.45, 8.0), (-0.10, 0.0, 0.45, -8.0),
        (0.0, 0.06, 0.40, 6.0), (0.0, -0.06, 0.40, -6.0),
        (0.08, 0.05, 0.50, 10.0), (-0.08, -0.05, 0.50, -10.0),
    ]:
        t_back = np.array([dx, dy, z])
        R_back = edge_pose_rotation(dx, dy, z, roll_deg)
        R_fw = R_bw_fw.T @ R_back
        t_fw = R_bw_fw.T @ t_back
        poses.append(make_pose(R_fw, t_fw, "back"))

    return poses


# ---------------------------------------------------------------------------
# Board image generation
# ---------------------------------------------------------------------------

def make_charuco_board() -> Tuple[cv2.aruco.CharucoBoard, np.ndarray]:
    dict_id = getattr(cv2.aruco, DICT_NAME)
    aruco_dict = cv2.aruco.getPredefinedDictionary(dict_id)
    board = cv2.aruco.CharucoBoard(
        (SQUARES_X, SQUARES_Y),
        SQUARE_LENGTH_M,
        MARKER_LENGTH_M,
        aruco_dict,
    )
    corners_3d = board.getChessboardCorners().astype(np.float64)
    return board, corners_3d


def render_board_texture(board: cv2.aruco.CharucoBoard, px_per_m: float = 4000.0) -> Tuple[np.ndarray, float, float]:
    """Render the Charuco board as a flat texture image.

    Returns:
        image: (H, W) uint8 grayscale texture
        tex_width_m: physical width of the texture in metres
        tex_height_m: physical height of the texture in metres
    """
    tex_width_m = SQUARES_X * SQUARE_LENGTH_M
    tex_height_m = SQUARES_Y * SQUARE_LENGTH_M
    tex_w_px = int(round(tex_width_m * px_per_m))
    tex_h_px = int(round(tex_height_m * px_per_m))
    img = board.generateImage((tex_w_px, tex_h_px), marginSize=0, borderBits=1)
    return img, tex_width_m, tex_height_m


# ---------------------------------------------------------------------------
# Reverse-map fisheye rendering
# ---------------------------------------------------------------------------

def render_fisheye_view(
    board_texture: np.ndarray,
    tex_width_m: float,
    tex_height_m: float,
    K: np.ndarray,
    D: np.ndarray,
    rvec: np.ndarray,
    tvec: np.ndarray,
    image_size: Tuple[int, int],
) -> np.ndarray:
    """Render the Charuco board as seen by a fisheye lens.

    Uses reverse mapping:
      For each output pixel (u, v):
        1. Undistort the pixel to a normalised camera ray using
           ``cv2.fisheye.undistortPoints``.
        2. Transform the ray to the board frame: p_board = R^T * (ray - t).
           Intersect with the board plane Z_board = 0 to get (X_board, Y_board).
        3. Map (X_board, Y_board) to texture UV, bilinearly sample the texture.

    Parameters
    ----------
    board_texture : (H_tex, W_tex) uint8 grayscale
    tex_width_m, tex_height_m : physical dimensions of the texture (metres)
    K : 3x3 camera matrix
    D : (4,) fisheye distortion coefficients (k1,k2,k3,k4)
    rvec : (3,) Rodrigues rotation of the board in the camera frame
    tvec : (3,) translation of the board origin in the camera frame
    image_size : (width, height) of the output image in pixels

    Returns
    -------
    image : (height, width) uint8 -- 128 (gray) where no board is visible
    """
    W, H = image_size
    R, _ = cv2.Rodrigues(rvec.reshape(3, 1))
    t = tvec.reshape(3, 1)

    # Build a grid of all pixel centres.
    ys, xs = np.mgrid[0:H, 0:W].astype(np.float32)
    pts_distorted = np.stack([xs.ravel(), ys.ravel()], axis=-1).reshape(-1, 1, 2)

    # Undistort: produces normalised camera-frame coordinates (x/z, y/z)
    pts_undistorted = cv2.fisheye.undistortPoints(
        pts_distorted, K, D, P=None
    ).reshape(-1, 2)

    # Lift to 3D rays in camera frame (Z=1 homogeneous)
    rays_cam = np.column_stack(
        [pts_undistorted, np.ones(len(pts_undistorted), dtype=np.float64)]
    )  # (N, 3)

    # Transform rays to board frame: ray_board = R^T @ ray_cam
    # (R rotates board->camera, so R^T rotates camera->board)
    rays_board = (R.T @ rays_cam.T).T  # (N, 3)

    # Translate: origin_board_in_camera = t, so camera origin in board frame = -R^T @ t
    cam_origin_in_board = (-R.T @ t).flatten()  # (3,)

    # Ray-plane intersection with Z_board = 0:
    # point = cam_origin + lambda * ray_board, Z=0 => lambda = -cam_origin_z / ray_board_z
    ray_z = rays_board[:, 2]
    valid = np.abs(ray_z) > 1e-6
    lam = np.where(valid, -cam_origin_in_board[2] / ray_z, np.nan)

    board_x = cam_origin_in_board[0] + lam * rays_board[:, 0]
    board_y = cam_origin_in_board[1] + lam * rays_board[:, 1]

    # Map board XY to texture UV
    tex_u = board_x / tex_width_m   # 0..1 across board width
    tex_v = board_y / tex_height_m  # 0..1 across board height

    tex_h, tex_w = board_texture.shape[:2]
    map_x = (tex_u * tex_w).astype(np.float32).reshape(H, W)
    map_y = (tex_v * tex_h).astype(np.float32).reshape(H, W)

    # Mask: only sample where lambda > 0 (in front of camera) and inside texture
    inside = (
        valid.reshape(H, W)
        & (lam.reshape(H, W) > 0)
        & (map_x >= 0) & (map_x < tex_w)
        & (map_y >= 0) & (map_y < tex_h)
    )

    output = np.full((H, W), 128, dtype=np.uint8)
    if inside.any():
        sampled = cv2.remap(
            board_texture,
            map_x,
            map_y,
            interpolation=cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=128,
        )
        output[inside] = sampled[inside]

    return output


# ---------------------------------------------------------------------------
# Project board corners (for ground-truth reprojection reference)
# ---------------------------------------------------------------------------

def project_charuco_corners(
    corners_3d: np.ndarray,
    K: np.ndarray,
    D: np.ndarray,
    rvec: np.ndarray,
    tvec: np.ndarray,
) -> Optional[np.ndarray]:
    """Project 3D Charuco interior corners to 2D image points.

    Returns (N, 2) float64 array, or None on error.
    """
    try:
        pts, _ = cv2.fisheye.projectPoints(
            corners_3d.reshape(-1, 1, 3),
            rvec.reshape(3, 1),
            tvec.reshape(3, 1),
            K,
            D,
        )
        return pts.reshape(-1, 2)
    except cv2.error:
        return None


def _is_in_image(pts: np.ndarray, w: int, h: int, margin: int = 10) -> bool:
    """Return True if at least half the corners project inside the image."""
    inside = (
        (pts[:, 0] >= margin) & (pts[:, 0] < w - margin)
        & (pts[:, 1] >= margin) & (pts[:, 1] < h - margin)
    )
    return inside.sum() >= len(pts) // 2


# ---------------------------------------------------------------------------
# Direct-projection stereo observations (no image rendering required)
# ---------------------------------------------------------------------------

def generate_projected_observations(
    n_pairs: int = 25,
    seed: int = 42,
) -> List[Dict]:
    """Generate synthetic observations by projecting board corners directly.

    This bypasses image rendering and aruco detection entirely, producing
    noise-free 2D corner observations for both the front and back lenses.
    Used primarily by ``test_back_euler_estimate`` to test
    ``estimate_residual_back_euler`` with a controlled known-GT residual.

    Strategy for creating stereo-paired observations
    ------------------------------------------------
    In a real dual-fisheye camera the front and back lenses face opposite
    directions, so a board that is in front of the front lens is behind the
    back lens and vice-versa.  True simultaneous visibility requires the board
    to be near the seam (~90° from each axis), which with our moderate-FOV
    test camera cannot be represented with a simple fronto-parallel pose.

    For testing the rotation-estimation algorithm we exploit the fact that
    ``estimate_residual_back_euler`` uses only the ROTATION recovered by
    ``solvePnP``, not the translation.  We therefore:

    1. For each pair, generate a random board *orientation* R_f.
    2. Project from the front lens using (R_f, t_f) with t_f[2] > 0.
    3. Derive the correct back-lens orientation: R_b = R_bw_fw @ R_f.
    4. Project from the back lens using (R_b, t_b) where t_b is an
       independent valid translation (t_b[2] > 0).  Because solvePnP
       recovers R_b exactly from the projected corners, the translation
       choice does not affect the recovered rotation.
    5. Bundle both observations under the same ``pair_key``.

    Returns a combined observations list (same schema as detect_charuco.py
    output) with ``side`` "front" or "back" and ``source_type`` "synthetic".
    """
    _, corners_3d = make_charuco_board()
    image_size = [HALF_WIDTH, HALF_HEIGHT]

    rng = np.random.default_rng(seed + 1000)
    observations = []

    # Rotation taking board pose from front-lens frame to back-lens frame
    R_bw_fw = euler_xyz_to_matrix(BACK_EULER_GT) @ BASE_FLIP

    for i in range(n_pairs):
        # Random board orientation (varied tilts, important for a well-conditioned fit)
        axis = rng.standard_normal(3)
        axis /= np.linalg.norm(axis)
        angle = np.deg2rad(rng.uniform(5, 40))
        R_front = rvec_to_matrix(axis * angle)
        rvec_f = matrix_to_rvec(R_front)

        # Front-lens: board in front of camera at varied distance and lateral offset
        t_f = np.array([
            rng.uniform(-0.08, 0.08),
            rng.uniform(-0.06, 0.06),
            rng.uniform(0.35, 0.60),
        ])

        # Back-lens: same board orientation expressed in back-lens frame;
        # use a fresh valid translation independent of t_f.
        R_back = R_bw_fw @ R_front
        rvec_b = matrix_to_rvec(R_back)
        t_b = np.array([
            rng.uniform(-0.08, 0.08),
            rng.uniform(-0.06, 0.06),
            rng.uniform(0.35, 0.60),
        ])

        pair_key = f"proj_{i:04d}"

        for side, K, D, rvec, tvec in [
            ("front", K_FRONT, D_FRONT, rvec_f, t_f),
            ("back",  K_BACK,  D_BACK,  rvec_b, t_b),
        ]:
            try:
                pts2d, _ = cv2.fisheye.projectPoints(
                    corners_3d.reshape(-1, 1, 3),
                    rvec.reshape(3, 1),
                    tvec.reshape(3, 1),
                    K, D,
                )
                pts2d = pts2d.reshape(-1, 2)
            except cv2.error:
                continue

            W, H = image_size
            in_frame = (
                (pts2d[:, 0] >= 5) & (pts2d[:, 0] < W - 5)
                & (pts2d[:, 1] >= 5) & (pts2d[:, 1] < H - 5)
            )
            if in_frame.sum() < 14:
                continue

            idx = np.where(in_frame)[0]
            observations.append({
                "source_type": "synthetic",
                "source_path": "generated",
                "pair_key": pair_key,
                "frame_index": i,
                "side": side,
                "image_size": image_size,
                "num_corners": int(len(idx)),
                "charuco_ids": idx.tolist(),
                "object_points": corners_3d[idx].tolist(),
                "image_points": pts2d[idx].tolist(),
            })

    return observations


# ---------------------------------------------------------------------------
# Main generation logic
# ---------------------------------------------------------------------------

def generate(
    output_dir: Path,
    num_images: int = 25,
    seed: int = 42,
) -> None:
    """Generate synthetic dual-fisheye images and ground_truth.json.

    Parameters
    ----------
    output_dir : directory to write PNG images and ground_truth.json
    num_images : maximum number of dual-fisheye images to write
    seed : random seed for reproducibility
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    board, corners_3d = make_charuco_board()
    board_texture, tex_w_m, tex_h_m = render_board_texture(board)

    all_poses = generate_poses(seed=seed)
    # Trim to requested count (prefer keeping seam pairs near the end)
    poses = all_poses[:num_images] if len(all_poses) > num_images else all_poses

    gt_images = []
    written = 0

    for pose in poses:
        side = pose["side"]
        rvec_f = np.array(pose["rvec_front"])
        tvec_f = np.array(pose["tvec_front"])
        rvec_b = np.array(pose["rvec_back"])
        tvec_b = np.array(pose["tvec_back"])

        # -- Render front half -----------------------------------------------
        front_visible = False
        front_corners_px = None
        front_half = np.full((HALF_HEIGHT, HALF_WIDTH), 128, dtype=np.uint8)

        if side in ("front", "both"):
            projected_f = project_charuco_corners(corners_3d, K_FRONT, D_FRONT, rvec_f, tvec_f)
            if projected_f is not None and _is_in_image(projected_f, HALF_WIDTH, HALF_HEIGHT):
                front_half = render_fisheye_view(
                    board_texture, tex_w_m, tex_h_m,
                    K_FRONT, D_FRONT, rvec_f, tvec_f,
                    (HALF_WIDTH, HALF_HEIGHT),
                )
                front_visible = True
                front_corners_px = projected_f.tolist()

        # -- Render back half ------------------------------------------------
        back_visible = False
        back_corners_px = None
        back_half = np.full((HALF_HEIGHT, HALF_WIDTH), 128, dtype=np.uint8)

        if side in ("back", "both"):
            projected_b = project_charuco_corners(corners_3d, K_BACK, D_BACK, rvec_b, tvec_b)
            if projected_b is not None and _is_in_image(projected_b, HALF_WIDTH, HALF_HEIGHT):
                back_half = render_fisheye_view(
                    board_texture, tex_w_m, tex_h_m,
                    K_BACK, D_BACK, rvec_b, tvec_b,
                    (HALF_WIDTH, HALF_HEIGHT),
                )
                back_visible = True
                back_corners_px = projected_b.tolist()

        if not front_visible and not back_visible:
            continue  # skip degenerate pose

        # -- Concatenate and write -------------------------------------------
        dual = np.concatenate([front_half, back_half], axis=1)  # (H, 2*W)
        dual_bgr = cv2.cvtColor(dual, cv2.COLOR_GRAY2BGR)
        filename = f"synth_{written:03d}.png"
        ok = cv2.imwrite(str(output_dir / filename), dual_bgr)
        if not ok:
            raise RuntimeError(f"Failed to write {output_dir / filename}")

        gt_images.append({
            "filename": filename,
            "front_pose": {"rvec": rvec_f.tolist(), "tvec": tvec_f.tolist()} if front_visible else None,
            "back_pose":  {"rvec": rvec_b.tolist(), "tvec": tvec_b.tolist()} if back_visible else None,
            "front_projected_corners": front_corners_px,
            "back_projected_corners":  back_corners_px,
        })
        written += 1

    # -- Write ground truth JSON --------------------------------------------
    gt = {
        "camera": {
            "half_width": HALF_WIDTH,
            "half_height": HALF_HEIGHT,
            "front": {
                "K": K_FRONT.tolist(),
                "D": D_FRONT.tolist(),
                "euler": [0.0, 0.0, 0.0],
                "aperture_gt": APERTURE_FRONT_GT,
            },
            "back": {
                "K": K_BACK.tolist(),
                "D": D_BACK.tolist(),
                "euler": BACK_EULER_GT.tolist(),
                "aperture_gt": APERTURE_BACK_GT,
            },
        },
        "board": {
            "squares_x": SQUARES_X,
            "squares_y": SQUARES_Y,
            "square_length_mm": SQUARE_LENGTH_M * 1000,
            "marker_length_mm": MARKER_LENGTH_M * 1000,
            "dict_name": DICT_NAME,
        },
        "images": gt_images,
    }
    gt_path = output_dir / "ground_truth.json"
    with gt_path.open("w", encoding="utf-8") as f:
        json.dump(gt, f, indent=2)

    print(f"Wrote {written} dual-fisheye images to {output_dir}")
    print(f"Wrote {gt_path}")
    print(f"  front visible in {sum(1 for i in gt_images if i['front_pose'])} images")
    print(f"  back  visible in {sum(1 for i in gt_images if i['back_pose'])} images")
    pairs = sum(1 for i in gt_images if i['front_pose'] and i['back_pose'])
    print(f"  both visible (paired) in {pairs} images")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output-dir", type=Path, required=True, help="Directory to write PNG images and ground_truth.json.")
    parser.add_argument("--num-images", type=int, default=25, help="Maximum number of dual-fisheye images to generate.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for pose generation.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    generate(args.output_dir, num_images=args.num_images, seed=args.seed)


if __name__ == "__main__":
    main()
