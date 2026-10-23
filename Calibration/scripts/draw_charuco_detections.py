#!/usr/bin/env python3
"""Draw ChArUco marker + corner detections onto extracted frames for eyeballing.
Run in the `calibration` env. Saves only frames where corners were found."""
import argparse, glob, os, subprocess, sys, tempfile
import cv2, numpy as np


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--insv")
    ap.add_argument("--frames-dir")
    ap.add_argument("--crop", choices=["none", "left", "right"], default="none")
    ap.add_argument("--squares", default="10x7")
    ap.add_argument("--dict", default="DICT_5X5_50")
    ap.add_argument("--legacy", type=int, default=1)
    ap.add_argument("--fps", type=float, default=5.0)
    ap.add_argument("--min-corners", type=int, default=4)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()

    if args.insv:
        tmp = tempfile.mkdtemp(prefix="draw_")
        subprocess.run(["/opt/homebrew/bin/ffmpeg", "-y", "-i", args.insv, "-vf",
                        f"fps={args.fps}", os.path.join(tmp, "f_%05d.png")],
                       capture_output=True)
        frames = sorted(glob.glob(os.path.join(tmp, "f_*.png")))
    else:
        frames = sorted(glob.glob(os.path.join(args.frames_dir, "*.png")))

    sx, sy = (int(v) for v in args.squares.lower().split("x"))
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, args.dict))
    board = cv2.aruco.CharucoBoard((sx, sy), 1.0, 0.75, dictionary)
    board.setLegacyPattern(bool(args.legacy))
    detector = cv2.aruco.CharucoDetector(board)
    os.makedirs(args.out_dir, exist_ok=True)

    saved = 0
    for fp in frames:
        color = cv2.imread(fp)
        if args.crop == "left":
            color = color[:, :color.shape[1] // 2]
        elif args.crop == "right":
            color = color[:, color.shape[1] // 2:]
        gray = cv2.cvtColor(color, cv2.COLOR_BGR2GRAY)
        cc, ci, mc, mi = detector.detectBoard(gray)
        nmark = 0 if mi is None else len(mi)
        ncorn = 0 if ci is None else len(ci)
        if ncorn < args.min_corners:
            continue
        vis = color.copy()
        if mi is not None and len(mi):
            cv2.aruco.drawDetectedMarkers(vis, mc, mi, (0, 180, 255))
        if ci is not None and len(ci):
            cv2.aruco.drawDetectedCornersCharuco(vis, cc, ci, (0, 0, 255))
        lens = {"left": "FRONT(left)", "right": "BACK(right)", "none": "FULL"}[args.crop]
        cv2.putText(vis, f"{lens}  {os.path.basename(fp)}  markers={nmark} corners={ncorn}",
                    (20, 40), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 255, 0), 2)
        op = os.path.join(args.out_dir, f"det_{args.crop}_{os.path.basename(fp)}")
        cv2.imwrite(op, vis)
        saved += 1
        print(f"{os.path.basename(fp)}: markers={nmark} corners={ncorn}", file=sys.stderr)
    print(f"saved {saved} annotated frames to {args.out_dir}")


if __name__ == "__main__":
    main()
