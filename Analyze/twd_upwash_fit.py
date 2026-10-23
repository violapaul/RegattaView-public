#!/usr/bin/env python
"""Fit an upwash AWA correction to null the settled tack ODD bias.

Physical chain at the masthead:  true --(upwash, aero, over-reads |AWA|)-->
--(heel-tilt, geometry, under-reads |AWA| by cos phi)--> measured.
So to recover true we undo heel-tilt (known geometry) then undo upwash (fit):

    AWA_1   = atan2(sin AWA_m, cos AWA_m * cos phi)        # un-tilt (|AWA| up)
    AWA_use = k * AWA_1     for |AWA_1| < 90 (upwind only)  # un-upwash (|AWA| down)

k<1 is the upwash reduction. We fit k to drive the SETTLED odd component of
ΔTWD across tacks (median over [+15,+40] s, antisymmetric in tack direction)
to zero, using the best estimator (groundref RTS + ground-ref calibration).
Upwash is applied upwind only so downwind TWD is untouched.

Run:
    conda run -n sailing --no-capture-output python Analyze/twd_upwash_fit.py
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
from Analyze.twd_maneuver_diag import collect_by_direction, WIN_S
from Analyze.twd_maneuver_corrections import heel_tilt_correct

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()
FS = float(G.SAMPLES_PER_SECOND)
H = int(WIN_S * FS)
HUBER_ONLY = AdaptiveFilterConfig(alpha_R_turn=0.0, alpha_R_accel=0.0, q_growth=1.0)
HERE = Path(__file__).resolve().parent
GR_CAL = HERE / 'calibration_ground_ref_2026.json'
OUT_DIR = HERE / 'Plots' / 'twd_maneuver'


def gr_twd(cache, cal, s_upwind, use_heel):
    """TWD with total upwind AWA scale s_upwind (the upwash lever, applied by
    the estimator's corrected_awa_aws). If use_heel, undo the heel-tilt
    geometry on the raw AWA/AWS first (then the estimator applies s_upwind)."""
    awa, aws = cache['rawa'], cache['raws_kt']
    if use_heel:
        awa, aws = heel_tilt_correct(awa, aws, cache['heel'])
    cal_s = replace(cal, awa_scale_upwind=float(s_upwind))
    est = pd.DataFrame({'awa': awa, 'aws': aws, 'hdg': cache['hdg'],
                        'cog': cache['cog'], 'sog': cache['sog']})
    return offline_ground_wind_adaptive(est, fs_hz=FS, cal=cal_s,
                                        adaptive_cfg=HUBER_ONLY)['twd'].to_numpy(float)


def settled_odd(cache_list, cal, s_upwind, use_heel):
    """Return (odd_trace, settled_odd_net) pooled over all sessions."""
    acc = {'p2s': ([], []), 's2p': ([], [])}
    for c in cache_list:
        twd = gr_twd(c, cal, s_upwind, use_heel)
        bd = collect_by_direction(twd, c['rawa'], c['man'], 'tack')
        for kk in ('p2s', 's2p'):
            acc[kk][0].extend(bd[kk][0]); acc[kk][1].extend(bd[kk][1])

    def med_tr(tr):
        return np.nanmedian(np.vstack([t for t in tr if len(t) == 2 * H + 1]), axis=0)
    odd_t = 0.5 * (med_tr(acc['p2s'][0]) - med_tr(acc['s2p'][0]))
    dp = np.median([x for x in acc['p2s'][1] if np.isfinite(x)])
    ds = np.median([x for x in acc['s2p'][1] if np.isfinite(x)])
    return odd_t, 0.5 * (dp - ds)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(GR_CAL) as f:
        cal = Calibration(**json.load(f)['calibration'])
    with open(HERE / 'leeway_model.json') as f:
        heel_off = float(json.load(f)['calibration']['heel_offset_deg'])

    # Cache every session once (loading dominates; estimator is cheap to re-run).
    cache_list = []
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
        hdg_true = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0
        rawa = df['rawa'].to_numpy(float)
        cache_list.append({
            'rawa': rawa,
            'raws_kt': df['raws'].to_numpy(float) * G.MS_2_KNOTS,
            'heel': df['zg100_roll'].to_numpy(float) - heel_off,
            'hdg': hdg_true, 'cog': df['rcog'].to_numpy(float),
            'sog': sog_clean * G.MS_2_KNOTS,
            'man': detect_maneuvers(rawa, hdg_true, FS)})
    print(f"[upwash] cached {len(cache_list)} sessions", flush=True)

    t_axis = (np.arange(2 * H + 1) - H) / FS
    s_now = float(cal.awa_scale_upwind)   # constancy-fit value (~0.93)

    def fit_null(use_heel, grid):
        """Sweep upwind AWA scale; return (s_grid, odd_grid, s_star)."""
        odds = []
        for s in grid:
            _, o = settled_odd(cache_list, cal, s, use_heel)
            odds.append(o)
            print(f"  awa_scale_up={s:.3f} (heel={'ON ' if use_heel else 'off'}): "
                  f"settled odd={o:+.2f}°", flush=True)
        g = np.array(grid); od = np.array(odds)
        order = np.argsort(od)               # np.interp needs increasing x
        if od.min() < 0 < od.max():
            sstar = float(np.interp(0.0, od[order], g[order]))
        else:
            sstar = float(g[np.argmin(np.abs(od))])
        return g, od, sstar

    # Primary: pure upwash (AWA-scale) fit, no extra heel-tilt geometry.
    print(f"[upwash] constancy-fit upwind AWA scale = {s_now:.3f}; "
          f"sweeping to null the settled tack odd:", flush=True)
    grid = [0.93, 0.90, 0.88, 0.86, 0.84]
    g, od, sstar = fit_null(False, grid)
    print(f"[upwash] s* (upwash only) that nulls settled odd = {sstar:.3f} "
          f"(~{100*(1-sstar):.0f}% total upwind AWA reduction; "
          f"constancy fit had {s_now:.3f})", flush=True)

    odd_t_now, odd_now = settled_odd(cache_list, cal, s_now, False)
    odd_t_fit, odd_fit = settled_odd(cache_list, cal, sstar, False)
    print(f"[upwash] settled odd: current({s_now:.2f})={odd_now:+.2f}° -> "
          f"fit({sstar:.2f})={odd_fit:+.2f}°", flush=True)

    fig, axs = plt.subplots(1, 2, figsize=(12, 4.4), dpi=120)
    axs[0].plot(g, od, 'o-', color='tab:purple')
    axs[0].axhline(0, color='0.5', lw=0.6)
    axs[0].axvline(sstar, color='tab:green', ls='--', label=f's* = {sstar:.3f}')
    axs[0].axvline(s_now, color='tab:red', ls=':', label=f'constancy fit = {s_now:.3f}')
    axs[0].set_xlabel('upwind AWA scale (upwash lever)')
    axs[0].set_ylabel('settled odd ΔTWD (deg)')
    axs[0].set_title('Fit: upwind AWA scale that nulls the settled tack odd')
    axs[0].invert_xaxis(); axs[0].legend(fontsize=8); axs[0].grid(alpha=0.3)

    axs[1].plot(t_axis, odd_t_now, color='tab:red', lw=1.6,
                label=f'current AWA scale {s_now:.2f}  (settled {odd_now:+.1f}°)')
    axs[1].plot(t_axis, odd_t_fit, color='tab:blue', lw=2.0,
                label=f'upwash fit {sstar:.2f}  (settled {odd_fit:+.1f}°)')
    axs[1].axhline(0, color='0.5', lw=0.6); axs[1].axvline(0, color='0.5', lw=0.6)
    axs[1].set_xlabel('time from tack (s)')
    axs[1].set_ylabel('odd component of ΔTWD (deg)')
    axs[1].set_title('Odd component vs time — upwash fit nulls the settled bias')
    axs[1].legend(fontsize=8); axs[1].grid(alpha=0.3)
    fig.suptitle('Upwash (upwind AWA-scale) fit to null the tack-flipping odd '
                 'TWD bias', fontsize=11)
    fig.tight_layout()
    fig.savefig(OUT_DIR / 'twd_upwash_fit.png')
    plt.close(fig)
    print(f"[upwash] wrote {OUT_DIR}/twd_upwash_fit.png", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
