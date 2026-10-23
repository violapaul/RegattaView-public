#!/usr/bin/env python3
"""Generate a printable Charuco calibration target image."""

from __future__ import annotations

import argparse
from pathlib import Path

import cv2


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--squares-x", type=int, default=10, help="Number of squares across.")
    parser.add_argument("--squares-y", type=int, default=7, help="Number of squares down.")
    parser.add_argument("--square-length-mm", type=float, default=40.0, help="Square size in mm.")
    parser.add_argument("--marker-length-mm", type=float, default=28.0, help="Marker size in mm.")
    parser.add_argument(
        "--dict-name",
        type=str,
        default="DICT_5X5_1000",
        help="OpenCV aruco dictionary enum name (e.g. DICT_4X4_250).",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="Rasterization DPI used to convert mm to pixels.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="Output image path (png recommended).",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()

    if args.marker_length_mm >= args.square_length_mm:
        raise ValueError("--marker-length-mm must be less than --square-length-mm")

    dict_id = getattr(cv2.aruco, args.dict_name)
    aruco_dict = cv2.aruco.getPredefinedDictionary(dict_id)
    board = cv2.aruco.CharucoBoard(
        (args.squares_x, args.squares_y),
        args.square_length_mm / 1000.0,
        args.marker_length_mm / 1000.0,
        aruco_dict,
    )

    width_mm = args.squares_x * args.square_length_mm
    height_mm = args.squares_y * args.square_length_mm
    px_per_mm = args.dpi / 25.4
    out_size = (int(round(width_mm * px_per_mm)), int(round(height_mm * px_per_mm)))

    image = board.generateImage(out_size, marginSize=16, borderBits=1)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    ok = cv2.imwrite(str(args.output), image)
    if not ok:
        raise RuntimeError(f"Failed to write {args.output}")

    print(f"Wrote {args.output}")
    print(f"Board size: {width_mm:.1f}mm x {height_mm:.1f}mm at {args.dpi} DPI")


if __name__ == "__main__":
    main()
