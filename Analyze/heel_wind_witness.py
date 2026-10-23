#!/usr/bin/env python
"""Heel as an independent witness of wind FORCE (AWS) -- diagnostic.

Physics: in quasi-steady sailing the aerodynamic heeling moment balances
the hydrostatic righting moment, so

    sin(heel) = c * AWS^2 * f(AWA)

with ``c`` lumping rig/sail area and the boat's righting stiffness, and
``f(AWA)`` the sail-force coefficient vs apparent angle. Heel is measured
by the ZG100 inclinometer, *independently* of the masthead anemometer, so a
mismatch between the heel-implied AWS and the measured AWS points at an
anemometer bias.

HONEST LIMITATION -- what this can and cannot do:
  * It CANNOT give an absolute AWS scale: ``c`` is degenerate with a
    multiplicative AWS error (both are slopes of sin(heel) vs AWS^2). So a
    constant under/over-read of the vane is absorbed into ``c``.
  * It CAN test the force law exponent (is sin(heel) ~ AWS^2? fit ``n`` in
    sin(heel)=c*AWS^n upwind; n far from 2 flags a problem).
  * It CAN flag per-session / per-point-of-sail OUTLIERS in the heel<->AWS^2
    relationship (a bad-sensor day, or a genuine up/down anemometer bias),
    against a pooled reference fit.
Therefore this tool is a QA / consistency witness and a *soft* anchor for the
upwind/downwind AWS split in ``calibrate_ground_ref.py`` -- not a source of an
absolute calibration number. The up/down split must still earn its keep in
held-out maneuver-shear CV.

Run:
    conda run -n sailing --no-capture-output \
        python Analyze/heel_wind_witness.py
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import datetime as dt
import glob
import json
import logging

import numpy as np

from Libraries.global_variables import G
from Libraries.log_helper import LOG
from Analyze import current_analysis as ca

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()

HERE = os.path.dirname(os.path.abspath(__file__))
LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
GLOB_PATTERN = "2026-*.parquet"
OUT_PATH = os.path.join(HERE, "heel_wind_witness.json")

FS = float(G.SAMPLES_PER_SECOND)
UPWIND_AWA = 70.0     # |AWA| < this -> upwind
DOWNWIND_AWA = 110.0  # |AWA| > this -> downwind
HEADING_RATE_MAX = 3.0  # deg/s; steady-leg gate
SPD_MIN_KT = 2.0
AWS_MIN_KT = 3.0
HEEL_MIN_DEG = 1.0
TACK_GUARD_S = 15.0


def steady_mask(df) -> np.ndarray:
    """Boolean mask of quasi-steady samples: low heading rate, off the dock,
    away from detected maneuvers."""
    hdg = np.unwrap(np.deg2rad(df['hdg_true'].to_numpy(float)))
    rate = np.abs(np.gradient(hdg) * FS * 180.0 / np.pi)  # deg/s
    m = (rate < HEADING_RATE_MAX) & \
        (df['spd_kt'].to_numpy(float) > SPD_MIN_KT) & \
        (df['aws_kt'].to_numpy(float) > AWS_MIN_KT) & \
        (np.abs(df['heel'].to_numpy(float)) > HEEL_MIN_DEG)
    # Exclude a guard window around each tack.
    try:
        tacks = ca.detect_tacks(df)
    except Exception:
        tacks = []
    g = int(round(TACK_GUARD_S * FS))
    for t in tacks:
        m[max(0, t - g):t + g] = False
    return m & np.isfinite(df['heel'].to_numpy(float))


def fit_through_origin(x: np.ndarray, y: np.ndarray) -> float:
    """Least-squares slope c for y = c*x (x = AWS^2, y = sin|heel|)."""
    x = np.asarray(x, float)
    y = np.asarray(y, float)
    denom = float(np.sum(x * x))
    return float(np.sum(x * y) / denom) if denom > 0 else float('nan')


def fit_exponent(aws: np.ndarray, sheel: np.ndarray):
    """Fit log(sin|heel|) = log(c) + n*log(AWS); return (n, c)."""
    good = (aws > 0) & (sheel > 1e-4)
    if good.sum() < 30:
        return float('nan'), float('nan')
    lx = np.log(aws[good])
    ly = np.log(sheel[good])
    n, logc = np.polyfit(lx, ly, 1)
    return float(n), float(np.exp(logc))


def analyze_session(name, path):
    df = ca.load_calibrated_log(name, path=path, calibration='global')
    m = steady_mask(df)
    awa = np.abs(df['awa'].to_numpy(float))
    aws = df['aws_kt'].to_numpy(float)
    sheel = np.sin(np.deg2rad(np.abs(df['heel'].to_numpy(float))))

    res = {'n_steady': int(m.sum())}
    for label, sel in (('up', m & (awa < UPWIND_AWA)),
                       ('down', m & (awa > DOWNWIND_AWA))):
        a = aws[sel]
        s = sheel[sel]
        if sel.sum() < 50:
            res[label] = {'n': int(sel.sum())}
            continue
        c = fit_through_origin(a ** 2, s)
        n, _ = fit_exponent(a, s)
        res[label] = {
            'n': int(sel.sum()),
            'c': c,
            'exponent_n': n,
            'median_aws_kt': float(np.median(a)),
            'median_heel_deg': float(np.median(np.abs(
                df['heel'].to_numpy(float)[sel]))),
        }
        res[label]['_aws2'] = (a ** 2)   # carried for pooled fit
        res[label]['_sheel'] = s
    return res


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--logs-dir', default=LOGS_DIR)
    p.add_argument('--pattern', default=GLOB_PATTERN)
    p.add_argument('--out', default=OUT_PATH)
    p.add_argument('--limit', type=int, default=0)
    args = p.parse_args()

    paths = sorted(glob.glob(os.path.join(args.logs_dir, args.pattern)))
    if args.limit:
        paths = paths[:args.limit]

    sessions = {}
    pooled = {'up': {'aws2': [], 'sheel': []},
              'down': {'aws2': [], 'sheel': []}}
    for path in paths:
        sid = os.path.basename(path).replace('.parquet', '')
        try:
            r = analyze_session(os.path.basename(path), os.path.dirname(path))
        except Exception as exc:
            print(f"  skip {sid}: {type(exc).__name__}: {exc}", flush=True)
            continue
        for lab in ('up', 'down'):
            if '_aws2' in r.get(lab, {}):
                pooled[lab]['aws2'].append(r[lab].pop('_aws2'))
                pooled[lab]['sheel'].append(r[lab].pop('_sheel'))
        sessions[sid] = r
        up = r.get('up', {})
        print(f"  {sid}: steady={r['n_steady']:6d}  "
              f"up n={up.get('n',0):5d} c={up.get('c',float('nan')):.5f} "
              f"n_exp={up.get('exponent_n',float('nan')):.2f}", flush=True)

    # Pooled global fit (upwind is the reliable regime).
    glob_fit = {}
    for lab in ('up', 'down'):
        if pooled[lab]['aws2']:
            a2 = np.concatenate(pooled[lab]['aws2'])
            sh = np.concatenate(pooled[lab]['sheel'])
            c = fit_through_origin(a2, sh)
            n, _ = fit_exponent(np.sqrt(a2), sh)
            glob_fit[lab] = {'c': c, 'exponent_n': n, 'n_samples': int(len(a2))}

    # Per-session AWS ratio vs the pooled upwind fit: implied AWS uses the
    # global c, so the ratio measured/implied reveals which sessions read
    # high/low *relative* to the corpus (c-degeneracy is shared, so this is
    # a relative, not absolute, bias).
    c_up = glob_fit.get('up', {}).get('c', float('nan'))
    for sid, r in sessions.items():
        up = r.get('up', {})
        if up.get('n', 0) >= 50 and np.isfinite(c_up) and c_up > 0:
            # median over the session of measured/implied
            # implied_aws = sqrt(sin|heel| / c_up); ratio = meas/implied
            # Using per-session medians of c gives ratio = sqrt(c_up / c_sess).
            c_sess = up.get('c', float('nan'))
            up['aws_bias_vs_corpus'] = (float(np.sqrt(c_sess / c_up))
                                        if c_sess > 0 else float('nan'))

    ratios = [r['up']['aws_bias_vs_corpus'] for r in sessions.values()
              if 'up' in r and 'aws_bias_vs_corpus' in r['up']
              and np.isfinite(r['up']['aws_bias_vs_corpus'])]

    payload = {
        'computed_at_utc': dt.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'model': 'sin(heel) = c * AWS_kt^2 * f(AWA); f=1 per point-of-sail bin',
        'caveat': ('c is degenerate with a multiplicative AWS error; this is a '
                   'RELATIVE/consistency witness, not an absolute AWS scale.'),
        'gates': {'heading_rate_max_dps': HEADING_RATE_MAX,
                  'spd_min_kt': SPD_MIN_KT, 'aws_min_kt': AWS_MIN_KT,
                  'upwind_awa': UPWIND_AWA, 'downwind_awa': DOWNWIND_AWA},
        'global_fit': glob_fit,
        'per_session': sessions,
        'aws_bias_vs_corpus_spread': {
            'median': float(np.median(ratios)) if ratios else float('nan'),
            'p10': float(np.percentile(ratios, 10)) if ratios else float('nan'),
            'p90': float(np.percentile(ratios, 90)) if ratios else float('nan'),
            'n_sessions': len(ratios)},
    }
    with open(args.out, 'w') as f:
        json.dump(payload, f, indent=2, default=float)
    print(f"\n[heel] wrote {args.out}", flush=True)

    print("\n[heel] === POOLED FORCE-LAW FIT ===", flush=True)
    for lab in ('up', 'down'):
        g = glob_fit.get(lab)
        if g:
            print(f"  {lab:4s}: c={g['c']:.5f}  exponent n={g['exponent_n']:.2f} "
                  f"(expect ~2.0)  [{g['n_samples']} samples]", flush=True)
    print(f"\n[heel] relative AWS bias across sessions "
          f"(sqrt(c_sess/c_corpus), 1.0=matches corpus):", flush=True)
    s = payload['aws_bias_vs_corpus_spread']
    print(f"  median={s['median']:.3f}  p10={s['p10']:.3f}  p90={s['p90']:.3f} "
          f"  (n={s['n_sessions']} sessions)", flush=True)
    if np.isfinite(s['p10']) and np.isfinite(s['p90']):
        print(f"  -> upwind AWS reading is consistent to within "
              f"~{100*(s['p90']-s['p10'])/2:.0f}% across the corpus", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
