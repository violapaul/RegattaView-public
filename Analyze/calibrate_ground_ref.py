#!/usr/bin/env python
"""Fit the ground-referenced true-wind calibration by tack/gybe constancy.

Target (per the user): a *shear-free, tactical* ground-referenced TWD --
one that stays steady through tacks and gybes. The objective is therefore
**cross-maneuver wind-vector constancy**: the instantaneous ground wind
(`true_wind_estimator.instantaneous_ground_wind`) measured just before a
maneuver should equal the wind just after it. We minimize the pooled
pre/post "jump" across every detected tack and gybe in the 2026 corpus.

Mathematical note -- why heading_offset is FIXED, not fit:
    A constant heading offset rotates hdg_true (hence the earth-frame
    apparent wind) by the same angle on both sides of a maneuver, so the
    pre/post *jump* is (to first order) invariant to it. Heading offset is
    therefore NOT identifiable from constancy alone (the in-repo
    `fit_ground_wind_calibration` docstring says the same). We pin it from
    the physical bundle -- the RC42 -2.3 deg from the even/odd tack-symmetry
    analysis (`Analyze/HEADING_CALIBRATION.md`, stored in
    `Analyze/leeway_model.json`) -- which separates compass offset from
    leeway using an *external* reference the constancy objective lacks.
    A1.scan below confirms the jump cost is ~flat in heading_offset.

Free parameters (identifiable from constancy):
    awa_offset_deg            (vane zero; tack-asymmetric -> identifiable)
    awa_scale_upwind/downwind (point-of-sail angular distortion)
    aws_scale_upwind/downwind (point-of-sail speed distortion)
    gps_delay_s               (SOG/COG lag; drives maneuver shear)

Writes ``Analyze/calibration_ground_ref_2026.json``.

Run:
    conda run -n sailing --no-capture-output \
        python Analyze/calibrate_ground_ref.py
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
import time
from dataclasses import asdict, replace
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from Libraries.global_variables import G
from Libraries import race_logs
from Libraries.log_helper import LOG
from Libraries.event_detectors import HeadingManeuverDetector

from Analyze.true_wind_estimator import (
    Calibration,
    CalibrationFitConfig,
    extract_tack_segments,
    instantaneous_ground_wind,
    _segment_cost,
    centered_boxcar,
)

LOG.logger.setLevel(logging.ERROR)
G.init_seattle()


# --------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------

LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
GLOB_PATTERN = "2026-*.parquet"
HERE = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(HERE, "calibration_ground_ref_2026.json")
LEEWAY_MODEL_PATH = os.path.join(HERE, "leeway_model.json")

SPEED_GLITCH_KT = 40.0
BOUNDARY_TRIM_S = 30.0
MIN_TACKS_PER_SESSION = 2

# Maneuver detector (validated tack settings from current_analysis.detect_tacks).
DET_MIN_TURN = 55
DET_STEADY_STD_MAX = 10
DET_STEADY_SECONDS = 20
DET_MIN_GAP_SECONDS = 25


# --------------------------------------------------------------------
# Fixed heading offset from the physical bundle
# --------------------------------------------------------------------

def load_fixed_heading_offset() -> Tuple[float, str]:
    """Return (heading_offset_deg, source_str) from the physical bundle."""
    try:
        with open(LEEWAY_MODEL_PATH) as f:
            m = json.load(f)
        off = float(m["calibration"]["heading_offset_deg"])
        return off, f"leeway_model.json:calibration.heading_offset_deg={off:+.3f}"
    except Exception as exc:  # pragma: no cover - config drift guard
        # Physical default per HEADING_CALIBRATION.md (RC42 even/odd analysis).
        return -2.3, f"fallback default -2.3 ({type(exc).__name__})"


# --------------------------------------------------------------------
# Speed-glitch pre-cleaner (mirror of calibrate_water_ref)
# --------------------------------------------------------------------

def _clean_speed_glitches(values_ms: np.ndarray,
                          threshold_kt: float = SPEED_GLITCH_KT,
                          ) -> Tuple[np.ndarray, int]:
    v = np.asarray(values_ms, dtype=float).copy()
    kt = np.abs(v) * G.MS_2_KNOTS
    bad = np.isfinite(v) & (kt > threshold_kt)
    if not bad.any():
        return v, 0
    good = np.isfinite(v) & ~bad
    idx = np.arange(len(v), dtype=float)
    if good.sum() < 2:
        return v, int(bad.sum())
    v[bad] = np.interp(idx[bad], idx[good], v[good])
    return v, int(bad.sum())


# --------------------------------------------------------------------
# Maneuver detection (tacks + gybes), classified by point of sail
# --------------------------------------------------------------------

def detect_maneuvers(awa: np.ndarray, hdg_true: np.ndarray, fs: float,
                     ) -> List[Tuple[int, str]]:
    """Return [(center_idx, 'tack'|'gybe'), ...].

    Reuses the shared ``HeadingManeuverDetector`` (the same detector behind
    ``current_analysis.detect_tacks``). A maneuver is a *tack* when the boat
    is upwind (pre |AWA| < 90) and the turn is < 120 deg; a *gybe* when
    downwind (pre |AWA| > 90) and the turn is < 120 deg. Roundings
    (turn >= 120) are dropped -- the wind is not continuous across them in
    the boat frame the way it is across a tack/gybe.
    """
    det = HeadingManeuverDetector(
        min_turn=DET_MIN_TURN, steady_std_max=DET_STEADY_STD_MAX,
        steady_seconds=DET_STEADY_SECONDS, min_gap_seconds=DET_MIN_GAP_SECONDS,
        sample_rate=fs)
    raw = det._find_maneuvers(np.asarray(hdg_true, dtype=float))
    pre_n = int(round(8 * fs))
    out: List[Tuple[int, str]] = []
    for idx, change in raw:
        idx = int(idx)
        if not (0 <= idx < len(awa)) or abs(change) >= 120:
            continue
        pre = np.nanmedian(np.abs(awa[max(0, idx - pre_n):idx]))
        if not np.isfinite(pre):
            continue
        out.append((idx, 'tack' if pre < 90 else 'gybe'))
    return out


# --------------------------------------------------------------------
# Session loader
# --------------------------------------------------------------------

def session_id_of(path: str) -> str:
    return os.path.basename(path).replace('.parquet', '')


def load_sessions(paths: List[str], fit_cfg: CalibrationFitConfig,
                  ) -> Tuple[List[dict], Dict[str, str]]:
    """Load every parquet; return (loaded, skipped).

    Each loaded entry carries the raw-signal ``est_df`` and the list of
    maneuver segments ``(start, center, stop, kind)`` for the constancy cost.
    """
    loaded: List[dict] = []
    skipped: Dict[str, str] = {}

    fs = float(G.SAMPLES_PER_SECOND)
    boundary_trim = int(round(BOUNDARY_TRIM_S * fs))

    for path in paths:
        sid = session_id_of(path)
        try:
            df = race_logs.read_log_file(
                os.path.basename(path),
                skip_dock_only=True, discard_columns=False,
                rename_columns=True, trim=True, process=True,
                path=os.path.dirname(path))
        except Exception as exc:
            skipped[sid] = f"{type(exc).__name__}: {exc}"
            continue
        if df is None:
            skipped[sid] = "read_log_file returned None (dock-only?)"
            continue
        for c in ('rawa', 'raws', 'rhdg', 'rcog', 'rsog'):
            if c not in df.columns:
                skipped[sid] = f"missing column {c}"
                break
        else:
            sog_clean, n_sog_bad = _clean_speed_glitches(df['rsog'].to_numpy())
            # Raw signals, true-north hdg WITHOUT the compass offset (the
            # Calibration object applies heading_offset on top).
            hdg_true_raw = (df['rhdg'].to_numpy(dtype=float)
                            + G.MAGNETIC_VARIATION) % 360.0
            est_df = pd.DataFrame({
                'awa': df['rawa'].to_numpy(dtype=float),
                'aws': df['raws'].to_numpy(dtype=float) * G.MS_2_KNOTS,
                'hdg': hdg_true_raw,
                'cog': df['rcog'].to_numpy(dtype=float),
                'sog': sog_clean * G.MS_2_KNOTS,
            }, index=df.index).reset_index(drop=True)

            # Detect on heading + AWA (constant offset is irrelevant to turns).
            man = detect_maneuvers(est_df['awa'].to_numpy(),
                                   est_df['hdg'].to_numpy(), fs)
            # Build segments, dropping any that touch the boundary trim.
            n = len(est_df)
            centers = [c for c, _ in man]
            kinds = {c: k for c, k in man}
            segs = extract_tack_segments(est_df, fs_hz=fs, tack_centers=centers,
                                         max_segments=None)
            seglist = []
            for (start, center, stop), c in zip(segs, centers):
                if start < boundary_trim or stop > n - boundary_trim:
                    continue
                seglist.append((start, center, stop, kinds[c]))

            n_tack = sum(1 for *_x, k in seglist if k == 'tack')
            n_gybe = sum(1 for *_x, k in seglist if k == 'gybe')
            if n_tack + n_gybe < MIN_TACKS_PER_SESSION:
                skipped[sid] = f"only {n_tack + n_gybe} usable maneuvers"
                continue

            loaded.append({
                'session_id': sid,
                'est_df': est_df,
                'segments': seglist,
                'n_tack': n_tack,
                'n_gybe': n_gybe,
                'sog_glitches': int(n_sog_bad),
            })
            print(f"  loaded {sid}: {n_tack} tacks + {n_gybe} gybes, "
                  f"{len(est_df)} rows, sog_glitches={n_sog_bad}", flush=True)

    return loaded, skipped


# --------------------------------------------------------------------
# Pooled constancy objective
# --------------------------------------------------------------------

# Master parameter table: name -> (init, bounds).
ALL_PARAMS = {
    'gps_delay_s':        (1.0,  (0.00, 3.00)),
    'awa_offset_deg':     (0.0,  (-12.0, 12.0)),
    'awa_scale_upwind':   (1.0,  (0.70, 1.30)),
    'awa_scale_downwind': (1.0,  (0.70, 1.30)),
    'aws_scale_upwind':   (1.0,  (0.80, 1.20)),
    'aws_scale_downwind': (1.0,  (0.80, 1.20)),
}

# Nested model presets (which params are free; the rest stay at unity/zero).
# Rationale for the ladder:
#  - heading_offset is FIXED externally (-2.3 deg, physical) -- not identifiable
#    from constancy (see module docstring).
#  - AWS scales mainly move TWS magnitude, barely the TWD *direction* that the
#    shear objective cares about; the heel witness shows AWS is consistent to
#    ~5% and cannot be pinned absolutely -- so default to fixing them at 1.0.
#  - gps_delay drives maneuver shear; awa_offset/awa_scale move TWD direction.
MODELS = {
    'minimal': ['gps_delay_s', 'awa_offset_deg'],
    'reduced': ['gps_delay_s', 'awa_offset_deg',
                'awa_scale_upwind', 'awa_scale_downwind'],
    'full':    list(ALL_PARAMS.keys()),
}


def unpack_cal(x: np.ndarray, base: Calibration, free: List[str]) -> Calibration:
    kwargs = {name: float(x[i]) for i, name in enumerate(free)}
    return replace(base, **kwargs)


def session_jump(est_df: pd.DataFrame, segments, fs: float, cal: Calibration,
                 fit_cfg: CalibrationFitConfig,
                 kind: str = None) -> Tuple[float, int]:
    """Sum of per-segment constancy cost (and segment count) for one session.

    ``kind`` optionally restricts to 'tack' or 'gybe'.
    """
    total = 0.0
    count = 0
    for start, center, stop, k in segments:
        if kind is not None and k != kind:
            continue
        seg_df = est_df.iloc[start:stop]
        total += _segment_cost(seg_df, fs_hz=fs, center=center, cal=cal,
                               fit_cfg=fit_cfg)
        count += 1
    return total, count


def pooled_jump(sessions: List[dict], fs: float, cal: Calibration,
                fit_cfg: CalibrationFitConfig) -> float:
    """Mean per-maneuver constancy cost over a set of sessions (the held-out
    metric for CV; also the in-sample data cost)."""
    total = 0.0
    count = 0
    for s in sessions:
        t, c = session_jump(s['est_df'], s['segments'], fs, cal, fit_cfg)
        total += t
        count += c
    return total / max(1, count)


class PooledConstancyObjective:
    def __init__(self, sessions: List[dict], fs: float, base: Calibration,
                 free: List[str], fit_cfg: CalibrationFitConfig,
                 prior_weight: float, log_every: int = 25):
        self.sessions = sessions
        self.fs = fs
        self.base = base
        self.free = free
        self.fit_cfg = fit_cfg
        self.prior_weight = prior_weight
        self.log_every = log_every
        self.nfev = 0
        self.best_f = np.inf

    def __call__(self, x: np.ndarray) -> float:
        self.nfev += 1
        cal = unpack_cal(x, self.base, self.free)
        data_cost = pooled_jump(self.sessions, self.fs, cal, self.fit_cfg)
        prior = sum((float(x[i]) - ALL_PARAMS[name][0]) ** 2
                    for i, name in enumerate(self.free))
        f = data_cost + self.prior_weight * prior
        if f < self.best_f:
            self.best_f = f
        if self.nfev % self.log_every == 0:
            print(f"    [obj] nfev={self.nfev:4d} f={f:.5f} "
                  f"(data={data_cost:.5f}, best={self.best_f:.5f})", flush=True)
        return f


def fit_one(sessions: List[dict], fs: float, base: Calibration,
            free: List[str], fit_cfg: CalibrationFitConfig,
            prior_weight: float, maxiter: int, verbose: bool = True
            ) -> Tuple[Calibration, float]:
    """Fit one model (list of free params) on `sessions`; return (cal, cost)."""
    obj = PooledConstancyObjective(sessions, fs, base, free, fit_cfg,
                                   prior_weight)
    x0 = np.array([ALL_PARAMS[n][0] for n in free], dtype=float)
    bounds = [ALL_PARAMS[n][1] for n in free]
    res = minimize(obj, x0, method='Powell', bounds=bounds,
                   options={'maxiter': maxiter, 'xtol': 1e-4, 'ftol': 1e-4,
                            'disp': False})
    return unpack_cal(np.asarray(res.x, float), base, free), float(res.fun)


# --------------------------------------------------------------------
# Per-session shear metric (mean pre/post jump, knots) for reporting
# --------------------------------------------------------------------

def per_session_shear(sessions: List[dict], fs: float, cal: Calibration,
                      fit_cfg: CalibrationFitConfig,
                      ) -> Dict[str, Dict[str, float]]:
    """Mean per-maneuver jump (knots) per session, split tack/gybe.

    Uses jump_weight=1, residual/roughness=0 so the number is a direct
    wind-vector discontinuity in knots, comparable across runs.
    """
    jcfg = replace(fit_cfg, residual_weight=0.0, roughness_weight=0.0,
                   jump_weight=1.0)
    out: Dict[str, Dict[str, float]] = {}
    for s in sessions:
        row = {}
        for kind in ('tack', 'gybe'):
            t, c = session_jump(s['est_df'], s['segments'], fs, cal, jcfg, kind)
            # _segment_cost returns huber(jump); for small jumps ~0.5*jump^2.
            # Report mean huberized cost as the shear proxy.
            row[kind] = float(t / c) if c else float('nan')
        out[s['session_id']] = row
    return out


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------

def cross_validate(loaded, fs, base, fit_cfg, prior_weight, maxiter):
    """2-fold (even/odd session) CV: for each model, fit on train and report
    held-out pooled jump. Returns {model: {'in_sample', 'held_out', 'gap'}}."""
    even = [s for i, s in enumerate(loaded) if i % 2 == 0]
    odd = [s for i, s in enumerate(loaded) if i % 2 == 1]
    folds = [(even, odd), (odd, even)]
    out = {}
    for model, free in MODELS.items():
        ins, held = [], []
        for train, test in folds:
            cal, _ = fit_one(train, fs, base, free, fit_cfg, prior_weight,
                             maxiter, verbose=False)
            ins.append(pooled_jump(train, fs, cal, fit_cfg))
            held.append(pooled_jump(test, fs, cal, fit_cfg))
        out[model] = {'in_sample': float(np.mean(ins)),
                      'held_out': float(np.mean(held)),
                      'gap': float(np.mean(held) - np.mean(ins))}
        print(f"  [cv] {model:8s}: in={out[model]['in_sample']:.5f} "
              f"held={out[model]['held_out']:.5f} "
              f"gap={out[model]['gap']:+.5f}", flush=True)
    return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--logs-dir', default=LOGS_DIR)
    p.add_argument('--pattern', default=GLOB_PATTERN)
    p.add_argument('--out', default=OUT_PATH)
    p.add_argument('--maxiter', type=int, default=80)
    p.add_argument('--prior-weight', type=float, default=0.002)
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--model', default='reduced', choices=list(MODELS),
                   help='param set to fit on all data and write out')
    p.add_argument('--cv', action='store_true',
                   help='run 2-fold held-out model selection, pick best')
    p.add_argument('--heading-scan', action='store_true')
    args = p.parse_args()

    t_start = time.time()
    fs = float(G.SAMPLES_PER_SECOND)
    fit_cfg = CalibrationFitConfig()

    head_off, head_src = load_fixed_heading_offset()
    base = Calibration(heading_offset_deg=head_off)
    print(f"[gr-cal] fixed heading_offset = {head_off:+.3f} deg ({head_src})",
          flush=True)

    paths = sorted(glob.glob(os.path.join(args.logs_dir, args.pattern)))
    if args.limit:
        paths = paths[:args.limit]
    loaded, skipped = load_sessions(paths, fit_cfg)
    print(f"[gr-cal] loaded {len(loaded)}, skipped {len(skipped)}", flush=True)
    for sid, reason in skipped.items():
        print(f"  skipped {sid}: {reason}", flush=True)
    if not loaded:
        print("[gr-cal] no sessions; aborting", file=sys.stderr)
        return 1

    tot_tack = sum(s['n_tack'] for s in loaded)
    tot_gybe = sum(s['n_gybe'] for s in loaded)
    print(f"[gr-cal] pooled maneuvers: {tot_tack} tacks + {tot_gybe} gybes",
          flush=True)

    cv_result = None
    chosen = args.model
    if args.cv:
        print("[gr-cal] 2-fold held-out model selection...", flush=True)
        cv_result = cross_validate(loaded, fs, base, fit_cfg,
                                   args.prior_weight, args.maxiter)
        chosen = min(cv_result, key=lambda m: cv_result[m]['held_out'])
        print(f"[gr-cal] best held-out model: {chosen}", flush=True)

    free = MODELS[chosen]
    print(f"[gr-cal] fitting model '{chosen}' (free: {free}) on all data...",
          flush=True)
    f0 = pooled_jump(loaded, fs, base, fit_cfg)
    shear0 = per_session_shear(loaded, fs, base, fit_cfg)
    t_opt = time.time()
    cal_fit, f_final = fit_one(loaded, fs, base, free, fit_cfg,
                               args.prior_weight, args.maxiter)
    print(f"[gr-cal] fit done in {time.time()-t_opt:.1f}s", flush=True)
    shear1 = per_session_shear(loaded, fs, cal_fit, fit_cfg)

    heading_scan = None
    if args.heading_scan:
        heading_scan = {}
        for ho in (-6.0, -4.0, -2.3, 0.0, 2.0, 4.0):
            cal_h = replace(cal_fit, heading_offset_deg=ho)
            heading_scan[f"{ho:+.1f}"] = pooled_jump(loaded, fs, cal_h, fit_cfg)
        print("[gr-cal] heading_offset scan (pooled jump cost):", flush=True)
        for k, v in heading_scan.items():
            print(f"    hdg_off={k}: {v:.5f}", flush=True)

    def mean_shear(d, kind):
        vals = [v[kind] for v in d.values() if np.isfinite(v[kind])]
        return float(np.mean(vals)) if vals else float('nan')

    payload = {
        'fitted_at_utc': dt.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'objective': 'cross-maneuver (tack+gybe) ground-wind constancy',
        'model': chosen,
        'free_params': free,
        'heading_offset_fixed': head_off,
        'heading_offset_source': head_src,
        'cv': cv_result,
        'sessions_loaded': [s['session_id'] for s in loaded],
        'sessions_skipped': skipped,
        'n_tacks': tot_tack,
        'n_gybes': tot_gybe,
        'calibration': asdict(cal_fit),
        'cost_initial': float(f0),
        'cost_final': float(f_final),
        'mean_shear_initial': {'tack': mean_shear(shear0, 'tack'),
                               'gybe': mean_shear(shear0, 'gybe')},
        'mean_shear_final': {'tack': mean_shear(shear1, 'tack'),
                             'gybe': mean_shear(shear1, 'gybe')},
        'per_session_shear_final': shear1,
        'heading_scan': heading_scan,
    }
    with open(args.out, 'w') as f:
        json.dump(payload, f, indent=2)
    print(f"[gr-cal] wrote {args.out}", flush=True)

    print(f"\n[gr-cal] === FITTED '{chosen}' (heading fixed {head_off:+.2f}) ===",
          flush=True)
    for name in ALL_PARAMS:
        tag = '' if name in free else '  (fixed)'
        print(f"  {name:22s} = {getattr(cal_fit, name):+.5f}{tag}", flush=True)
    print(f"\n[gr-cal] mean maneuver jump cost (lower=less shear):", flush=True)
    print(f"  tack: {mean_shear(shear0,'tack'):.5f} -> "
          f"{mean_shear(shear1,'tack'):.5f}", flush=True)
    print(f"  gybe: {mean_shear(shear0,'gybe'):.5f} -> "
          f"{mean_shear(shear1,'gybe'):.5f}", flush=True)
    print(f"\n[gr-cal] wall-clock: {time.time()-t_start:.1f}s", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
