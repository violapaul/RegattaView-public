#!/usr/bin/env python
"""Regenerate the pre/post maneuver overlays, current vs corrected.

current   = groundref with the shipped ground-ref calibration, raw AWA.
corrected = groundref with cos(phi) heel-tilt + upwash Delta-beta(AWS) applied
            UPWIND ONLY (downwind/gybe left on the shipped calibration).

2x2: rows = tack / gybe, cols = current / corrected; each axis overlays the two
maneuver directions (port->stbd, stbd->port).

Trace half-window is configurable (--win-s, default 300). NOTE: beyond the
median tack spacing (drawn as dashed verticals) the window overlaps ADJACENT
maneuvers, so the per-direction medians mix tacks and wash toward 0 with a large
IQR -- the clean odd signal is the inner ~+-60-90 s. The even/odd numbers in the
titles are always computed on the [+-15,40] s windows, independent of --win-s.

Run:
    conda run -n sailing --no-capture-output python Analyze/twd_maneuver_corrected.py --win-s 300
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
from Analyze.true_wind_estimator import (
    AdaptiveFilterConfig, Calibration, offline_ground_wind_adaptive)
from Analyze.calibrate_ground_ref import detect_maneuvers, _clean_speed_glitches
from Analyze.twd_maneuver_diag import w180, circmean, BASE, POST
from Analyze.twd_maneuver_corrections import heel_tilt_correct

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()
FS = float(G.SAMPLES_PER_SECOND)
HUBER_ONLY = AdaptiveFilterConfig(alpha_R_turn=0.0, alpha_R_accel=0.0, q_growth=1.0)
HERE = Path(__file__).resolve().parent
GR_CAL = HERE / 'calibration_ground_ref_2026.json'
OUT_DIR = HERE / 'Plots' / 'twd_maneuver'
FIT_A, FIT_B = 6.13, 0.095     # Delta-beta(AWS)=max(0,a-b*AWS), from twd_upwash_aws_fit


def gr_current(c, cal):
    est = pd.DataFrame({'awa': c['rawa'], 'aws': c['raws_kt'], 'hdg': c['hdg'],
                        'cog': c['cog'], 'sog': c['sog']})
    return offline_ground_wind_adaptive(est, fs_hz=FS, cal=cal,
                                        adaptive_cfg=HUBER_ONLY)['twd'].to_numpy(float)


def gr_corrected(c, cal_corr):
    """heel-tilt + upwash UPWIND ONLY; downwind AWA raw (shipped downwind scale)."""
    rawa = c['rawa']; aws = c['raws_kt'].copy(); awa = rawa.copy()
    up = np.abs(rawa) < 90.0
    a_h, s_h = heel_tilt_correct(rawa[up], c['raws_kt'][up], c['heel'][up])
    dbeta = np.maximum(0.0, FIT_A - FIT_B * s_h)
    awa[up] = a_h - dbeta * np.sign(a_h)
    aws[up] = s_h
    est = pd.DataFrame({'awa': awa, 'aws': aws, 'hdg': c['hdg'],
                        'cog': c['cog'], 'sog': c['sog']})
    return offline_ground_wind_adaptive(est, fs_hz=FS, cal=cal_corr,
                                        adaptive_cfg=HUBER_ONLY)['twd'].to_numpy(float)


def collect_dir(twd, rawa, man, kind, H):
    """Traces (half-window H samples) + net deltas ([+-15,40]s) split by entry tack."""
    out = {'p2s': ([], []), 's2p': ([], [])}
    for c, k in man:
        if k != kind or c - H < 0 or c + H + 1 > len(twd):
            continue
        base = circmean(twd[c - int(BASE[0]*FS):c - int(BASE[1]*FS)])
        post = circmean(twd[c + int(POST[0]*FS):c + int(POST[1]*FS)])
        pre_awa = np.nanmedian(rawa[c - int(BASE[0]*FS):c - int(BASE[1]*FS)])
        if not all(np.isfinite([base, post, pre_awa])):
            continue
        key = 's2p' if pre_awa > 0 else 'p2s'
        out[key][0].append(w180(twd[c - H:c + H + 1] - base))
        out[key][1].append(w180(post - base))
    return out


def plot_axis(ax, acc, title, H, spacing_s):
    t = (np.arange(2 * H + 1) - H) / FS
    meds = {}
    for key, col, lbl in (('p2s', 'tab:red', 'port→stbd'),
                          ('s2p', 'tab:blue', 'stbd→port')):
        tr = [x for x in acc[key][0] if len(x) == 2 * H + 1]
        d = np.array([x for x in acc[key][1] if np.isfinite(x)])
        if tr:
            M = np.vstack(tr)
            ax.plot(t, np.nanmedian(M, axis=0), color=col, lw=1.4,
                    label=f'{lbl} (n={len(tr)}, net {np.median(d):+.1f}°)')
            ax.fill_between(t, np.nanpercentile(M, 25, axis=0),
                           np.nanpercentile(M, 75, axis=0), color=col, alpha=0.12)
        meds[key] = float(np.median(d)) if d.size else np.nan
    if np.isfinite(spacing_s):
        for s in (-spacing_s, spacing_s):
            ax.axvline(s, color='0.6', ls='--', lw=0.8)
    ax.axhline(0, color='0.5', lw=0.6); ax.axvline(0, color='0.5', lw=0.6)
    ax.set_ylim(-18, 18); ax.grid(alpha=0.3); ax.legend(fontsize=8, loc='upper left')
    ax.set_xlabel('time from maneuver (s)')
    ax.set_ylabel('TWD − pre-maneuver mean (deg)')
    even = np.nanmean([meds['p2s'], meds['s2p']]); odd = 0.5*(meds['p2s']-meds['s2p'])
    ax.set_title(f'{title}\neven={even:+.1f}°  ODD={odd:+.1f}°  '
                 f'(±15-40s windows)')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--win-s', type=float, default=300.0)
    args = ap.parse_args()
    H = int(args.win_s * FS)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(GR_CAL) as f:
        cal = Calibration(**json.load(f)['calibration'])
    with open(HERE / 'leeway_model.json') as f:
        heel_off = float(json.load(f)['calibration']['heel_offset_deg'])
    cal_corr = replace(cal, awa_scale_upwind=1.0, awa_offset_deg=0.0)

    acc = {(cfg, kind): {'p2s': ([], []), 's2p': ([], [])}
           for cfg in ('current', 'corrected') for kind in ('tack', 'gybe')}
    spacings = []
    n = 0
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
        sog_clean, _ = _clean_speed_glitches(df['rsog'].to_numpy())
        rawa = df['rawa'].to_numpy(float)
        hdg_true = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0
        c = {'rawa': rawa, 'raws_kt': df['raws'].to_numpy(float) * G.MS_2_KNOTS,
             'heel': df['zg100_roll'].to_numpy(float) - heel_off,
             'hdg': hdg_true, 'cog': df['rcog'].to_numpy(float),
             'sog': sog_clean * G.MS_2_KNOTS}
        man = detect_maneuvers(rawa, hdg_true, FS)
        tack_centers = [cc for cc, k in man if k == 'tack']
        spacings.extend(np.diff(tack_centers) / FS)
        for cfg, twd in (('current', gr_current(c, cal)),
                         ('corrected', gr_corrected(c, cal_corr))):
            for kind in ('tack', 'gybe'):
                bd = collect_dir(twd, rawa, man, kind, H)
                for kk in ('p2s', 's2p'):
                    acc[(cfg, kind)][kk][0].extend(bd[kk][0])
                    acc[(cfg, kind)][kk][1].extend(bd[kk][1])
        n += 1
    spacing = float(np.median(spacings)) if spacings else np.nan
    print(f"[regen] {n} sessions; median tack spacing {spacing:.0f}s "
          f"(window ±{args.win_s:.0f}s)", flush=True)

    fig, axs = plt.subplots(2, 2, figsize=(14, 9), dpi=120)
    plot_axis(axs[0, 0], acc[('current', 'tack')], 'TACK — current (raw AWA)', H, spacing)
    plot_axis(axs[0, 1], acc[('corrected', 'tack')], 'TACK — corrected (heel + upwash)', H, spacing)
    plot_axis(axs[1, 0], acc[('current', 'gybe')], 'GYBE — current (raw AWA)', H, spacing)
    plot_axis(axs[1, 1], acc[('corrected', 'gybe')], 'GYBE — corrected (heel + upwash)', H, spacing)
    fig.suptitle(f'Pre/post maneuver TWD by direction, ±{args.win_s:.0f}s — '
                 'before vs after the two-step masthead correction\n'
                 '(dashed = median tack spacing; beyond it, adjacent maneuvers '
                 'contaminate the average)', fontsize=11)
    fig.tight_layout()
    out = OUT_DIR / f'twd_maneuver_by_direction_corrected_{int(args.win_s)}s.png'
    fig.savefig(out); plt.close(fig)
    print(f"[regen] wrote {out}", flush=True)
    for kind in ('tack', 'gybe'):
        for cfg in ('current', 'corrected'):
            a = acc[(cfg, kind)]
            dp = np.median([x for x in a['p2s'][1] if np.isfinite(x)])
            ds = np.median([x for x in a['s2p'][1] if np.isfinite(x)])
            print(f"   {kind:5s} {cfg:9s}: even={0.5*(dp+ds):+.2f}°  "
                  f"ODD={0.5*(dp-ds):+.2f}°", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
