#!/usr/bin/env python3
"""Detect Charuco observations from dual-fisheye images or video."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import cv2
import numpy as np


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="Image directory, image file, or video file.")
    parser.add_argument("--output", type=Path, required=True, help="Output observations JSON.")
    parser.add_argument("--squares-x", type=int, default=10)
    parser.add_argument("--squares-y", type=int, default=7)
    parser.add_argument("--square-length-mm", type=float, default=40.0)
    parser.add_argument("--marker-length-mm", type=float, default=28.0)
    parser.add_argument("--dict-name", type=str, default="DICT_5X5_1000")
    parser.add_argument("--min-corners", type=int, default=14, help="Minimum Charuco corners to keep an observation.")
    parser.add_argument("--sample-every", type=int, default=15, help="Video frame stride.")
    parser.add_argument(
        "--layout",
        choices=["dual_half", "front_only", "back_only"],
        default="dual_half",
        help="How the input frame should be interpreted.",
    )
    return parser.parse_args()


def make_board(args: argparse.Namespace) -> Tuple[cv2.aruco.CharucoBoard, np.ndarray]:
    dict_id = getattr(cv2.aruco, args.dict_name)
    aruco_dict = cv2.aruco.getPredefinedDictionary(dict_id)
    board = cv2.aruco.CharucoBoard(
        (args.squares_x, args.squares_y),
        args.square_length_mm / 1000.0,
        args.marker_length_mm / 1000.0,
        aruco_dict,
    )
    corners_3d = board.getChessboardCorners().astype(np.float64)
    return board, corners_3d


def detect_charuco(
    image_bgr: np.ndarray,
    board: cv2.aruco.CharucoBoard,
    board_corners_3d: np.ndarray,
    min_corners: int,
) -> Optional[Dict]:
    gray = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2GRAY)
    corners, ids, _ = cv2.aruco.detectMarkers(gray, board.getDictionary())
    if ids is None or len(ids) == 0:
        return None

    retval, charuco_corners, charuco_ids = cv2.aruco.interpolateCornersCharuco(
        markerCorners=corners,
        markerIds=ids,
        image=gray,
        board=board,
    )
    if retval is None or retval < min_corners or charuco_ids is None:
        return None

    idx = charuco_ids.flatten().astype(int)
    object_points = board_corners_3d[idx]
    image_points = charuco_corners.reshape(-1, 2).astype(np.float64)
    return {
        "num_corners": int(len(idx)),
        "charuco_ids": idx.tolist(),
        "object_points": object_points.tolist(),
        "image_points": image_points.tolist(),
    }


def split_dual_half(frame_bgr: np.ndarray) -> Dict[str, np.ndarray]:
    h, w = frame_bgr.shape[:2]
    half = w // 2
    return {
        "front": frame_bgr[:, :half],
        "back": frame_bgr[:, half:half * 2],
    }


def read_image_files(path: Path) -> List[Path]:
    if path.is_file():
        return [path]
    exts = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
    return sorted([p for p in path.iterdir() if p.suffix.lower() in exts])


def process_images(args: argparse.Namespace, board, board_corners_3d) -> List[Dict]:
    observations = []
    files = read_image_files(args.input)
    for idx, image_path in enumerate(files):
        image = cv2.imread(str(image_path))
        if image is None:
            continue
        pair_key = image_path.stem
        views = (
            split_dual_half(image)
            if args.layout == "dual_half"
            else {("front" if args.layout == "front_only" else "back"): image}
        )
        for side, img in views.items():
            det = detect_charuco(img, board, board_corners_3d, args.min_corners)
            if not det:
                continue
            h, w = img.shape[:2]
            observations.append(
                {
                    "source_type": "image",
                    "source_path": str(image_path),
                    "pair_key": pair_key,
                    "frame_index": idx,
                    "side": side,
                    "image_size": [int(w), int(h)],
                    **det,
                }
            )
    return observations


def process_video(args: argparse.Namespace, board, board_corners_3d) -> List[Dict]:
    cap = cv2.VideoCapture(str(args.input))
    if not cap.isOpened():
        raise RuntimeError(f"Failed to open video {args.input}")

    observations = []
    frame_idx = 0
    kept = 0
    sampled = 0
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        if frame_idx % args.sample_every != 0:
            frame_idx += 1
            continue

        sampled += 1
        pair_key = f"f{frame_idx:06d}"
        views = (
            split_dual_half(frame)
            if args.layout == "dual_half"
            else {("front" if args.layout == "front_only" else "back"): frame}
        )
        for side, img in views.items():
            det = detect_charuco(img, board, board_corners_3d, args.min_corners)
            if not det:
                continue
            h, w = img.shape[:2]
            observations.append(
                {
                    "source_type": "video",
                    "source_path": str(args.input),
                    "pair_key": pair_key,
                    "frame_index": frame_idx,
                    "side": side,
                    "image_size": [int(w), int(h)],
                    **det,
                }
            )
            kept += 1

        if sampled % 100 == 0:
            print(f"  ... frame {frame_idx} ({sampled} sampled, {kept} detections so far)")

        frame_idx += 1
    cap.release()
    print(f"Processed {frame_idx} frames, kept {kept} side-observations")
    return observations


def main() -> None:
    args = parse_args()
    board, board_corners_3d = make_board(args)

    if args.input.is_file() and args.input.suffix.lower() in {".mp4", ".mov", ".mkv", ".avi", ".insv"}:
        observations = process_video(args, board, board_corners_3d)
    else:
        observations = process_images(args, board, board_corners_3d)

    payload = {
        "schema_version": 1,
        "board": {
            "squares_x": args.squares_x,
            "squares_y": args.squares_y,
            "square_length_mm": args.square_length_mm,
            "marker_length_mm": args.marker_length_mm,
            "dict_name": args.dict_name,
        },
        "layout": args.layout,
        "observations": observations,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    print(f"Wrote {args.output} with {len(observations)} observations")


if __name__ == "__main__":
    main()
