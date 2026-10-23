#!/usr/bin/env python3
"""Calibrate ONE Insta360 fisheye lens from a ChArUco capture, in the pixel space
the viewer samples, and emit OpenCV-fisheye intrinsics (fx,fy,cx,cy,k1..k4).

Run in the `calibration` conda env (cv2 4.9, CharucoDetector + fisheye).

The viewer renders each lens as a 1920x1920 half of the 3840x1920 hstack
(Libraries/insta360.py: `scale=-2:1920 ... hstack`). The raw per-lens .insv stream
is 2880x2880; we extract frames from it, calibrate at native res, then the
companion `intrinsics_to_preset.py` scales fx/fy/cx/cy by 1920/<srcW> into UV.

Distortion (k1..k4) is resolution-independent (a function of theta), so it carries
straight into the shader's `theta*(1+k1*theta^2+k2*theta^4[+k3*theta^6+k4*theta^8])`.

Board: auto-detects the ArUco dictionary and tries both 7x10 / 10x7 orientations,
so you don't need to know either. Square/marker absolute sizes don't affect
intrinsics (chessboard corners live on an integer grid), so defaults are fine.

Usage:
  conda run --no-capture-output -n calibration python Calibration/scripts/charuco_calibrate.py \
      --insv /path/VID_..._00_xxx.insv --lens front --out /tmp/front_intr.json
  # or from pre-extracted frames:
  conda run ... charuco_calibrate.py --frames-dir /tmp/front_frames --lens front --out ...
"""
import argparse, glob, json, os, subprocess, sys, tempfile
import numpy as np
import cv2

DICTS = [  # tried in order; the one decoding the most markers wins
    "DICT_4X4_50", "DICT_4X4_100", "DICT_4X4_250", "DICT_4X4_1000",
    "DICT_5X5_50", "DICT_5X5_100", "DICT_5X5_250", "DICT_5X5_1000",
    "DICT_6X6_50", "DICT_6X6_100", "DICT_6X6_250", "DICT_6X6_1000",
    "DICT_7X7_50", "DICT_ARUCO_ORIGINAL",
]


def log(*a):
    print(*a, file=sys.stderr, flush=True)


_CROP = None  # (x0, x1) column slice applied to every frame; None = full frame


def load_gray(fp):
    img = cv2.imread(fp, cv2.IMREAD_GRAYSCALE)
    if img is None:
        return None
    if _CROP is not None:
        img = img[:, _CROP[0]:_CROP[1]]
    return img


def extract_frames(insv, out_dir, fps):
    os.makedirs(out_dir, exist_ok=True)
    pat = os.path.join(out_dir, "f_%05d.png")
    cmd = ["/opt/homebrew/bin/ffmpeg", "-y", "-i", insv, "-vf", f"fps={fps}", pat]
    log(f"[extract] {insv} -> {out_dir} @ {fps}fps")
    r = subprocess.run(cmd, capture_output=True, text=True)
    frames = sorted(glob.glob(os.path.join(out_dir, "f_*.png")))
    if not frames:
        log(r.stderr[-2000:])
        raise SystemExit("ffmpeg produced no frames")
    log(f"[extract] {len(frames)} frames")
    return frames


def pick_dictionary(frames, sample=12):
    """Detect which predefined dictionary the board uses by counting markers."""
    best = (None, -1)
    probe = frames[:: max(1, len(frames) // sample)][:sample]
    for dname in DICTS:
        d = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dname))
        det = cv2.aruco.ArucoDetector(d, cv2.aruco.DetectorParameters())
        total = 0
        for fp in probe:
            img = load_gray(fp)
            if img is None:
                continue
            corners, ids, _ = det.detectMarkers(img)
            total += 0 if ids is None else len(ids)
        if total > best[1]:
            best = (dname, total)
    log(f"[dict] best={best[0]} markers={best[1]}")
    if best[1] <= 0:
        raise SystemExit("No ArUco markers detected with any predefined dictionary.")
    return best[0]


def collect(frames, dictionary, squaresX, squaresY, square_len=1.0, marker_len=0.75,
            min_corners=8):
    board = cv2.aruco.CharucoBoard((squaresX, squaresY), square_len, marker_len, dictionary)
    board.setLegacyPattern(True)  # most printed boards predate the 4.6 layout flip
    detector = cv2.aruco.CharucoDetector(board)
    obj_pts, img_pts, used, sizes = [], [], [], set()
    for fp in frames:
        img = load_gray(fp)
        if img is None:
            continue
        sizes.add(img.shape[::-1])
        cc, ci, mc, mi = detector.detectBoard(img)
        if ci is None or len(ci) < min_corners:
            continue
        op, ip = board.matchImagePoints(cc, ci)
        if op is None or len(op) < min_corners:
            continue
        obj_pts.append(op.reshape(-1, 1, 3).astype(np.float64))
        img_pts.append(ip.reshape(-1, 1, 2).astype(np.float64))
        used.append((fp, len(op), cc.reshape(-1, 2)))
    if len(sizes) != 1:
        raise SystemExit(f"mixed frame sizes {sizes}")
    return obj_pts, img_pts, used, sizes.pop(), board


def coverage(used, size, grid=6):
    """Fraction of a grid-by-grid map touched by detected corners (rim coverage)."""
    W, H = size
    hit = np.zeros((grid, grid), bool)
    for _, _, cc in used:
        for x, y in cc:
            gx = min(grid - 1, int(x / W * grid)); gy = min(grid - 1, int(y / H * grid))
            hit[gy, gx] = True
    return hit


def fisheye_calibrate(obj_pts, img_pts, size, fix_k34):
    flags = (cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC | cv2.fisheye.CALIB_FIX_SKEW
             | cv2.fisheye.CALIB_CHECK_COND)
    if fix_k34:
        flags |= cv2.fisheye.CALIB_FIX_K3 | cv2.fisheye.CALIB_FIX_K4
    K = np.eye(3); D = np.zeros((4, 1))
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 60, 1e-6)
    op, ip = list(obj_pts), list(img_pts)
    dropped = 0
    while len(op) >= 6:
        try:
            rms, K, D, _, _ = cv2.fisheye.calibrate(op, ip, size, K, D, flags=flags, criteria=crit)
            return rms, K, D, len(op), dropped
        except cv2.error as e:
            msg = str(e)
            # CALIB_CHECK_COND reports the offending view index: "input array %d"
            import re
            m = re.search(r"input array (\d+)", msg)
            if m:
                idx = int(m.group(1))
                if 0 <= idx < len(op):
                    op.pop(idx); ip.pop(idx); dropped += 1
                    K = np.eye(3); D = np.zeros((4, 1))
                    continue
            raise
    raise SystemExit(f"calibration failed; too many ill-conditioned views (dropped {dropped})")


def bootstrap_stability(op, ip, size, fix_k34, n=20, frac=0.7, seed=0):
    """Refit on random subsets; report spread of fx,k1,k2 -> is the fit data-constrained?"""
    rng = np.random.default_rng(seed)
    m = len(op)
    k = max(8, int(frac * m))
    rows = []
    for _ in range(n):
        idx = rng.choice(m, size=k, replace=False)
        try:
            rms, K, D, _, _ = fisheye_calibrate([op[i] for i in idx], [ip[i] for i in idx], size, fix_k34)
            rows.append((K[0, 0], float(D[0]), float(D[1])))
        except SystemExit:
            continue
    if len(rows) < 3:
        return None
    a = np.array(rows)
    mean, std = a.mean(0), a.std(0)
    def cv_(i):
        return abs(std[i] / mean[i]) if mean[i] else float("inf")
    return dict(n=len(rows), fx=(mean[0], std[0], cv_(0)),
                k1=(mean[1], std[1], cv_(1)), k2=(mean[2], std[2], cv_(2)))


def main():
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--insv")
    src.add_argument("--frames-dir")
    ap.add_argument("--lens", choices=["front", "back"], required=True)
    ap.add_argument("--crop", choices=["none", "left", "right"], default="none",
                    help="left/right half of a pre-hstacked 3840x1920 dual-fisheye frame")
    ap.add_argument("--squares", default="7x10", help="squaresX x squaresY (auto-tries swap)")
    ap.add_argument("--dict", help="force an ArUco dict name; default = auto-detect")
    ap.add_argument("--fps", type=float, default=2.0, help="frame sampling rate from .insv")
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    global _CROP
    if args.crop != "none":
        # frames are full hstack width; split at the midpoint
        _CROP = (0, None)  # finalized after we know frame width below

    if args.insv:
        tmp = tempfile.mkdtemp(prefix=f"charuco_{args.lens}_")
        frames = extract_frames(args.insv, tmp, args.fps)
    else:
        frames = sorted(glob.glob(os.path.join(args.frames_dir, "*.png"))
                        + glob.glob(os.path.join(args.frames_dir, "*.jpg")))
        if not frames:
            raise SystemExit(f"no frames in {args.frames_dir}")
        log(f"[frames] {len(frames)} from {args.frames_dir}")

    if args.crop != "none":
        probe = cv2.imread(frames[0])
        W = probe.shape[1]
        mid = W // 2
        _CROP = (0, mid) if args.crop == "left" else (mid, W)
        log(f"[crop] {args.crop} half of width {W} -> cols {_CROP}")

    dname = args.dict or pick_dictionary(frames)
    dictionary = cv2.aruco.getPredefinedDictionary(getattr(cv2.aruco, dname))
    sx, sy = (int(v) for v in args.squares.lower().split("x"))

    # try both orientations, keep the one yielding more detected views
    best = None
    for cand in [(sx, sy), (sy, sx)]:
        op, ip, used, size, board = collect(frames, dictionary, cand[0], cand[1])
        log(f"[detect] squares={cand} -> {len(used)} usable views, frame={size}")
        if best is None or len(used) > len(best[2]):
            best = (op, ip, used, size, board, cand)
    op, ip, used, size, board, squares = best
    if len(used) < 8:
        raise SystemExit(f"only {len(used)} usable views — capture more frames covering the rim")

    hit = coverage(used, size)
    log(f"[coverage {hit.shape[0]}x{hit.shape[1]}] {hit.sum()}/{hit.size} cells; rim rows/cols:")
    for row in hit.astype(int):
        log("   " + " ".join("#" if c else "." for c in row))

    results = {}
    for fix in (True, False):
        rms, K, D, nviews, dropped = fisheye_calibrate(op, ip, size, fix_k34=fix)
        key = "k1k2" if fix else "k1k2k3k4"
        results[key] = dict(rms=float(rms), fx=float(K[0, 0]), fy=float(K[1, 1]),
                            cx=float(K[0, 2]), cy=float(K[1, 2]),
                            k1=float(D[0]), k2=float(D[1]), k3=float(D[2]), k4=float(D[3]),
                            nviews=nviews, dropped=dropped)
        log(f"[fit {key}] rms={rms:.3f}px fx={K[0,0]:.1f} fy={K[1,1]:.1f} "
            f"cx={K[0,2]:.1f} cy={K[1,2]:.1f} D={D.ravel().round(5).tolist()} "
            f"({nviews} views, {dropped} dropped)")

    # stability gate: how much do fx,k1,k2 move across random frame subsets?
    boot = bootstrap_stability(op, ip, size, fix_k34=True)
    if boot:
        log(f"[bootstrap k1k2, {boot['n']} resamples @70%]")
        for name in ("fx", "k1", "k2"):
            mean, std, cv = boot[name]
            log(f"   {name:3s} = {mean:+.4f} +/- {std:.4f}  (CV {cv*100:.1f}%)"
                + ("  <-- UNSTABLE" if cv > 0.15 else ""))
        results["bootstrap"] = {k: dict(mean=float(boot[k][0]), std=float(boot[k][1]),
                                        cv=float(boot[k][2])) for k in ("fx", "k1", "k2")}
        results["bootstrap"]["n"] = boot["n"]

    out = dict(lens=args.lens, dictionary=dname, squares=list(squares),
               src_width=size[0], src_height=size[1], n_input_frames=len(frames),
               coverage_cells=int(hit.sum()), coverage_total=int(hit.size),
               fits=results)
    json.dump(out, open(args.out, "w"), indent=2)
    print(f"wrote {args.out}  (rms k1k2={results['k1k2']['rms']:.3f}px, "
          f"k1k2k3k4={results['k1k2k3k4']['rms']:.3f}px)")


if __name__ == "__main__":
    main()
