#!/usr/bin/env python3
"""Convert an Insta360 embedded lens calibration into a RegattaView viewer preset.

The Insta360 trailer stores a per-lens calibration string (exiftool surfaces it
as ``Parameters``; race_upload.py also dumps it into the ``.telemetry`` sidecar as
``offset``). For an X3 dual-fisheye it looks like::

    2  cx0 cy0 R0 d0a d0b fov0   cx1 cy1 R1 d1a d1b fov1   STACK ACROSS X

where each lens block is (center-across, center-stack, radius, two distortion-ish
terms, half-FOV in degrees), all in the camera's native dual-fisheye frame
(ACROSS wide x STACK tall, two circles stacked along the STACK axis), and the
trailing pair is (STACK, ACROSS) in native pixels.

The served video is an hstack of the two lenses at dimension {x:long, y:short}, so
the native STACK axis maps to video-x and the native ACROSS axis maps to video-y,
with a single uniform scale. The viewer's fisheye shader samples the texture as::

    UV = center + (scaleX * aperture * theta * cos, scaleY * aperture * theta * sin)

so a rim ray (theta = half-FOV) lands at UV-radius = scaleY*aperture*halfFOV, which
must equal R / ACROSS (the circle radius as a fraction of the short video axis).
We fix scaleY_front = 1.0, solve aperture from the front lens, and let the back
lens's slightly different radius live in scaleY_back. scaleX = +/-0.5*scaleY is the
2:1 aspect correction (back negative because the rear fisheye is mirrored).

Only geometry (centers + radius) is mapped -- the parts the default geometric guess
and the photometric seam fit never measured. euler/distortion are left at the
viewer default so an A/B isolates the geometry change.
"""
import argparse
import json
import math
import subprocess
import sys


def parse_offset_from_insv(path):
    """Run exiftool on an .insv/.insp and return the offset float list."""
    out = subprocess.check_output(
        ["exiftool", "-ee", "-api", "LargeFileSupport=1", "-s3",
         "-Insta360:Parameters", path],
        text=True,
    ).strip()
    if not out:
        raise SystemExit(f"No Insta360:Parameters found in {path}")
    return [float(x) for x in out.replace("_", " ").split()]


def load_offset(args):
    if args.insv:
        return parse_offset_from_insv(args.insv)
    if args.telemetry:
        with open(args.telemetry) as f:
            tel = json.load(f)
        return [float(x) for x in tel["offset"]]
    if args.offset:
        return [float(x) for x in args.offset.replace("_", " ").split()]
    raise SystemExit("Provide one of --insv, --telemetry, or --offset")


def convert(offset):
    n = int(round(offset[0]))
    if n != 2:
        raise SystemExit(f"Only 2-lens dual-fisheye supported (got {n})")
    # two 6-tuples then trailing [STACK, ACROSS, X]
    l0 = offset[1:7]
    l1 = offset[7:13]
    stack_len, across_len = offset[13], offset[14]

    def lens(block):
        c_across, c_stack, R, _d1, _d2, fov_deg = block
        return dict(c_across=c_across, c_stack=c_stack, R=R, fov_deg=fov_deg)

    f, b = lens(l0), lens(l1)

    # centers, normalized into the served (hstacked) video frame
    def center(L):
        return [L["c_stack"] / stack_len, L["c_across"] / across_len]

    front_center = center(f)
    back_center = center(b)

    # radius -> aperture / per-lens scaleY (rim ray theta == half-FOV)
    rnorm_f = f["R"] / across_len           # circle radius as fraction of short axis
    rnorm_b = b["R"] / across_len
    theta_f = math.radians(f["fov_deg"])
    theta_b = math.radians(b["fov_deg"])
    aperture = rnorm_f / theta_f            # with scaleY_front == 1.0
    scaleY_front = 1.0
    scaleY_back = (rnorm_b / theta_b) / aperture
    aspect = 0.5                            # 2:1 video -> scaleX = 0.5*scaleY

    calib = {
        "fov": 180.0,
        "front": {
            "center": [round(front_center[0], 5), round(front_center[1], 5)],
            "scale": [round(aspect * scaleY_front, 5), round(scaleY_front, 5)],
            "euler": [0.0, 0.0, 0.0],
            "distortion": [0.0, 0.0],
        },
        "back": {
            "center": [round(back_center[0], 5), round(back_center[1], 5)],
            "scale": [round(-aspect * scaleY_back, 5), round(scaleY_back, 5)],
            "euler": [0.0, 0.0, 0.0],
            "distortion": [0.0, 0.0],
        },
        "projection": {"aperture": round(aperture, 5), "viewCorrection": -0.2},
        "seam": {"centerYaw": 0.0, "width": 0.1, "feather": 1.0},
    }
    diag = {
        "native_frame": {"stack": stack_len, "across": across_len},
        "front_R_px": f["R"], "back_R_px": b["R"],
        "front_halfFOV_deg": f["fov_deg"], "back_halfFOV_deg": b["fov_deg"],
        "default_aperture": 0.296, "derived_aperture": round(aperture, 5),
    }
    return calib, diag


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--insv", help="path to .insv/.insp (runs exiftool)")
    src.add_argument("--telemetry", help="path to race_upload .telemetry sidecar")
    src.add_argument("--offset", help='raw offset string, e.g. "2 2903.95 ..."')
    ap.add_argument("-o", "--out", help="output preset JSON path (default: stdout)")
    ap.add_argument("--name", default="Insta360 embedded (geometry)")
    args = ap.parse_args()

    offset = load_offset(args)
    calib, diag = convert(offset)
    preset = {"schemaVersion": 1, "name": args.name,
              "calibration": calib, "sourceDiagnostics": diag}
    text = json.dumps(preset, indent=2)
    if args.out:
        with open(args.out, "w") as fh:
            fh.write(text + "\n")
        print(f"wrote {args.out}", file=sys.stderr)
    else:
        print(text)
    print(json.dumps(diag, indent=2), file=sys.stderr)


if __name__ == "__main__":
    main()
