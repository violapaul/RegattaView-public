#!/usr/bin/env python
"""Evaluate adaptive vs linear TWD estimators on five sessions.

Compares three variants:
  - linear: offline_water_wind_rts (current baseline)
  - huber_only: offline_water_wind_adaptive with alpha_R_turn=0,
    alpha_R_accel=0, q_growth=1.0 (effectively disables dynamic R/Q;
    only Huber outlier clipping is active)
  - adaptive: offline_water_wind_adaptive with default AdaptiveFilterConfig

Metrics per (session, variant):
  - Full-session circmean + RMS of sangle(filter_twd - rtwd).
  - Tack-window metrics: peak |error| and decay time in +-30 s around
    each maneuver midpoint (median across tacks).
  - Steady-leg RMS in windows with low turn/accel for >=30 s.
  - Rolling-60 s RMS trace for time-varying fidelity.

Outputs land in ``Analyze/Plots/adaptive_eval/``:
  - summary.md + summary.csv
  - rolling_rms_<session>.png
  - tack_overlay_<session>.png
  - timeseries_<session>.png

Run:
    conda run -n sailing --no-capture-output \
        python Analyze/adaptive_eval.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO))

from Libraries.global_variables import G  # noqa: E402
from Libraries import race_logs  # noqa: E402
from Libraries.log_helper import LOG  # noqa: E402

from Analyze.true_wind_estimator import (  # noqa: E402
    AdaptiveFilterConfig,
    Calibration,
    OfflineSmootherConfig,
    _compute_boat_dynamics,
    centered_boxcar,
    detect_tacks,
    offline_water_wind_adaptive,
    offline_water_wind_rts,
)

import logging
LOG.logger.setLevel(logging.WARNING)


# --------------------------------------------------------------------
# Config
# --------------------------------------------------------------------

LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
CAL_PATH = HERE / 'calibration_water_ref_2026.json'
OUT_DIR = HERE / 'Plots' / 'adaptive_eval'
MANEUVERS_CSV = HERE / 'Plots' / 'stwd_audit' / 'maneuvers.csv'

# session_id, is_2019 (uses .pd.gz + G.init_peer_gynt), label
SESSIONS: List[Tuple[str, bool, str]] = [
    ("2026-04-19T08_59", False, "2026-04-19 08:59 (clean)"),
    ("2026-04-20T18_00", False, "2026-04-20 18:00 (hard)"),
    ("2019-11-16_10:09", True,  "2019-11-16 10:09 (Paul primary)"),
    ("2025-08-02T09_41", False, "2025-08-02 09:41 (rsog glitch)"),
    ("2026-04-18T10_06", False, "2026-04-18 10:06 (outlier)"),
]

# Filtering thresholds (match calibrate_water_ref.py).
RTWS_MIN_KN = 1.0
SPD_MIN_KN = 0.3
BOUNDARY_TRIM_S = 30.0
RTWD_NAN_FRAC_SKIP = 0.80
SPEED_GLITCH_KT = 40.0


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def _wrap180(a):
    return ((np.asarray(a, dtype=float) + 180.0) % 360.0) - 180.0


def circular_rms(err_deg):
    e = _wrap180(err_deg)
    e = e[np.isfinite(e)]
    if e.size == 0:
        return float('nan')
    return float(np.sqrt(np.mean(e ** 2)))


def circular_mean(err_deg):
    e = _wrap180(err_deg)
    e = e[np.isfinite(e)]
    if e.size == 0:
        return float('nan')
    r = np.deg2rad(e)
    return float(np.rad2deg(np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


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


def build_est_df_raw(df: pd.DataFrame) -> Tuple[pd.DataFrame, np.ndarray, np.ndarray]:
    """Build the adaptive-estimator input DF + rtwd/rtws references
    from a raw parquet (or 2019 pd.gz) frame. Uses the variation that
    has been set on G.
    """
    rsog_clean, _ = _clean_speed_glitches(df['rsog'].to_numpy())
    rspd_clean, _ = _clean_speed_glitches(df['rspd'].to_numpy())
    variation = float(G.MAGNETIC_VARIATION)

    est_df = pd.DataFrame({
        'awa': df['rawa'].to_numpy(dtype=float),
        'aws': df['raws'].to_numpy(dtype=float) * G.MS_2_KNOTS,
        'hdg': (df['rhdg'].to_numpy(dtype=float) + variation) % 360.0,
        'cog': df['rcog'].to_numpy(dtype=float),
        'sog': rsog_clean * G.MS_2_KNOTS,
        'spd': rspd_clean * G.MS_2_KNOTS,
    }, index=df.index)

    rtwd = df['rtwd'].to_numpy(dtype=float)
    rtws = df['rtws'].to_numpy(dtype=float) * G.MS_2_KNOTS
    return est_df, rtwd, rtws


def load_session(session_id: str, is_2019: bool) -> Optional[Tuple[pd.DataFrame, np.ndarray, np.ndarray, float]]:
    """Return (est_df, rtwd, rtws, fs_hz) or None if session should be skipped."""
    if is_2019:
        G.init_peer_gynt()
        filename = f"{session_id}.pd.gz"
    else:
        G.init_seattle()
        filename = f"{session_id}.parquet"

    path = os.path.join(LOGS_DIR, filename)
    if not os.path.exists(path):
        print(f"  SKIP: file not found at {path}")
        return None

    try:
        df = race_logs.read_log_file(
            filename,
            skip_dock_only=True, discard_columns=False,
            rename_columns=True, trim=True, process=True,
            path=LOGS_DIR,
        )
    except Exception as exc:
        print(f"  SKIP: read_log_file failed: {type(exc).__name__}: {exc}")
        return None
    if df is None:
        print("  SKIP: read_log_file returned None (dock only)")
        return None
    if 'rtwd' not in df.columns or 'rtws' not in df.columns:
        print("  SKIP: no rtwd/rtws columns")
        return None

    nan_frac = float(np.mean(~np.isfinite(df['rtwd'].to_numpy(dtype=float))))
    if nan_frac >= RTWD_NAN_FRAC_SKIP:
        print(f"  SKIP: rtwd is {nan_frac * 100:.1f}% NaN")
        return None

    est_df, rtwd, rtws = build_est_df_raw(df)
    fs_hz = float(G.SAMPLES_PER_SECOND)
    return est_df, rtwd, rtws, fs_hz


def load_calibration() -> Calibration:
    with open(CAL_PATH) as f:
        cal_json = json.load(f)['calibration']
    return Calibration(**cal_json)


# --------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------

def build_valid_mask(est_df: pd.DataFrame, rtwd: np.ndarray, rtws: np.ndarray,
                     fs_hz: float) -> np.ndarray:
    n = len(est_df)
    trim = int(round(BOUNDARY_TRIM_S * fs_hz))
    mask = np.zeros(n, dtype=bool)
    mask[trim:n - trim] = True
    mask &= np.isfinite(rtwd)
    mask &= np.isfinite(rtws) & (rtws >= RTWS_MIN_KN)
    spd = est_df['spd'].to_numpy(dtype=float)
    mask &= np.isfinite(spd) & (spd >= SPD_MIN_KN)
    return mask


def full_session_metrics(twd: np.ndarray, rtwd: np.ndarray, mask: np.ndarray) -> Dict[str, float]:
    err = _wrap180(twd - rtwd)
    err_valid = err[mask & np.isfinite(twd)]
    return {
        'n_valid': int(err_valid.size),
        'circmean_deg': circular_mean(err_valid),
        'rms_deg': circular_rms(err_valid),
    }


def rolling_rms_trace(twd: np.ndarray, rtwd: np.ndarray, mask: np.ndarray,
                      window: int) -> np.ndarray:
    err = _wrap180(twd - rtwd)
    err[~mask] = np.nan

    s = pd.Series(err)
    sq = s.pow(2)
    mean_sq = sq.rolling(window, center=True, min_periods=int(window * 0.2)).mean()
    return np.sqrt(mean_sq.to_numpy())


def steady_leg_rms(twd: np.ndarray, rtwd: np.ndarray, mask: np.ndarray,
                   turn_rate: np.ndarray, accel: np.ndarray, fs_hz: float,
                   min_leg_s: float = 30.0,
                   turn_thresh: float = 1.0, accel_thresh: float = 0.1) -> Optional[float]:
    cond = (np.abs(turn_rate) < turn_thresh) & (np.abs(accel) < accel_thresh)
    cond &= mask & np.isfinite(twd)
    min_len = int(round(min_leg_s * fs_hz))
    # find contiguous true runs of length >= min_len
    err_sq_parts: List[np.ndarray] = []
    i = 0
    n = len(cond)
    while i < n:
        if cond[i]:
            j = i
            while j < n and cond[j]:
                j += 1
            if j - i >= min_len:
                err = _wrap180(twd[i:j] - rtwd[i:j])
                err_sq_parts.append(err ** 2)
            i = j
        else:
            i += 1
    if not err_sq_parts:
        return None
    all_sq = np.concatenate(err_sq_parts)
    all_sq = all_sq[np.isfinite(all_sq)]
    if all_sq.size == 0:
        return None
    return float(np.sqrt(np.mean(all_sq)))


# --------------------------------------------------------------------
# Tack windows (from maneuvers.csv if present, else detect_tacks)
# --------------------------------------------------------------------

def load_maneuvers_for_session(session_id: str) -> Optional[np.ndarray]:
    if not MANEUVERS_CSV.exists():
        return None
    df = pd.read_csv(MANEUVERS_CSV, usecols=['session', 'kind', 'mid_idx'])
    # The maneuvers CSV stores sessions with the parquet suffix.
    key = f"{session_id}.parquet"
    rows = df[df['session'] == key]
    if rows.empty:
        return None
    return rows['mid_idx'].to_numpy(dtype=int)


def get_tack_centers(session_id: str, est_df: pd.DataFrame, fs_hz: float) -> np.ndarray:
    cs = load_maneuvers_for_session(session_id)
    if cs is not None and cs.size > 0:
        return cs
    # Fallback.
    return detect_tacks(est_df, fs_hz=fs_hz)


def tack_error_traces(twd: np.ndarray, rtwd: np.ndarray, mask: np.ndarray,
                      centers: np.ndarray, fs_hz: float,
                      pre_s: float = 30.0, post_s: float = 30.0) -> np.ndarray:
    """Return (n_tacks, n_samples) matrix of |err| in ±30 s windows.

    Tacks without enough valid samples are dropped (NaN-padded rows are
    returned; caller can nanmedian/nanquantile them).
    """
    pre_n = int(round(pre_s * fs_hz))
    post_n = int(round(post_s * fs_hz))
    w = pre_n + post_n + 1
    n = len(twd)

    err = np.abs(_wrap180(twd - rtwd))
    err[~mask] = np.nan

    traces = []
    for c in centers:
        c = int(c)
        s = c - pre_n
        e = c + post_n + 1
        if s < 0 or e > n:
            continue
        trace = err[s:e]
        if not np.any(np.isfinite(trace)):
            continue
        traces.append(trace)
    if not traces:
        return np.zeros((0, w))
    return np.asarray(traces)


def tack_metrics(traces: np.ndarray, fs_hz: float) -> Dict[str, float]:
    """Median peak |error| and median decay time (peak -> 50% of peak)."""
    if traces.size == 0:
        return {'n_tacks': 0, 'median_peak_deg': float('nan'), 'median_decay_s': float('nan')}

    peaks = np.nanmax(traces, axis=1)
    decays = []
    for row in traces:
        if not np.all(np.isfinite(row)):
            # use finite entries only
            row = row.copy()
        if not np.any(np.isfinite(row)):
            continue
        pk_idx = int(np.nanargmax(row))
        pk = row[pk_idx]
        target = 0.5 * pk
        # Search forward from peak for first sample <= target (persistent).
        t = np.nan
        post = row[pk_idx:]
        below = np.where(np.isfinite(post) & (post <= target))[0]
        if below.size:
            t = below[0] / fs_hz
        decays.append(t)
    return {
        'n_tacks': int(traces.shape[0]),
        'median_peak_deg': float(np.nanmedian(peaks)),
        'median_decay_s': float(np.nanmedian(decays)) if decays else float('nan'),
    }


# --------------------------------------------------------------------
# Plots
# --------------------------------------------------------------------

VARIANT_COLORS = {
    'linear': 'tab:blue',
    'huber_only': 'tab:orange',
    'adaptive': 'tab:red',
}


def plot_rolling_rms(out_path: Path, session_label: str,
                     t_s: np.ndarray, rolling: Dict[str, np.ndarray]) -> None:
    fig, ax = plt.subplots(figsize=(10, 3.0), dpi=120)
    for variant, trace in rolling.items():
        ax.plot(t_s, trace, color=VARIANT_COLORS[variant], label=variant, lw=1.2)
    ax.set_xlabel('time (s)')
    ax.set_ylabel('rolling 60 s RMS |err| (deg)')
    ax.set_title(f'Rolling 60 s RMS vs rtwd — {session_label}')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper right', fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_tack_overlay(out_path: Path, session_label: str,
                      traces_by_variant: Dict[str, np.ndarray], fs_hz: float) -> None:
    if not any(v.size > 0 for v in traces_by_variant.values()):
        return
    fig, ax = plt.subplots(figsize=(10, 3.5), dpi=120)
    sample_w = next(v.shape[1] for v in traces_by_variant.values() if v.size > 0)
    t_s = (np.arange(sample_w) - sample_w // 2) / fs_hz
    for variant, traces in traces_by_variant.items():
        if traces.size == 0:
            continue
        med = np.nanmedian(traces, axis=0)
        q25 = np.nanquantile(traces, 0.25, axis=0)
        q75 = np.nanquantile(traces, 0.75, axis=0)
        color = VARIANT_COLORS[variant]
        ax.plot(t_s, med, color=color, label=f'{variant} (n={traces.shape[0]})', lw=1.5)
        ax.fill_between(t_s, q25, q75, color=color, alpha=0.15)
    ax.axvline(0.0, color='k', lw=0.5, alpha=0.4)
    ax.set_xlabel('time from tack midpoint (s)')
    ax.set_ylabel('|TWD error| (deg)  median + IQR')
    ax.set_title(f'Tack-window |error| — {session_label}')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper right', fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


def plot_timeseries(out_path: Path, session_label: str,
                    t_s: np.ndarray, rtwd: np.ndarray,
                    twd_by_variant: Dict[str, np.ndarray]) -> None:
    # Unwrap around the median rtwd to make the plot readable.
    ref = float(np.nanmedian(rtwd))
    def unwrap_to(v):
        return ref + _wrap180(v - ref)
    fig, ax = plt.subplots(figsize=(11, 3.2), dpi=120)
    ax.plot(t_s, unwrap_to(rtwd), color='k', label='rtwd (boat)', lw=0.8, alpha=0.8)
    for variant, twd in twd_by_variant.items():
        ax.plot(t_s, unwrap_to(twd), color=VARIANT_COLORS[variant], label=variant,
                lw=0.7, alpha=0.85)
    ax.set_xlabel('time (s)')
    ax.set_ylabel('TWD (deg, unwrapped to median)')
    ax.set_title(f'TWD timeseries — {session_label}')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='upper right', fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path)
    plt.close(fig)


# --------------------------------------------------------------------
# Main
# --------------------------------------------------------------------

def run_variant(name: str, variant: str, est_df: pd.DataFrame, fs_hz: float,
                cal: Calibration) -> pd.DataFrame:
    if variant == 'linear':
        return offline_water_wind_rts(est_df, fs_hz=fs_hz, cal=cal)
    if variant == 'huber_only':
        cfg = AdaptiveFilterConfig(
            alpha_R_turn=0.0,
            alpha_R_accel=0.0,
            q_growth=1.0,  # disable Q inflation
        )
        return offline_water_wind_adaptive(est_df, fs_hz=fs_hz, cal=cal, adaptive_cfg=cfg)
    if variant == 'adaptive':
        return offline_water_wind_adaptive(est_df, fs_hz=fs_hz, cal=cal)
    raise ValueError(variant)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    t0 = time.time()

    cal = load_calibration()
    print(f"Calibration loaded from {CAL_PATH.name}:")
    print(f"  stw_scale={cal.stw_scale:.4f}  heading_offset={cal.heading_offset_deg:+.3f}°  "
          f"awa_off={cal.awa_offset_deg:+.3f}°  awa_sc_uw={cal.awa_scale_upwind:.4f}  "
          f"aws_sc_uw={cal.aws_scale_upwind:.4f}")
    print()

    summary_rows: List[Dict] = []
    per_session_bundles: List[Dict] = []

    for sid, is_2019, label in SESSIONS:
        print(f"=== {label} ===")
        t_s0 = time.time()
        loaded = load_session(sid, is_2019)
        if loaded is None:
            continue
        est_df, rtwd, rtws, fs_hz = loaded
        print(f"  loaded n={len(est_df)} ({len(est_df) / fs_hz / 60:.1f} min)")

        valid_mask = build_valid_mask(est_df, rtwd, rtws, fs_hz)
        print(f"  valid samples: {int(valid_mask.sum())} / {len(est_df)}")

        # Tack centers.
        centers = get_tack_centers(sid, est_df, fs_hz)
        print(f"  tacks: {len(centers)} (source: {'maneuvers.csv' if load_maneuvers_for_session(sid) is not None else 'detect_tacks'})")

        twd_by_variant: Dict[str, np.ndarray] = {}
        rolling_by_variant: Dict[str, np.ndarray] = {}
        tack_traces_by_variant: Dict[str, np.ndarray] = {}

        # Pre-compute boat dynamics once (shared across variants, used for
        # steady-leg filtering). Use a wider smoothing than the adaptive
        # filter's 5-sample boxcar so genuinely steady autopilot legs don't
        # fall foul of 10 Hz heading jitter.
        hdg_arr = est_df['hdg'].to_numpy(dtype=float)
        spd_arr = est_df['spd'].to_numpy(dtype=float)
        turn_rate_shared, accel_shared = _compute_boat_dynamics(hdg_arr, spd_arr, fs_hz)
        # Further smooth for steady-leg gate.
        turn_rate_smooth = centered_boxcar(turn_rate_shared, int(round(3.0 * fs_hz)))
        accel_smooth = centered_boxcar(accel_shared, int(round(3.0 * fs_hz)))

        row: Dict = {'session': sid, 'label': label, 'n_valid': int(valid_mask.sum()), 'n_tacks': int(len(centers))}

        for variant in ('linear', 'huber_only', 'adaptive'):
            t_run = time.time()
            try:
                out = run_variant(sid, variant, est_df, fs_hz, cal)
            except Exception as exc:
                print(f"  {variant}: FAIL  {type(exc).__name__}: {exc}")
                continue
            dt_run = time.time() - t_run
            twd = out['twd'].to_numpy(dtype=float)
            twd_by_variant[variant] = twd

            m = full_session_metrics(twd, rtwd, valid_mask)
            rolling = rolling_rms_trace(twd, rtwd, valid_mask,
                                        window=int(round(60.0 * fs_hz)))
            rolling_by_variant[variant] = rolling

            slr = steady_leg_rms(twd, rtwd, valid_mask, turn_rate_smooth, accel_smooth, fs_hz)

            traces = tack_error_traces(twd, rtwd, valid_mask, centers, fs_hz)
            tack_traces_by_variant[variant] = traces
            tm = tack_metrics(traces, fs_hz)

            row[f'{variant}_rms_deg'] = m['rms_deg']
            row[f'{variant}_circmean_deg'] = m['circmean_deg']
            row[f'{variant}_tack_peak_deg'] = tm['median_peak_deg']
            row[f'{variant}_tack_decay_s'] = tm['median_decay_s']
            row[f'{variant}_steady_rms_deg'] = slr if slr is not None else float('nan')
            row[f'{variant}_wall_s'] = dt_run

            print(f"  {variant:10s}: RMS={m['rms_deg']:5.2f}°  circmean={m['circmean_deg']:+5.2f}°  "
                  f"tack_peak={tm['median_peak_deg']:5.2f}°  tack_decay={tm['median_decay_s']:4.1f}s  "
                  f"steady={slr if slr is not None else float('nan'):5.2f}°  ({dt_run:.2f}s)")

        # Plots.
        t_s = np.arange(len(est_df)) / fs_hz
        plot_rolling_rms(OUT_DIR / f'rolling_rms_{sid.replace(":", "-")}.png',
                         label, t_s, rolling_by_variant)
        plot_timeseries(OUT_DIR / f'timeseries_{sid.replace(":", "-")}.png',
                        label, t_s, rtwd, twd_by_variant)
        if any(v.size > 0 for v in tack_traces_by_variant.values()):
            plot_tack_overlay(OUT_DIR / f'tack_overlay_{sid.replace(":", "-")}.png',
                              label, tack_traces_by_variant, fs_hz)

        summary_rows.append(row)
        per_session_bundles.append({
            'sid': sid, 'label': label,
            'rolling_by_variant': rolling_by_variant,
            'twd_by_variant': twd_by_variant,
            'rtwd': rtwd, 'valid_mask': valid_mask, 'fs_hz': fs_hz,
        })
        print(f"  session wall: {time.time() - t_s0:.2f}s")
        print()

    # Write summary.
    summary_df = pd.DataFrame(summary_rows)
    csv_path = OUT_DIR / 'summary.csv'
    summary_df.to_csv(csv_path, index=False)
    md_path = OUT_DIR / 'summary.md'
    _write_markdown_summary(md_path, summary_df)

    # Pick winners.
    _pick_winners(summary_df)

    total = time.time() - t0
    print(f"\nTotal wall: {total:.1f}s")
    print(f"Plots -> {OUT_DIR}")

    return 0


def _write_markdown_summary(path: Path, df: pd.DataFrame) -> None:
    if df.empty:
        path.write_text("No sessions successfully evaluated.\n")
        return
    rows = []
    rows.append("# Adaptive TWD estimator evaluation\n")
    rows.append("Three variants compared on five sessions. See per-session plots alongside this summary.\n")
    rows.append(f"Calibration: `{CAL_PATH.name}`\n")
    rows.append("")
    rows.append("## Full-session RMS (deg)")
    rows.append("")
    rows.append("| session | n_valid | n_tacks | linear | huber_only | adaptive |")
    rows.append("|---|---:|---:|---:|---:|---:|")
    for _, r in df.iterrows():
        rows.append(
            f"| {r['session']} | {int(r['n_valid'])} | {int(r['n_tacks'])} | "
            f"{r['linear_rms_deg']:.2f} | {r['huber_only_rms_deg']:.2f} | {r['adaptive_rms_deg']:.2f} |"
        )
    rows.append("")
    rows.append("## Tack-window median peak |error| (deg)")
    rows.append("")
    rows.append("| session | linear | huber_only | adaptive |")
    rows.append("|---|---:|---:|---:|")
    for _, r in df.iterrows():
        rows.append(
            f"| {r['session']} | {r['linear_tack_peak_deg']:.2f} | "
            f"{r['huber_only_tack_peak_deg']:.2f} | {r['adaptive_tack_peak_deg']:.2f} |"
        )
    rows.append("")
    rows.append("## Tack-window median decay time (s, peak -> 50% of peak)")
    rows.append("")
    rows.append("| session | linear | huber_only | adaptive |")
    rows.append("|---|---:|---:|---:|")
    for _, r in df.iterrows():
        rows.append(
            f"| {r['session']} | {r['linear_tack_decay_s']:.1f} | "
            f"{r['huber_only_tack_decay_s']:.1f} | {r['adaptive_tack_decay_s']:.1f} |"
        )
    rows.append("")
    rows.append("## Steady-leg RMS (deg, windows with <1°/s turn + <0.1 kn/s accel, >=30 s)")
    rows.append("")
    rows.append("| session | linear | huber_only | adaptive |")
    rows.append("|---|---:|---:|---:|")
    for _, r in df.iterrows():
        rows.append(
            f"| {r['session']} | {r['linear_steady_rms_deg']:.2f} | "
            f"{r['huber_only_steady_rms_deg']:.2f} | {r['adaptive_steady_rms_deg']:.2f} |"
        )
    rows.append("")
    path.write_text("\n".join(rows))


def _pick_winners(df: pd.DataFrame) -> None:
    if df.empty:
        return
    print("\n--- per-session winners (lowest full-session RMS) ---")
    for _, r in df.iterrows():
        vals = {
            'linear': r['linear_rms_deg'],
            'huber_only': r['huber_only_rms_deg'],
            'adaptive': r['adaptive_rms_deg'],
        }
        best = min(vals, key=vals.get)
        print(f"  {r['session']}: {best} ({vals[best]:.2f}°)")
    print("\n--- per-session winners (lowest tack-peak) ---")
    for _, r in df.iterrows():
        vals = {
            'linear': r['linear_tack_peak_deg'],
            'huber_only': r['huber_only_tack_peak_deg'],
            'adaptive': r['adaptive_tack_peak_deg'],
        }
        vals = {k: v for k, v in vals.items() if np.isfinite(v)}
        if not vals:
            continue
        best = min(vals, key=vals.get)
        print(f"  {r['session']}: {best} ({vals[best]:.2f}°)")


if __name__ == '__main__':
    raise SystemExit(main())
