#!/usr/bin/env python
"""Diagnose production gradient-descent TWD gybe shear (option 1).

Production `race_logs.estimate_true_wind_helper` predicts apparent wind as
    f_aw_n = twn + btn
    f_aw_e = twe - bte      # <-- asymmetric sign, flagged "WHY IS THIS NEGATIVE"
We test whether the east-term sign is the cause of the large gybe shear by
re-running a faithful copy of the solver with the sign as a parameter, on
gybe-heavy sessions, measuring tack vs gybe |dTWD| (the same shear metric as
ground_ref_shear_eval).

Run:
    conda run -n sailing --no-capture-output python Analyze/diag_prod_gybe.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import argparse, glob, logging
import numpy as np
from numba import jit

from Libraries.global_variables import G
from Libraries import race_logs
from Libraries.log_helper import LOG
from Analyze.calibrate_ground_ref import detect_maneuvers, _clean_speed_glitches

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()
FS = float(G.SAMPLES_PER_SECOND)
PRE_W, POST_W = (25.0, 10.0), (10.0, 25.0)


@jit(nopython=True)
def _helper(epsilon, aws, awa, hdg, cog, sog, tws_init, twd_init,
            variation, tws_mult, time_delay, e_sign):
    """Faithful copy of estimate_true_wind_helper with the east-term sign
    `e_sign` exposed (production uses e_sign=-1.0). spd unused (ground-ref via
    cog/sog), matching production."""
    ptwd = np.radians(twd_init) + np.zeros(awa.shape)
    ptws = tws_init + np.zeros(awa.shape)
    aw_n = aws * np.cos(np.radians(awa))
    aw_e = aws * np.sin(np.radians(awa))
    rhdg = np.radians(hdg)
    rcog = np.radians(cog)
    variation = np.radians(variation)
    eps = epsilon
    for i in range(1, len(aws) - time_delay):
        twa = ptwd[i - 1] - (rhdg[i] + variation)
        course_angle = rcog[i + time_delay] - (rhdg[i] + variation)
        c = np.cos(twa); s = np.sin(twa)
        twn = c * ptws[i - 1]; twe = s * ptws[i - 1]
        btn = np.cos(course_angle) * sog[i + time_delay]
        bte = np.sin(course_angle) * sog[i + time_delay]
        f_aw_n = twn + btn
        f_aw_e = twe + e_sign * bte
        res_n = aw_n[i] - f_aw_n
        res_e = aw_e[i] - f_aw_e
        delta_tws = res_n * c + res_e * s
        delta_twd = res_n * ptws[i - 1] * -s + res_e * ptws[i - 1] * c
        ptws[i] = np.absolute(eps * tws_mult * delta_tws + ptws[i - 1])
        ptwd[i] = eps * delta_twd + ptwd[i - 1]
        re = (np.abs(res_n) + np.abs(res_e))
        if re > 1.0 and re < 5.0:
            eps = min(1.05 * eps, 10 * epsilon)
        elif re > 5.0:
            eps = min(1.1 * eps, 1000 * epsilon)
        else:
            eps = (eps + epsilon) / 2
    return np.degrees(ptwd)


def run_twd(df, e_sign):
    aws = np.asarray(df.caws); awa = np.asarray(df.cawa)
    var = G.MAGNETIC_VARIATION
    twd = _helper(0.0001, aws, awa, np.asarray(df.rhdg),
                  np.asarray(df.rcog), np.asarray(df.rsog),
                  float(df.aws.iloc[0]),
                  float(df.rawa.iloc[0] + df.rhdg.iloc[0] + var),
                  var, 16.0, 12, float(e_sign))
    return twd % 360.0


def circmean(a):
    a = np.asarray(a, float); a = a[np.isfinite(a)]
    if a.size == 0:
        return np.nan
    r = np.deg2rad(a)
    return float(np.rad2deg(np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


def wrap180(a):
    return ((np.asarray(a, float) + 180.0) % 360.0) - 180.0


def dtwd(twd, c):
    pre0, pre1 = c - int(PRE_W[0] * FS), c - int(PRE_W[1] * FS)
    po0, po1 = c + int(POST_W[0] * FS), c + int(POST_W[1] * FS)
    if pre0 < 0 or po1 > len(twd):
        return np.nan
    return abs(wrap180(circmean(twd[po0:po1]) - circmean(twd[pre0:pre1])))


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--limit', type=int, default=0)
    args = p.parse_args()
    paths = sorted(glob.glob('/Users/viola/Data/SailingData/PandasLogs/2026-*.parquet'))
    if args.limit:
        paths = paths[:args.limit]
    agg = {sgn: {'tack': [], 'gybe': []} for sgn in (-1.0, 1.0)}
    for path in paths:
        name = os.path.basename(path)
        try:
            df = race_logs.read_log_file(name, skip_dock_only=True,
                discard_columns=False, rename_columns=True, trim=True,
                process=True, path=os.path.dirname(path))
        except Exception:
            continue
        if df is None or 'cawa' not in df.columns:
            continue
        df = df.reset_index(drop=True)
        hdg_true = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0
        man = detect_maneuvers(df['rawa'].to_numpy(float), hdg_true, FS)
        sess = {}
        for sgn in (-1.0, 1.0):
            twd = run_twd(df, sgn)
            for kind in ('tack', 'gybe'):
                ds = [dtwd(twd, c) for c, k in man if k == kind]
                ds = [x for x in ds if np.isfinite(x)]
                sess[(sgn, kind)] = float(np.median(ds)) if ds else np.nan
                if ds:
                    agg[sgn][kind].append(np.median(ds))
        print(f"  {name.replace('.parquet','')}: "
              f"tack[-1]={sess[(-1.0,'tack')]:.1f} gybe[-1]={sess[(-1.0,'gybe')]:.1f}  "
              f"tack[+1]={sess[(1.0,'tack')]:.1f} gybe[+1]={sess[(1.0,'gybe')]:.1f}",
              flush=True)
    print("\n=== production east-sign diagnosis (median across sessions) ===")
    print(f"  {'e_sign':>8s} {'tack |dTWD|':>12s} {'gybe |dTWD|':>12s}")
    for sgn in (-1.0, 1.0):
        t = np.median(agg[sgn]['tack']) if agg[sgn]['tack'] else np.nan
        g = np.median(agg[sgn]['gybe']) if agg[sgn]['gybe'] else np.nan
        tag = '  (production)' if sgn == -1.0 else '  (flipped)'
        print(f"  {sgn:8.0f} {t:12.2f} {g:12.2f}{tag}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
