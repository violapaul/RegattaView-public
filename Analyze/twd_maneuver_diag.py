#!/usr/bin/env python
"""TWD continuity across tacks/gybes — the true-wind analog of the current
cross-tack diagnostic (cf. Analyze/tack_current_diag.py).

The true wind does not change because the boat tacks/gybes, so a good TWD
estimate is CONTINUOUS across a maneuver. This visualizes that, mirroring the
current diagnostic's panels:

  POOLED figure (twd_maneuver_pooled.png):
    - tack OVERLAY : per-tack TWD, baseline-subtracted (sits at 0 before the
      tack), vs time-from-tack; median + IQR band.
    - gybe OVERLAY : same for gybes.
    - JUMP hist    : distribution of per-maneuver net delta (post - pre).

  EXAMPLE session figure (twd_maneuver_<sid>.png):
    - TWD vs time with tacks (blue) / gybes (orange) marked + pre/post means.

Best available estimate: vector-state Kalman + RTS smoother
(offline_ground_wind_adaptive, zero-phase, unbiased) with the ground-ref
calibration (heading -2.3 deg, AWA offset/scale, GPS-delay). The corrected
production pipeline twd (race_logs, +bte fix) is overlaid as a lighter line.

Run:
    conda run -n sailing --no-capture-output python Analyze/twd_maneuver_diag.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import argparse, glob, json, logging
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
WIN_S = 45.0                 # half-window around each maneuver
BASE = (40.0, 15.0)          # baseline window [c-40s, c-15s]
POST = (15.0, 40.0)          # post window     [c+15s, c+40s]


def w180(a):
    return ((np.asarray(a, float) + 180.0) % 360.0) - 180.0


def circmean(a):
    a = np.asarray(a, float); a = a[np.isfinite(a)]
    if a.size == 0:
        return np.nan
    r = np.deg2rad(a)
    return float(np.rad2deg(np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


def best_twd(df, cal):
    sog_clean, _ = _clean_speed_glitches(df['rsog'].to_numpy())
    est = pd.DataFrame({
        'awa': df['rawa'].to_numpy(float),
        'aws': df['raws'].to_numpy(float) * G.MS_2_KNOTS,
        'hdg': (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0,
        'cog': df['rcog'].to_numpy(float),
        'sog': sog_clean * G.MS_2_KNOTS})
    return offline_ground_wind_adaptive(est, fs_hz=FS, cal=cal,
                                        adaptive_cfg=HUBER_ONLY)['twd'].to_numpy(float)


def collect_traces(twd, man, kind):
    """Baseline-subtracted traces (len 2*W+1) and net deltas for one kind."""
    H = int(WIN_S * FS)
    traces, deltas = [], []
    for c, k in man:
        if k != kind or c - H < 0 or c + H + 1 > len(twd):
            continue
        base = circmean(twd[c - int(BASE[0] * FS):c - int(BASE[1] * FS)])
        post = circmean(twd[c + int(POST[0] * FS):c + int(POST[1] * FS)])
        if not (np.isfinite(base) and np.isfinite(post)):
            continue
        seg = w180(twd[c - H:c + H + 1] - base)
        traces.append(seg)
        deltas.append(w180(post - base))
    return traces, deltas


def collect_by_direction(twd, awa_signed, man, kind):
    """Like collect_traces, but split by entry tack (sign of pre-maneuver AWA).
    Returns {'p2s': (traces, deltas), 's2p': (traces, deltas)} where
    p2s = leaving port tack (pre AWA<0) -> entering starboard;
    s2p = leaving starboard tack (pre AWA>0) -> entering port.
    (AWA>0 == wind from starboard == starboard tack, per p.sign_angle.)"""
    H = int(WIN_S * FS)
    out = {'p2s': ([], []), 's2p': ([], [])}
    for c, k in man:
        if k != kind or c - H < 0 or c + H + 1 > len(twd):
            continue
        base = circmean(twd[c - int(BASE[0] * FS):c - int(BASE[1] * FS)])
        post = circmean(twd[c + int(POST[0] * FS):c + int(POST[1] * FS)])
        pre_awa = float(np.nanmedian(awa_signed[c - int(BASE[0] * FS):c - int(BASE[1] * FS)]))
        if not (np.isfinite(base) and np.isfinite(post) and np.isfinite(pre_awa)):
            continue
        key = 's2p' if pre_awa > 0 else 'p2s'
        out[key][0].append(w180(twd[c - H:c + H + 1] - base))
        out[key][1].append(w180(post - base))
    return out


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--example', default='2026-04-19T08_59')
    args = p.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(GR_CAL) as f:
        cal = Calibration(**json.load(f)['calibration'])

    paths = sorted(glob.glob('/Users/viola/Data/SailingData/PandasLogs/2026-*.parquet'))
    if args.limit:
        paths = paths[:args.limit]

    pooled = {'tack': {'gr': [], 'prod': [], 'd_gr': [], 'd_prod': []},
              'gybe': {'gr': [], 'prod': [], 'd_gr': [], 'd_prod': []}}
    # direction-split (best estimate = groundref): {kind: {'p2s':(tr,d),'s2p':(tr,d)}}
    bydir = {k: {'p2s': ([], []), 's2p': ([], [])} for k in ('tack', 'gybe')}
    example = None
    H = int(WIN_S * FS)
    t_axis = (np.arange(2 * H + 1) - H) / FS

    for path in paths:
        name = os.path.basename(path); sid = name.replace('.parquet', '')
        try:
            df = race_logs.read_log_file(name, skip_dock_only=True,
                discard_columns=False, rename_columns=True, trim=True,
                process=True, path=os.path.dirname(path))
        except Exception:
            continue
        if df is None or 'twd' not in df.columns:
            continue
        df = df.reset_index(drop=True)
        gr = best_twd(df, cal)
        prod = df['twd'].to_numpy(float)
        hdg_true = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0
        man = detect_maneuvers(df['rawa'].to_numpy(float), hdg_true, FS)
        awa_signed = df['rawa'].to_numpy(float)
        for kind in ('tack', 'gybe'):
            tg, dg = collect_traces(gr, man, kind)
            tp, dp = collect_traces(prod, man, kind)
            pooled[kind]['gr'] += tg; pooled[kind]['d_gr'] += dg
            pooled[kind]['prod'] += tp; pooled[kind]['d_prod'] += dp
            bd = collect_by_direction(gr, awa_signed, man, kind)
            for key in ('p2s', 's2p'):
                bydir[kind][key][0].extend(bd[key][0])
                bydir[kind][key][1].extend(bd[key][1])
        if sid == args.example:
            example = (sid, gr, prod, man, hdg_true)

    # ---- pooled figure ----
    fig, axs = plt.subplots(1, 3, figsize=(15, 4.2), dpi=120)
    for ax, kind in ((axs[0], 'tack'), (axs[1], 'gybe')):
        for src, col, lbl in (('gr', 'tab:red', 'groundref RTS (best)'),
                              ('prod', 'tab:blue', 'production (fixed)')):
            mats = [t for t in pooled[kind][src] if len(t) == 2 * H + 1]
            if not mats:
                continue
            M = np.vstack(mats)
            med = np.nanmedian(M, axis=0)
            q1 = np.nanpercentile(M, 25, axis=0)
            q3 = np.nanpercentile(M, 75, axis=0)
            ax.plot(t_axis, med, color=col, lw=1.6, label=f'{lbl} (n={M.shape[0]})')
            ax.fill_between(t_axis, q1, q3, color=col, alpha=0.15)
        ax.axhline(0, color='0.5', lw=0.6); ax.axvline(0, color='0.5', lw=0.6)
        ax.set_xlabel('time from maneuver (s)')
        ax.set_ylabel('TWD - pre-maneuver mean (deg)')
        ax.set_title(f'{kind.upper()} overlay  (continuous wind => flat at 0)')
        ax.set_ylim(-20, 20); ax.grid(alpha=0.3); ax.legend(fontsize=8)
    # jump histogram
    axh = axs[2]
    for kind, col in (('tack', 'tab:green'), ('gybe', 'tab:orange')):
        d = np.array([x for x in pooled[kind]['d_gr'] if np.isfinite(x)])
        if d.size:
            axh.hist(d, bins=np.arange(-20, 21, 2), alpha=0.5, color=col,
                     label=f"{kind} (med |Δ|={np.median(np.abs(d)):.1f}°, n={d.size})")
    axh.axvline(0, color='0.5', lw=0.6)
    axh.set_xlabel('net TWD delta across maneuver (post - pre, deg)')
    axh.set_ylabel('count'); axh.set_title('JUMP: per-maneuver net Δ (groundref)')
    axh.legend(fontsize=8); axh.grid(alpha=0.3)
    fig.suptitle('TWD continuity across maneuvers — best estimate '
                 '(groundref RTS + ground-ref calibration)', fontsize=11)
    fig.tight_layout()
    fig.savefig(OUT_DIR / 'twd_maneuver_pooled.png')
    plt.close(fig)
    print(f"[twd-diag] wrote {OUT_DIR}/twd_maneuver_pooled.png", flush=True)
    for kind in ('tack', 'gybe'):
        dg = np.abs([x for x in pooled[kind]['d_gr'] if np.isfinite(x)])
        print(f"  {kind}: median |net ΔTWD| = {np.median(dg):.2f}° "
              f"(n={len(dg)})", flush=True)

    # ---- direction-split figure (best estimate) + even/odd ----
    DIRS = (('p2s', 'tab:red', 'port→stbd (leaving port tack)'),
            ('s2p', 'tab:blue', 'stbd→port (leaving stbd tack)'))
    fig, axs = plt.subplots(1, 2, figsize=(11, 4.4), dpi=120)
    print("\n[twd-diag] even/odd decomposition of net ΔTWD (groundref):",
          flush=True)
    print("  even = real common shift; ODD = tack-flipping bias "
          "(residual AWA/heading/leeway asymmetry)", flush=True)
    for ax, kind in ((axs[0], 'tack'), (axs[1], 'gybe')):
        meds = {}
        for key, col, lbl in DIRS:
            tr = [t for t in bydir[kind][key][0] if len(t) == 2 * H + 1]
            d = np.array([x for x in bydir[kind][key][1] if np.isfinite(x)])
            if tr:
                M = np.vstack(tr)
                med = np.nanmedian(M, axis=0)
                q1 = np.nanpercentile(M, 25, axis=0)
                q3 = np.nanpercentile(M, 75, axis=0)
                ax.plot(t_axis, med, color=col, lw=1.6,
                        label=f'{lbl}  n={len(tr)}, net={np.median(d):+.1f}°')
                ax.fill_between(t_axis, q1, q3, color=col, alpha=0.13)
            meds[key] = float(np.median(d)) if d.size else np.nan
        ax.axhline(0, color='0.5', lw=0.6); ax.axvline(0, color='0.5', lw=0.6)
        ax.set_xlabel('time from maneuver (s)')
        ax.set_ylabel('TWD - pre-maneuver mean (deg)')
        ax.set_ylim(-15, 15); ax.grid(alpha=0.3); ax.legend(fontsize=8, loc='upper left')
        even = np.nanmean([meds['p2s'], meds['s2p']])
        odd = 0.5 * (meds['p2s'] - meds['s2p'])
        ax.set_title(f'{kind.upper()}: even(shift)={even:+.1f}°  '
                     f'ODD(bias)={odd:+.1f}°')
        print(f"  {kind:5s}: port→stbd={meds['p2s']:+.2f}°  "
              f"stbd→port={meds['s2p']:+.2f}°  | even={even:+.2f}°  "
              f"ODD={odd:+.2f}°", flush=True)
    fig.suptitle('TWD across maneuvers, split by direction — best estimate '
                 '(groundref RTS). ODD≈0 ⇒ no tack-flipping bias', fontsize=11)
    fig.tight_layout()
    fig.savefig(OUT_DIR / 'twd_maneuver_by_direction.png')
    plt.close(fig)
    print(f"[twd-diag] wrote {OUT_DIR}/twd_maneuver_by_direction.png", flush=True)

    # ---- example session time panel ----
    if example:
        sid, gr, prod, man, hdg_true = example
        t = np.arange(len(gr)) / 60.0 / FS  # minutes
        ref = circmean(gr)
        fig, ax = plt.subplots(figsize=(13, 4), dpi=120)
        ax.plot(t, ref + w180(gr - ref), color='tab:red', lw=0.8,
                label='groundref RTS (best)')
        ax.plot(t, ref + w180(prod - ref), color='tab:blue', lw=0.6, alpha=0.6,
                label='production (fixed)')
        for c, k in man:
            ax.axvline(c / 60.0 / FS, color='tab:blue' if k == 'tack' else 'tab:orange',
                       lw=0.7, alpha=0.5)
        ax.set_xlabel('time (min)'); ax.set_ylabel('TWD (deg, unwrapped)')
        ax.set_title(f'{sid}: TWD through maneuvers '
                     '(blue lines=tacks, orange=gybes)')
        ax.grid(alpha=0.3); ax.legend(fontsize=8)
        fig.tight_layout(); fig.savefig(OUT_DIR / f'twd_maneuver_{sid}.png')
        plt.close(fig)
        print(f"[twd-diag] wrote {OUT_DIR}/twd_maneuver_{sid}.png", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
