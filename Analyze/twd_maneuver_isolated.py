#!/usr/bin/env python
"""Pre/post TWD by direction for ISOLATED tacks only (no other maneuver within
+-ISO seconds), current vs corrected. With isolation the baseline cleanly zeroes
the pre-tack (leaving-tack) stretch, so the plot shows the TRUE shape:
flat before the tack, the tack-state bias growing for ~1-2 min after.

Window is +-WIN_S (< ISO so the window never reaches a neighbor).

Run:
    conda run -n sailing --no-capture-output python Analyze/twd_maneuver_isolated.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import argparse, glob, json, logging
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
from Analyze.true_wind_estimator import Calibration
from Analyze.calibrate_ground_ref import detect_maneuvers, _clean_speed_glitches
from Analyze.twd_maneuver_corrected import gr_current, gr_corrected
from Analyze.twd_maneuver_diag import circmean, w180, BASE, POST

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()
FS = float(G.SAMPLES_PER_SECOND)
HERE = Path(__file__).resolve().parent
GR_CAL = HERE / 'calibration_ground_ref_2026.json'
OUT_DIR = HERE / 'Plots' / 'twd_maneuver'


def collect_isolated(twd, rawa, man, centers, H, iso_n):
    out = {'p2s': ([], []), 's2p': ([], [])}
    for i, (cc, k) in enumerate(man):
        if k != 'tack':
            continue
        prev = centers[i-1] if i > 0 else -10**9
        nxt = centers[i+1] if i+1 < len(centers) else 10**9
        if (cc - prev) < iso_n or (nxt - cc) < iso_n:
            continue
        if cc - H < 0 or cc + H + 1 > len(twd):
            continue
        base = circmean(twd[cc-int(BASE[0]*FS):cc-int(BASE[1]*FS)])
        post = circmean(twd[cc+int(POST[0]*FS):cc+int(POST[1]*FS)])
        pre = np.nanmedian(rawa[cc-int(BASE[0]*FS):cc-int(BASE[1]*FS)])
        if not all(np.isfinite([base, post, pre])):
            continue
        key = 's2p' if pre > 0 else 'p2s'
        out[key][0].append(w180(twd[cc-H:cc+H+1] - base))
        out[key][1].append(w180(post - base))
    return out


def plot_axis(ax, acc, title, H):
    t = (np.arange(2*H+1) - H) / FS
    meds = {}
    for key, col, lbl in (('p2s', 'tab:red', 'port→stbd'),
                          ('s2p', 'tab:blue', 'stbd→port')):
        tr = [x for x in acc[key][0] if len(x) == 2*H+1]
        d = np.array([x for x in acc[key][1] if np.isfinite(x)])
        if tr:
            M = np.vstack(tr)
            ax.plot(t, np.nanmedian(M, axis=0), color=col, lw=1.6,
                    label=f'{lbl} (n={len(tr)})')
            ax.fill_between(t, np.nanpercentile(M, 25, axis=0),
                           np.nanpercentile(M, 75, axis=0), color=col, alpha=0.13)
        meds[key] = float(np.median(d)) if d.size else np.nan
    ax.axhline(0, color='0.5', lw=0.6); ax.axvline(0, color='0.5', lw=0.6)
    ax.set_ylim(-14, 14); ax.grid(alpha=0.3); ax.legend(fontsize=9, loc='upper left')
    ax.set_xlabel('time from tack (s)'); ax.set_ylabel('TWD − pre-tack mean (deg)')
    odd = 0.5*(meds['p2s'] - meds['s2p'])
    ax.set_title(f'{title}\nODD@[15-40s]={odd:+.1f}°')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--win-s', type=float, default=180.0)
    ap.add_argument('--iso-s', type=float, default=200.0)
    args = ap.parse_args()
    H = int(args.win_s * FS); iso_n = int(args.iso_s * FS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(GR_CAL) as f:
        cal = Calibration(**json.load(f)['calibration'])
    with open(HERE / 'leeway_model.json') as f:
        heel_off = float(json.load(f)['calibration']['heel_offset_deg'])
    cal_corr = replace(cal, awa_scale_upwind=1.0, awa_offset_deg=0.0)

    acc = {cfg: {'p2s': ([], []), 's2p': ([], [])} for cfg in ('current', 'corrected')}
    nkept = nall = 0
    for path in sorted(glob.glob('/Users/viola/Data/SailingData/PandasLogs/2026-*.parquet')):
        name = os.path.basename(path)
        try:
            df = race_logs.read_log_file(name, skip_dock_only=True,
                discard_columns=False, rename_columns=True, trim=True,
                process=True, path=os.path.dirname(path))
        except Exception:
            continue
        if df is None or 'zg100_roll' not in df.columns:
            continue
        df = df.reset_index(drop=True)
        sog, _ = _clean_speed_glitches(df['rsog'].to_numpy())
        rawa = df['rawa'].to_numpy(float)
        hdg = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360
        c = {'rawa': rawa, 'raws_kt': df['raws'].to_numpy(float) * G.MS_2_KNOTS,
             'heel': df['zg100_roll'].to_numpy(float) - heel_off, 'hdg': hdg,
             'cog': df['rcog'].to_numpy(float), 'sog': sog * G.MS_2_KNOTS}
        man = detect_maneuvers(rawa, hdg, FS)
        centers = [cc for cc, _ in man]
        nall += sum(1 for _, k in man if k == 'tack')
        for cfg, twd in (('current', gr_current(c, cal)),
                         ('corrected', gr_corrected(c, cal_corr))):
            bd = collect_isolated(twd, rawa, man, centers, H, iso_n)
            for kk in ('p2s', 's2p'):
                acc[cfg][kk][0].extend(bd[kk][0]); acc[cfg][kk][1].extend(bd[kk][1])
        nkept = max(nkept, len(acc['current']['p2s'][0]) + len(acc['current']['s2p'][0]))
    print(f"[iso] kept {nkept}/{nall} isolated tacks (gap>={args.iso_s:.0f}s)", flush=True)

    fig, axs = plt.subplots(1, 2, figsize=(13, 4.8), dpi=120)
    plot_axis(axs[0], acc['current'], 'Current (raw AWA)', H)
    plot_axis(axs[1], acc['corrected'], 'Corrected (heel + upwash, upwind)', H)
    fig.suptitle(f'ISOLATED tacks (no maneuver within ±{args.iso_s:.0f}s), '
                 f'±{args.win_s:.0f}s — baseline zeroes the pre-tack stretch; the '
                 'bias is post-tack and grows ~1-2 min', fontsize=11)
    fig.tight_layout()
    out = OUT_DIR / 'twd_maneuver_isolated.png'
    fig.savefig(out); plt.close(fig)
    print(f"[iso] wrote {out}", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
