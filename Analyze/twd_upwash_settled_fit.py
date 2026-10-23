#!/usr/bin/env python
"""Re-fit the upwash Delta-beta(AWS) on the SETTLED window (+60..150 s), using
isolated tacks (no maneuver within +-ISO s) so the window is clean, and compare
the odd-component-vs-time for raw / old-fit / new-fit.

Old fit (twd_upwash_aws_fit, +15..40 s window): Delta-beta = max(0, 6.13 - 0.095*AWS).
This script fits to the settled window where the long-board bias fully develops.

Run:
    conda run -n sailing --no-capture-output python Analyze/twd_upwash_settled_fit.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import glob, json, logging
from dataclasses import replace
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
from Analyze.twd_maneuver_corrections import heel_tilt_correct
from Analyze.twd_maneuver_diag import circmean, w180, BASE

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()
FS = float(G.SAMPLES_PER_SECOND)
HUBER = AdaptiveFilterConfig(alpha_R_turn=0.0, alpha_R_accel=0.0, q_growth=1.0)
HERE = Path(__file__).resolve().parent
OUT_DIR = HERE / 'Plots' / 'twd_maneuver'
ISO_S, WIN_S = 200.0, 180.0
SETTLED = (60.0, 150.0)             # settled post window
BINS = [0, 12, 15, 18, 40]
OLD_A, OLD_B = 6.13, 0.095
H = int(WIN_S * FS)


def corrected_twd(c, cal_corr, a, b):
    rawa = c['rawa']; aws = c['raws_kt'].copy(); awa = rawa.copy()
    up = np.abs(rawa) < 90.0
    a_h, s_h = heel_tilt_correct(rawa[up], c['raws_kt'][up], c['heel'][up])
    dbeta = np.maximum(0.0, a - b * s_h)
    awa[up] = a_h - dbeta * np.sign(a_h); aws[up] = s_h
    est = pd.DataFrame({'awa': awa, 'aws': aws, 'hdg': c['hdg'],
                        'cog': c['cog'], 'sog': c['sog']})
    return offline_ground_wind_adaptive(est, fs_hz=FS, cal=cal_corr,
                                        adaptive_cfg=HUBER)['twd'].to_numpy(float)


def collect(twd, c):
    """Per isolated tack: dir, settled_delta, settled_aws, and the ±H trace."""
    recs = []
    for cc in c['iso_tacks']:
        base = circmean(twd[cc-int(BASE[0]*FS):cc-int(BASE[1]*FS)])
        post = circmean(twd[cc+int(SETTLED[0]*FS):cc+int(SETTLED[1]*FS)])
        pre = np.nanmedian(c['rawa'][cc-int(BASE[0]*FS):cc-int(BASE[1]*FS)])
        saws = np.nanmedian(c['raws_kt'][cc+int(SETTLED[0]*FS):cc+int(SETTLED[1]*FS)])
        if not all(np.isfinite([base, post, pre, saws])):
            continue
        recs.append(dict(dir='s2p' if pre > 0 else 'p2s',
                         delta=float(w180(post-base)), aws=float(saws),
                         trace=w180(twd[cc-H:cc+H+1]-base)))
    return recs


def per_bin_odd(recs):
    out = []
    R = pd.DataFrame([{k: r[k] for k in ('dir', 'delta', 'aws')} for r in recs])
    for lo, hi in zip(BINS[:-1], BINS[1:]):
        s = R[(R.aws >= lo) & (R.aws < hi)]
        dp = s[s.dir == 'p2s'].delta; ds = s[s.dir == 's2p'].delta
        odd = (0.5*(dp.median()-ds.median())
               if len(dp) >= 4 and len(ds) >= 4 else np.nan)
        out.append(dict(aws=float(s.aws.median()) if len(s) else (lo+hi)/2,
                        odd=float(odd), n=len(s)))
    return out


def odd_trace(recs):
    def med(key):
        M = [r['trace'] for r in recs if r['dir'] == key and len(r['trace']) == 2*H+1]
        return np.nanmedian(np.vstack(M), axis=0)
    return 0.5*(med('p2s') - med('s2p'))


def main():
    with open(HERE / 'calibration_ground_ref_2026.json') as f:
        cal = Calibration(**json.load(f)['calibration'])
    with open(HERE / 'leeway_model.json') as f:
        heel_off = float(json.load(f)['calibration']['heel_offset_deg'])
    cal_corr = replace(cal, awa_scale_upwind=1.0, awa_offset_deg=0.0)
    iso_n = int(ISO_S * FS)

    cache = []
    for path in sorted(glob.glob('/Users/viola/Data/SailingData/PandasLogs/2026-*.parquet')):
        df = race_logs.read_log_file(os.path.basename(path), skip_dock_only=True,
            discard_columns=False, rename_columns=True, trim=True, process=True,
            path=os.path.dirname(path))
        if df is None or 'zg100_roll' not in df.columns:
            continue
        df = df.reset_index(drop=True)
        sog, _ = _clean_speed_glitches(df['rsog'].to_numpy())
        rawa = df['rawa'].to_numpy(float)
        hdg = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360
        man = detect_maneuvers(rawa, hdg, FS); centers = [cc for cc, _ in man]
        iso = []
        for i, (cc, k) in enumerate(man):
            if k != 'tack':
                continue
            prev = centers[i-1] if i > 0 else -10**9
            nxt = centers[i+1] if i+1 < len(centers) else 10**9
            if (cc-prev) >= iso_n and (nxt-cc) >= iso_n and cc-H >= 0 and cc+H+1 <= len(df):
                iso.append(cc)
        if iso:
            cache.append(dict(rawa=rawa, raws_kt=df['raws'].to_numpy(float)*G.MS_2_KNOTS,
                heel=df['zg100_roll'].to_numpy(float)-heel_off, hdg=hdg,
                cog=df['rcog'].to_numpy(float), sog=sog*G.MS_2_KNOTS, iso_tacks=iso))
    ntot = sum(len(c['iso_tacks']) for c in cache)
    print(f"[settled] {ntot} isolated tacks across {len(cache)} sessions", flush=True)

    def collect_all(a, b):
        recs = []
        for c in cache:
            recs += collect(corrected_twd(c, cal_corr, a, b), c)
        return recs

    # Linearized fit on the settled window: O0 (no upwash), O2 (uniform 2 deg).
    B0 = per_bin_odd(collect_all(0.0, 0.0))
    B2 = per_bin_odd(collect_all(2.0, 0.0))
    ks = [(d0['odd']-d2['odd'])/2.0 for d0, d2 in zip(B0, B2)
          if np.isfinite(d0['odd']) and np.isfinite(d2['odd'])]
    k = float(np.median(ks))
    aws = np.array([d['aws'] for d in B0 if np.isfinite(d['odd'])])
    req = np.array([d['odd'] for d in B0 if np.isfinite(d['odd'])]) / k
    wts = np.sqrt([d['n'] for d in B0 if np.isfinite(d['odd'])])
    slope, intercept = np.polyfit(aws, req, 1, w=wts)
    a_new, b_new = float(intercept), float(-slope)
    print(f"[settled] k={k:.2f}; settled-fit Delta-beta = max(0, {a_new:.2f} - "
          f"{b_new:+.3f}*AWS)   (old: 6.13 - 0.095*AWS)", flush=True)
    for v in (8, 12, 15, 18):
        print(f"    AWS={v:2d}: new Δβ={max(0,a_new-b_new*v):.2f}°  "
              f"old Δβ={max(0,OLD_A-OLD_B*v):.2f}°", flush=True)

    recs0 = collect_all(0.0, 0.0)
    recs_old = collect_all(OLD_A, OLD_B)
    recs_new = collect_all(a_new, b_new)
    print("[settled] per-bin settled odd (raw / old-fit / new-fit):", flush=True)
    for d0, do, dn in zip(per_bin_odd(recs0), per_bin_odd(recs_old), per_bin_odd(recs_new)):
        print(f"   AWS~{d0['aws']:4.1f} (n={d0['n']:3d}): {d0['odd']:+5.1f} / "
              f"{do['odd']:+5.1f} / {dn['odd']:+5.1f}", flush=True)

    t = (np.arange(2*H+1)-H)/FS
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=120)
    for recs, col, lbl in ((recs0, 'tab:red', 'raw (heel only)'),
                           (recs_old, 'tab:orange', 'old Δβ (+15-40s fit)'),
                           (recs_new, 'tab:blue', f'new Δβ (settled fit)')):
        ax.plot(t, odd_trace(recs), color=col, lw=1.8, label=lbl)
    for x in (SETTLED[0], SETTLED[1]):
        ax.axvspan(SETTLED[0], SETTLED[1], color='0.85', alpha=0.3)
    ax.axhline(0, color='0.5', lw=0.6); ax.axvline(0, color='0.5', lw=0.6)
    ax.set_xlabel('time from tack (s)'); ax.set_ylabel('odd component of ΔTWD (deg)')
    ax.set_title(f'Settled-window upwash re-fit (isolated tacks, n={ntot}); '
                 f'grey = +{SETTLED[0]:.0f}..{SETTLED[1]:.0f}s fit window')
    ax.legend(fontsize=9); ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(OUT_DIR / 'twd_upwash_settled_fit.png')
    plt.close(fig)
    print(f"[settled] wrote {OUT_DIR}/twd_upwash_settled_fit.png", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
