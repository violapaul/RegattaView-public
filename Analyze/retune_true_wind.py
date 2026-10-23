#!/usr/bin/env python
"""Retune the gradient-descent TWD estimator after the +bte sign fix.

Why: the old `twe - bte` bug left a chronic ~2.5-3.3 deg forward-model residual
even at convergence, which kept the helper's eps-adaptation (`res_error > 1`)
permanently elevated. With the corrected model the residual ~0, so eps decays
to its baseline and the estimator becomes MORE damped / slower to track real
wind shifts. This sweeps `epsilon` and `tws_mult` to restore responsiveness.

Metrics, pooled across sessions, vs the trusted vector-state RTS estimator
(`offline_ground_wind_adaptive`, "groundref" -- correct formulation, zero-phase):
  - shift_rms  : circular RMS(prod - groundref) in SHIFT windows (|d twd/dt| high)
                 -> tracking / lag (the thing the bug-removal hurt). LOWER better.
  - all_rms    : circular RMS over all valid samples.
  - steady_std : reference-free within-leg TWD std on steady legs. Want LOW
                 (don't trade lag fix for noise).
Pick the epsilon at the knee: lowest shift_rms before steady_std inflates.

Run:
    conda run -n sailing --no-capture-output python Analyze/retune_true_wind.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import argparse, glob, logging
import numpy as np

from Libraries.global_variables import G
from Libraries import race_logs
from Libraries.log_helper import LOG
from Analyze.true_wind_estimator import (
    AdaptiveFilterConfig, Calibration, offline_ground_wind_adaptive)
from Analyze.calibrate_ground_ref import _clean_speed_glitches
import pandas as pd
import json

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()
FS = float(G.SAMPLES_PER_SECOND)
HUBER_ONLY = AdaptiveFilterConfig(alpha_R_turn=0.0, alpha_R_accel=0.0, q_growth=1.0)
GR_CAL = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'calibration_ground_ref_2026.json')


def w180(a):
    return ((np.asarray(a, float) + 180.0) % 360.0) - 180.0


def circ_rms(err):
    e = w180(err); e = e[np.isfinite(e)]
    return float(np.sqrt(np.mean(e ** 2))) if e.size else np.nan


def prod_twd(df, epsilon, tws_mult, time_delay):
    """Production-style TWD with the (fixed) helper at given params."""
    var = G.MAGNETIC_VARIATION
    twd, *_ = race_logs.estimate_true_wind_helper(
        epsilon, np.asarray(df.caws), np.asarray(df.cawa), np.asarray(df.rhdg),
        np.asarray(df.rspd), np.asarray(df.rcog), np.asarray(df.rsog),
        float(df.aws.iloc[0]),
        float(df.rawa.iloc[0] + df.rhdg.iloc[0] + var),
        var, float(tws_mult), int(time_delay))
    return twd % 360.0


def groundref_twd(df, cal):
    sog_clean, _ = _clean_speed_glitches(df['rsog'].to_numpy())
    est = pd.DataFrame({
        'awa': df['rawa'].to_numpy(float),
        'aws': df['raws'].to_numpy(float) * G.MS_2_KNOTS,
        'hdg': (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0,
        'cog': df['rcog'].to_numpy(float),
        'sog': sog_clean * G.MS_2_KNOTS})
    return offline_ground_wind_adaptive(est, fs_hz=FS, cal=cal,
                                        adaptive_cfg=HUBER_ONLY)['twd'].to_numpy(float)


def steady_leg_std(twd, hdg_true):
    rate = np.abs(np.gradient(np.unwrap(np.deg2rad(hdg_true))) * FS * 180 / np.pi)
    steady = rate < 1.0
    ml = int(30 * FS)
    stds, i, n = [], 0, len(twd)
    while i < n:
        if steady[i]:
            j = i
            while j < n and steady[j]:
                j += 1
            if j - i >= ml:
                e = w180(twd[i:j] - np.rad2deg(np.arctan2(
                    np.mean(np.sin(np.deg2rad(twd[i:j]))),
                    np.mean(np.cos(np.deg2rad(twd[i:j]))))))
                stds.append(np.std(e))
            i = j
        else:
            i += 1
    return float(np.median(stds)) if stds else np.nan


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--epsilons', default='5e-5,1e-4,2e-4,4e-4,8e-4')
    p.add_argument('--tws-mults', default='16,30')
    p.add_argument('--time-delay', type=int, default=12)
    args = p.parse_args()

    with open(GR_CAL) as f:
        cal = Calibration(**json.load(f)['calibration'])
    epsilons = [float(x) for x in args.epsilons.split(',')]
    tws_mults = [float(x) for x in args.tws_mults.split(',')]

    paths = sorted(glob.glob('/Users/viola/Data/SailingData/PandasLogs/2026-*.parquet'))
    if args.limit:
        paths = paths[:args.limit]

    # Precompute per-session: groundref twd, shift mask, hdg_true.
    sessions = []
    for path in paths:
        try:
            df = race_logs.read_log_file(os.path.basename(path), skip_dock_only=True,
                discard_columns=False, rename_columns=True, trim=True,
                process=True, path=os.path.dirname(path))
        except Exception:
            continue
        if df is None or 'cawa' not in df.columns:
            continue
        df = df.reset_index(drop=True)
        gr = groundref_twd(df, cal)
        hdg_true = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0
        # shift windows: high rate-of-change of groundref twd, but NOT during
        # big heading turns (exclude maneuvers via heading rate).
        gr_rate = np.abs(w180(np.diff(gr, prepend=gr[0]))) * FS  # deg/s
        hdg_rate = np.abs(np.gradient(np.unwrap(np.deg2rad(hdg_true))) * FS * 180 / np.pi)
        shift = (gr_rate > 0.5) & (hdg_rate < 3.0)
        sessions.append({'df': df, 'gr': gr, 'hdg_true': hdg_true, 'shift': shift})
    print(f"[retune] {len(sessions)} sessions; groundref reference ready", flush=True)

    def xcorr_lag_s(a, b, max_lag_s=15.0):
        """Lag (s) maximizing corr of a vs b; +ve => a lags b. Uses detrended
        signed angle series."""
        a = w180(a - np.rad2deg(np.arctan2(np.nanmean(np.sin(np.deg2rad(a))),
                                           np.nanmean(np.cos(np.deg2rad(a))))))
        b = w180(b - np.rad2deg(np.arctan2(np.nanmean(np.sin(np.deg2rad(b))),
                                           np.nanmean(np.cos(np.deg2rad(b))))))
        m = np.isfinite(a) & np.isfinite(b)
        a, b = a[m], b[m]
        if a.size < int(60 * FS):
            return np.nan
        ml = int(max_lag_s * FS)
        best, blag = -2, 0
        for lag in range(-ml, ml + 1, max(1, int(0.2 * FS))):
            if lag >= 0:
                x, y = a[lag:], b[:a.size - lag]
            else:
                x, y = a[:a.size + lag], b[-lag:]
            if x.size < int(60 * FS):
                continue
            c = np.corrcoef(x, y)[0, 1]
            if c > best:
                best, blag = c, lag
        return blag / FS

    print(f"\n  {'epsilon':>8s} {'tws_mult':>8s} | {'shift_rms':>9s} "
          f"{'all_rms':>8s} {'steady_std':>10s} {'lag_s':>7s}", flush=True)
    results = []
    for tm in tws_mults:
        for eps in epsilons:
            srms, arms, sstd, lags = [], [], [], []
            for s in sessions:
                twd = prod_twd(s['df'], eps, tm, args.time_delay)
                n = min(len(twd), len(s['gr']))
                err = w180(twd[:n] - s['gr'][:n])
                arms.append(circ_rms(err))
                sh = s['shift'][:n]
                if sh.sum() > 50:
                    srms.append(circ_rms(err[sh]))
                sstd.append(steady_leg_std(twd, s['hdg_true']))
                lags.append(xcorr_lag_s(twd[:n], s['gr'][:n]))
            row = {'epsilon': eps, 'tws_mult': tm,
                   'shift_rms': float(np.nanmedian(srms)),
                   'all_rms': float(np.nanmedian(arms)),
                   'steady_std': float(np.nanmedian(sstd)),
                   'lag_s': float(np.nanmedian(lags))}
            results.append(row)
            print(f"  {eps:8.0e} {tm:8.0f} | {row['shift_rms']:9.2f} "
                  f"{row['all_rms']:8.2f} {row['steady_std']:10.2f} "
                  f"{row['lag_s']:7.1f}", flush=True)

    # Tactical TWD must stay responsive: cap lag, then maximise agreement with
    # the gold-standard groundref (lowest all_rms). steady_std is a tiebreaker.
    LAG_CAP_S = 6.0
    feasible = [r for r in results if np.isfinite(r['lag_s'])
                and r['lag_s'] <= LAG_CAP_S]
    if feasible:
        rec = min(feasible, key=lambda r: (r['all_rms'], r['steady_std']))
        print(f"\n[retune] responsive (lag<= {LAG_CAP_S:.0f}s) pick: "
              f"epsilon={rec['epsilon']:.0e} tws_mult={rec['tws_mult']:.0f} "
              f"(lag={rec['lag_s']:.1f}s, all_rms={rec['all_rms']:.2f}, "
              f"steady={rec['steady_std']:.2f})", flush=True)
    # Smooth fallback (stwd channel): minimise steady_std subject to lag<=12s.
    smooth = [r for r in results if np.isfinite(r['lag_s']) and r['lag_s'] <= 12.0]
    if smooth:
        srec = min(smooth, key=lambda r: r['steady_std'])
        print(f"[retune] smooth (lag<=12s) pick: epsilon={srec['epsilon']:.0e} "
              f"tws_mult={srec['tws_mult']:.0f} (lag={srec['lag_s']:.1f}s, "
              f"steady={srec['steady_std']:.2f})", flush=True)
    print("[retune] current default: eps=1e-4 tws_mult=16 (twd), "
          "eps=1e-5 tws_mult=16 (stwd)", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
