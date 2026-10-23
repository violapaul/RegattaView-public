#!/usr/bin/env python
"""Identify the boat's water-referenced true-wind calibration offboard.

Goal: fit a single global ``Calibration`` so that
``offline_water_wind_rts`` on raw signals best reproduces the boat's
on-board ``rtwd``/``rtws`` (PGN 130306, emitted by the autopilot).
The result is the offboard "fallback" used when the autopilot is off
the N2K bus.

Fit is pooled across all 2026 parquets in
``/Users/viola/Data/SailingData/PandasLogs/2026-*.parquet``.

Writes ``Analyze/calibration_water_ref_2026.json``.

Run:
    conda run -n sailing --no-capture-output \
        python Analyze/calibrate_water_ref.py
"""
from __future__ import annotations

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import datetime as dt
import glob
import json
import time
from dataclasses import asdict, replace
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd
from scipy.optimize import minimize

from Libraries.global_variables import G
from Libraries import race_logs
from Libraries.log_helper import LOG

from Analyze.true_wind_estimator import (
    Calibration,
    OfflineSmootherConfig,
    _kalman_filter_random_walk,
    _rts_smoother_random_walk,
    _kalman_rts_rw_numba,
    instantaneous_water_wind,
)


# Silence race_logs chatter during the fit.
import logging
LOG.logger.setLevel(logging.WARNING)

G.init_seattle()


# --------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------

LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
GLOB_PATTERN = "2026-*.parquet"
OUT_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "calibration_water_ref_2026.json",
)

SPEED_GLITCH_KT = 40.0
RTWS_MIN_KN = 1.0          # exclude low-wind samples (noise-dominated rtwd)
SPD_MIN_KN = 0.3           # exclude at-dock samples
BOUNDARY_TRIM_S = 30.0     # drop first/last seconds of each session
RTWD_NAN_FRAC_SKIP = 0.80  # skip sessions with >=80% NaN rtwd


# --------------------------------------------------------------------
# Speed-glitch pre-cleaner (copied from stwd_narrative.py)
# --------------------------------------------------------------------

def _clean_speed_glitches(values_ms: np.ndarray,
                          threshold_kt: float = SPEED_GLITCH_KT,
                          ) -> Tuple[np.ndarray, int]:
    """Replace any sample with |speed| > threshold_kt by linear interp.

    Returns (cleaned_ms, n_replaced).  Input/output in m/s; threshold
    expressed in knots for readability.
    """
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
# Raw-signal estimator DataFrame builder
# --------------------------------------------------------------------

def build_est_df_raw(df: pd.DataFrame,
                     ) -> Tuple[pd.DataFrame, np.ndarray, np.ndarray, dict]:
    """Build an estimator DataFrame using **raw** parquet signals and
    reference vectors ``(rtw_n, rtw_e)`` from the on-board rtwd/rtws.

    Raw signals bypass the heel correction and pipeline smoothing that
    ``race_logs.process_sensors`` applies upstream — this is what the
    boat itself sees.

    Angles are expressed in compass degrees (0=N, +clockwise).  AWS/TWS
    in knots.  HDG is converted from magnetic to true by adding
    ``G.MAGNETIC_VARIATION`` (see project note about stale on-board
    variation).
    """
    rsog_clean, n_rsog_bad = _clean_speed_glitches(df['rsog'].to_numpy())
    rspd_clean, n_rspd_bad = _clean_speed_glitches(df['rspd'].to_numpy())

    est_df = pd.DataFrame({
        'awa': df['rawa'].to_numpy(dtype=float),
        'aws': df['raws'].to_numpy(dtype=float) * G.MS_2_KNOTS,
        'hdg': (df['rhdg'].to_numpy(dtype=float)
                + G.MAGNETIC_VARIATION) % 360.0,
        'cog': df['rcog'].to_numpy(dtype=float),
        'sog': rsog_clean * G.MS_2_KNOTS,
        'spd': rspd_clean * G.MS_2_KNOTS,
    }, index=df.index)

    rtws_kt = df['rtws'].to_numpy(dtype=float) * G.MS_2_KNOTS
    rtwd_deg = df['rtwd'].to_numpy(dtype=float)
    rtw_n = rtws_kt * np.cos(np.deg2rad(rtwd_deg))
    rtw_e = rtws_kt * np.sin(np.deg2rad(rtwd_deg))

    stats = {
        'rsog_glitches': int(n_rsog_bad),
        'rspd_glitches': int(n_rspd_bad),
        'rtwd_nan_frac': float(np.mean(~np.isfinite(rtwd_deg))),
        'rtws_nan_frac': float(np.mean(~np.isfinite(rtws_kt))),
    }
    return est_df, rtw_n, rtw_e, stats


# --------------------------------------------------------------------
# Session loader
# --------------------------------------------------------------------

def session_id_of(path: str) -> str:
    return os.path.basename(path).replace('.parquet', '')


def load_sessions(paths: List[str]) -> Tuple[List[dict], Dict[str, str]]:
    """Load every 2026 parquet; return (loaded, skipped).

    Each loaded entry carries precomputed arrays for the cost:
        session_id
        est_df                (raw-signal)
        rtw_n, rtw_e          (reference wind vector in N/E knots)
        valid                 (boolean mask; see below)
        n_valid
        z0_n, z0_e            (first valid rtwd/rtws for KF initial state)
        glitch_stats
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

        if 'rtwd' not in df.columns or 'rtws' not in df.columns:
            skipped[sid] = "no rtwd/rtws columns"
            continue

        nan_frac = float(np.mean(~np.isfinite(df['rtwd'].to_numpy(dtype=float))))
        if nan_frac >= RTWD_NAN_FRAC_SKIP:
            skipped[sid] = (
                f"rtwd is {nan_frac*100:.1f}% NaN "
                f"(>={RTWD_NAN_FRAC_SKIP*100:.0f}%: autopilot off)")
            continue

        est_df, rtw_n, rtw_e, gstats = build_est_df_raw(df)
        rtws_kt = df['rtws'].to_numpy(dtype=float) * G.MS_2_KNOTS
        rtwd_deg = df['rtwd'].to_numpy(dtype=float)
        spd_kt = est_df['spd'].to_numpy()

        n = len(est_df)
        valid = (
            np.isfinite(rtwd_deg)
            & np.isfinite(rtws_kt)
            & (rtws_kt >= RTWS_MIN_KN)
            & np.isfinite(spd_kt)
            & (spd_kt >= SPD_MIN_KN)
            & np.isfinite(est_df['awa'].to_numpy())
            & np.isfinite(est_df['aws'].to_numpy())
            & np.isfinite(est_df['hdg'].to_numpy())
        )
        if boundary_trim < n // 2:
            valid[:boundary_trim] = False
            valid[-boundary_trim:] = False

        n_valid = int(valid.sum())
        if n_valid < 1000:
            skipped[sid] = f"only {n_valid} valid samples after masking"
            continue

        # KF initial state: first valid rtwd/rtws (should always exist here).
        first_valid = int(np.argmax(valid))
        z0_n = float(rtw_n[first_valid])
        z0_e = float(rtw_e[first_valid])

        loaded.append({
            'session_id': sid,
            'est_df': est_df,
            'rtw_n': rtw_n,
            'rtw_e': rtw_e,
            'valid': valid,
            'n_valid': n_valid,
            'z0_n': z0_n,
            'z0_e': z0_e,
            'glitch_stats': gstats,
        })
        print(f"  loaded {sid}: {n_valid}/{n} valid ({100*n_valid/n:.1f}%), "
              f"rtwd_nan={nan_frac*100:.1f}%, "
              f"glitches=(sog={gstats['rsog_glitches']}, "
              f"spd={gstats['rspd_glitches']})", flush=True)

    return loaded, skipped


# --------------------------------------------------------------------
# Cost function
# --------------------------------------------------------------------

# Order of free parameters in the optimizer vector.
FREE_PARAMS = [
    ('awa_offset_deg',      0.0,  (-15.0, 15.0)),
    ('awa_scale_upwind',    1.0,  (0.70, 1.30)),
    ('awa_scale_downwind',  1.0,  (0.70, 1.30)),
    ('aws_scale_upwind',    1.0,  (0.80, 1.20)),
    ('aws_scale_downwind',  1.0,  (0.80, 1.20)),
    ('stw_scale',           1.0,  (0.80, 1.30)),
    ('heading_offset_deg',  0.0,  (-10.0, 10.0)),
]


def unpack_cal(x: np.ndarray, base: Calibration = Calibration()) -> Calibration:
    kwargs = {name: float(x[i]) for i, (name, _, _) in enumerate(FREE_PARAMS)}
    return replace(base, **kwargs)


def smooth_one_session(est_df: pd.DataFrame, fs_hz: float,
                       cal: Calibration, cfg: OfflineSmootherConfig,
                       z0_n: float, z0_e: float,
                       ) -> Tuple[np.ndarray, np.ndarray]:
    """Return ``(wind_north_smooth, wind_east_smooth)`` for a single
    session using the Numba kalman/RTS path.
    """
    meas = instantaneous_water_wind(est_df, fs_hz=fs_hz, cal=cal)
    z_n = meas['wind_north_inst'].to_numpy(dtype=float)
    z_e = meas['wind_east_inst'].to_numpy(dtype=float)

    dt_s = 1.0 / fs_hz
    q_var = (cfg.process_std_kn_per_s * dt_s) ** 2
    r_var = cfg.measurement_std_kn ** 2
    p0_var = cfg.initial_state_std_kn ** 2

    # Choose initial state: prefer rtwd-based seed, else first finite
    # instantaneous-wind sample, else zero.
    xn0 = z0_n
    xe0 = z0_e
    if not (np.isfinite(xn0) and np.isfinite(xe0)):
        idx = np.where(np.isfinite(z_n) & np.isfinite(z_e))[0]
        if idx.size:
            xn0 = float(z_n[idx[0]])
            xe0 = float(z_e[idx[0]])
        else:
            xn0 = 0.0
            xe0 = 0.0

    return _kalman_rts_rw_numba(z_n, z_e, dt_s, q_var, r_var, p0_var,
                                xn0, xe0)


class PooledObjective:
    """Callable objective; keeps iteration telemetry."""
    def __init__(self, sessions: List[dict], cfg: OfflineSmootherConfig,
                 fs_hz: float, prior_weight: float,
                 log_every: int = 10):
        self.sessions = sessions
        self.cfg = cfg
        self.fs_hz = fs_hz
        self.prior_weight = prior_weight
        self.log_every = log_every
        self.nfev = 0
        self.best_f = np.inf

    def __call__(self, x: np.ndarray) -> float:
        self.nfev += 1
        cal = unpack_cal(x)

        total_sq = 0.0
        total_n = 0
        for s in self.sessions:
            wn_s, we_s = smooth_one_session(
                s['est_df'], fs_hz=self.fs_hz, cal=cal,
                cfg=self.cfg, z0_n=s['z0_n'], z0_e=s['z0_e'])
            valid = s['valid']
            dn = wn_s[valid] - s['rtw_n'][valid]
            de = we_s[valid] - s['rtw_e'][valid]
            total_sq += float(np.sum(dn * dn + de * de))
            total_n += int(valid.sum())

        data_cost = total_sq / max(1, total_n)

        # Mild priors centered on initial values to stabilize Powell.
        prior = 0.0
        for i, (name, init, _) in enumerate(FREE_PARAMS):
            prior += (float(x[i]) - init) ** 2
        prior_cost = self.prior_weight * len(self.sessions) * prior

        f = data_cost + prior_cost
        if f < self.best_f:
            self.best_f = f
        if self.nfev % self.log_every == 0:
            print(f"    [obj] nfev={self.nfev:4d}  f={f:.6f}  "
                  f"(data={data_cost:.6f}, prior={prior_cost:.6f}, "
                  f"best={self.best_f:.6f})", flush=True)
        return f


# --------------------------------------------------------------------
# Per-session post-fit metrics
# --------------------------------------------------------------------

def _wrap_signed_deg(a: np.ndarray) -> np.ndarray:
    return (np.asarray(a, dtype=float) + 180.0) % 360.0 - 180.0


def per_session_metrics(sessions: List[dict], cal: Calibration,
                        cfg: OfflineSmootherConfig, fs_hz: float,
                        ) -> Tuple[Dict[str, float], Dict[str, float]]:
    rms_kn: Dict[str, float] = {}
    rms_deg: Dict[str, float] = {}
    for s in sessions:
        wn_s, we_s = smooth_one_session(
            s['est_df'], fs_hz=fs_hz, cal=cal, cfg=cfg,
            z0_n=s['z0_n'], z0_e=s['z0_e'])
        valid = s['valid']
        dn = wn_s[valid] - s['rtw_n'][valid]
        de = we_s[valid] - s['rtw_e'][valid]
        rms_kn[s['session_id']] = float(np.sqrt(np.mean(dn * dn + de * de)))

        twd_ours = (np.rad2deg(np.arctan2(we_s, wn_s))) % 360.0
        rtws_kt_tmp = np.hypot(s['rtw_n'], s['rtw_e'])
        rtwd_deg = np.where(rtws_kt_tmp > 0,
                            (np.rad2deg(np.arctan2(s['rtw_e'], s['rtw_n']))
                             ) % 360.0,
                            np.nan)
        d = _wrap_signed_deg(twd_ours - rtwd_deg)
        d_valid = d[valid]
        d_valid = d_valid[np.isfinite(d_valid)]
        rms_deg[s['session_id']] = (float(np.sqrt(np.mean(d_valid ** 2)))
                                    if d_valid.size else float('nan'))
    return rms_kn, rms_deg


# --------------------------------------------------------------------
# Numba smoke test
# --------------------------------------------------------------------

def smoke_test_numba() -> float:
    """Verify _kalman_rts_rw_numba matches the pure-numpy pair.

    Returns max |jit - numpy| over the smooth output.
    """
    rng = np.random.default_rng(0)
    n = 2000
    z_n = rng.standard_normal(n) * 3.0 + 10.0
    z_e = rng.standard_normal(n) * 3.0 - 2.0
    # Inject some NaNs.
    nan_mask = rng.random(n) < 0.03
    z_n[nan_mask] = np.nan
    z_e[nan_mask] = np.nan

    fs = 10.0
    cfg = OfflineSmootherConfig()

    z = np.stack([z_n, z_e], axis=1)
    filt = _kalman_filter_random_walk(z, fs_hz=fs, config=cfg)
    smooth = _rts_smoother_random_walk(filt)
    x_smooth_np = smooth['x_smooth']

    dt_s = 1.0 / fs
    q_var = (cfg.process_std_kn_per_s * dt_s) ** 2
    r_var = cfg.measurement_std_kn ** 2
    p0_var = cfg.initial_state_std_kn ** 2
    # Match initial-state behaviour: use z[0] when finite else zero (numpy branch).
    x0_n = z_n[0] if np.isfinite(z_n[0]) else 0.0
    x0_e = z_e[0] if np.isfinite(z_e[0]) else 0.0
    xn_j, xe_j = _kalman_rts_rw_numba(z_n, z_e, dt_s, q_var, r_var, p0_var,
                                      float(x0_n), float(x0_e))

    d = np.maximum(np.abs(xn_j - x_smooth_np[:, 0]),
                   np.abs(xe_j - x_smooth_np[:, 1]))
    max_d = float(np.nanmax(d))
    return max_d


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--logs-dir', default=LOGS_DIR)
    parser.add_argument('--pattern', default=GLOB_PATTERN)
    parser.add_argument('--out', default=OUT_PATH)
    parser.add_argument('--maxiter', type=int, default=200)
    parser.add_argument('--prior-weight', type=float, default=0.001)
    args = parser.parse_args()

    t_start = time.time()

    print("[calibrate] numba smoke test...", flush=True)
    max_d = smoke_test_numba()
    assert max_d < 1e-9, f"numba vs numpy mismatch: {max_d:.3e}"
    print(f"  max |jit - numpy| = {max_d:.3e}  OK", flush=True)

    paths = sorted(glob.glob(os.path.join(args.logs_dir, args.pattern)))
    print(f"[calibrate] found {len(paths)} candidate sessions under "
          f"{args.logs_dir}/{args.pattern}", flush=True)
    for p in paths:
        print(f"  candidate: {os.path.basename(p)}", flush=True)

    print("[calibrate] loading sessions...", flush=True)
    loaded, skipped = load_sessions(paths)
    print(f"[calibrate] loaded {len(loaded)} sessions; "
          f"skipped {len(skipped)}.", flush=True)
    for sid, reason in skipped.items():
        print(f"  skipped {sid}: {reason}", flush=True)

    if not loaded:
        print("[calibrate] no sessions loaded; aborting.", file=sys.stderr)
        return 1

    fs_hz = float(G.SAMPLES_PER_SECOND)
    cfg = OfflineSmootherConfig(
        measurement_std_kn=0.60,
        process_std_kn_per_s=0.08,
        initial_state_std_kn=8.0,
    )

    # Warm up the JIT on the first session (first call compiles).
    print("[calibrate] warming up JIT on first session...", flush=True)
    t0 = time.time()
    s0 = loaded[0]
    _ = smooth_one_session(s0['est_df'], fs_hz, Calibration(), cfg,
                           s0['z0_n'], s0['z0_e'])
    print(f"  JIT warmup: {time.time()-t0:.2f} s", flush=True)

    # Initial cost + per-session RMS before fit.
    print("[calibrate] initial cost / pre-fit per-session RMS...", flush=True)
    init_cal = Calibration()
    obj = PooledObjective(loaded, cfg, fs_hz, prior_weight=args.prior_weight)
    x0 = np.array([init for _, init, _ in FREE_PARAMS], dtype=float)
    f0 = obj(x0)
    print(f"  initial pooled cost f0 = {f0:.6f}", flush=True)
    rms_kn_init, rms_deg_init = per_session_metrics(loaded, init_cal, cfg, fs_hz)
    for sid, v in rms_kn_init.items():
        print(f"    [initial] {sid}: "
              f"RMS = {v:.3f} kn, TWD-RMS = {rms_deg_init[sid]:.2f}°",
              flush=True)

    # Optimize.
    bounds = [b for _, _, b in FREE_PARAMS]
    print("[calibrate] running Powell...", flush=True)
    t_opt0 = time.time()
    result = minimize(
        obj, x0, method='Powell', bounds=bounds,
        options={'maxiter': args.maxiter, 'disp': False, 'xtol': 1e-4, 'ftol': 1e-4},
    )
    t_opt = time.time() - t_opt0
    print(f"[calibrate] Powell done in {t_opt:.1f} s  "
          f"(nfev={result.nfev}, success={result.success}); "
          f"msg: {result.message}", flush=True)

    cal_fit = unpack_cal(np.asarray(result.x, dtype=float))
    f_final = float(result.fun)

    print("[calibrate] post-fit per-session RMS...", flush=True)
    rms_kn_fit, rms_deg_fit = per_session_metrics(loaded, cal_fit, cfg, fs_hz)
    for sid in rms_kn_fit:
        print(f"    [final]   {sid}: "
              f"RMS = {rms_kn_fit[sid]:.3f} kn, "
              f"TWD-RMS = {rms_deg_fit[sid]:.2f}° "
              f"(init RMS {rms_kn_init[sid]:.3f}, "
              f"TWD {rms_deg_init[sid]:.2f}°)", flush=True)

    # --------------------------------------------------------------
    # Output JSON.
    # --------------------------------------------------------------
    total_valid = int(sum(s['n_valid'] for s in loaded))
    payload = {
        'fitted_at_utc': dt.datetime.utcnow().isoformat(timespec='seconds') + 'Z',
        'sessions_loaded': [s['session_id'] for s in loaded],
        'sessions_skipped': skipped,
        'n_valid_samples': total_valid,
        'calibration': asdict(cal_fit),
        'cost_initial': float(f0),
        'cost_final': float(f_final),
        'opt_result': {
            'nfev': int(result.nfev),
            'success': bool(result.success),
            'message': str(result.message),
        },
        'smoother_config': {
            'measurement_std_kn': cfg.measurement_std_kn,
            'process_std_kn_per_s': cfg.process_std_kn_per_s,
            'initial_state_std_kn': cfg.initial_state_std_kn,
        },
        'per_session_rms_kn': rms_kn_fit,
        'per_session_rms_kn_initial': rms_kn_init,
        'per_session_rms_twd_deg': rms_deg_fit,
        'per_session_rms_twd_deg_initial': rms_deg_init,
        'free_params': [name for name, _, _ in FREE_PARAMS],
        'bounds': {name: list(b) for name, _, b in FREE_PARAMS},
        'filtering': {
            'rtws_min_kn': RTWS_MIN_KN,
            'spd_min_kn': SPD_MIN_KN,
            'boundary_trim_s': BOUNDARY_TRIM_S,
            'speed_glitch_kt': SPEED_GLITCH_KT,
            'rtwd_nan_frac_skip_threshold': RTWD_NAN_FRAC_SKIP,
        },
        'numba_smoke_test_max_abs_diff': float(max_d),
    }

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, 'w') as f:
        json.dump(payload, f, indent=2)
    print(f"[calibrate] wrote {args.out}", flush=True)

    # --------------------------------------------------------------
    # Summary.
    # --------------------------------------------------------------
    print()
    print("[calibrate] === FITTED CALIBRATION ===", flush=True)
    for name, _, _ in FREE_PARAMS:
        print(f"  {name:24s} = {getattr(cal_fit, name):+.5f}", flush=True)
    print(f"  gps_delay_s (fixed)      = {cal_fit.gps_delay_s:.3f}", flush=True)
    print(f"  stw_delay_s (fixed)      = {cal_fit.stw_delay_s:.3f}", flush=True)

    print()
    print(f"[calibrate] cost initial -> final: "
          f"{f0:.6f} -> {f_final:.6f}  "
          f"(drop {f0-f_final:+.6f})", flush=True)

    # Paul's notebook reference: awa_scale_upwind=0.85, aws_scale_upwind=1.05,
    # stw_scale=1.10.  Compare.
    paul_ref = {
        'awa_scale_upwind': 0.85,
        'aws_scale_upwind': 1.05,
        'stw_scale': 1.10,
    }
    print(f"[calibrate] comparison to Paul's notebook hand-tune:", flush=True)
    for k, v in paul_ref.items():
        ours = getattr(cal_fit, k)
        print(f"  {k:20s}  ours={ours:.4f}  paul={v:.4f}  "
              f"delta={ours-v:+.4f}", flush=True)

    print()
    print("[calibrate] per-session RMS TWD (deg), initial vs final:", flush=True)
    print(f"  {'session':30s}  {'init':>6s}  {'final':>6s}", flush=True)
    for sid in rms_deg_fit:
        print(f"  {sid:30s}  {rms_deg_init[sid]:6.2f}  "
              f"{rms_deg_fit[sid]:6.2f}", flush=True)

    print()
    print(f"[calibrate] wall-clock: {time.time()-t_start:.1f} s", flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
