#!/usr/bin/env python3
"""Transfer the Insta360 factory calibration into the viewer's shader model.

Approach: Insta360 Studio applies the (undecodable) factory lens model when it
stitches raw dual-fisheye into an equirect. Every equirect pixel has a known
world ray, so feature matches between the raw fisheye halves and the stitched
equirect give thousands of (ray <-> fisheye pixel) correspondences across the
full FOV -- including the rim a close-range board can never reach. We then fit
the viewer's exact shader model (per-lens center, focal, k1/k2 distortion,
rotation) to those correspondences.

Stages per lens:
  0. bootstrap: SIFT fisheye-half <-> equirect directly (central region matches
     fine -- pixel scale is ~equal there), Kabsch -> initial rotation.
  1..N: render OUR equirect from the raw frame with the current model, SIFT
     our-render <-> studio render (now well aligned everywhere incl. rim),
     map matches back to fisheye pixels, robust least-squares refit.

Fit params per lens: rotvec(3), cx, cy, f, k1, k2  (shader-exact projection,
back lens carries the same x-mirror the shader uses). Optional rim anchor from
the .telemetry v1 block (image-circle radius R at theta=halfFOV).

Run in the `calibration` env:
  python Calibration/scripts/fit_factory_transfer.py \
      --raw VID.insv --stitched studio_export.mp4 --times 2,8,14 \
      [--telemetry file.telemetry] [-o preset.json] [--debug-dir DIR]
"""
import argparse, json, math, os, subprocess, sys, tempfile
import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

FFMPEG = "/opt/homebrew/bin/ffmpeg"


def log(*a):
    print(*a, file=sys.stderr)


# ----------------------------------------------------------------------------
# frame IO
# ----------------------------------------------------------------------------
def extract_frame(video, t):
    fd, tmp = tempfile.mkstemp(suffix=".png")
    os.close(fd)
    subprocess.run([FFMPEG, "-y", "-ss", f"{t:.3f}", "-i", video,
                    "-frames:v", "1", tmp], capture_output=True, check=True)
    img = cv2.imread(tmp)
    os.unlink(tmp)
    if img is None:
        raise RuntimeError(f"no frame at t={t} in {video}")
    return img


# ----------------------------------------------------------------------------
# geometry
# ----------------------------------------------------------------------------
def equirect_rays(px, W, H):
    """Equirect pixel (x,y) -> unit world ray. Convention is arbitrary (the
    fitted per-lens rotation absorbs it); handedness mismatches show up as an
    improper Kabsch solution and are folded into the lens y-sign."""
    lon = (px[:, 0] + 0.5) / W * 2.0 * math.pi - math.pi
    lat = math.pi / 2.0 - (px[:, 1] + 0.5) / H * math.pi
    cl = np.cos(lat)
    return np.stack([cl * np.sin(lon), np.sin(lat), cl * np.cos(lon)], axis=1)


def project_lens(v, cx, cy, f, k1, k2, back, sy):
    """Shader-exact projection of lens-frame rays v (Nx3) -> raw-frame pixels.
    front: theta from -z, +x scale.  back: theta from +z, mirrored x (like the
    shader's negative scale.x). sy in {+1,-1} folds the texture y handedness."""
    rxy = np.hypot(v[:, 0], v[:, 1])
    zax = v[:, 2] if back else -v[:, 2]
    theta = np.arctan2(rxy, zax)
    t2 = theta * theta
    thetad = theta * (1.0 + k1 * t2 + k2 * t2 * t2)
    phi = np.arctan2(v[:, 1], v[:, 0])
    sx = -1.0 if back else 1.0
    return np.stack([cx + sx * f * thetad * np.cos(phi),
                     cy + sy * f * thetad * np.sin(phi)], axis=1), theta


def unproject_lens(px, cx, cy, f, k1, k2, back, sy):
    """Inverse of project_lens (Newton on thetad->theta). Pixels -> unit rays."""
    dx = px[:, 0] - cx
    dy = (px[:, 1] - cy) * sy
    if back:
        dx = -dx
    r = np.hypot(dx, dy)
    thetad = r / f
    theta = thetad.copy()
    for _ in range(8):
        t2 = theta * theta
        fv = theta * (1.0 + k1 * t2 + k2 * t2 * t2) - thetad
        dfv = 1.0 + 3.0 * k1 * t2 + 5.0 * k2 * t2 * t2
        theta = theta - fv / np.maximum(dfv, 1e-6)
    phi = np.arctan2(dy, dx)
    st = np.sin(theta)
    z = -np.cos(theta) if not back else np.cos(theta)
    return np.stack([st * np.cos(phi), st * np.sin(phi), z], axis=1)


def kabsch(v, w):
    """Best orthogonal O with v ~= O @ w. Returns (R proper, sy) -- an improper
    solution is folded into a lens-frame y flip (diag(1,-1,1) on v)."""
    H = (v.T @ w)
    U, _, Vt = np.linalg.svd(H)
    O = U @ Vt
    if np.linalg.det(O) < 0:
        # improper: flip lens y handedness and re-solve properly
        F = np.diag([1.0, -1.0, 1.0])
        Hf = ((v @ F).T @ w)
        U, _, Vt = np.linalg.svd(Hf)
        O = U @ Vt
        O = U @ np.diag([1.0, 1.0, np.sign(np.linalg.det(O))]) @ Vt
        return O, -1.0
    return O, 1.0


# ----------------------------------------------------------------------------
# rendering our equirect (numpy replica of the shader texture lookup)
# ----------------------------------------------------------------------------
def render_equirect(raw, params, back, sy, W, H, theta_max=1.66):
    """Render this lens's hemisphere of an equirect from the raw frame using the
    current model. Returns (render, mapx, mapy, valid_mask)."""
    xs, ys = np.meshgrid(np.arange(W, dtype=np.float64),
                         np.arange(H, dtype=np.float64))
    px = np.stack([xs.ravel(), ys.ravel()], axis=1)
    w = equirect_rays(px, W, H)
    R = params["R"]
    v = w @ R.T
    uv, theta = project_lens(v, params["cx"], params["cy"], params["f"],
                             params["k1"], params["k2"], back, sy)
    mapx = uv[:, 0].reshape(H, W).astype(np.float32)
    mapy = uv[:, 1].reshape(H, W).astype(np.float32)
    valid = (theta < theta_max).reshape(H, W)
    mapx[~valid] = -1
    mapy[~valid] = -1
    out = cv2.remap(raw, mapx, mapy, cv2.INTER_LINEAR,
                    borderMode=cv2.BORDER_CONSTANT)
    return out, mapx, mapy, valid


# ----------------------------------------------------------------------------
# matching
# ----------------------------------------------------------------------------
def sift_match(img_a, img_b, mask_a=None, mask_b=None, ratio=0.78):
    sift = cv2.SIFT_create()
    ka, da = sift.detectAndCompute(img_a, mask_a)
    kb, db = sift.detectAndCompute(img_b, mask_b)
    if da is None or db is None or len(ka) < 8 or len(kb) < 8:
        return np.zeros((0, 2)), np.zeros((0, 2))
    bf = cv2.BFMatcher(cv2.NORM_L2)
    raw = bf.knnMatch(da, db, k=2)
    pa, pb = [], []
    for m in raw:
        if len(m) == 2 and m[0].distance < ratio * m[1].distance:
            pa.append(ka[m[0].queryIdx].pt)
            pb.append(kb[m[0].trainIdx].pt)
    return np.asarray(pa, dtype=np.float64), np.asarray(pb, dtype=np.float64)


def bilinear(arr, pts):
    """Sample float32 map arr (H,W) at subpixel pts (N,2 = x,y)."""
    H, W = arr.shape
    x = np.clip(pts[:, 0], 0, W - 1.001)
    y = np.clip(pts[:, 1], 0, H - 1.001)
    x0 = x.astype(int); y0 = y.astype(int)
    fx = x - x0; fy = y - y0
    a = arr[y0, x0] * (1 - fx) * (1 - fy) + arr[y0, x0 + 1] * fx * (1 - fy) \
        + arr[y0 + 1, x0] * (1 - fx) * fy + arr[y0 + 1, x0 + 1] * fx * fy
    return a


# ----------------------------------------------------------------------------
# fitting
# ----------------------------------------------------------------------------
def fit_lens(obs_rays, obs_px, p0, back, sy, anchor=None):
    """Robust LM fit of (rotvec, cx, cy, f, k1, k2) to ray<->pixel matches.
    anchor: optional (theta_max_rad, R_px, weight) rim constraint."""
    def pack(p):
        return np.concatenate([Rotation.from_matrix(p["R"]).as_rotvec(),
                               [p["cx"], p["cy"], p["f"], p["k1"], p["k2"]]])

    def unpack(x):
        return dict(R=Rotation.from_rotvec(x[:3]).as_matrix(),
                    cx=x[3], cy=x[4], f=x[5], k1=x[6], k2=x[7])

    def resid(x):
        p = unpack(x)
        v = obs_rays @ p["R"].T
        uv, _ = project_lens(v, p["cx"], p["cy"], p["f"], p["k1"], p["k2"],
                             back, sy)
        r = (uv - obs_px).ravel()
        if anchor is not None:
            tm, Rpx, wgt = anchor
            t2 = tm * tm
            rim = p["f"] * tm * (1 + p["k1"] * t2 + p["k2"] * t2 * t2)
            r = np.concatenate([r, [wgt * (rim - Rpx)]])
        return r

    sol = least_squares(resid, pack(p0), loss="huber", f_scale=2.0,
                        x_scale="jac", max_nfev=200)
    p = unpack(sol.x)
    v = obs_rays @ p["R"].T
    uv, theta = project_lens(v, p["cx"], p["cy"], p["f"], p["k1"], p["k2"],
                             back, sy)
    err = np.linalg.norm(uv - obs_px, axis=1)
    return p, err, theta


def report_theta_bins(theta, err, label):
    deg = np.degrees(theta)
    log(f"  {label}: {len(err)} obs, rms={np.sqrt(np.mean(err**2)):.2f}px, "
        f"median={np.median(err):.2f}px, theta range "
        f"{deg.min():.1f}..{deg.max():.1f} deg")
    for lo in range(0, 95, 15):
        m = (deg >= lo) & (deg < lo + 15)
        if m.sum():
            log(f"    theta {lo:2d}-{lo+15:2d} deg: n={m.sum():5d}  "
                f"rms={np.sqrt(np.mean(err[m]**2)):6.2f}px  "
                f"median={np.median(err[m]):6.2f}px")


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--raw", required=True, help="raw dual-fisheye .insv/.mp4")
    ap.add_argument("--stitched", required=True, help="Studio equirect export")
    ap.add_argument("--times", default="2,8,14",
                    help="comma-separated sample times (s)")
    ap.add_argument("--telemetry", help=".telemetry JSON for the v1 rim anchor")
    ap.add_argument("--anchor-weight", type=float, default=0.0,
                    help="rim-anchor weight in px-equivalents (0 disables). "
                    "Off by default: the telemetry R is in photo-sensor pixels "
                    "and video modes are NOT a pure rescale of the sensor "
                    "(measured disc ~900px vs naive-scaled 964px), so the "
                    "anchor is wrong for video frames.")
    ap.add_argument("--seam-cut-deg", type=float, default=88.0,
                    help="drop matches beyond this theta (Studio blend band)")
    ap.add_argument("--rounds", type=int, default=2,
                    help="render+rematch refinement rounds after bootstrap")
    ap.add_argument("--debug-dir", help="write renders/diffs here")
    ap.add_argument("-o", "--out", help="write candidate preset JSON here")
    args = ap.parse_args()

    times = [float(t) for t in args.times.split(",")]
    raws = [extract_frame(args.raw, t) for t in times]
    sts = [extract_frame(args.stitched, t) for t in times]
    rh, rw = raws[0].shape[:2]
    H, W = sts[0].shape[:2]
    half = rw // 2
    log(f"raw {rw}x{rh}, stitched {W}x{H}, {len(times)} frame pairs")

    # rim anchor from v1 telemetry block, rescaled native -> served pixels
    anchor = None
    if args.telemetry and args.anchor_weight > 0:
        tel = json.load(open(args.telemetry))
        off = [float(x) for x in tel["offset"]]
        ACROSS = off[14]
        scale = (rh / ACROSS)
        anchor_per = {}
        for name, base in (("front", 1), ("back", 7)):
            Rpx = off[base + 2] * scale
            fov = math.radians(off[base + 5])
            anchor_per[name] = (fov, Rpx, args.anchor_weight)
            log(f"anchor {name}: r({math.degrees(fov):.2f}deg) = {Rpx:.1f}px "
                f"(weight {args.anchor_weight})")
    else:
        anchor_per = {"front": None, "back": None}

    if args.debug_dir:
        os.makedirs(args.debug_dir, exist_ok=True)

    fitted = {}
    for name, back in (("front", False), ("back", True)):
        x0 = 0 if not back else half
        log(f"\n=== {name} lens (raw x {x0}..{x0+half}) ===")
        # initial model: equidistant, center mid-half, f from 90deg at circle edge
        p = dict(R=np.eye(3), cx=x0 + half / 2.0, cy=rh / 2.0,
                 f=(half / 2.0) * 0.98 / (math.pi / 2.0), k1=0.0, k2=0.0)
        sy = 1.0

        # ---- bootstrap: direct SIFT fisheye-half <-> equirect, Kabsch -------
        pa_all, pb_all = [], []
        for raw_img, st_img in zip(raws, sts):
            ga = cv2.cvtColor(raw_img[:, x0:x0 + half], cv2.COLOR_BGR2GRAY)
            gb = cv2.cvtColor(st_img, cv2.COLOR_BGR2GRAY)
            pa, pb = sift_match(ga, gb)
            if len(pa):
                pa_all.append(pa + [x0, 0])  # back to full-raw coords
                pb_all.append(pb)
        if not pa_all:
            log(f"!! no bootstrap matches for {name}; aborting this lens")
            continue
        fish_px = np.vstack(pa_all)
        st_px = np.vstack(pb_all)
        log(f"bootstrap: {len(fish_px)} direct matches")
        w = equirect_rays(st_px, W, H)
        v = unproject_lens(fish_px, p["cx"], p["cy"], p["f"], 0, 0, back, sy)
        R0, sy = kabsch(v, w)
        p["R"] = R0
        log(f"bootstrap rotation: rotvec={Rotation.from_matrix(R0).as_rotvec()}"
            f" sy={sy:+.0f}")
        # quick robust fit on bootstrap matches to seed round 1
        p, err, theta = fit_lens(w, fish_px, p, back, sy, anchor_per[name])
        report_theta_bins(theta, err, "bootstrap fit")

        # ---- refinement rounds: render -> rematch -> refit ------------------
        for rnd in range(args.rounds):
            obs_w, obs_px = [], []
            for ti, (raw_img, st_img) in enumerate(zip(raws, sts)):
                ours, mapx, mapy, valid = render_equirect(
                    raw_img, p, back, sy, W, H)
                mask = (valid & (mapx >= x0) & (mapx < x0 + half)).astype(
                    np.uint8) * 255
                ga = cv2.cvtColor(ours, cv2.COLOR_BGR2GRAY)
                gb = cv2.cvtColor(st_img, cv2.COLOR_BGR2GRAY)
                pa, pb = sift_match(ga, gb, mask_a=mask, mask_b=mask)
                if not len(pa):
                    continue
                # our-render px -> fisheye px via the remap fields
                fx = bilinear(mapx, pa)
                fy = bilinear(mapy, pa)
                keep = (fx > 0) & (fy > 0)
                obs_px.append(np.stack([fx[keep], fy[keep]], axis=1))
                obs_w.append(equirect_rays(pb[keep], W, H))
                if args.debug_dir and rnd == args.rounds - 1:
                    cv2.imwrite(os.path.join(
                        args.debug_dir, f"{name}_ours_t{ti}.png"), ours)
            if not obs_w:
                log(f"!! round {rnd}: no matches; keeping previous fit")
                break
            w = np.vstack(obs_w)
            fpx = np.vstack(obs_px)
            # cut the Studio blend band
            v = w @ p["R"].T
            _, theta = project_lens(v, p["cx"], p["cy"], p["f"], p["k1"],
                                    p["k2"], back, sy)
            keep = theta < math.radians(args.seam_cut_deg)
            w, fpx = w[keep], fpx[keep]
            p, err, theta = fit_lens(w, fpx, p, back, sy, anchor_per[name])
            # trim gross outliers and refit once
            keep = err < 5.0
            if keep.sum() > 50 and keep.sum() < len(err):
                p, err, theta = fit_lens(w[keep], fpx[keep], p, back, sy,
                                         anchor_per[name])
            report_theta_bins(theta, err, f"round {rnd}")
        log(f"{name}: cx={p['cx']:.1f} cy={p['cy']:.1f} f={p['f']:.2f} "
            f"k1={p['k1']:.5f} k2={p['k2']:.5f} sy={sy:+.0f}")
        fitted[name] = dict(p=p, sy=sy)

    if len(fitted) < 2:
        log("\n!! both lenses required for a preset; stopping")
        sys.exit(1)

    # ---- final visual check: composite render vs studio ---------------------
    if args.debug_dir:
        for ti, (raw_img, st_img) in enumerate(zip(raws, sts)):
            comp = np.zeros_like(st_img)
            for name, back in (("front", False), ("back", True)):
                fp = fitted[name]
                ours, mapx, mapy, valid = render_equirect(
                    raw_img, fp["p"], back, fp["sy"], W, H,
                    theta_max=math.pi / 2)
                m = valid & (comp.sum(axis=2) == 0)
                comp[m] = ours[m]
            cv2.imwrite(os.path.join(args.debug_dir, f"composite_t{ti}.png"),
                        comp)
            diff = cv2.absdiff(comp, st_img)
            cv2.imwrite(os.path.join(args.debug_dir, f"diff_t{ti}.png"), diff)
        log(f"\nwrote composite/diff renders to {args.debug_dir}")

    # ---- map to viewer preset ------------------------------------------------
    pf, pb_ = fitted["front"]["p"], fitted["back"]["p"]
    syf, syb = fitted["front"]["sy"], fitted["back"]["sy"]
    aperture = pf["f"] / rh                       # scaleY_front := syf
    back_ratio = (pb_["f"] / rh) / aperture
    E = pb_["R"] @ pf["R"].T                      # back euler with front := 0
    # GLSL mat3 constructors are column-major, so the shader's rotateEuler
    # actually applies Rz(-ez)*Ry(-ey)*Rx(-ex). We need that to equal E,
    # i.e. E^T = Rx(ex)*Ry(ey)*Rz(ez)  -> intrinsic 'XYZ' decomposition of E^T.
    # (Verified empirically: seam dy = +0.02deg with E, +1.38deg with E^T.)
    ex, ey, ez = Rotation.from_matrix(E.T).as_euler("XYZ")
    preset = {
        "schemaVersion": 1,
        "name": "Insta360 factory transfer (Studio-stitch fit)",
        "calibration": {
            "fov": 180.0,
            "front": {
                "center": [round(pf["cx"] / rw, 5), round(pf["cy"] / rh, 5)],
                "scale": [round(0.5, 5), round(float(syf), 5)],
                "euler": [0.0, 0.0, 0.0],
                "distortion": [round(pf["k1"], 6), round(pf["k2"], 6)],
            },
            "back": {
                "center": [round(pb_["cx"] / rw, 5), round(pb_["cy"] / rh, 5)],
                "scale": [round(-0.5 * back_ratio, 5),
                          round(float(syb) * back_ratio, 5)],
                "euler": [round(ex, 5), round(ey, 5), round(ez, 5)],
                "distortion": [round(pb_["k1"], 6), round(pb_["k2"], 6)],
            },
            "projection": {"aperture": round(aperture, 5),
                           "viewCorrection": -0.2},
            "seam": {"centerYaw": 0.0, "width": 0.1, "feather": 1.0},
        },
    }
    log(f"\nback-relative-to-front euler (deg): "
        f"x={math.degrees(ex):.3f} y={math.degrees(ey):.3f} "
        f"z={math.degrees(ez):.3f}")
    out = json.dumps(preset, indent=2)
    if args.out:
        open(args.out, "w").write(out + "\n")
        print(f"wrote {args.out}")
    else:
        print(out)


if __name__ == "__main__":
    main()
