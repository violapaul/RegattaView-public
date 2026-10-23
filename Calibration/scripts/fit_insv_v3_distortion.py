#!/usr/bin/env python3
"""Decode the Insta360 .insv `offset_v3` lens model and refit it into the viewer's
OpenCV-theta fisheye model (r = f * theta * (1 + k1*theta^2 + k2*theta^4)).

Why: the shipped viewer preset used only the crude `offset` (centers + a single
half-FOV) in an equidistant `r = aperture*theta` model with zero distortion. That
can match the lens at the center OR the rim but not both -> seam mismatch. The
richer `offset_v3` carries per-lens fx/fy/cx/cy + a distortion polynomial + a
rotation. This script:

  1. parses offset / offset_v2 / offset_v3 from a .telemetry sidecar,
  2. brute-forces which polynomial *convention* in v3 reproduces the trusted
     anchor (image-circle radius R at theta=halfFOV, from the v1 `offset`) while
     staying monotonic on [0, halfFOV],
  3. samples r(theta) from the validated model and least-squares fits the viewer's
     2-param OpenCV model (f, k1, k2) per lens,
  4. emits a candidate default.json.

Run: python3 Calibration/scripts/fit_insv_v3_distortion.py --telemetry <file> [-o out.json]
All diagnostics go to stderr; only the chosen path summary + written file to stdout.
"""
import argparse, json, math, sys
import numpy as np


def log(*a):
    print(*a, file=sys.stderr)


def parse_v1(off):
    # [2, cx0,cy0,R0,d0a,d0b,fov0, cx1,cy1,R1,d1a,d1b,fov1, STACK,ACROSS,X]
    n = int(off[0])
    assert n == 2, f"expected 2 lenses, got {n}"
    f = off[1:1 + 6]
    b = off[7:7 + 6]
    STACK, ACROSS = off[13], off[14]
    return {
        "front": dict(cx=f[0], cy=f[1], R=f[2], d1=f[3], d2=f[4], fov=f[5]),
        "back":  dict(cx=b[0], cy=b[1], R=b[2], d1=b[3], d2=b[4], fov=b[5]),
        "STACK": STACK, "ACROSS": ACROSS,
    }


def parse_v3(off):
    # leading 2.0 lens count, then per lens a 19-float block, trailing total.
    # block: [tag, fx, fy, cx, cy, p1, p2, fov, rx, ry, rz, c0, c1, c2, e0, e1, STACK, ACROSS, K]
    n = int(off[0])
    assert n == 2, f"expected 2 lenses, got {n}"
    rest = off[1:]
    per = (len(rest) - 1) // 2  # minus trailing total
    log(f"[v3] {len(rest)-1} floats over 2 lenses -> {per} per lens (trailing={rest[-1]})")
    blocks = [rest[0:per], rest[per:2 * per]]
    out = {}
    for name, bl in zip(("front", "back"), blocks):
        out[name] = dict(
            tag=bl[0], fx=bl[1], fy=bl[2], cx=bl[3], cy=bl[4],
            p1=bl[5], p2=bl[6], fov=bl[7],
            rot=(bl[8], bl[9], bl[10]),
            poly3=(bl[11], bl[12], bl[13]),
            poly2=(bl[14], bl[15]),
            STACK=bl[16], ACROSS=bl[17], tail=bl[18],
        )
    return out


def candidate_models(L3, R, fov_deg):
    """Yield (label, r_of_theta_callable) candidates built from a v3 lens block.
    theta in radians, returns image radius in native px."""
    fx = L3["fx"]
    th_max = math.radians(fov_deg)
    coeffs_all = [L3["p1"], L3["p2"], *L3["poly3"], *L3["poly2"]]  # 7 numbers seen in block

    def kb(th, f, ks):  # Kannala-Brandt forward: r = f*(th + k1 th^3 + k2 th^5 + ...)
        s = th
        for i, k in enumerate(ks):
            s = s + k * th ** (3 + 2 * i)
        return f * s

    # Hypothesis families. Each must be checked against the anchor + monotonicity.
    yield ("KB f=fx coeffs=poly3", lambda th: kb(th, fx, L3["poly3"]))
    yield ("KB f=fx coeffs=poly3+poly2", lambda th: kb(th, fx, [*L3["poly3"], *L3["poly2"]]))
    yield ("KB f=fx coeffs=p1,p2,poly3", lambda th: kb(th, fx, [L3["p1"], L3["p2"], *L3["poly3"]]))
    # equidistant-with-f solved so rim hits R (a baseline reference, no v3 poly)
    yield ("equidistant f=R/th_max", lambda th, f=R / th_max: f * th)


def monotone(rfun, th_max, nsamp=200):
    ths = np.linspace(0, th_max, nsamp)
    rs = np.array([rfun(t) for t in ths])
    return np.all(np.diff(rs) > 0), rs[-1]


def fit_opencv(rfun, th_max, R, nsamp=400):
    """Fit r = f*theta*(1 + k1 theta^2 + k2 theta^4) to the truth curve rfun.
    f pinned to the central slope (dr/dtheta at 0) via finite diff."""
    ths = np.linspace(1e-3, th_max, nsamp)
    rs = np.array([rfun(t) for t in ths])
    f0 = rfun(1e-3) / 1e-3  # central focal (px per rad)
    # solve k1,k2 by least squares on (rs/(f0*ths) - 1) = k1 th^2 + k2 th^4
    y = rs / (f0 * ths) - 1.0
    A = np.vstack([ths**2, ths**4]).T
    (k1, k2), *_ = np.linalg.lstsq(A, y, rcond=None)
    pred = f0 * ths * (1 + k1 * ths**2 + k2 * ths**4)
    rms = float(np.sqrt(np.mean((pred - rs) ** 2)))
    return f0, float(k1), float(k2), rms


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--telemetry", required=True)
    ap.add_argument("-o", "--out")
    args = ap.parse_args()

    tel = json.load(open(args.telemetry))
    v1 = parse_v1([float(x) for x in tel["offset"]])
    v3 = parse_v3([float(x) for x in tel["offset_v3"]])
    ACROSS, STACK = v1["ACROSS"], v1["STACK"]
    log(f"native frame ACROSS={ACROSS} STACK={STACK}  serial={tel.get('serial_number')}")

    chosen = {}
    for lens in ("front", "back"):
        R = v1[lens]["R"]
        fov_deg = v1[lens]["fov"]
        th_max = math.radians(fov_deg)
        log(f"\n=== {lens}: R={R:.1f}px halfFOV={fov_deg:.3f}deg  v3.fx={v3[lens]['fx']:.1f} "
            f"poly3={v3[lens]['poly3']} poly2={v3[lens]['poly2']} rot={v3[lens]['rot']}")
        best = None
        for label, rfun in candidate_models(v3[lens], R, fov_deg):
            try:
                mono, r_rim = monotone(rfun, th_max)
            except Exception as e:
                log(f"   - {label}: ERR {e}"); continue
            err = abs(r_rim - R) / R
            ok = mono and err < 0.05
            log(f"   - {label}: r(rim)={r_rim:.1f} (target {R:.1f}, err {err*100:.1f}%) "
                f"mono={mono} {'<== ANCHOR OK' if ok else ''}")
            if ok and (best is None or err < best[1]):
                best = (label, err, rfun)
        if best is None:
            log(f"   !! no v3 model reproduced the anchor for {lens}; the v3 poly "
                f"convention is not one we tried. Falling back to equidistant.")
            f0 = R / th_max
            chosen[lens] = dict(model="equidistant-fallback", f=f0, k1=0.0, k2=0.0,
                                fov=fov_deg, R=R, rms=0.0)
            continue
        label, err, rfun = best
        f0, k1, k2, rms = fit_opencv(rfun, th_max, R)
        log(f"   => chose '{label}' (anchor err {err*100:.2f}%); "
            f"OpenCV fit f={f0:.1f} k1={k1:.4f} k2={k2:.4f} rms={rms:.2f}px")
        chosen[lens] = dict(model=label, f=f0, k1=k1, k2=k2, fov=fov_deg, R=R, rms=rms)

    # ---- map to viewer preset --------------------------------------------------
    # Served video is hstack 3840x1920; native STACK->video-x, ACROSS->video-y,
    # uniform scale s = 1920/ACROSS. center = [c_stack/STACK, c_across/ACROSS].
    # shader: r_uv = aperture*scaleY*theta*(1+k1 th^2 + k2 th^4); want r_uv at center
    # slope = f_native/ACROSS (px/rad as fraction of short axis). So aperture*scaleY = f/ACROSS.
    def lens_preset(lens, scaleY_sign_x):
        c = chosen[lens]
        # center in native px: use v1 cx (ACROSS axis) & cy (STACK axis)
        cx_across = v1[lens]["cx"]
        cy_stack = v1[lens]["cy"]
        center = [cy_stack / STACK, cx_across / ACROSS]
        return center, c

    fc, fchosen = lens_preset("front", +1)
    bc, bchosen = lens_preset("back", -1)
    # pick a shared aperture from the front lens with scaleY=1.0; back gets a scale ratio.
    ap_val = fchosen["f"] / ACROSS  # aperture*scaleY_front, scaleY_front:=1
    back_scaleY = (bchosen["f"] / ACROSS) / ap_val

    preset = {
        "schemaVersion": 1,
        "name": "Insta360 v3-derived (OpenCV k1/k2 fit)",
        "calibration": {
            "fov": 180.0,
            "front": {"center": fc, "scale": [0.5, 1.0], "euler": [0.0, 0.0, 0.0],
                       "distortion": [round(fchosen["k1"], 5), round(fchosen["k2"], 5)]},
            "back":  {"center": bc, "scale": [-0.5 * back_scaleY, back_scaleY],
                       "euler": [round(v3["back"]["rot"][0], 5), round(v3["back"]["rot"][1], 5),
                                  round(v3["back"]["rot"][2], 5)],
                       "distortion": [round(bchosen["k1"], 5), round(bchosen["k2"], 5)]},
            "projection": {"aperture": round(ap_val, 5), "viewCorrection": 0.0},
            "seam": {"centerYaw": 0.0, "width": 0.1, "feather": 1.0},
        },
    }
    log(f"\naperture(front,scaleY=1)={ap_val:.5f}  back_scaleY={back_scaleY:.5f}")
    out = json.dumps(preset, indent=2)
    if args.out:
        open(args.out, "w").write(out + "\n")
        print(f"wrote {args.out}")
    else:
        print(out)


if __name__ == "__main__":
    main()
