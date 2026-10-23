#!/usr/bin/env python
"""A3: physical self-consistency of the ground-ref TWD via the current vector.

The water-referenced wind (the boat's B&G ``rtwd``) and a ground-referenced
wind differ, in steady conditions, by the *current-drag bias*:

    TWD_water - TWD_ground  ~=  (180/pi) * (|C| / TWS) * sin(C_dir - TWD_ground)

(see ``Analyze/true_wind_approaches.md`` Section 5 / ``estimator_math_review.md``).

This is a genuine cross-check, NOT a tautology, because the two sides come
from DIFFERENT sensor combinations:
  * left side  = rtwd (apparent wind + HDG, B&G firmware) minus our
                 ground-ref TWD (apparent wind + SOG/COG).
  * right side = C from ``current_analysis.estimate_current`` (GPS SOG/COG
                 minus STW-along-water-track, the leeway model) -- it never
                 touches the wind sensors.

If the leeway/current machinery and the wind estimators agree, the predicted
and observed biases line up on steady legs. Reported per steady leg, pooled
across sessions that carry rtwd.

Run:
    conda run -n sailing --no-capture-output \
        python Analyze/twd_frame_consistency.py
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
from typing import List

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from Libraries.global_variables import G
from Libraries import race_logs
from Libraries.log_helper import LOG

from Analyze.true_wind_estimator import (
    AdaptiveFilterConfig, Calibration, offline_ground_wind_adaptive,
)
from Analyze import current_analysis as ca
from Analyze.calibrate_ground_ref import _clean_speed_glitches

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()

HERE = Path(__file__).resolve().parent
LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
GR_CAL = HERE / 'calibration_ground_ref_2026.json'
OUT_DIR = HERE / 'Plots' / 'twd_frame_consistency'
FS = float(G.SAMPLES_PER_SECOND)
HEADING_RATE_MAX = 1.0
STEADY_MIN_S = 40.0
HUBER_ONLY = AdaptiveFilterConfig(alpha_R_turn=0.0, alpha_R_accel=0.0,
                                  q_growth=1.0)


def _wrap180(a):
    return ((np.asarray(a, float) + 180.0) % 360.0) - 180.0


def circmean(a):
    a = np.asarray(a, float); a = a[np.isfinite(a)]
    if a.size == 0:
        return np.nan
    r = np.deg2rad(a)
    return float(np.rad2deg(np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


def steady_legs(hdg_true):
    rate = np.abs(np.gradient(np.unwrap(np.deg2rad(hdg_true))) * FS * 180 / np.pi)
    steady = rate < HEADING_RATE_MAX
    min_len = int(STEADY_MIN_S * FS)
    legs = []
    i, n = 0, len(hdg_true)
    while i < n:
        if steady[i]:
            j = i
            while j < n and steady[j]:
                j += 1
            if j - i >= min_len:
                legs.append((i, j))
            i = j
        else:
            i += 1
    return legs


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--logs-dir', default=LOGS_DIR)
    p.add_argument('--pattern', default='2026-*.parquet')
    p.add_argument('--limit', type=int, default=0)
    args = p.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    with open(GR_CAL) as f:
        cal = Calibration(**json.load(f)['calibration'])
    lee = ca.load_leeway_model()
    K = lee['leeway']['K']
    print(f"[A3] ground-ref cal heading={cal.heading_offset_deg:+.2f} "
          f"gps_delay={cal.gps_delay_s:.2f}; leeway K={K:.2f}", flush=True)

    paths = sorted(glob.glob(os.path.join(args.logs_dir, args.pattern)))
    if args.limit:
        paths = paths[:args.limit]

    obs_all: List[float] = []
    pred_all: List[float] = []
    rows = []
    for path in paths:
        name = os.path.basename(path)
        sid = name.replace('.parquet', '')
        try:
            df = race_logs.read_log_file(name, skip_dock_only=True,
                                         discard_columns=False,
                                         rename_columns=True, trim=True,
                                         process=True, path=os.path.dirname(path))
        except Exception as exc:
            print(f"  skip {sid}: {type(exc).__name__}", flush=True)
            continue
        if df is None or 'rtwd' not in df.columns:
            continue
        rtwd = df['rtwd'].to_numpy(float)
        if np.mean(np.isfinite(rtwd)) < 0.3:
            continue  # autopilot mostly off -> no water-ref reference
        df = df.reset_index(drop=True)
        rtwd = df['rtwd'].to_numpy(float)

        # Our ground-ref TWD.
        sog_clean, _ = _clean_speed_glitches(df['rsog'].to_numpy())
        est_df = pd.DataFrame({
            'awa': df['rawa'].to_numpy(float),
            'aws': df['raws'].to_numpy(float) * G.MS_2_KNOTS,
            'hdg': (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0,
            'cog': df['rcog'].to_numpy(float),
            'sog': sog_clean * G.MS_2_KNOTS,
        })
        gr = offline_ground_wind_adaptive(est_df, fs_hz=FS, cal=cal,
                                          adaptive_cfg=HUBER_ONLY)
        twd_g = gr['twd'].to_numpy(float)
        hdg_true = est_df['hdg'].to_numpy() + cal.heading_offset_deg

        # Current vector from the (separate) leeway machinery.
        cdf = ca.load_calibrated_log(name, path=os.path.dirname(path),
                                     calibration='global')
        cur_e, cur_n, _ = ca.estimate_current(
            cdf, model=lee['leeway']['model'], params=(K,))
        cur_spd = np.hypot(cur_e, cur_n)
        cur_set = (np.degrees(np.arctan2(cur_e, cur_n))) % 360.0

        n = min(len(twd_g), len(cur_spd), len(rtwd))
        legs = steady_legs(hdg_true[:n])
        sess_obs, sess_pred = [], []
        for (a, b) in legs:
            tw = circmean(twd_g[a:b])
            rt = circmean(rtwd[a:b])
            if not (np.isfinite(tw) and np.isfinite(rt)):
                continue
            tws = np.nanmedian(np.hypot(gr['wind_north'].to_numpy()[a:b],
                                        gr['wind_east'].to_numpy()[a:b]))
            C = np.nanmedian(cur_spd[a:b])
            Cdir = circmean(cur_set[a:b])
            if not (np.isfinite(tws) and tws > 1 and np.isfinite(C)):
                continue
            obs = _wrap180(rt - tw)                       # observed water-ground
            pred = (180.0 / np.pi) * (C / tws) * np.sin(np.deg2rad(Cdir - tw))
            sess_obs.append(float(obs)); sess_pred.append(float(pred))
        if not sess_obs:
            continue
        obs_all += sess_obs; pred_all += sess_pred
        rows.append({'session': sid, 'n_legs': len(sess_obs),
                     'median_obs': float(np.median(sess_obs)),
                     'median_pred': float(np.median(sess_pred)),
                     'median_C_kt': float(np.nanmedian(cur_spd))})
        print(f"  {sid}: legs={len(sess_obs):2d} "
              f"obs(rtwd-gr)={np.median(sess_obs):+5.1f} "
              f"pred(drag)={np.median(sess_pred):+5.1f} deg", flush=True)

    if not obs_all:
        print("[A3] no sessions with rtwd + steady legs", file=sys.stderr)
        return 1

    obs_all = np.array(obs_all); pred_all = np.array(pred_all)
    # Robust line fit obs ~ a + b*pred.
    b, a = np.polyfit(pred_all, obs_all, 1)
    corr = float(np.corrcoef(pred_all, obs_all)[0, 1])
    resid_rms = float(np.sqrt(np.mean((obs_all - pred_all) ** 2)))

    fig, ax = plt.subplots(figsize=(5, 5), dpi=120)
    ax.scatter(pred_all, obs_all, s=10, alpha=0.4)
    lim = max(np.abs(pred_all).max(), np.abs(obs_all).max()) * 1.1
    ax.plot([-lim, lim], [-lim, lim], 'k--', lw=0.8, label='y=x (perfect)')
    ax.plot([-lim, lim], [a - b * lim, a + b * lim], 'r-', lw=1.0,
            label=f'fit slope={b:.2f}')
    ax.set_xlabel('predicted current-drag bias (deg)')
    ax.set_ylabel('observed rtwd - groundref (deg)')
    ax.set_title(f'TWD frame consistency  r={corr:.2f}  RMS={resid_rms:.1f}deg')
    ax.legend(fontsize=8); ax.grid(alpha=0.3); ax.set_aspect('equal')
    fig.tight_layout(); fig.savefig(OUT_DIR / 'consistency_scatter.png')
    plt.close(fig)

    payload = {'n_legs': len(obs_all), 'n_sessions': len(rows),
               'corr': corr, 'fit_slope': float(b), 'fit_intercept': float(a),
               'resid_rms_deg': resid_rms, 'per_session': rows}
    with open(OUT_DIR / 'summary.json', 'w') as f:
        json.dump(payload, f, indent=2)

    print(f"\n[A3] === FRAME CONSISTENCY (water-ground bias vs current drag) ===",
          flush=True)
    print(f"  legs={len(obs_all)} sessions={len(rows)}", flush=True)
    print(f"  correlation(obs, pred) = {corr:.3f}", flush=True)
    print(f"  fit: obs = {a:+.2f} + {b:.2f} * pred  (ideal slope 1, intercept 0)",
          flush=True)
    print(f"  residual RMS = {resid_rms:.2f} deg", flush=True)
    print(f"[A3] wrote {OUT_DIR}/summary.json + scatter; "
          f"wall {time.time()-t0:.1f}s", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
