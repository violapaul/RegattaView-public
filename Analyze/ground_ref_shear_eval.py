#!/usr/bin/env python
"""Three-way TWD shear benchmark (A2): is the ground-ref estimate steadier?

Compares the cross-maneuver TWD shear of three true-wind sources on the
2026 corpus:
  (a) prod      -- production gradient-descent twd (race_logs.estimate_true_wind,
                   eps=1e-4) as stored in the parquet (df['twd']).
  (b) rtwd      -- the on-boat B&G PGN 130306 value (df['rtwd']).
  (c) groundref -- offline_ground_wind_adaptive + HUBER_ONLY + the new
                   ground-ref calibration (calibration_ground_ref_2026.json).

Metric is SELF-REFERENTIAL shear, not error-vs-reference: the true wind does
not change just because the boat tacked, so across each maneuver the change
in TWD (pre vs post) should be ~0. We compute, for the SAME tacks/gybes for
all three sources:
  * median |dTWD| across maneuvers (lower = less artifact). Because it's a
    *difference* across the maneuver, constant frame offsets cancel -- the
    comparison is frame-agnostic.
  * steady-leg TWD circular std (between maneuvers, low heading-rate, >=30 s).

Outputs Analyze/Plots/ground_ref_shear/{summary.md,summary.csv} + a pooled
tack-overlay plot.

Run:
    conda run -n sailing --no-capture-output \
        python Analyze/ground_ref_shear_eval.py
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import glob
import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from Libraries.global_variables import G
from Libraries import race_logs
from Libraries.log_helper import LOG

from Analyze.true_wind_estimator import (
    AdaptiveFilterConfig,
    Calibration,
    offline_ground_wind_adaptive,
)
from Analyze.calibrate_ground_ref import detect_maneuvers, _clean_speed_glitches

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()

HERE = Path(__file__).resolve().parent
LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
CAL_PATH = HERE / 'calibration_ground_ref_2026.json'
OUT_DIR = HERE / 'Plots' / 'ground_ref_shear'

FS = float(G.SAMPLES_PER_SECOND)
PRE_W = (25.0, 10.0)    # pre-maneuver window [center-25s, center-10s]
POST_W = (10.0, 25.0)   # post-maneuver window [center+10s, center+25s]
HEADING_RATE_MAX = 1.0  # deg/s for steady legs
STEADY_MIN_S = 30.0
SOURCES = ('prod', 'rtwd', 'groundref')

HUBER_ONLY = AdaptiveFilterConfig(alpha_R_turn=0.0, alpha_R_accel=0.0,
                                  q_growth=1.0)


def _wrap180(a):
    return ((np.asarray(a, dtype=float) + 180.0) % 360.0) - 180.0


def circmean_deg(a):
    a = np.asarray(a, float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return float('nan')
    r = np.deg2rad(a)
    return float(np.rad2deg(np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


def circstd_deg(a):
    a = np.asarray(a, float)
    a = a[np.isfinite(a)]
    if a.size < 2:
        return float('nan')
    r = np.deg2rad(a)
    R = np.hypot(np.mean(np.sin(r)), np.mean(np.cos(r)))
    R = min(max(R, 1e-9), 1.0)
    return float(np.rad2deg(np.sqrt(-2.0 * np.log(R))))


def load_calibration() -> Calibration:
    with open(CAL_PATH) as f:
        return Calibration(**json.load(f)['calibration'])


def maneuver_dtwd(twd: np.ndarray, center: int) -> float:
    """Signed dTWD across a maneuver (post circmean - pre circmean)."""
    pre0, pre1 = center - int(PRE_W[0] * FS), center - int(PRE_W[1] * FS)
    po0, po1 = center + int(POST_W[0] * FS), center + int(POST_W[1] * FS)
    if pre0 < 0 or po1 > len(twd):
        return float('nan')
    pre = circmean_deg(twd[pre0:pre1])
    post = circmean_deg(twd[po0:po1])
    return float(_wrap180(post - pre))


def steady_std(twd: np.ndarray, hdg_true: np.ndarray) -> float:
    """Pooled within-leg circular std of TWD over steady legs."""
    rate = np.abs(np.gradient(np.unwrap(np.deg2rad(hdg_true))) * FS * 180 / np.pi)
    steady = rate < HEADING_RATE_MAX
    min_len = int(STEADY_MIN_S * FS)
    stds = []
    i, n = 0, len(twd)
    while i < n:
        if steady[i]:
            j = i
            while j < n and steady[j]:
                j += 1
            if j - i >= min_len:
                # detrend within leg by subtracting leg circmean, accumulate std
                s = circstd_deg(twd[i:j])
                if np.isfinite(s):
                    stds.append(s)
            i = j
        else:
            i += 1
    return float(np.median(stds)) if stds else float('nan')


def build_est_df(df: pd.DataFrame) -> pd.DataFrame:
    sog_clean, _ = _clean_speed_glitches(df['rsog'].to_numpy())
    return pd.DataFrame({
        'awa': df['rawa'].to_numpy(float),
        'aws': df['raws'].to_numpy(float) * G.MS_2_KNOTS,
        'hdg': (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0,
        'cog': df['rcog'].to_numpy(float),
        'sog': sog_clean * G.MS_2_KNOTS,
    }, index=df.index).reset_index(drop=True)


def eval_session(name: str, path: str, cal: Calibration
                 ) -> Optional[Tuple[dict, dict]]:
    df = race_logs.read_log_file(name, skip_dock_only=True,
                                 discard_columns=False, rename_columns=True,
                                 trim=True, process=True, path=path)
    if df is None or 'twd' not in df.columns:
        return None
    df = df.reset_index(drop=True)
    est_df = build_est_df(df)
    hdg_true = est_df['hdg'].to_numpy()

    twd_sources: Dict[str, np.ndarray] = {}
    twd_sources['prod'] = df['twd'].to_numpy(float)
    twd_sources['rtwd'] = (df['rtwd'].to_numpy(float)
                           if 'rtwd' in df.columns
                           else np.full(len(df), np.nan))
    gr = offline_ground_wind_adaptive(est_df, fs_hz=FS, cal=cal,
                                      adaptive_cfg=HUBER_ONLY)
    twd_sources['groundref'] = gr['twd'].to_numpy(float)

    man = detect_maneuvers(est_df['awa'].to_numpy(), hdg_true, FS)
    res = {'session': name.replace('.parquet', ''),
           'n_tack': sum(1 for _, k in man if k == 'tack'),
           'n_gybe': sum(1 for _, k in man if k == 'gybe')}
    traces = {s: [] for s in SOURCES}   # for pooled overlay
    for src in SOURCES:
        twd = twd_sources[src]
        if not np.any(np.isfinite(twd)):
            for k in ('tack', 'gybe'):
                res[f'{src}_{k}_dtwd'] = float('nan')
            res[f'{src}_steady_std'] = float('nan')
            continue
        for kind in ('tack', 'gybe'):
            d = [abs(maneuver_dtwd(twd, c)) for c, k in man if k == kind]
            d = [x for x in d if np.isfinite(x)]
            res[f'{src}_{kind}_dtwd'] = float(np.median(d)) if d else float('nan')
        res[f'{src}_steady_std'] = steady_std(twd, hdg_true)
        # collect tack-centered |dTWD-relative| traces for the overlay
        for c, k in man:
            if k != 'tack':
                continue
            pre0 = c - int(PRE_W[0] * FS)
            po1 = c + int(POST_W[0] * FS) + int((POST_W[1] - POST_W[0]) * FS)
            w0, w1 = c - int(35 * FS), c + int(35 * FS)
            if w0 < 0 or w1 > len(twd):
                continue
            seg = twd[w0:w1]
            base = circmean_deg(twd[pre0:c - int(PRE_W[1] * FS)])
            if not np.isfinite(base):
                continue
            traces[src].append(np.abs(_wrap180(seg - base)))
    return res, traces


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--logs-dir', default=LOGS_DIR)
    p.add_argument('--pattern', default='2026-*.parquet')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--gps-delay', type=float, default=None,
                   help='override groundref gps_delay_s (diagnostic sweep)')
    args = p.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    cal = load_calibration()
    if args.gps_delay is not None:
        from dataclasses import replace as _replace
        cal = _replace(cal, gps_delay_s=args.gps_delay)
    print(f"[shear] calibration: heading={cal.heading_offset_deg:+.2f} "
          f"awa_off={cal.awa_offset_deg:+.2f} gps_delay={cal.gps_delay_s:.2f} "
          f"awa_sc_uw={cal.awa_scale_upwind:.3f} aws_sc_uw={cal.aws_scale_upwind:.3f}",
          flush=True)

    paths = sorted(glob.glob(os.path.join(args.logs_dir, args.pattern)))
    if args.limit:
        paths = paths[:args.limit]

    rows: List[dict] = []
    pooled_traces = {s: [] for s in SOURCES}
    for path in paths:
        try:
            r = eval_session(os.path.basename(path), os.path.dirname(path), cal)
        except Exception as exc:
            print(f"  skip {os.path.basename(path)}: {type(exc).__name__}: {exc}",
                  flush=True)
            continue
        if r is None:
            continue
        res, traces = r
        rows.append(res)
        for s in SOURCES:
            pooled_traces[s].extend(traces[s])
        print(f"  {res['session']}: tacks={res['n_tack']:3d}  "
              f"|dTWD| tack prod={res['prod_tack_dtwd']:.1f} "
              f"rtwd={res['rtwd_tack_dtwd']:.1f} "
              f"gr={res['groundref_tack_dtwd']:.1f}  "
              f"steady_std gr={res['groundref_steady_std']:.1f}", flush=True)

    df = pd.DataFrame(rows)
    df.to_csv(OUT_DIR / 'summary.csv', index=False)

    # Pooled medians (weight each maneuver equally would need raw; use
    # session-median-of-medians for robustness, and also a tack-count-weighted
    # mean as a check).
    def pooled(col):
        v = df[col].to_numpy(float)
        v = v[np.isfinite(v)]
        return float(np.median(v)) if v.size else float('nan')

    print("\n[shear] === POOLED (median across sessions) ===", flush=True)
    print(f"  {'metric':22s} {'prod':>8s} {'rtwd':>8s} {'groundref':>10s}",
          flush=True)
    summary = {}
    for label, key in (('tack |dTWD| (deg)', 'tack_dtwd'),
                       ('gybe |dTWD| (deg)', 'gybe_dtwd'),
                       ('steady-leg std (deg)', 'steady_std')):
        a = pooled(f'prod_{key}')
        b = pooled(f'rtwd_{key}')
        c = pooled(f'groundref_{key}')
        summary[key] = {'prod': a, 'rtwd': b, 'groundref': c}
        print(f"  {label:22s} {a:8.2f} {b:8.2f} {c:10.2f}", flush=True)

    # Pooled tack overlay plot + transient-excursion metric.
    # The trace is |TWD - pre-tack mean|, so it starts ~0, bumps during the
    # tack, and settles to |net shift| after. We separate:
    #   settle    = median over [+25,+35]s  (the real, slow wind shift)
    #   peak      = max over [-10,+25]s
    #   transient = peak - settle           (the recovering ARTIFACT, the
    #               metric that does NOT reward over-damping)
    fig, ax = plt.subplots(figsize=(9, 3.4), dpi=120)
    colors = {'prod': 'tab:blue', 'rtwd': 'k', 'groundref': 'tab:red'}
    print("\n[shear] === TACK TRANSIENT (artifact, damping-robust) ===",
          flush=True)
    print(f"  {'source':10s} {'peak':>7s} {'settle':>7s} {'transient':>10s}",
          flush=True)
    for s in SOURCES:
        mats = [t for t in pooled_traces[s] if len(t) == int(70 * FS)]
        if not mats:
            continue
        M = np.vstack(mats)
        t_s = (np.arange(M.shape[1]) - M.shape[1] // 2) / FS
        med = np.nanmedian(M, axis=0)
        peak_win = (t_s >= -10) & (t_s <= 25)
        settle_win = (t_s >= 25) & (t_s <= 35)
        peak = float(np.nanmax(med[peak_win]))
        settle = float(np.nanmedian(med[settle_win]))
        transient = peak - settle
        summary.setdefault('tack_transient', {})[s] = transient
        print(f"  {s:10s} {peak:7.2f} {settle:7.2f} {transient:10.2f}",
              flush=True)
        ax.plot(t_s, med, color=colors[s], lw=1.5,
                label=f'{s} (n={M.shape[0]}, transient={transient:.1f}°)')
    ax.axvline(0, color='gray', lw=0.5)
    ax.set_xlabel('time from tack midpoint (s)')
    ax.set_ylabel('|TWD - pre-tack mean| (deg)')
    ax.set_title('Pooled tack-window TWD excursion (lower = less shear)')
    ax.legend(fontsize=8)
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT_DIR / 'pooled_tack_overlay.png')
    plt.close(fig)

    # Markdown.
    lines = ["# Ground-ref TWD shear benchmark\n",
             f"Calibration: `{CAL_PATH.name}`  |  sessions: {len(df)}  |  "
             f"tacks: {int(df['n_tack'].sum())}, gybes: {int(df['n_gybe'].sum())}\n",
             "Self-referential shear: change in TWD across a maneuver "
             "(should be ~0). Median across sessions.\n",
             "| metric | prod (grad-desc) | rtwd (B&G) | groundref (new) |",
             "|---|---:|---:|---:|"]
    for label, key in (('tack \\|dTWD\\| (deg)', 'tack_dtwd'),
                       ('gybe \\|dTWD\\| (deg)', 'gybe_dtwd'),
                       ('steady-leg std (deg)', 'steady_std')):
        s = summary[key]
        lines.append(f"| {label} | {s['prod']:.2f} | {s['rtwd']:.2f} | "
                     f"{s['groundref']:.2f} |")
    lines.append("\n![pooled tack overlay](pooled_tack_overlay.png)\n")
    (OUT_DIR / 'summary.md').write_text("\n".join(lines))

    with open(OUT_DIR / 'summary.json', 'w') as f:
        json.dump({'summary': summary,
                   'sessions': len(df),
                   'n_tacks': int(df['n_tack'].sum()),
                   'n_gybes': int(df['n_gybe'].sum())}, f, indent=2)

    print(f"\n[shear] wrote {OUT_DIR}/summary.{{md,csv,json}} + overlay png",
          flush=True)
    print(f"[shear] wall-clock: {time.time()-t0:.1f}s", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
