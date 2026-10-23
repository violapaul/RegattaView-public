#!/usr/bin/env python
"""Two tests on the tack ODD (tack-flipping) TWD bias.

Test 1 (gps_delay): the near-t=0 transient should be timing-driven -> it scales
        with gps_delay, while the settled (+45 s) odd bias should not.
Test 2 (heel-tilt AWA/AWS correction): the masthead vane sits in a plane heeled
        by phi, so it under-reads the athwartship wind component by cos(phi):
            AWA_true = atan2(sin AWA_m, cos AWA_m * cos phi)
            AWS_true = AWS_m * sqrt(cos^2 AWA_m + sin^2 AWA_m / cos^2 phi)
        cos(phi) is EVEN in heel, but Delta-AWA carries sign(AWA) => the
        correction is ODD in tack. If the settled odd bias is this vane tilt,
        feeding heel-corrected AWA/AWS to the estimator should collapse it.

The "odd" trace plotted is O(t) = 0.5*(median p2s trace - median s2p trace),
the antisymmetric (tack-flipping) part vs time-from-tack.

Run:
    conda run -n sailing --no-capture-output python Analyze/twd_maneuver_corrections.py
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
from Analyze.twd_maneuver_diag import (
    collect_by_direction, w180, circmean, WIN_S, BASE, POST)

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()
FS = float(G.SAMPLES_PER_SECOND)
H = int(WIN_S * FS)
HUBER_ONLY = AdaptiveFilterConfig(alpha_R_turn=0.0, alpha_R_accel=0.0, q_growth=1.0)
HERE = Path(__file__).resolve().parent
GR_CAL = HERE / 'calibration_ground_ref_2026.json'
OUT_DIR = HERE / 'Plots' / 'twd_maneuver'


def heel_tilt_correct(awa_deg, aws_kt, heel_deg):
    """Recover horizontal apparent wind from the heel-tilted masthead reading."""
    awa = np.deg2rad(np.asarray(awa_deg, float))
    cphi = np.cos(np.deg2rad(np.asarray(heel_deg, float)))
    cphi = np.clip(cphi, 0.3, 1.0)             # guard (heel < ~72 deg)
    awa_true = np.degrees(np.arctan2(np.sin(awa), np.cos(awa) * cphi))
    aws_true = np.asarray(aws_kt, float) * np.sqrt(
        np.cos(awa) ** 2 + (np.sin(awa) / cphi) ** 2)
    return awa_true, aws_true


def build_est(df, heel_off, heel_correct=False):
    sog_clean, _ = _clean_speed_glitches(df['rsog'].to_numpy())
    awa = df['rawa'].to_numpy(float)
    aws = df['raws'].to_numpy(float) * G.MS_2_KNOTS
    if heel_correct:
        heel = df['zg100_roll'].to_numpy(float) - heel_off   # calibrated heel deg
        awa, aws = heel_tilt_correct(awa, aws, heel)
    return pd.DataFrame({
        'awa': awa, 'aws': aws,
        'hdg': (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0,
        'cog': df['rcog'].to_numpy(float),
        'sog': sog_clean * G.MS_2_KNOTS})


def gr_twd(df, cal, heel_off, gps_delay=None, heel_correct=False):
    if gps_delay is not None:
        cal = replace(cal, gps_delay_s=float(gps_delay))
    est = build_est(df, heel_off, heel_correct)
    return offline_ground_wind_adaptive(est, fs_hz=FS, cal=cal,
                                        adaptive_cfg=HUBER_ONLY)['twd'].to_numpy(float)


def odd_trace_and_net(per_session_bydir):
    """Given accumulated {'p2s':(traces,deltas),'s2p':(...)}, return
    (t_axis, odd_trace, even_net, odd_net)."""
    def med_trace(traces):
        M = np.vstack([t for t in traces if len(t) == 2 * H + 1])
        return np.nanmedian(M, axis=0)
    mp = med_trace(per_session_bydir['p2s'][0])
    ms = med_trace(per_session_bydir['s2p'][0])
    odd_t = 0.5 * (mp - ms)
    dp = np.median([x for x in per_session_bydir['p2s'][1] if np.isfinite(x)])
    ds = np.median([x for x in per_session_bydir['s2p'][1] if np.isfinite(x)])
    return (np.arange(2 * H + 1) - H) / FS, odd_t, 0.5 * (dp + ds), 0.5 * (dp - ds)


def accumulate(cfg_fn, paths, heel_off, cal):
    """Run a TWD config over all sessions; return tack by-direction accumulator."""
    acc = {'p2s': ([], []), 's2p': ([], [])}
    for path in paths:
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
        twd = cfg_fn(df, cal, heel_off)
        hdg_true = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0
        man = detect_maneuvers(df['rawa'].to_numpy(float), hdg_true, FS)
        bd = collect_by_direction(twd, df['rawa'].to_numpy(float), man, 'tack')
        for k in ('p2s', 's2p'):
            acc[k][0].extend(bd[k][0]); acc[k][1].extend(bd[k][1])
    return acc


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(GR_CAL) as f:
        cal = Calibration(**json.load(f)['calibration'])
    # heel offset from the physical bundle (calibrated heel = roll - heel_off).
    with open(HERE / 'leeway_model.json') as f:
        heel_off = float(json.load(f)['calibration']['heel_offset_deg'])
    paths = sorted(glob.glob('/Users/viola/Data/SailingData/PandasLogs/2026-*.parquet'))

    # ---- Test 1: gps_delay sweep (no heel correction) ----
    fig, ax = plt.subplots(figsize=(8, 4.4), dpi=120)
    print("[corr] Test 1 — gps_delay sweep (tack odd component):", flush=True)
    for gd, col in ((0.0, 'tab:green'), (1.0, 'tab:orange'), (2.15, 'tab:red')):
        acc = accumulate(lambda d, c, h, _gd=gd: gr_twd(d, c, h, gps_delay=_gd),
                         paths, heel_off, cal)
        t, odd_t, even_net, odd_net = odd_trace_and_net(acc)
        ax.plot(t, odd_t, color=col, lw=1.6,
                label=f'gps_delay={gd:.2f}s  (settled odd={odd_net:+.1f}°)')
        print(f"  gps_delay={gd:.2f}s: even={even_net:+.2f}° odd={odd_net:+.2f}°",
              flush=True)
    ax.axhline(0, color='0.5', lw=0.6); ax.axvline(0, color='0.5', lw=0.6)
    ax.set_xlabel('time from tack (s)'); ax.set_ylabel('odd component of ΔTWD (deg)')
    ax.set_title('Test 1: gps_delay moves the t≈0 transient, not the +45s bias')
    ax.legend(fontsize=8); ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(OUT_DIR / 'twd_corr_gpsdelay.png')
    plt.close(fig)
    print(f"[corr] wrote {OUT_DIR}/twd_corr_gpsdelay.png\n", flush=True)

    # ---- Test 2: heel-tilt AWA/AWS correction (at cal gps_delay) ----
    fig, ax = plt.subplots(figsize=(8, 4.4), dpi=120)
    print("[corr] Test 2 — heel-tilt AWA/AWS correction (tack odd component):",
          flush=True)
    for hc, col, lbl in ((False, 'tab:red', 'raw AWA (current)'),
                         (True, 'tab:blue', 'heel-tilt corrected AWA/AWS')):
        acc = accumulate(lambda d, c, h, _hc=hc: gr_twd(d, c, h, heel_correct=_hc),
                         paths, heel_off, cal)
        t, odd_t, even_net, odd_net = odd_trace_and_net(acc)
        ax.plot(t, odd_t, color=col, lw=1.8, label=f'{lbl}  (settled odd={odd_net:+.1f}°)')
        print(f"  heel_correct={hc}: even={even_net:+.2f}° odd={odd_net:+.2f}°",
              flush=True)
    ax.axhline(0, color='0.5', lw=0.6); ax.axvline(0, color='0.5', lw=0.6)
    ax.set_xlabel('time from tack (s)'); ax.set_ylabel('odd component of ΔTWD (deg)')
    ax.set_title('Test 2: heel-tilt correction vs the settled odd bias')
    ax.legend(fontsize=8); ax.grid(alpha=0.3)
    fig.tight_layout(); fig.savefig(OUT_DIR / 'twd_corr_heel.png')
    plt.close(fig)
    print(f"[corr] wrote {OUT_DIR}/twd_corr_heel.png", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
