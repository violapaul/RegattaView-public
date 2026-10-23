#!/usr/bin/env python3
"""Distant-seam registration: refine the lens-tail distortion so front/back
agree at the stitch line for far-field content.

Why: the Studio-stitch transfer fit (fit_factory_transfer.py) constrains
theta < ~85deg well, but Studio's own seam blending / dynamic stitching warps
content near the seam, so the 85-91deg tail is extrapolation -- measured ~1.3deg
of radial mismatch at the horizon. Here we use the physics of distance instead:
a far-away feature matched between the two lenses' renders must unproject to
the SAME world direction. Each match gives an exact constraint on the tails.

Fit: shared (dk1, dk2) on top of the transferred k1/k2 (same optics both
lenses), optional per-lens mode, optional euler refinement, with a
regularization grid pinning r(theta) for theta < reg-max so the well-measured
interior doesn't move.

Run in the `calibration` env:
  python Calibration/scripts/refine_seam_distance.py \
      --video pipeline.mp4 --times 120,300,... --preset current.json -o out.json
"""
import argparse, json, math, sys
import cv2
import numpy as np
from scipy.optimize import least_squares
from scipy.spatial.transform import Rotation

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from fit_factory_transfer import (extract_frame, render_equirect, sift_match,
                                  bilinear, unproject_lens, equirect_rays)


def log(*a):
    print(*a, file=sys.stderr)


def preset_to_params(preset, rw, rh):
    """Viewer preset -> per-lens pixel-space params + relative rotation E.
    Inverse of fit_factory_transfer's emission (incl. the GLSL column-major
    euler transpose)."""
    c = preset["calibration"]
    A = c["projection"]["aperture"]
    out = {}
    for name in ("front", "back"):
        L = c[name]
        f = abs(L["scale"][1]) * A * rh
        out[name] = dict(cx=L["center"][0] * rw, cy=L["center"][1] * rh,
                         f=f, k1=L["distortion"][0], k2=L["distortion"][1])
    ex, ey, ez = c["back"]["euler"]
    E = Rotation.from_euler("XYZ", [ex, ey, ez]).as_matrix().T
    return out, E


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", required=True, help="pipeline dual-fisheye mp4")
    ap.add_argument("--times", default="120,240,360,480,600,720,840,960,1080")
    ap.add_argument("--preset", required=True, help="current preset JSON")
    ap.add_argument("--band-deg", type=float, default=5.0)
    ap.add_argument("--horizon-deg", type=float, default=8.0,
                    help="keep matches within this elevation of the horizon "
                    "(far-field content; rejects near-field parallax)")
    ap.add_argument("--per-lens", action="store_true",
                    help="independent dk per lens instead of shared")
    ap.add_argument("--fit-euler", action="store_true",
                    help="also refine the back lens relative rotation")
    ap.add_argument("--reg-max", type=float, default=80.0,
                    help="regularization: pin r(theta) below this many deg")
    ap.add_argument("--reg-weight", type=float, default=3.0,
                    help="px-equivalent weight per regularization sample")
    ap.add_argument("-o", "--out", help="write refined preset here")
    args = ap.parse_args()

    preset = json.load(open(args.preset))
    times = [float(t) for t in args.times.split(",")]
    frame0 = extract_frame(args.video, times[0])
    rh, rw = frame0.shape[:2]
    P, E = preset_to_params(preset, rw, rh)
    log(f"video {rw}x{rh}; front f={P['front']['f']:.2f} "
        f"k=({P['front']['k1']:.5f},{P['front']['k2']:.5f}); "
        f"back f={P['back']['f']:.2f} "
        f"k=({P['back']['k1']:.5f},{P['back']['k2']:.5f})")

    W, H = 2400, 1200
    xs = np.arange(W)
    lon = (xs + 0.5) / W * 360 - 180
    band = (np.abs(np.abs(lon) - 90) < args.band_deg)
    bm = np.tile(band, (H, 1)).astype(np.uint8) * 255

    pf = dict(R=np.eye(3), **P["front"])
    pb = dict(R=E, **P["back"])
    fpx, bpx = [], []
    for t in times:
        raw = frame0 if t == times[0] else extract_frame(args.video, t)
        outf, mfx, mfy, _ = render_equirect(raw, pf, False, 1.0, W, H,
                                            theta_max=math.radians(97))
        outb, mbx, mby, _ = render_equirect(raw, pb, True, 1.0, W, H,
                                            theta_max=math.radians(97))
        gf = cv2.cvtColor(outf, cv2.COLOR_BGR2GRAY)
        gb = cv2.cvtColor(outb, cv2.COLOR_BGR2GRAY)
        pa, pq = sift_match(gf, gb, mask_a=bm, mask_b=bm)
        if not len(pa):
            continue
        elev = 90 - (pa[:, 1] + 0.5) / H * 180
        keep = np.abs(elev) < args.horizon_deg
        pa, pq = pa[keep], pq[keep]
        if not len(pa):
            continue
        # render px -> source fisheye px via the remap fields
        fpx.append(np.stack([bilinear(mfx, pa), bilinear(mfy, pa)], axis=1))
        bpx.append(np.stack([bilinear(mbx, pq), bilinear(mby, pq)], axis=1))
        log(f"t={t:6.0f}s: {len(pa)} far-field matches")
    fpx = np.vstack(fpx)
    bpx = np.vstack(bpx)
    log(f"total {len(fpx)} matches")

    # regularization grid: r(theta) must not move in the interior
    treg = np.radians(np.linspace(5, args.reg_max, 16))

    rot0 = Rotation.from_matrix(E).as_rotvec()

    def unpack(x):
        if args.per_lens:
            dkf, dkb = x[0:2], x[2:4]
            rv = x[4:7] if args.fit_euler else rot0
        else:
            dkf = dkb = x[0:2]
            rv = x[2:5] if args.fit_euler else rot0
        return dkf, dkb, Rotation.from_rotvec(rv).as_matrix()

    def resid(x):
        dkf, dkb, Em = unpack(x)
        vf = unproject_lens(fpx, P["front"]["cx"], P["front"]["cy"],
                            P["front"]["f"], P["front"]["k1"] + dkf[0],
                            P["front"]["k2"] + dkf[1], False, 1.0)
        vb = unproject_lens(bpx, P["back"]["cx"], P["back"]["cy"],
                            P["back"]["f"], P["back"]["k1"] + dkb[0],
                            P["back"]["k2"] + dkb[1], True, 1.0)
        wb = vb @ Em  # back lens ray -> world (E^T applied)
        # angular disagreement, scaled to rim pixels for unit consistency
        r = np.cross(vf, wb) * P["front"]["f"]
        reg = []
        for name, dk in (("front", dkf), ("back", dkb)):
            fl = P[name]["f"]
            reg.append(args.reg_weight * fl * treg *
                       (dk[0] * treg**2 + dk[1] * treg**4))
        return np.concatenate([r.ravel(), *reg])

    n = (4 if args.per_lens else 2) + (3 if args.fit_euler else 0)
    x0 = np.zeros(n)
    if args.fit_euler:
        x0[-3:] = rot0
    before = resid(x0)
    nobs = len(fpx) * 3
    log(f"before: seam rms={np.sqrt(np.mean(before[:nobs]**2)):.2f}px-equiv")
    sol = least_squares(resid, x0, loss="huber", f_scale=2.0, x_scale="jac",
                        max_nfev=200)
    after = resid(sol.x)
    log(f"after:  seam rms={np.sqrt(np.mean(after[:nobs]**2)):.2f}px-equiv")
    dkf, dkb, Em = unpack(sol.x)
    log(f"dk front: ({dkf[0]:+.6f}, {dkf[1]:+.6f})  "
        f"dk back: ({dkb[0]:+.6f}, {dkb[1]:+.6f})")
    for name, dk in (("front", dkf), ("back", dkb)):
        fl = P[name]["f"]
        for deg in (45, 60, 75, 85, 90):
            th = math.radians(deg)
            dr = fl * th * (dk[0] * th**2 + dk[1] * th**4)
            log(f"  {name} delta-r({deg}deg) = {dr:+.2f}px")
    if args.fit_euler:
        log(f"euler refinement (deg): "
            f"{np.degrees(Rotation.from_matrix(Em).as_rotvec() - rot0)}")

    # ---- emit refined preset --------------------------------------------------
    out = json.loads(json.dumps(preset))  # deep copy
    c = out["calibration"]
    c["front"]["distortion"] = [round(P["front"]["k1"] + dkf[0], 6),
                                round(P["front"]["k2"] + dkf[1], 6)]
    c["back"]["distortion"] = [round(P["back"]["k1"] + dkb[0], 6),
                               round(P["back"]["k2"] + dkb[1], 6)]
    if args.fit_euler:
        ex, ey, ez = Rotation.from_matrix(Em.T).as_euler("XYZ")
        c["back"]["euler"] = [round(ex, 5), round(ey, 5), round(ez, 5)]
    out["name"] = preset.get("name", "") + " + distant-seam refinement"
    s = json.dumps(out, indent=2)
    if args.out:
        open(args.out, "w").write(s + "\n")
        print(f"wrote {args.out}")
    else:
        print(s)


if __name__ == "__main__":
    main()
