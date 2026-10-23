#!/usr/bin/env python3
"""Photometric seam optimizer for dual-fisheye calibration.

Adjusts ``back.euler`` and ``seam.centerYaw`` to minimise the colour
discontinuity across the overlap zone, using only regular footage (no
calibration targets required).

All other preset fields (center, scale, distortion, aperture) are held fixed.

Usage::

    conda run -n calibration python Calibration/scripts/optimize_seam.py \\
        --input-dir Calibration/tmp/frames_20260330_131521/ \\
        --seed-preset Src/src/assets/calibration/legacy_guess.json \\
        --output Calibration/presets/seam_optimized.json \\
        --num-frames 15

The script also accepts a video file via ``--input`` instead of an image
directory.
"""

from __future__ import annotations

import argparse
import copy
import json
import glob
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np
from scipy.optimize import minimize

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
    p.add_argument("--num-frames", type=int, default=15,
                   help="Number of frames to sample (default: 15)")
    p.add_argument("--sample-every", type=int, default=30,
                   help="When using --input video: take every Nth frame (default: 30)")
    p.add_argument("--exclude-observations", type=Path, default=None,
                   help="Observations JSON whose frame indices should be excluded")
    p.add_argument("--n-yaw", type=int, default=120,
                   help="Number of azimuth samples in overlap grid (default: 120)")
    p.add_argument("--n-pitch", type=int, default=40,
                   help="Number of polar-angle samples in overlap grid (default: 40)")
    p.add_argument("--verbose", action="store_true")
    return p.parse_args()


# ---------------------------------------------------------------------------
# Frame loading
# ---------------------------------------------------------------------------

def load_frames_from_dir(
    directory: Path,
    num_frames: int,
    exclude_indices: Optional[set] = None,
) -> List[np.ndarray]:
    """Load up to ``num_frames`` images, evenly spaced, as grayscale."""
    exts = ("*.png", "*.jpg", "*.jpeg")
    paths = []
    for ext in exts:
        paths.extend(sorted(directory.glob(ext)))
    if not paths:
        raise RuntimeError(f"No image files found in {directory}")

    if exclude_indices:
        paths = [p for i, p in enumerate(paths) if i not in exclude_indices]

    # Score frames by overlap-band brightness / gradient to pick textured ones
    step = max(1, len(paths) // (num_frames * 3))
    candidates = paths[::step]

    scored: List[Tuple[float, Path]] = []
    for p in candidates:
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is None:
            continue
        h, w = img.shape
        # Overlap is at the edges of each fisheye circle -- approximate as
        # two vertical strips near the centre of the full frame.
        mid = w // 2
        band_w = max(w // 20, 10)
        strip = img[:, mid - band_w : mid + band_w]
        grad = float(np.mean(np.abs(np.diff(strip.astype(np.float32), axis=1))))
        scored.append((grad, p))

    scored.sort(key=lambda x: -x[0])  # highest gradient first
    selected = scored[:num_frames]
    print(f"Selected {len(selected)} / {len(paths)} frames by overlap texture score")

    frames = []
    for _, p in selected:
        img = cv2.imread(str(p), cv2.IMREAD_GRAYSCALE)
        if img is not None:
            frames.append(img)
    return frames


def load_frames_from_video(
    video_path: Path,
    num_frames: int,
    sample_every: int,
    exclude_indices: Optional[set] = None,
) -> List[np.ndarray]:
    """Decode frames from a video, keeping every ``sample_every``-th frame."""
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {video_path}")

    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    print(f"Video: {total} total frames, sampling every {sample_every}")

    candidates: List[Tuple[int, np.ndarray]] = []
    idx = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % sample_every == 0:
            if exclude_indices is None or idx not in exclude_indices:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                candidates.append((idx, gray))
        idx += 1
    cap.release()

    if not candidates:
        raise RuntimeError("No usable frames extracted from video")

    # Score by overlap-band gradient
    scored: List[Tuple[float, np.ndarray]] = []
    for _, gray in candidates:
        h, w = gray.shape
        mid = w // 2
        band_w = max(w // 20, 10)
        strip = gray[:, mid - band_w : mid + band_w]
        grad = float(np.mean(np.abs(np.diff(strip.astype(np.float32), axis=1))))
        scored.append((grad, gray))

    scored.sort(key=lambda x: -x[0])
    frames = [g for _, g in scored[:num_frames]]
    print(f"Selected {len(frames)} / {len(candidates)} sampled frames by overlap texture score")
    return frames


def parse_exclude_indices(obs_path: Optional[Path]) -> Optional[set]:
    """Extract frame indices from an observations JSON to exclude."""
    if obs_path is None or not obs_path.exists():
        return None
    with obs_path.open() as f:
        payload = json.load(f)
    indices = set()
    for obs in payload.get("observations", []):
        fi = obs.get("frame_index")
        if fi is not None:
            indices.add(int(fi))
    return indices if indices else None


# ---------------------------------------------------------------------------
# Loss function
# ---------------------------------------------------------------------------

def compute_overlap_loss(
    back_euler: np.ndarray,
    seam_center_yaw: float,
    rays: np.ndarray,
    frames: List[np.ndarray],
    base_params: Dict,
) -> float:
    """Mean normalised grayscale difference in the overlap band.

    Modifies only ``back_euler`` and ``seam_center_yaw`` in the params copy.
    """
    params = dict(base_params)
    params["back_euler"] = back_euler
    params["seam_center_yaw"] = seam_center_yaw

    front_uv = fp.world_ray_to_uv(rays, "front", params)
    back_uv = fp.world_ray_to_uv(rays, "back", params)

    total_loss = 0.0
    n_valid = 0

    for frame in frames:
        f_vals = fp.sample_image_at_uvs(frame, front_uv)
        b_vals = fp.sample_image_at_uvs(frame, back_uv)

        valid = np.isfinite(f_vals) & np.isfinite(b_vals)
        if valid.sum() < 10:
            continue

        fv = f_vals[valid]
        bv = b_vals[valid]

        # Per-side normalisation: zero-mean, unit-std.
        # This removes global exposure and vignetting-level differences.
        f_mean, f_std = fv.mean(), max(fv.std(), 1.0)
        b_mean, b_std = bv.mean(), max(bv.std(), 1.0)
        fn = (fv - f_mean) / f_std
        bn = (bv - b_mean) / b_std

        total_loss += float(np.mean(np.abs(fn - bn)))
        n_valid += 1

    if n_valid == 0:
        return 1e6
    return total_loss / n_valid


def _objective(x: np.ndarray, rays, frames, base_params, call_count, verbose):
    """Scipy-compatible objective wrapper."""
    back_euler = x[:3]
    seam_center_yaw = float(x[3])
    loss = compute_overlap_loss(back_euler, seam_center_yaw, rays, frames, base_params)
    call_count[0] += 1
    if verbose and call_count[0] % 20 == 0:
        print(f"  iter {call_count[0]:4d}  loss={loss:.4f}  "
              f"euler=({np.degrees(x[0]):.3f}° {np.degrees(x[1]):.3f}° {np.degrees(x[2]):.3f}°)  "
              f"seamYaw={np.degrees(x[3]):.3f}°")
    return loss


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
        raise RuntimeError(f"Unsupported schemaVersion in seed preset")

    base_params = fp.preset_to_params(seed_preset)
    print(f"Seed preset: {args.seed_preset}")
    print(f"  aperture={base_params['aperture']:.4f}")
    print(f"  back.euler (seed): {[round(np.degrees(v), 4) for v in base_params['back_euler']]}°")
    print(f"  seam.centerYaw (seed): {np.degrees(base_params['seam_center_yaw']):.4f}°")

    # -- Load frames --------------------------------------------------------
    exclude = parse_exclude_indices(args.exclude_observations)
    if args.input_dir is not None:
        frames = load_frames_from_dir(args.input_dir, args.num_frames, exclude)
    else:
        frames = load_frames_from_video(args.input, args.num_frames, args.sample_every, exclude)

    if not frames:
        raise RuntimeError("No frames loaded")
    print(f"Frame size: {frames[0].shape[1]}x{frames[0].shape[0]}")

    # -- Sample overlap rays ------------------------------------------------
    rays = fp.sample_overlap_rays(base_params, n_yaw=args.n_yaw, n_pitch=args.n_pitch)
    print(f"Overlap rays: {rays.shape[0]}")
    if rays.shape[0] < 50:
        raise RuntimeError("Too few overlap rays -- is the aperture/scale correct in the seed preset?")

    # -- Initial loss -------------------------------------------------------
    init_euler = base_params["back_euler"].copy()
    init_seam = base_params["seam_center_yaw"]
    init_loss = compute_overlap_loss(init_euler, init_seam, rays, frames, base_params)
    print(f"\nInitial loss: {init_loss:.4f}")

    # -- Optimize -----------------------------------------------------------
    x0 = np.array([init_euler[0], init_euler[1], init_euler[2], init_seam])
    call_count = [0]

    result = minimize(
        _objective,
        x0,
        args=(rays, frames, base_params, call_count, args.verbose),
        method="Powell",
        options={
            "maxiter": 500,
            "ftol": 1e-7,
            "xtol": 1e-7,
        },
    )

    opt_euler = result.x[:3]
    opt_seam_yaw = float(result.x[3])
    final_loss = result.fun

    print(f"\nOptimization {'converged' if result.success else 'did not converge'} "
          f"in {result.nfev} evaluations")
    print(f"Initial loss: {init_loss:.4f}  Final loss: {final_loss:.4f}")
    print(f"back.euler: [{np.degrees(init_euler[0]):.4f}°, {np.degrees(init_euler[1]):.4f}°, "
          f"{np.degrees(init_euler[2]):.4f}°] -> [{np.degrees(opt_euler[0]):.4f}°, "
          f"{np.degrees(opt_euler[1]):.4f}°, {np.degrees(opt_euler[2]):.4f}°]")
    print(f"seam.centerYaw: {np.degrees(init_seam):.4f}° -> {np.degrees(opt_seam_yaw):.4f}°")

    # -- Build output preset ------------------------------------------------
    out = copy.deepcopy(seed_preset)
    out_cal = out.setdefault("calibration", {})
    out_cal.setdefault("back", {})["euler"] = [float(v) for v in opt_euler]
    out_cal.setdefault("seam", {})["centerYaw"] = float(opt_seam_yaw)
    out["name"] = f"seam-optimized-{Path(args.output).stem}"
    out.setdefault("fitDiagnostics", {}).update({
        "mode": "photometric_seam",
        "seedPreset": str(args.seed_preset),
        "numFrames": len(frames),
        "overlapRays": int(rays.shape[0]),
        "initialLoss": round(float(init_loss), 6),
        "finalLoss": round(float(final_loss), 6),
        "converged": bool(result.success),
        "functionEvaluations": int(result.nfev),
        "initialBackEulerDeg": [round(np.degrees(v), 4) for v in init_euler],
        "finalBackEulerDeg": [round(np.degrees(v), 4) for v in opt_euler],
        "initialSeamCenterYawDeg": round(np.degrees(init_seam), 4),
        "finalSeamCenterYawDeg": round(np.degrees(opt_seam_yaw), 4),
    })

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    print(f"\nWrote {args.output}")


if __name__ == "__main__":
    main()
