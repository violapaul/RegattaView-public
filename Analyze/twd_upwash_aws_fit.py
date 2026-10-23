#!/usr/bin/env python
"""Fit the loading-dependent masthead correction: cos(phi) heel-tilt (exact,
no free param) + additive upwash Delta-beta(AWS)=max(0, a - b*AWS) upwind, and
verify it flattens the tack ODD bias across wind speed (which a constant scale
cannot).

Design:
 - Estimator AWA scale/offset are ZEROED (awa_scale=1, awa_offset=0) so the
   fitted Delta-beta IS the physical upwash (heel geometry removed separately).
 - heading_offset and gps_delay kept from the ground-ref calibration.
 - Fit by local linearization: measure per-AWS-bin odd at Delta-beta=0 (O0) and
   the sensitivity k = d(odd)/d(Delta-beta) from a uniform-2deg probe, solve the
   required upwash per bin, fit the line a - b*AWS, then VERIFY with a live run.

Run:
    conda run -n sailing --no-capture-output python Analyze/twd_upwash_aws_fit.py
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
from Analyze.twd_maneuver_diag import w180, circmean, BASE, POST
from Analyze.retune_true_wind import steady_leg_std

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()
FS = float(G.SAMPLES_PER_SECOND)
HUBER_ONLY = AdaptiveFilterConfig(alpha_R_turn=0.0, alpha_R_accel=0.0, q_growth=1.0)
HERE = Path(__file__).resolve().parent
GR_CAL = HERE / 'calibration_ground_ref_2026.json'
OUT_DIR = HERE / 'Plots' / 'twd_maneuver'
BINS = [0, 12, 15, 18, 40]


def vane_correct(rawa, raws_kt, heel, a, b, use_heel=True):
    awa1, aws1 = (heel_tilt_correct(rawa, raws_kt, heel) if use_heel
                  else (np.asarray(rawa, float), np.asarray(raws_kt, float)))
    dbeta = np.maximum(0.0, a - b * aws1)          # upwash angle, deg
    up = np.abs(awa1) < 90.0                        # upwind only
    awa2 = awa1.copy()
    awa2[up] = awa1[up] - dbeta[up] * np.sign(awa1[up])
    return awa2, aws1


def run_collect(cache_list, cal0, a, b, use_heel=True):
    """Return (recs DataFrame: dir/delta/aws, steady_std median)."""
    recs, sstd = [], []
    for c in cache_list:
        awa, aws = vane_correct(c['rawa'], c['raws_kt'], c['heel'], a, b, use_heel)
        est = pd.DataFrame({'awa': awa, 'aws': aws, 'hdg': c['hdg'],
                            'cog': c['cog'], 'sog': c['sog']})
        twd = offline_ground_wind_adaptive(est, fs_hz=FS, cal=cal0,
                                           adaptive_cfg=HUBER_ONLY)['twd'].to_numpy(float)
        sstd.append(steady_leg_std(twd, c['hdg']))
        for cc, k in c['man']:
            if k != 'tack':
                continue
            b0, b1 = cc - int(BASE[0]*FS), cc - int(BASE[1]*FS)
            p0, p1 = cc + int(POST[0]*FS), cc + int(POST[1]*FS)
            if b0 < 0 or p1 > len(twd):
                continue
            base = circmean(twd[b0:b1]); post = circmean(twd[p0:p1])
            pre_awa = np.nanmedian(c['rawa'][b0:b1])
            paws = np.nanmedian(c['raws_kt'][p0:p1])
            if not all(np.isfinite([base, post, pre_awa, paws])):
                continue
            recs.append({'dir': 's2p' if pre_awa > 0 else 'p2s',
                         'delta': w180(post - base), 'aws': float(paws)})
    return pd.DataFrame(recs), float(np.nanmedian(sstd))


def per_bin_odd(R):
    """Return list of dicts: {lo,hi,aws,odd,n} per AWS bin."""
    out = []
    for lo, hi in zip(BINS[:-1], BINS[1:]):
        s = R[(R.aws >= lo) & (R.aws < hi)]
        dp = s[s.dir == 'p2s'].delta; ds = s[s.dir == 's2p'].delta
        odd = (0.5 * (dp.median() - ds.median())
               if len(dp) >= 5 and len(ds) >= 5 else np.nan)
        out.append({'lo': lo, 'hi': hi,
                    'aws': float(s.aws.median()) if len(s) else (lo+hi)/2,
                    'odd': float(odd), 'n': len(s)})
    return out


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    with open(GR_CAL) as f:
        cal = Calibration(**json.load(f)['calibration'])
    with open(HERE / 'leeway_model.json') as f:
        heel_off = float(json.load(f)['calibration']['heel_offset_deg'])
    # cal0: zero the estimator's AWA scale/offset; keep heading + gps_delay.
    cal0 = replace(cal, awa_scale_upwind=1.0, awa_scale_downwind=1.0,
                   awa_offset_deg=0.0)

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
        rawa = df['rawa'].to_numpy(float)
        hdg_true = (df['rhdg'].to_numpy(float) + G.MAGNETIC_VARIATION) % 360.0
        cache_list.append({
            'rawa': rawa, 'raws_kt': df['raws'].to_numpy(float) * G.MS_2_KNOTS,
            'heel': df['zg100_roll'].to_numpy(float) - heel_off,
            'hdg': hdg_true, 'cog': df['rcog'].to_numpy(float),
            'sog': sog_clean * G.MS_2_KNOTS, 'man': detect_maneuvers(rawa, hdg_true, FS)})
    print(f"[fit] cached {len(cache_list)} sessions", flush=True)

    # Reference: current shipped calibration (for the steady-std sanity check).
    _, sstd_cur = run_collect(cache_list, cal, 0.0, 0.0, use_heel=False)

    # O0: heel ON, no upwash.   O2: heel ON, uniform 2deg upwash (sensitivity).
    R0, sstd0 = run_collect(cache_list, cal0, 0.0, 0.0, use_heel=True)
    R2, _ = run_collect(cache_list, cal0, 2.0, 0.0, use_heel=True)
    B0, B2 = per_bin_odd(R0), per_bin_odd(R2)
    print("[fit] per-bin odd (heel ON): Delta-beta=0 -> 2deg uniform", flush=True)
    ks = []
    for d0, d2 in zip(B0, B2):
        if np.isfinite(d0['odd']) and np.isfinite(d2['odd']):
            ks.append((d0['odd'] - d2['odd']) / 2.0)
        print(f"   AWS {d0['lo']:2d}-{d0['hi']:2d} (med {d0['aws']:4.1f}): "
              f"O0={d0['odd']:+.2f}  O2={d2['odd']:+.2f}  n={d0['n']}", flush=True)
    k = float(np.median(ks))
    print(f"[fit] sensitivity k = d(odd)/d(Delta-beta) = {k:.2f} deg TWD per deg AWA",
          flush=True)

    # Required upwash per bin and the line fit a - b*AWS (weighted).
    aws_pts = np.array([d['aws'] for d in B0 if np.isfinite(d['odd'])])
    req = np.array([d['odd'] for d in B0 if np.isfinite(d['odd'])]) / k
    wts = np.sqrt([d['n'] for d in B0 if np.isfinite(d['odd'])])
    slope, intercept = np.polyfit(aws_pts, req, 1, w=wts)
    a_fit, b_fit = float(intercept), float(-slope)
    print(f"[fit] Delta-beta(AWS) = max(0, {a_fit:.2f} - {b_fit:.3f}*AWS)  deg",
          flush=True)
    for v in (8, 10, 12, 15, 18, 22):
        print(f"     AWS={v:2d} kt -> Delta-beta={max(0.0, a_fit-b_fit*v):.2f} deg",
              flush=True)

    # Verify live.
    Rv, sstdv = run_collect(cache_list, cal0, a_fit, b_fit, use_heel=True)
    Bv = per_bin_odd(Rv)
    print("[fit] VERIFY per-bin odd after Delta-beta(AWS):", flush=True)
    for d0, dv in zip(B0, Bv):
        print(f"   AWS {d0['lo']:2d}-{d0['hi']:2d}: {d0['odd']:+.2f} -> "
              f"{dv['odd']:+.2f}  (n={dv['n']})", flush=True)
    pooled = lambda B: np.nanmedian([d['odd'] for d in B if np.isfinite(d['odd'])])
    print(f"[fit] pooled |odd| across bins: {np.nanmean(np.abs([d['odd'] for d in B0])):.2f} "
          f"-> {np.nanmean(np.abs([d['odd'] for d in Bv])):.2f}", flush=True)
    print(f"[fit] steady-leg std: current cal={sstd_cur:.2f}  heel-only={sstd0:.2f}  "
          f"heel+upwash(fit)={sstdv:.2f}  (want <= current)", flush=True)

    # Plot.
    fig, axs = plt.subplots(1, 2, figsize=(12, 4.4), dpi=120)
    aws_b = [d['aws'] for d in B0]
    axs[0].plot(aws_b, [d['odd'] for d in B0], 'o-', color='tab:red',
                label='heel only (no upwash)')
    axs[0].plot(aws_b, [d['odd'] for d in Bv], 's-', color='tab:blue',
                label='heel + upwash Δβ(AWS)')
    axs[0].axhline(0, color='0.5', lw=0.6)
    axs[0].set_xlabel('post-tack AWS (kt)'); axs[0].set_ylabel('settled odd ΔTWD (deg)')
    axs[0].set_title('Odd bias vs AWS: flattened to ~0 across the range')
    axs[0].legend(fontsize=8); axs[0].grid(alpha=0.3)
    xs = np.linspace(6, 24, 100)
    axs[1].plot(xs, np.maximum(0, a_fit - b_fit * xs), color='tab:green', lw=2)
    axs[1].set_xlabel('AWS (kt)'); axs[1].set_ylabel('upwash Δβ (deg of AWA)')
    axs[1].set_title(f'Fitted upwash: Δβ = max(0, {a_fit:.1f} − {b_fit:.2f}·AWS)')
    axs[1].grid(alpha=0.3)
    fig.suptitle('Two-step masthead correction: cos φ heel-tilt + loading-dependent '
                 'upwash', fontsize=11)
    fig.tight_layout(); fig.savefig(OUT_DIR / 'twd_upwash_aws_fit.png')
    plt.close(fig)
    print(f"[fit] wrote {OUT_DIR}/twd_upwash_aws_fit.png", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
