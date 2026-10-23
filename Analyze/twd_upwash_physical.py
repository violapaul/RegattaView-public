#!/usr/bin/env python
"""Is the upwash correction physically reasonable, and is it constant?

(1) Convert the fitted upwind AWA scale to an ANGLE at the real AWA, to check
    it is a few degrees (physical upwash) rather than "12% of the wind".
(2) Test whether the required upwash is CONSTANT or depends on loading by
    binning tacks by apparent wind speed and measuring the settled odd ΔTWD
    per bin (at the current calibration). A trend => upwash is loading-
    dependent (CL-driven), not a constant.

Run:
    conda run -n sailing --no-capture-output python Analyze/twd_upwash_physical.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import glob, json, logging
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from Libraries.global_variables import G
from Libraries import race_logs
from Libraries.log_helper import LOG
from Analyze.true_wind_estimator import (
    AdaptiveFilterConfig, Calibration, offline_ground_wind_adaptive)
from Analyze.calibrate_ground_ref import detect_maneuvers, _clean_speed_glitches

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()
FS = float(G.SAMPLES_PER_SECOND)
HUBER_ONLY = AdaptiveFilterConfig(alpha_R_turn=0.0, alpha_R_accel=0.0, q_growth=1.0)
HERE = Path(__file__).resolve().parent
GR_CAL = HERE / 'calibration_ground_ref_2026.json'
OUT_DIR = HERE / 'Plots' / 'twd_maneuver'
BASE = (40.0, 15.0); POST = (15.0, 40.0)


def w180(a):
    return ((np.asarray(a, float) + 180.0) % 360.0) - 180.0


def circmean(a):
    a = np.asarray(a, float); a = a[np.isfinite(a)]
    if a.size == 0:
        return np.nan
    r = np.deg2rad(a)
    return float(np.rad2deg(np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(GR_CAL) as f:
        cal = Calibration(**json.load(f)['calibration'])
    s_now = float(cal.awa_scale_upwind)

    recs = []                 # per-tack: dir, net_delta, post_aws, pre_awa_abs
    awa_up_all = []
    for path in sorted(glob.glob('/Users/viola/Data/SailingData/PandasLogs/2026-*.parquet')):
        name = os.path.basename(path)
        try:
            df = race_logs.read_log_file(name, skip_dock_only=True,
                discard_columns=False, rename_columns=True, trim=True,
                process=True, path=os.path.dirname(path))
        except Exception:
            continue
        if df is None or 'rawa' not in df.columns:
            continue
        df = df.reset_index(drop=True)
        sog_clean, _ = _clean_speed_glitches(df['rsog'].to_numpy())
        rawa = df['rawa'].to_numpy(float)
        aws_kt = df['raws'].to_numpy(float) * G.MS_2_KNOTS
        spd_kt = df['rspd'].to_numpy(float) * G.MS_2_KNOTS
        hdg_true = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0
        est = pd.DataFrame({'awa': rawa, 'aws': aws_kt, 'hdg': hdg_true,
                            'cog': df['rcog'].to_numpy(float),
                            'sog': sog_clean * G.MS_2_KNOTS})
        twd = offline_ground_wind_adaptive(est, fs_hz=FS, cal=cal,
                                           adaptive_cfg=HUBER_ONLY)['twd'].to_numpy(float)
        # collect upwind AWA for the angle conversion
        up = (np.abs(rawa) < 50) & (spd_kt > 3)
        awa_up_all.append(np.abs(rawa[up]))
        man = detect_maneuvers(rawa, hdg_true, FS)
        for c, k in man:
            if k != 'tack':
                continue
            b0, b1 = c - int(BASE[0]*FS), c - int(BASE[1]*FS)
            p0, p1 = c + int(POST[0]*FS), c + int(POST[1]*FS)
            if b0 < 0 or p1 > len(twd):
                continue
            base = circmean(twd[b0:b1]); post = circmean(twd[p0:p1])
            pre_awa = np.nanmedian(rawa[b0:b1])
            paws = np.nanmedian(aws_kt[p0:p1])
            if not all(np.isfinite([base, post, pre_awa, paws])):
                continue
            recs.append({'dir': 's2p' if pre_awa > 0 else 'p2s',
                         'delta': w180(post - base),
                         'aws': float(paws), 'awa_abs': abs(float(pre_awa))})
    R = pd.DataFrame(recs)
    awa_med = float(np.median(np.concatenate(awa_up_all)))
    print(f"[phys] median upwind |AWA| = {awa_med:.1f}°  ({len(R)} tacks)", flush=True)

    # (1) angle conversion of the scale.
    def odd_of(df_sub):
        dp = df_sub[df_sub.dir == 'p2s'].delta
        ds = df_sub[df_sub.dir == 's2p'].delta
        if len(dp) < 5 or len(ds) < 5:
            return np.nan, len(dp) + len(ds)
        return 0.5 * (dp.median() - ds.median()), len(dp) + len(ds)

    odd_now, _ = odd_of(R)
    red_from_raw = (1 - s_now) * awa_med           # current reduction, deg
    red_to_null = (1 - 0.88) * awa_med             # reduction at s*=0.88, deg
    print(f"[phys] AWA reduction as an ANGLE at |AWA|={awa_med:.0f}°:", flush=True)
    print(f"   current scale {s_now:.3f} -> {red_from_raw:.1f}° below raw vane", flush=True)
    print(f"   null scale    0.880 -> {red_to_null:.1f}° below raw vane "
          f"(extra {red_to_null-red_from_raw:.1f}° beyond current)", flush=True)
    print(f"   => the 'big 12%' correction is ~{red_to_null:.1f}° of AWA "
          f"(textbook upwash is 2-5°)", flush=True)

    # (2) AWS dependence: settled odd per AWS bin (at current cal).
    edges = [0, 12, 15, 18, 40]
    centers, odds, ns = [], [], []
    print(f"[phys] settled odd ΔTWD vs apparent wind speed (current cal):",
          flush=True)
    for lo, hi in zip(edges[:-1], edges[1:]):
        sub = R[(R.aws >= lo) & (R.aws < hi)]
        o, n = odd_of(sub)
        centers.append(np.median(sub.aws) if len(sub) else (lo+hi)/2)
        odds.append(o); ns.append(n)
        print(f"   AWS {lo:2d}-{hi:2d} kt: odd={o:+.2f}°  (n={n})", flush=True)

    fig, ax = plt.subplots(figsize=(7, 4.4), dpi=120)
    ax.plot(centers, odds, 'o-', color='tab:purple', lw=1.6)
    ax.axhline(0, color='0.5', lw=0.6)
    for x, y, n in zip(centers, odds, ns):
        if np.isfinite(y):
            ax.annotate(f'n={n}', (x, y), textcoords='offset points',
                        xytext=(0, 8), fontsize=8, ha='center')
    ax.set_xlabel('apparent wind speed in post-tack window (kt)')
    ax.set_ylabel('settled odd ΔTWD (deg)')
    ax.set_title('Required upwash is NOT constant — it varies with wind speed\n'
                 '(odd bias at the current calibration, by AWS bin)')
    ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(OUT_DIR / 'twd_upwash_vs_aws.png')
    plt.close(fig)
    print(f"[phys] wrote {OUT_DIR}/twd_upwash_vs_aws.png", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
