#!/usr/bin/env python3
"""Simplified overlap refinement for dual-fisheye calibration.

This script is the lightweight alternative to ``fit_dual_fisheye.py``.  Instead
of solving the full camera intrinsics, it:

1. Loads an existing calibration preset as a seed (default:
   ``Src/src/assets/calibration/legacy_guess.json``).
2. Keeps all geometry-independent parameters fixed:
   - ``front/back.center``
   - ``front/back.scale``
   - ``projection.aperture``
   - ``front/back.distortion``
3. Estimates only the overlap-sensitive parameters from paired Charuco
   observations (frames where the same board is visible in both lenses):
   - ``back.euler``  -- residual back-lens orientation relative to the
     ideal 180-degree flip
   - ``seam.centerYaw`` -- consistent yaw bias between lenses that should
     be absorbed by the seam rather than attributed to a lens rotation
4. Writes a new preset preserving all fixed fields and replacing only the
   refined fields, with a detailed diagnostic log.

Usage::

    conda run -n calibration python Calibration/scripts/refine_overlap.py \\
        --observations Calibration/tmp/charuco_observations.json \\
        --seed-preset Src/src/assets/calibration/legacy_guess.json \\
        --output Calibration/presets/overlap_refined.json

Typical workflow
----------------
Use this script when the existing calibration is visually close but the seam
overlap region shows misalignment artefacts.  Only capture is needed: a short
clip (~60 s) with the Charuco board visible in the overlap region of both
lenses, with clear, sharp frames at varied tilts.  Full-lens coverage is not
required.

To run a full calibration from scratch (new camera or major geometry change)
use ``fit_dual_fisheye.py`` instead.
"""

from __future__ import annotations

import argparse
import copy
import json
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


# ---------------------------------------------------------------------------
# Filtering constants
# ---------------------------------------------------------------------------

# Both sides of a pair must have at least this many corners.
DEFAULT_MIN_CORNERS = 20

# Detected corners that are within this many pixels of the image edge are
# considered edge-clipped; observations with very tight margins are weak.
DEFAULT_MIN_EDGE_MARGIN = 30

# Pairs where either side's bounding box covers less than this fraction of
# the image area are considered "too small" (target too far or too partial).
DEFAULT_MIN_AREA_FRAC = 0.003


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).parent.parent.parent
    default_seed = repo_root / "Src" / "src" / "assets" / "calibration" / "legacy_guess.json"

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--observations", type=Path, required=True,
                        help="Observations JSON from detect_charuco.py")
    parser.add_argument("--output", type=Path, required=True,
                        help="Output calibration preset JSON")
    parser.add_argument("--seed-preset", type=Path, default=default_seed,
                        help="Seed calibration preset (default: legacy_guess.json)")
    parser.add_argument("--min-corners", type=int, default=DEFAULT_MIN_CORNERS,
                        help=f"Min Charuco corners per side per pair (default: {DEFAULT_MIN_CORNERS})")
    parser.add_argument("--min-edge-margin", type=float, default=DEFAULT_MIN_EDGE_MARGIN,
                        help=f"Min pixel margin from image edge (default: {DEFAULT_MIN_EDGE_MARGIN})")
    parser.add_argument("--min-area-frac", type=float, default=DEFAULT_MIN_AREA_FRAC,
                        help=f"Min bbox area fraction of image (default: {DEFAULT_MIN_AREA_FRAC})")
    parser.add_argument("--min-pairs", type=int, default=5,
                        help="Minimum accepted pairs needed to proceed (default: 5)")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Preset helpers
# ---------------------------------------------------------------------------

def load_preset(path: Path) -> Dict:
    with path.open("r", encoding="utf-8") as f:
        preset = json.load(f)
    schema = preset.get("schemaVersion")
    if schema != 1:
        raise RuntimeError(
            f"Unsupported preset schemaVersion: {schema!r} (expected 1). "
            f"Preset: {path}"
        )
    return preset


def preset_to_camera_matrices(
    preset: Dict,
    side: str,
    front_size: Tuple[int, int],
    back_size: Tuple[int, int],
) -> Tuple[np.ndarray, np.ndarray]:
    """Convert a normalized preset side config into pixel-space K and D.

    The preset stores all values in UV-normalised coordinates relative to the
    full dual-frame width.  This function scales them to the raw pixel
    coordinates used by the calibration observation.
    """
    calibration = preset.get("calibration", {})
    side_cfg = calibration.get(side, {})
    projection = calibration.get("projection", {})

    full_width = front_size[0] + back_size[0]
    image_size = front_size if side == "front" else back_size
    aperture = float(projection.get("aperture", 0.296))
    center = side_cfg.get("center", [0.25 if side == "front" else 0.75, 0.5])
    scale = side_cfg.get("scale", [0.5 if side == "front" else -0.5, 1.0])
    distortion = side_cfg.get("distortion", [0.0, 0.0])

    fx = aperture * image_size[0]
    scale_y = abs(float(scale[1])) if len(scale) > 1 else 1.0
    fy = fx / max(scale_y, 1e-6)

    cx_full = float(center[0]) * full_width
    cx = cx_full if side == "front" else cx_full - front_size[0]
    cy = float(center[1]) * image_size[1]

    K = np.array([[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]], dtype=np.float64)
    D = np.zeros((4, 1), dtype=np.float64)
    if len(distortion) > 0:
        D[0, 0] = float(distortion[0])
    if len(distortion) > 1:
        D[1, 0] = float(distortion[1])
    return K, D


# ---------------------------------------------------------------------------
# Pair filtering
# ---------------------------------------------------------------------------

def _obs_bbox_stats(obs: Dict) -> Tuple[float, float]:
    """Return (area_fraction, min_edge_margin) for one observation."""
    pts = obs["image_points"]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    img_w, img_h = obs["image_size"]
    w = max(xs) - min(xs)
    h = max(ys) - min(ys)
    area_frac = (w * h) / (img_w * img_h)
    edge_margin = min(min(xs), min(ys), img_w - max(xs), img_h - max(ys))
    return area_frac, edge_margin


def filter_paired_observations(
    observations: List[Dict],
    min_corners: int,
    min_edge_margin: float,
    min_area_frac: float,
) -> Tuple[Dict[str, Dict], Dict[str, str]]:
    """Find paired (front+back) observations that pass quality thresholds.

    Returns:
        accepted: {pair_key: {"front": obs, "back": obs}} for pairs that pass
        drop_reasons: {pair_key: human-readable reason string} for dropped pairs
    """
    by_key: Dict[str, Dict[str, Dict]] = defaultdict(dict)
    for obs in observations:
        by_key[obs["pair_key"]][obs["side"]] = obs

    accepted: Dict[str, Dict] = {}
    drop_reasons: Dict[str, str] = {}

    for key, sides in sorted(by_key.items()):
        if "front" not in sides or "back" not in sides:
            drop_reasons[key] = f"missing side ({list(sides.keys())} only)"
            continue

        fobs = sides["front"]
        bobs = sides["back"]

        if fobs["num_corners"] < min_corners:
            drop_reasons[key] = f"front corners {fobs['num_corners']} < {min_corners}"
            continue
        if bobs["num_corners"] < min_corners:
            drop_reasons[key] = f"back corners {bobs['num_corners']} < {min_corners}"
            continue

        f_area, f_margin = _obs_bbox_stats(fobs)
        b_area, b_margin = _obs_bbox_stats(bobs)

        if f_area < min_area_frac:
            drop_reasons[key] = f"front bbox area {f_area:.4f} < {min_area_frac}"
            continue
        if b_area < min_area_frac:
            drop_reasons[key] = f"back bbox area {b_area:.4f} < {min_area_frac}"
            continue

        if f_margin < min_edge_margin:
            drop_reasons[key] = f"front edge margin {f_margin:.1f} < {min_edge_margin}"
            continue
        if b_margin < min_edge_margin:
            drop_reasons[key] = f"back edge margin {b_margin:.1f} < {min_edge_margin}"
            continue

        accepted[key] = {"front": fobs, "back": bobs}

    return accepted, drop_reasons


# ---------------------------------------------------------------------------
# Rotation utilities
# ---------------------------------------------------------------------------

def _to_cv_points(
    points3d: List[List[float]], points2d: List[List[float]]
) -> Tuple[np.ndarray, np.ndarray]:
    obj = np.asarray(points3d, dtype=np.float64).reshape(-1, 1, 3)
    img = np.asarray(points2d, dtype=np.float64).reshape(-1, 1, 2)
    return obj, img


def _rotation_from_rvec(rvec: np.ndarray) -> np.ndarray:
    R, _ = cv2.Rodrigues(rvec)
    return R


def _average_rotation(rotations: List[np.ndarray]) -> Optional[np.ndarray]:
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


def _matrix_to_euler_xyz(R: np.ndarray) -> List[float]:
    sy = float(np.sqrt(R[0, 0] ** 2 + R[1, 0] ** 2))
    singular = sy < 1e-6
    if not singular:
        x = float(np.arctan2(R[2, 1], R[2, 2]))
        y = float(np.arctan2(-R[2, 0], sy))
        z = float(np.arctan2(R[1, 0], R[0, 0]))
    else:
        x = float(np.arctan2(-R[1, 2], R[1, 1]))
        y = float(np.arctan2(-R[2, 0], sy))
        z = 0.0
    return [x, y, z]


# ---------------------------------------------------------------------------
# Core refinement
# ---------------------------------------------------------------------------

BASE_FLIP = np.array([[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]], dtype=np.float64)


def estimate_back_euler_from_pairs(
    accepted_pairs: Dict[str, Dict],
    Kf: np.ndarray,
    Df: np.ndarray,
    Kb: np.ndarray,
    Db: np.ndarray,
) -> Tuple[List[float], List[str]]:
    """Estimate residual back-lens euler from accepted paired observations.

    For each pair, undistort the image points using the seeded intrinsics and
    solve PnP for both lenses.  The relative rotation between the two
    solutions (after removing the ideal 180-degree base flip) is the residual
    back-lens misalignment.  The final result is the SVD average of all
    per-pair residuals, which is robust to individual outlier pairs.

    Returns:
        euler: [pitch, yaw, roll] in radians (XYZ convention)
        pair_notes: human-readable notes per pair (one-liner per pair)
    """
    K_unit = np.eye(3, dtype=np.float64)
    D_zero = np.zeros((4, 1), dtype=np.float64)

    residuals: List[np.ndarray] = []
    pair_notes: List[str] = []

    for key in sorted(accepted_pairs.keys()):
        fobs = accepted_pairs[key]["front"]
        bobs = accepted_pairs[key]["back"]

        fobj, fimg = _to_cv_points(fobs["object_points"], fobs["image_points"])
        bobj, bimg = _to_cv_points(bobs["object_points"], bobs["image_points"])

        try:
            fimg_u = cv2.fisheye.undistortPoints(fimg, Kf, Df).reshape(-1, 1, 2)
            bimg_u = cv2.fisheye.undistortPoints(bimg, Kb, Db).reshape(-1, 1, 2)
        except cv2.error as e:
            pair_notes.append(f"{key}: undistort failed ({e})")
            continue

        ok_f, rvec_f, _ = cv2.solvePnP(fobj, fimg_u, K_unit, D_zero)
        ok_b, rvec_b, _ = cv2.solvePnP(bobj, bimg_u, K_unit, D_zero)

        if not ok_f or not ok_b:
            pair_notes.append(f"{key}: solvePnP failed (front={ok_f} back={ok_b})")
            continue

        Rf = _rotation_from_rvec(rvec_f)
        Rb = _rotation_from_rvec(rvec_b)
        rel = Rb @ Rf.T
        residual = rel @ BASE_FLIP.T
        residuals.append(residual)

        euler = _matrix_to_euler_xyz(residual)
        pair_notes.append(
            f"{key}: used  euler=({np.degrees(euler[0]):.3f}° {np.degrees(euler[1]):.3f}° {np.degrees(euler[2]):.3f}°)"
            f"  fc={fobs['num_corners']} bc={bobs['num_corners']}"
        )

    R_mean = _average_rotation(residuals)
    if R_mean is None:
        return [0.0, 0.0, 0.0], pair_notes

    return _matrix_to_euler_xyz(R_mean), pair_notes


def extract_seam_yaw(back_euler: List[float], seed_euler: List[float]) -> Tuple[List[float], float]:
    """Split the yaw component of back.euler into lens-residual vs seam bias.

    The yaw component (index 1) of back.euler is the dominant driver of seam
    left/right placement.  In practice, some of the estimated yaw may be due
    to the board being placed slightly asymmetrically rather than a real lens
    rotation.  This function uses a simple heuristic: if the estimated yaw
    shift relative to the seed is larger than a small lens-rotation threshold
    (~0.015 rad, ~0.9°), the excess is moved into seam.centerYaw so the
    stitch seam tracks it, while the lens rotation is capped.

    This keeps ``back.euler`` conservative (small residual rotation that was
    genuinely observed) while letting the seam track any remaining yaw bias.
    """
    LENS_YAW_CAP = 0.015  # radians, ~0.9 degrees

    delta_yaw = back_euler[1] - seed_euler[1]

    if abs(delta_yaw) > LENS_YAW_CAP:
        excess = delta_yaw - np.sign(delta_yaw) * LENS_YAW_CAP
        capped_euler = [back_euler[0], seed_euler[1] + np.sign(delta_yaw) * LENS_YAW_CAP, back_euler[2]]
        seam_yaw_shift = float(excess)
    else:
        capped_euler = list(back_euler)
        seam_yaw_shift = 0.0

    return capped_euler, seam_yaw_shift


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    # -- Load observations ---------------------------------------------------
    with args.observations.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    if payload.get("schema_version") != 1:
        raise RuntimeError(
            f"Unsupported observations schema_version: {payload.get('schema_version')!r} (expected 1). "
            "Re-run detect_charuco.py to regenerate the observations file."
        )

    observations = payload.get("observations", [])
    front_obs = [o for o in observations if o.get("side") == "front"]
    back_obs = [o for o in observations if o.get("side") == "back"]
    print(f"Loaded observations: {len(front_obs)} front, {len(back_obs)} back")

    # -- Load seed preset ----------------------------------------------------
    if not args.seed_preset.exists():
        raise RuntimeError(
            f"Seed preset not found: {args.seed_preset}\n"
            "Pass --seed-preset <path> to specify a different preset."
        )
    seed_preset = load_preset(args.seed_preset)
    seed_cal = seed_preset.get("calibration", {})
    seed_back_euler = seed_cal.get("back", {}).get("euler", [0.0, 0.0, 0.0])
    seed_seam_yaw = seed_cal.get("seam", {}).get("centerYaw", 0.0)
    print(f"Seed preset: {args.seed_preset}")
    print(f"  back.euler (seed): {[round(np.degrees(v), 4) for v in seed_back_euler]} deg")
    print(f"  seam.centerYaw (seed): {np.degrees(seed_seam_yaw):.4f} deg")

    # -- Build camera matrices from seed ------------------------------------
    if not front_obs or not back_obs:
        raise RuntimeError("Observations must contain both front and back side entries.")

    front_size: Tuple[int, int] = tuple(front_obs[0]["image_size"])  # type: ignore[assignment]
    back_size: Tuple[int, int] = tuple(back_obs[0]["image_size"])  # type: ignore[assignment]

    Kf, Df = preset_to_camera_matrices(seed_preset, "front", front_size, back_size)
    Kb, Db = preset_to_camera_matrices(seed_preset, "back", front_size, back_size)
    print(f"  front fx={Kf[0, 0]:.1f}  back fx={Kb[0, 0]:.1f}  (from seed aperture)")

    # -- Filter paired observations ------------------------------------------
    accepted, drop_reasons = filter_paired_observations(
        observations,
        min_corners=args.min_corners,
        min_edge_margin=args.min_edge_margin,
        min_area_frac=args.min_area_frac,
    )

    print(f"\nPair filtering: {len(accepted)} accepted, {len(drop_reasons)} dropped")
    if drop_reasons:
        print("  Dropped pairs:")
        for key, reason in sorted(drop_reasons.items()):
            print(f"    {key}: {reason}")

    if len(accepted) < args.min_pairs:
        raise RuntimeError(
            f"Only {len(accepted)} pairs passed quality filters (need at least {args.min_pairs}). "
            "Try a shorter --min-corners, wider --min-edge-margin, or re-capture with more overlap frames."
        )

    # -- Estimate back euler -------------------------------------------------
    print(f"\nEstimating back.euler from {len(accepted)} accepted pairs ...")
    raw_euler, pair_notes = estimate_back_euler_from_pairs(accepted, Kf, Df, Kb, Db)
    print("  Per-pair results:")
    for note in pair_notes:
        print(f"    {note}")

    # -- Split yaw into lens residual vs seam shift --------------------------
    refined_euler, seam_yaw_shift = extract_seam_yaw(raw_euler, seed_back_euler)
    new_seam_yaw = seed_seam_yaw + seam_yaw_shift

    # -- Build output preset -------------------------------------------------
    result = copy.deepcopy(seed_preset)
    result_cal = result.setdefault("calibration", {})

    result_cal.setdefault("back", {})["euler"] = [float(v) for v in refined_euler]
    result_cal.setdefault("seam", {})["centerYaw"] = float(new_seam_yaw)
    result["name"] = f"overlap-refined-{args.observations.stem}"
    result.setdefault("fitDiagnostics", {}).update({
        "mode": "overlap_refinement",
        "seedPreset": str(args.seed_preset),
        "observationsFile": str(args.observations),
        "totalPairs": len(accepted) + len(drop_reasons),
        "acceptedPairs": len(accepted),
        "droppedPairs": len(drop_reasons),
        "rawBackEulerDeg": [round(np.degrees(v), 4) for v in raw_euler],
        "refinedBackEulerDeg": [round(np.degrees(v), 4) for v in refined_euler],
        "seamYawShiftDeg": round(np.degrees(seam_yaw_shift), 4),
        "newSeamCenterYawDeg": round(np.degrees(new_seam_yaw), 4),
    })

    # -- Print summary -------------------------------------------------------
    print(f"\nRefined back.euler: {[round(np.degrees(v), 4) for v in refined_euler]} deg"
          f"  (was {[round(np.degrees(v), 4) for v in seed_back_euler]} deg)")
    print(f"Seam yaw shift: {np.degrees(seam_yaw_shift):.4f} deg -> new seam.centerYaw {np.degrees(new_seam_yaw):.4f} deg")

    # -- Write output --------------------------------------------------------
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
