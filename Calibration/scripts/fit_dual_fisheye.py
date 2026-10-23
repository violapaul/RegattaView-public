#!/usr/bin/env python3
"""Fit a starting dual-fisheye calibration and export viewer preset JSON."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--observations", type=Path, required=True, help="Input observations JSON from detect_charuco.py")
    parser.add_argument("--output", type=Path, required=True, help="Output calibration preset JSON")
    parser.add_argument(
        "--seed-preset",
        type=Path,
        default=None,
        help="Optional existing preset JSON to use as the initial fisheye seed. "
             "Its normalized values are scaled to the current observation size.",
    )
    parser.add_argument("--min-observations", type=int, default=10)
    parser.add_argument("--default-fov", type=float, default=180.0)
    parser.add_argument(
        "--override-aperture",
        type=float,
        default=None,
        help="Override aperture instead of deriving it from the fitted focal length. "
             "Use only if the automatic estimate looks wrong.",
    )
    parser.add_argument("--default-view-correction", type=float, default=-0.2)
    parser.add_argument("--default-seam-width", type=float, default=0.1)
    parser.add_argument("--default-seam-feather", type=float, default=1.0)
    return parser.parse_args()


def to_cv_points(points3d: List[List[float]], points2d: List[List[float]]) -> Tuple[np.ndarray, np.ndarray]:
    obj = np.asarray(points3d, dtype=np.float64).reshape(-1, 1, 3)
    img = np.asarray(points2d, dtype=np.float64).reshape(-1, 1, 2)
    return obj, img


def calibrate_side(
    side_observations: List[Dict],
    initial_guess: Optional[Tuple[np.ndarray, np.ndarray]] = None,
) -> Tuple[np.ndarray, np.ndarray, List[np.ndarray], List[np.ndarray], Tuple[int, int]]:
    if not side_observations:
        raise ValueError("No observations available for side.")

    image_size = tuple(side_observations[0]["image_size"])
    object_points = []
    image_points = []
    for obs in side_observations:
        obj, img = to_cv_points(obs["object_points"], obs["image_points"])
        object_points.append(obj)
        image_points.append(img)

    if initial_guess is None:
        K = np.array(
            [
                [image_size[0], 0.0, image_size[0] / 2.0],
                [0.0, image_size[1], image_size[1] / 2.0],
                [0.0, 0.0, 1.0],
            ],
            dtype=np.float64,
        )
        D = np.zeros((4, 1), dtype=np.float64)
    else:
        K, D = initial_guess
        K = np.array(K, dtype=np.float64, copy=True)
        D = np.array(D, dtype=np.float64, copy=True).reshape(4, 1)

    flags = (
        cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC
        | cv2.fisheye.CALIB_CHECK_COND
        | cv2.fisheye.CALIB_FIX_SKEW
    )
    if initial_guess is not None:
        flags |= cv2.fisheye.CALIB_USE_INTRINSIC_GUESS
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 200, 1e-8)

    rms, K, D, rvecs, tvecs = cv2.fisheye.calibrate(
        object_points,
        image_points,
        image_size,
        K,
        D,
        flags=flags,
        criteria=criteria,
    )
    print(f"{side_observations[0]['side']} RMS: {rms:.4f}")
    return K, D, rvecs, tvecs, image_size


def build_seed_guess(
    preset_payload: Dict,
    side: str,
    front_size: Tuple[int, int],
    back_size: Tuple[int, int],
) -> Tuple[np.ndarray, np.ndarray]:
    calibration = preset_payload.get("calibration", {})
    side_cfg = calibration.get(side, {})
    projection = calibration.get("projection", {})

    if side not in {"front", "back"}:
        raise ValueError(f"Unsupported side {side!r}")

    full_width = front_size[0] + back_size[0]
    image_size = front_size if side == "front" else back_size
    center = side_cfg.get("center", [0.25 if side == "front" else 0.75, 0.5])
    scale = side_cfg.get("scale", [0.5 if side == "front" else -0.5, 1.0])
    distortion = side_cfg.get("distortion", [0.0, 0.0])
    aperture = float(projection.get("aperture", 0.296))

    fx = aperture * image_size[0]
    scale_y = abs(float(scale[1])) if len(scale) > 1 else 1.0
    fy = fx / max(scale_y, 1e-6)

    cx_full = float(center[0]) * full_width
    cx = cx_full if side == "front" else cx_full - front_size[0]
    cy = float(center[1]) * image_size[1]

    K = np.array(
        [
            [fx, 0.0, cx],
            [0.0, fy, cy],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    D = np.zeros((4, 1), dtype=np.float64)
    if len(distortion) > 0:
        D[0, 0] = float(distortion[0])
    if len(distortion) > 1:
        D[1, 0] = float(distortion[1])
    return K, D


def rotation_matrix_from_rvec(rvec: np.ndarray) -> np.ndarray:
    R, _ = cv2.Rodrigues(rvec)
    return R


def average_rotation(rotations: List[np.ndarray]) -> Optional[np.ndarray]:
    if not rotations:
        return None
    M = np.zeros((3, 3), dtype=np.float64)
    for R in rotations:
        M += R
    U, _, Vt = np.linalg.svd(M)
    R = U @ Vt
    if np.linalg.det(R) < 0:
        U[:, -1] *= -1
        R = U @ Vt
    return R


def matrix_to_euler_xyz(R: np.ndarray) -> List[float]:
    sy = np.sqrt(R[0, 0] * R[0, 0] + R[1, 0] * R[1, 0])
    singular = sy < 1e-6
    if not singular:
        x = np.arctan2(R[2, 1], R[2, 2])
        y = np.arctan2(-R[2, 0], sy)
        z = np.arctan2(R[1, 0], R[0, 0])
    else:
        x = np.arctan2(-R[1, 2], R[1, 1])
        y = np.arctan2(-R[2, 0], sy)
        z = 0.0
    return [float(x), float(y), float(z)]


def match_pairs(observations: List[Dict]) -> Dict[str, Dict[str, Dict]]:
    grouped = defaultdict(dict)
    for obs in observations:
        grouped[obs["pair_key"]][obs["side"]] = obs
    return grouped


def estimate_residual_back_euler(
    front_by_pair: Dict[str, Dict],
    back_by_pair: Dict[str, Dict],
    Kf: np.ndarray,
    Df: np.ndarray,
    Kb: np.ndarray,
    Db: np.ndarray,
) -> List[float]:
    rel_rots = []
    base_flip = np.array([[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]], dtype=np.float64)
    common_keys = sorted(set(front_by_pair.keys()).intersection(back_by_pair.keys()))

    for key in common_keys:
        fobs = front_by_pair[key]
        bobs = back_by_pair[key]
        fobj, fimg = to_cv_points(fobs["object_points"], fobs["image_points"])
        bobj, bimg = to_cv_points(bobs["object_points"], bobs["image_points"])

        # cv2.fisheye.solvePnP was removed in OpenCV 4.8+.  Instead, undistort
        # the image points to normalised camera-plane coordinates and then use
        # the standard solvePnP with a unit camera matrix and zero distortion.
        K_unit = np.eye(3, dtype=np.float64)
        D_zero = np.zeros((4, 1), dtype=np.float64)
        fimg_undist = cv2.fisheye.undistortPoints(fimg, Kf, Df).reshape(-1, 1, 2)
        bimg_undist = cv2.fisheye.undistortPoints(bimg, Kb, Db).reshape(-1, 1, 2)
        ok_f, rvec_f, _ = cv2.solvePnP(fobj, fimg_undist, K_unit, D_zero)
        ok_b, rvec_b, _ = cv2.solvePnP(bobj, bimg_undist, K_unit, D_zero)
        if not ok_f or not ok_b:
            continue
        Rf = rotation_matrix_from_rvec(rvec_f)
        Rb = rotation_matrix_from_rvec(rvec_b)
        rel = Rb @ Rf.T
        residual = rel @ base_flip.T
        rel_rots.append(residual)

    R_mean = average_rotation(rel_rots)
    if R_mean is None:
        return [0.0, 0.0, 0.0]
    return matrix_to_euler_xyz(R_mean)


def clip(val: float, lo: float, hi: float) -> float:
    return float(max(lo, min(hi, val)))


def main() -> None:
    args = parse_args()
    with args.observations.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    schema_version = payload.get("schema_version")
    if schema_version != 1:
        raise RuntimeError(
            f"Unsupported observations schema_version: {schema_version!r} (expected 1). "
            "Re-run detect_charuco.py to regenerate the observations file."
        )

    observations = payload.get("observations", [])
    front_obs = [o for o in observations if o.get("side") == "front"]
    back_obs = [o for o in observations if o.get("side") == "back"]
    if len(front_obs) < args.min_observations or len(back_obs) < args.min_observations:
        raise RuntimeError(
            f"Not enough observations. front={len(front_obs)} back={len(back_obs)} "
            f"(need at least {args.min_observations} each)"
        )

    front_size = tuple(front_obs[0]["image_size"])
    back_size = tuple(back_obs[0]["image_size"])

    front_seed = None
    back_seed = None
    if args.seed_preset is not None:
        with args.seed_preset.open("r", encoding="utf-8") as f:
            seed_payload = json.load(f)
        seed_schema = seed_payload.get("schemaVersion")
        if seed_schema != 1:
            raise RuntimeError(
                f"Unsupported seed preset schemaVersion: {seed_schema!r} (expected 1). "
                f"Preset: {args.seed_preset}"
            )
        front_seed = build_seed_guess(seed_payload, "front", front_size, back_size)
        back_seed = build_seed_guess(seed_payload, "back", front_size, back_size)
        print(
            "Using seed preset "
            f"{args.seed_preset}: front fx={front_seed[0][0, 0]:.1f} "
            f"back fx={back_seed[0][0, 0]:.1f}"
        )

    Kf, Df, _, _, front_size = calibrate_side(front_obs, initial_guess=front_seed)
    Kb, Db, _, _, back_size = calibrate_side(back_obs, initial_guess=back_seed)

    pair_map = match_pairs(observations)
    front_by_pair = {k: v["front"] for k, v in pair_map.items() if "front" in v}
    back_by_pair = {k: v["back"] for k, v in pair_map.items() if "back" in v}
    back_euler = estimate_residual_back_euler(front_by_pair, back_by_pair, Kf, Df, Kb, Db)

    full_width = front_size[0] + back_size[0]
    front_cx = float(Kf[0, 2] / full_width)
    front_cy = float(Kf[1, 2] / front_size[1])
    back_cx = float((front_size[0] + Kb[0, 2]) / full_width)
    back_cy = float(Kb[1, 2] / back_size[1])

    front_y_scale = clip(float(Kf[0, 0] / Kf[1, 1]), 0.7, 1.3)
    back_y_scale = clip(float(Kb[0, 0] / Kb[1, 1]), 0.7, 1.3)

    # Derive aperture from fitted focal length.
    # Equidistant model: r_pixels = f * theta, and in UV: r_uv = aperture * theta.
    # Therefore aperture = f / image_width (since scale maps UV to half-frame width).
    front_aperture = float(Kf[0, 0]) / front_size[0]
    back_aperture = float(Kb[0, 0]) / back_size[0]
    fitted_aperture = (front_aperture + back_aperture) / 2.0
    aperture = args.override_aperture if args.override_aperture is not None else fitted_aperture
    print(f"Aperture: front={front_aperture:.4f} back={back_aperture:.4f} -> using {aperture:.4f}"
          + (" (overridden)" if args.override_aperture is not None else " (fitted)"))

    result = {
        "schemaVersion": 1,
        "name": f"auto-charuco-{args.observations.stem}",
        "calibration": {
            "fov": args.default_fov,
            "front": {
                "center": [front_cx, front_cy],
                "scale": [0.5, front_y_scale],
                "euler": [0.0, 0.0, 0.0],
                "distortion": [float(Df[0, 0]), float(Df[1, 0])],
            },
            "back": {
                "center": [back_cx, back_cy],
                "scale": [-0.5, back_y_scale],
                "euler": back_euler,
                "distortion": [float(Db[0, 0]), float(Db[1, 0])],
            },
            "projection": {
                "aperture": aperture,
                "viewCorrection": args.default_view_correction,
            },
            "seam": {
                "centerYaw": 0.0,
                "width": args.default_seam_width,
                "feather": args.default_seam_feather,
            },
        },
        "fitDiagnostics": {
            "frontImageSize": front_size,
            "backImageSize": back_size,
            "frontFxFy": [float(Kf[0, 0]), float(Kf[1, 1])],
            "backFxFy": [float(Kb[0, 0]), float(Kb[1, 1])],
            "frontAperture": front_aperture,
            "backAperture": back_aperture,
            "sharedPairsUsed": int(len(set(front_by_pair.keys()).intersection(back_by_pair.keys()))),
        },
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()
