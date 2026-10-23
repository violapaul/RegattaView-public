#!/usr/bin/env python3
"""Feature-based overlap matching for dual-fisheye seam alignment.

Reprojects both lens halves into a common cylindrical overlap strip, runs
ORB feature matching, and estimates the ``back.euler`` correction needed to
align the two views.

This complements ``optimize_seam.py`` (photometric approach) with explicit
geometric correspondences that can be validated visually.

Usage::

    conda run -n calibration python Calibration/scripts/match_overlap_features.py \\
        --input-dir Calibration/tmp/frames_20260330_131521/ \\
        --seed-preset Src/src/assets/calibration/legacy_guess.json \\
        --output Calibration/presets/feature_matched.json \\
        --num-frames 20
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np

_SCRIPTS_DIR = Path(__file__).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import fisheye_projection as fp


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    repo_root = Path(__file__).parent.parent.parent
    default_seed = repo_root / "Src" / "src" / "assets" / "calibration" / "legacy_guess.json"

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--input", type=Path, help="Video file (mp4/mov/insv)")
    grp.add_argument("--input-dir", type=Path, help="Directory of dual-fisheye PNG/JPG frames")
    p.add_argument("--seed-preset", type=Path, default=default_seed)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--num-frames", type=int, default=20)
    p.add_argument("--sample-every", type=int, default=30)
    p.add_argument("--strip-width", type=int, default=1000,
                   help="Cylindrical strip width in pixels (default: 1000)")
    p.add_argument("--strip-height", type=int, default=200,
                   help="Cylindrical strip height in pixels (default: 200)")
    p.add_argument("--max-features", type=int, default=2000,
                   help="Max ORB features per strip (default: 2000)")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Frame loading (shared with optimize_seam but self-contained here)
# ---------------------------------------------------------------------------

def load_frames(
    input_path: Optional[Path],
    input_dir: Optional[Path],
    num_frames: int,
    sample_every: int,
) -> List[np.ndarray]:
    """Load grayscale frames from either a directory or a video file."""
    if input_dir is not None:
        exts = ("*.png", "*.jpg", "*.jpeg")
        paths = []
        for ext in exts:
            paths.extend(sorted(input_dir.glob(ext)))
        if not paths:
            raise RuntimeError(f"No image files in {input_dir}")
        step = max(1, len(paths) // num_frames)
        selected = paths[::step][:num_frames]
        frames = []
        for p in selected:
            img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
            if img is not None:
                frames.append(img)
        return frames
    else:
        cap = cv2.VideoCapture(str(input_path))
        if not cap.isOpened():
            raise RuntimeError(f"Cannot open video: {input_path}")
        frames = []
        idx = 0
        while len(frames) < num_frames:
            ok, frame = cap.read()
            if not ok:
                break
            if idx % sample_every == 0:
                frames.append(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
            idx += 1
        cap.release()
        return frames


# ---------------------------------------------------------------------------
# Strip reprojection and feature matching
# ---------------------------------------------------------------------------

def reproject_strip(
    frame: np.ndarray,
    side: str,
    params: Dict,
    strip_width: int,
    strip_height: int,
) -> np.ndarray:
    """Reproject one lens half into a cylindrical overlap strip (uint8)."""
    strip_f = fp.reproject_to_overlap_strip(
        frame, side, params,
        strip_width=strip_width, strip_height=strip_height,
    )
    # Replace NaN with 0 and convert to uint8 for feature detection
    strip_f = np.nan_to_num(strip_f, nan=0.0)
    return np.clip(strip_f, 0, 255).astype(np.uint8)


def match_single_frame(
    frame: np.ndarray,
    params: Dict,
    strip_width: int,
    strip_height: int,
    max_features: int,
) -> Optional[Tuple[np.ndarray, np.ndarray, int]]:
    """Run ORB matching on one frame's overlap strips.

    Returns (front_pts, back_pts, total_matches) or None if matching failed.
    ``front_pts`` and ``back_pts`` are (M, 2) arrays in strip pixel coords.
    """
    front_strip = reproject_strip(frame, "front", params, strip_width, strip_height)
    back_strip = reproject_strip(frame, "back", params, strip_width, strip_height)

    orb = cv2.ORB_create(nfeatures=max_features)
    kp_f, desc_f = orb.detectAndCompute(front_strip, None)
    kp_b, desc_b = orb.detectAndCompute(back_strip, None)

    if desc_f is None or desc_b is None or len(kp_f) < 5 or len(kp_b) < 5:
        return None

    bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=True)
    raw_matches = bf.match(desc_f, desc_b)
    if len(raw_matches) < 4:
        return None

    pts_f = np.array([kp_f[m.queryIdx].pt for m in raw_matches], dtype=np.float64)
    pts_b = np.array([kp_b[m.trainIdx].pt for m in raw_matches], dtype=np.float64)

    # RANSAC on a pure-translation model: the dominant error is a horizontal
    # shift.  We use findHomography with RANSAC and then extract only the
    # translation, but the RANSAC step gives us an inlier mask.
    if len(pts_f) >= 4:
        _, mask = cv2.findHomography(pts_f, pts_b, cv2.RANSAC, ransacReprojThreshold=5.0)
        if mask is not None:
            inliers = mask.ravel().astype(bool)
            pts_f = pts_f[inliers]
            pts_b = pts_b[inliers]

    return pts_f, pts_b, len(raw_matches)


def displacement_to_euler(
    dx_px: float,
    dy_px: float,
    params: Dict,
    strip_width: int,
    strip_height: int,
) -> np.ndarray:
    """Convert a pixel displacement in the cylindrical strip to euler angles.

    The strip horizontal axis spans 0..2*pi azimuth, so a 1-pixel horizontal
    shift corresponds to ``2*pi / strip_width`` radians of yaw.  The vertical
    axis spans the overlap band's polar-angle range, so a 1-pixel vertical
    shift corresponds to ``(alpha_max - alpha_min) / strip_height`` radians
    of pitch.
    """
    theta_max = fp.max_theta_for_preset(params)
    margin = np.radians(1.0)
    alpha_min = np.pi - theta_max + margin
    alpha_max = theta_max - margin

    yaw_per_px = 2.0 * np.pi / strip_width
    pitch_per_px = (alpha_max - alpha_min) / strip_height

    # dx > 0 means back strip is shifted right (positive yaw error)
    euler_yaw = dx_px * yaw_per_px
    euler_pitch = dy_px * pitch_per_px

    return np.array([euler_pitch, euler_yaw, 0.0])


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    # -- Load seed preset ---------------------------------------------------
    if not args.seed_preset.exists():
        raise RuntimeError(f"Seed preset not found: {args.seed_preset}")
    with args.seed_preset.open() as f:
        seed_preset = json.load(f)
    if seed_preset.get("schemaVersion") != 1:
        raise RuntimeError("Unsupported schemaVersion in seed preset")

    params = fp.preset_to_params(seed_preset)
    seed_euler = params["back_euler"].copy()
    print(f"Seed preset: {args.seed_preset}")
    print(f"  back.euler (seed): {[round(np.degrees(v), 4) for v in seed_euler]}°")

    # -- Load frames --------------------------------------------------------
    frames = load_frames(args.input, args.input_dir, args.num_frames, args.sample_every)
    if not frames:
        raise RuntimeError("No frames loaded")
    print(f"Loaded {len(frames)} frames, size {frames[0].shape[1]}x{frames[0].shape[0]}")

    # -- Match each frame ---------------------------------------------------
    all_dx: List[float] = []
    all_dy: List[float] = []
    total_inliers = 0
    frames_matched = 0

    for i, frame in enumerate(frames):
        result = match_single_frame(
            frame, params, args.strip_width, args.strip_height, args.max_features
        )
        if result is None:
            if args.verbose:
                print(f"  Frame {i}: no matches")
            continue

        pts_f, pts_b, n_raw = result
        if len(pts_f) < 3:
            if args.verbose:
                print(f"  Frame {i}: only {len(pts_f)} inliers (need >= 3)")
            continue

        displacements = pts_b - pts_f
        med_dx = float(np.median(displacements[:, 0]))
        med_dy = float(np.median(displacements[:, 1]))
        all_dx.append(med_dx)
        all_dy.append(med_dy)
        total_inliers += len(pts_f)
        frames_matched += 1

        if args.verbose:
            print(f"  Frame {i}: {len(pts_f)} inliers / {n_raw} raw  "
                  f"dx={med_dx:.2f}px  dy={med_dy:.2f}px")

    print(f"\nMatched {frames_matched} / {len(frames)} frames  "
          f"({total_inliers} total inliers)")

    if frames_matched < 2:
        raise RuntimeError(
            f"Only {frames_matched} frames produced matches. "
            "Need at least 2 for a robust estimate."
        )

    # -- Aggregate displacements -------------------------------------------
    med_dx_all = float(np.median(all_dx))
    med_dy_all = float(np.median(all_dy))
    print(f"Median displacement: dx={med_dx_all:.2f}px  dy={med_dy_all:.2f}px "
          f"(strip {args.strip_width}x{args.strip_height})")

    euler_correction = displacement_to_euler(
        med_dx_all, med_dy_all, params, args.strip_width, args.strip_height
    )
    new_euler = seed_euler + euler_correction

    print(f"Euler correction: {[round(np.degrees(v), 4) for v in euler_correction]}°")
    print(f"New back.euler: {[round(np.degrees(v), 4) for v in new_euler]}°  "
          f"(was {[round(np.degrees(v), 4) for v in seed_euler]}°)")

    # -- Build output preset ------------------------------------------------
    out = copy.deepcopy(seed_preset)
    out_cal = out.setdefault("calibration", {})
    out_cal.setdefault("back", {})["euler"] = [float(v) for v in new_euler]
    out["name"] = f"feature-matched-{Path(args.output).stem}"
    out.setdefault("fitDiagnostics", {}).update({
        "mode": "feature_matching",
        "seedPreset": str(args.seed_preset),
        "numFrames": len(frames),
        "framesMatched": frames_matched,
        "totalInliers": total_inliers,
        "medianDisplacementPx": [round(med_dx_all, 3), round(med_dy_all, 3)],
        "stripSize": [args.strip_width, args.strip_height],
        "eulerCorrectionDeg": [round(np.degrees(v), 4) for v in euler_correction],
        "seedBackEulerDeg": [round(np.degrees(v), 4) for v in seed_euler],
        "newBackEulerDeg": [round(np.degrees(v), 4) for v in new_euler],
    })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
