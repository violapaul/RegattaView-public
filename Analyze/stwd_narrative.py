#!/usr/bin/env python
"""Narrative plots for the new vector-state Kalman + RTS true-wind
estimator at ``Analyze/true_wind_estimator.py``.

Water-ref and ground-ref lines use the Huber-clipped adaptive variant
(``huber_only`` config); see ``Analyze/Plots/adaptive_eval/summary.md``
for the A/B justification.

Produces six presentation-quality PNGs under
``Analyze/Plots/stwd_narrative/``.  Each TWD panel carries three
lines:

  - ``rtwd`` on-boat reference (PGN 130306)              — thin black solid
  - **water-ref RTS** (SPD + HDG+variation)              — brown,  medium
  - **ground-ref RTS** (SOG + COG)    THE PROPOSED OUTPUT — blue,   thick

The two "RTS" lines are the ``offline_ground_wind_adaptive`` /
``offline_water_wind_adaptive`` smoothers from the new estimator
module, invoked with the ``huber_only`` config.
Calibration is the **global water-ref identification fit** written by
``Analyze/calibrate_water_ref.py`` at
``Analyze/calibration_water_ref_2026.json``; both the water-ref and
ground-ref paths use the same calibration (see
``build_session_refs`` — tradeoff noted there).  If the file is
missing, ``load_calibration_water_ref`` raises; run
``calibrate_water_ref.py`` first.

Figures (filenames preserved from the previous revision):
  1. ``tack_clean.png``            — 70th-percentile tack from 2026-04-19T08_59
  2. ``gybe_clean.png``            — cleanest gybe, same session
  3. ``steady_leg.png``            — >=60 s steady leg, 4-panel variant
  4. ``rsog_glitch_recovery.png``  — rsog-spike event from 2025-08-02T09_41
  5. ``session_overview.png``      — full 2026-04-19T08_59, 1 Hz decimated
  6. ``current_drag_shear.png``    — max-|SOG-SPD| session, shows the
                                      water-vs-ground-ref bias and its
                                      first-order prediction.

All inputs are read-only; this script does not modify ``Libraries/``.

Run:
    conda run -n sailing --no-capture-output python Analyze/stwd_narrative.py
"""
from __future__ import annotations

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import datetime as dt
import json
import time
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from Libraries.global_variables import G
from Libraries import race_logs

from Analyze.true_wind_estimator import (
    AdaptiveFilterConfig,
    Calibration,
    detect_tacks,
    instantaneous_ground_wind,
    offline_ground_wind_adaptive,
    offline_water_wind_adaptive,
)
import glob

# The Huber-only config is the benchmark winner (see
# Analyze/Plots/adaptive_eval/summary.md). Dynamic R/Q are disabled
# because the default full-adaptive gains starve the RTS at tacks.
HUBER_ONLY_CFG = AdaptiveFilterConfig(
    alpha_R_turn=0.0,
    alpha_R_accel=0.0,
    q_growth=1.0,
    q_decay=1.0,
    huber_delta=1.5,
)

G.init_peer_gynt()


# ----------------------------------------------------------------------
# Paths / constants
# ----------------------------------------------------------------------

LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
OUTPUT_DIR = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "Plots", "stwd_narrative",
)

CLEAN_SESSION = "2026-04-19T08_59.parquet"
GLITCH_SESSION = "2025-08-02T09_41.parquet"

# rsog / rspd pre-clean threshold: any sample whose absolute speed
# exceeds this gets replaced by a linear interpolation from neighbors.
SPEED_GLITCH_KT = 40.0

# Plot color conventions (see module docstring).
RTWD_COLOR    = 'black'
WATER_COLOR   = '#8c564b'  # brown
GROUND_COLOR  = '#1f77b4'  # blue (the proposed estimator output)


# ----------------------------------------------------------------------
# Tiny helpers (copied inline; kept self-contained).
# ----------------------------------------------------------------------

def _wrap_signed_deg(a) -> np.ndarray:
    """Wrap an angle (deg) into [-180, 180)."""
    a = np.asarray(a, dtype=float)
    return (a + 180.0) % 360.0 - 180.0


def _circ_median_deg(values: np.ndarray) -> float:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return 0.0
    r = np.deg2rad(v)
    return float(np.rad2deg(
        np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r))))) % 360.0


def _circmean_deg_signed(values: np.ndarray) -> float:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return float('nan')
    r = np.deg2rad(v)
    return float(np.rad2deg(
        np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


def _unwrap_to_center(values: np.ndarray, center: float) -> np.ndarray:
    """Map compass angles into [center-180, center+180)."""
    v = np.asarray(values, dtype=float).copy()
    return center + _wrap_signed_deg(v - center)


def _session_local_ts(df: pd.DataFrame, idx: int) -> str:
    """HH:MM:SS Seattle local (UTC-7 during both Apr and Aug here)."""
    try:
        utc = dt.datetime.utcfromtimestamp(float(df.unixtime.iloc[idx]))
        local = utc - dt.timedelta(hours=7)
        return local.strftime('%H:%M:%S')
    except Exception:
        return '??:??:??'


def _slice(df: pd.DataFrame, lo: int, hi: int) -> slice:
    lo = max(0, int(lo))
    hi = min(len(df), int(hi))
    return slice(lo, hi)


def _pick_ylim(arrays, min_span: float, pad_frac: float = 0.15,
               pad_min: float = 0.0) -> tuple[float, float]:
    """Tight y-axis helper.

    arrays: iterable of 1-D arrays (all series in the panel).
    min_span: smallest allowed visible span in axis units.
    pad_frac, pad_min: padding above/below the data range.
    """
    parts = []
    for a in arrays:
        arr = np.asarray(a, dtype=float)
        if arr.size == 0:
            continue
        finite = arr[np.isfinite(arr)]
        if finite.size:
            parts.append(finite)
    if not parts:
        return (-1.0, 1.0)
    values = np.concatenate(parts)
    lo, hi = float(np.min(values)), float(np.max(values))
    pad = max(pad_min, pad_frac * (hi - lo))
    lo, hi = lo - pad, hi + pad
    if hi - lo < min_span:
        mid = 0.5 * (lo + hi)
        lo, hi = mid - 0.5 * min_span, mid + 0.5 * min_span
    return (lo, hi)


def _rms(v: np.ndarray) -> float:
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return float('nan')
    return float(np.sqrt(np.mean(v ** 2)))


def _p95_abs(v: np.ndarray) -> float:
    v = np.asarray(v, dtype=float)
    v = v[np.isfinite(v)]
    if v.size == 0:
        return float('nan')
    return float(np.percentile(np.abs(v), 95))


# ----------------------------------------------------------------------
# Speed glitch pre-cleaner
# ----------------------------------------------------------------------

def _clean_speed_glitches(values_ms: np.ndarray,
                          threshold_kt: float = SPEED_GLITCH_KT,
                          ) -> tuple[np.ndarray, np.ndarray]:
    """Replace any sample with |speed| > threshold_kt by linear
    interpolation from finite neighbors.

    Returns (cleaned_ms, mask_replaced).  Operates on m/s input; the
    threshold is expressed in knots for readability.
    """
    v = np.asarray(values_ms, dtype=float).copy()
    kt = np.abs(v) * G.MS_2_KNOTS
    bad = np.isfinite(v) & (kt > threshold_kt)
    if not bad.any():
        return v, bad
    # NaN-out the bad samples so interp skips them, then interp.
    good = np.isfinite(v) & ~bad
    idx = np.arange(len(v), dtype=float)
    if good.sum() < 2:
        # Not enough anchors; leave original (caller will log).
        return v, bad
    v[bad] = np.interp(idx[bad], idx[good], v[good])
    return v, bad


# ----------------------------------------------------------------------
# DataFrame prep for the new estimator
# ----------------------------------------------------------------------

def build_estimator_df(df: pd.DataFrame,
                       ) -> tuple[pd.DataFrame, dict]:
    """Project ``df`` (from ``race_logs.read_log_file``) onto the columns
    the new estimator expects, using **raw** signals (rawa, raws, rhdg,
    rcog, rsog, rspd) and converting speeds to knots.

    Raw signals bypass the heel correction and pipeline smoothing that
    ``race_logs.process_sensors`` applies upstream — this is what the
    boat itself sees, and what the water-ref calibration
    (``Analyze/calibration_water_ref_2026.json``) was fit against.

    Calibration.heading_offset_deg is used = 0; we pass HDG_true =
    rhdg + G.MAGNETIC_VARIATION directly.  This mirrors the convention
    in ``true_wind_estimator.rotate_boat_to_earth`` which expects
    compass angle measured clockwise from true north.

    Returns (est_df, stats) where ``stats`` reports how many rsog/rspd
    samples were pre-cleaned.
    """
    rsog_ms_clean, rsog_bad = _clean_speed_glitches(df['rsog'].to_numpy())
    rspd_ms_clean, rspd_bad = _clean_speed_glitches(df['rspd'].to_numpy())

    hdg_true = (df['rhdg'].to_numpy(dtype=float)
                + G.MAGNETIC_VARIATION) % 360.0

    est_df = pd.DataFrame({
        'awa': df['rawa'].to_numpy(dtype=float),
        'aws': df['raws'].to_numpy(dtype=float) * G.MS_2_KNOTS,
        'hdg': hdg_true,
        'sog': rsog_ms_clean * G.MS_2_KNOTS,
        'cog': df['rcog'].to_numpy(dtype=float),
        'spd': rspd_ms_clean * G.MS_2_KNOTS,
    }, index=df.index)

    stats = {
        'rsog_glitches': int(rsog_bad.sum()),
        'rspd_glitches': int(rspd_bad.sum()),
        'rsog_bad_mask': rsog_bad,
        'rspd_bad_mask': rspd_bad,
    }
    return est_df, stats


# ----------------------------------------------------------------------
# Per-session reference pipeline
# ----------------------------------------------------------------------

def load_calibration_water_ref() -> tuple[Calibration, str]:
    """Load the global water-ref calibration fit written by
    ``Analyze/calibrate_water_ref.py``.

    This is the identification fit that reproduces the boat's on-board
    rtwd/rtws.  We use the same calibration for BOTH the water-ref and
    ground-ref paths below — a simplification.  The ground-ref path may
    benefit from a separately-fit calibration, but for now we prioritize
    the water-ref reproduction (that's the whole point of this fit).
    """
    path = Path(__file__).with_name('calibration_water_ref_2026.json')
    if not path.exists():
        raise RuntimeError(
            f"Missing {path}. Run calibrate_water_ref.py first.")
    data = json.loads(path.read_text())
    cal = Calibration(**data['calibration'])
    desc = (f"global water-ref fit ({data['n_valid_samples']} samples, "
            f"{len(data['sessions_loaded'])} sessions)")
    return cal, desc


def build_session_refs(df: pd.DataFrame, session_id: str) -> dict:
    """Compute: estimator DataFrame, calibration (global), ground-ref
    RTS, water-ref RTS, instantaneous ground wind.

    Uses the global water-ref calibration for BOTH rts_ground and
    rts_water.  Tradeoff: the ground-ref path is no longer
    independently optimized for tack-invariance — the water-ref
    reproduction is the priority.

    Returns a dict with:
      - est_df, stats
      - cal, cal_desc
      - rts_ground, rts_water, inst_ground
    """
    fs_hz = float(G.SAMPLES_PER_SECOND)
    est_df, stats = build_estimator_df(df)
    print(f"  [prep] rsog glitches pre-cleaned: {stats['rsog_glitches']}  "
          f"rspd glitches pre-cleaned: {stats['rspd_glitches']}",
          flush=True)

    cal, cal_desc = load_calibration_water_ref()
    print(f"  [cal] {session_id}: {cal_desc}", flush=True)
    print(f"        hdg_off={cal.heading_offset_deg:+.3f}°  "
          f"awa_off={cal.awa_offset_deg:+.3f}°  "
          f"awa_sc_up={cal.awa_scale_upwind:.3f}  "
          f"awa_sc_dn={cal.awa_scale_downwind:.3f}  "
          f"aws_sc_up={cal.aws_scale_upwind:.3f}  "
          f"aws_sc_dn={cal.aws_scale_downwind:.3f}  "
          f"stw_sc={cal.stw_scale:.3f}", flush=True)

    t0 = time.time()
    rts_ground = offline_ground_wind_adaptive(
        est_df, fs_hz=fs_hz, cal=cal, adaptive_cfg=HUBER_ONLY_CFG)
    t_g = time.time() - t0
    t0 = time.time()
    rts_water = offline_water_wind_adaptive(
        est_df, fs_hz=fs_hz, cal=cal, adaptive_cfg=HUBER_ONLY_CFG)
    t_w = time.time() - t0
    t0 = time.time()
    inst_ground = instantaneous_ground_wind(est_df, fs_hz=fs_hz, cal=cal,
                                            align_gps_offline=True)
    t_i = time.time() - t0
    print(f"  [run] ground-RTS {t_g:.1f}s  water-RTS {t_w:.1f}s  "
          f"inst-ground {t_i:.1f}s", flush=True)

    return {
        'est_df': est_df,
        'stats': stats,
        'cal': cal,
        'cal_desc': cal_desc,
        'rts_ground': rts_ground,
        'rts_water': rts_water,
        'inst_ground': inst_ground,
    }


# ----------------------------------------------------------------------
# Event figure (6-panel, or 4-panel for the steady leg)
# ----------------------------------------------------------------------

def plot_event_figure(df: pd.DataFrame, refs: dict,
                      mid_idx: int, window_s: float,
                      title: str, out_path: str,
                      four_panel: bool = False) -> None:
    fs = int(G.SAMPLES_PER_SECOND)
    half_n = int(window_s * fs)
    lo = mid_idx - half_n
    hi = mid_idx + half_n + 1
    sl = _slice(df, lo, hi)
    n = sl.stop - sl.start
    if n <= 10:
        print(f"[narrative] {title}: window too small, skipping",
              file=sys.stderr, flush=True)
        return
    t = (np.arange(sl.start, sl.stop) - mid_idx) / fs

    rts_g = refs['rts_ground']
    rts_w = refs['rts_water']
    stats = refs['stats']

    # --- TWD series (unwrap each to the rtwd median for tight axes) ---
    rtwd = df['rtwd'].iloc[sl].values
    ground_twd = rts_g['twd'].to_numpy()[sl]
    water_twd = rts_w['twd'].to_numpy()[sl]

    center = _circ_median_deg(rtwd)
    if not np.isfinite(center) or np.sum(np.isfinite(rtwd)) < 5:
        center = _circ_median_deg(ground_twd)
    rtwd_u = _unwrap_to_center(rtwd, center)
    ground_twd_u = _unwrap_to_center(ground_twd, center)
    water_twd_u = _unwrap_to_center(water_twd, center)
    twd_lo, twd_hi = _pick_ylim(
        [rtwd_u, water_twd_u, ground_twd_u],
        min_span=8.0, pad_frac=0.15, pad_min=2.0)

    # --- TWS series (all knots) ---
    rtws_kt = df['rtws'].iloc[sl].values * G.MS_2_KNOTS
    ground_tws_kt = rts_g['tws'].to_numpy()[sl]
    water_tws_kt = rts_w['tws'].to_numpy()[sl]
    tws_lo, tws_hi = _pick_ylim(
        [rtws_kt, water_tws_kt, ground_tws_kt],
        min_span=1.0, pad_frac=0.15, pad_min=0.3)

    # --- AWA / AWS ---
    rawa = _wrap_signed_deg(df['rawa'].iloc[sl].values)
    cawa = _wrap_signed_deg(df['cawa'].iloc[sl].values)
    raws_kt = df['raws'].iloc[sl].values * G.MS_2_KNOTS
    caws_kt = df['caws'].iloc[sl].values * G.MS_2_KNOTS
    awa_lo, awa_hi = _pick_ylim([rawa, cawa],
                                min_span=20.0, pad_frac=0.15, pad_min=3.0)
    aws_lo, aws_hi = _pick_ylim([raws_kt, caws_kt],
                                min_span=2.0, pad_frac=0.15, pad_min=0.5)

    # --- HDG (magnetic) ---
    rhdg = df['rhdg'].iloc[sl].values
    hdg_center = _circ_median_deg(rhdg)
    rhdg_u = _unwrap_to_center(rhdg, hdg_center)
    hdg_lo, hdg_hi = _pick_ylim([rhdg_u], min_span=30.0,
                                pad_frac=0.15, pad_min=5.0)

    # --- Boat motion (SOG + SPD, both kt, pre-cleaned) ---
    rsog_kt_raw = df['rsog'].iloc[sl].values * G.MS_2_KNOTS
    sog_kt = refs['est_df']['sog'].iloc[sl].values
    spd_kt = refs['est_df']['spd'].iloc[sl].values
    bad_sog = stats['rsog_bad_mask'][sl]
    bad_spd = stats['rspd_bad_mask'][sl]

    # Pick ylim on the CLEANED series so a raw 600-kt spike doesn't blow the axis.
    mot_lo, mot_hi = _pick_ylim(
        [sog_kt, spd_kt],
        min_span=1.0, pad_frac=0.15, pad_min=0.3)

    # --- Figure ---
    if four_panel:
        n_panels = 4
        figsize = (6.5, 7.0)
    else:
        n_panels = 6
        figsize = (6.5, 10.0)
    fig, axes = plt.subplots(n_panels, 1, sharex=True, figsize=figsize)
    fig.subplots_adjust(hspace=0.28)

    # Panel 1: TWD.  Legend in top panel only.
    ax = axes[0]
    ax.plot(t, rtwd_u, color=RTWD_COLOR, linewidth=0.9, zorder=2,
            label='rtwd (on-boat)')
    ax.plot(t, water_twd_u, color=WATER_COLOR, linewidth=1.4, zorder=3,
            label='water-ref (B&G-style)')
    ax.plot(t, ground_twd_u, color=GROUND_COLOR, linewidth=2.2, zorder=5,
            label='ground-ref (proposed estimator)')
    ax.set_ylim(twd_lo, twd_hi)
    ax.set_ylabel(f'TWD deg true ({twd_lo:.0f}-{twd_hi:.0f})')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc='best', ncol=1, framealpha=0.85)
    ax.set_title(title, fontsize=9)

    # Panel 2: TWS.
    ax = axes[1]
    ax.plot(t, rtws_kt, color=RTWD_COLOR, linewidth=0.9, zorder=2)
    ax.plot(t, water_tws_kt, color=WATER_COLOR, linewidth=1.4, zorder=3)
    ax.plot(t, ground_tws_kt, color=GROUND_COLOR, linewidth=2.2, zorder=5)
    ax.set_ylim(tws_lo, tws_hi)
    ax.set_ylabel(f'TWS kt ({tws_lo:.1f}-{tws_hi:.1f})')
    ax.grid(True, alpha=0.3)

    # Panel 3: AWA.
    ax = axes[2]
    ax.plot(t, rawa, color='0.65', linewidth=0.7, label='rawa (raw)')
    ax.plot(t, cawa, color='0.2', linewidth=1.0, label='cawa (heel-corr)')
    ax.set_ylim(awa_lo, awa_hi)
    ax.set_ylabel(f'AWA deg ({awa_lo:.0f}-{awa_hi:.0f})')
    ax.grid(True, alpha=0.3)

    # Panel 4: AWS.
    ax = axes[3]
    ax.plot(t, raws_kt, color='0.65', linewidth=0.7, label='raws (raw)')
    ax.plot(t, caws_kt, color='0.2', linewidth=1.0, label='caws (heel-corr)')
    ax.set_ylim(aws_lo, aws_hi)
    ax.set_ylabel(f'AWS kt ({aws_lo:.1f}-{aws_hi:.1f})')
    ax.grid(True, alpha=0.3)

    if four_panel:
        axes[-1].set_xlabel('t - t_event (s)')
        fig.tight_layout()
        fig.savefig(out_path, dpi=120)
        plt.close(fig)
        return

    # Panel 5: HDG.
    ax = axes[4]
    ax.plot(t, rhdg_u, color='0.3', linewidth=0.9, label='rhdg (mag)')
    ax.set_ylim(hdg_lo, hdg_hi)
    ax.set_ylabel(f'HDG deg mag ({hdg_lo:.0f}-{hdg_hi:.0f})')
    ax.grid(True, alpha=0.3)

    # Panel 6: Motion (SOG + SPD on same axis).
    ax = axes[5]
    ax.plot(t, sog_kt, color='C3', linewidth=1.0, label='SOG (cleaned)')
    ax.plot(t, spd_kt, color='C2', linewidth=1.0, label='SPD (cleaned)')
    # Mark pre-cleaned glitches with red x at the bottom of the axis.
    if bad_sog.any():
        t_bad = t[bad_sog]
        ax.plot(t_bad, np.full_like(t_bad, mot_lo, dtype=float),
                'x', color='red', markersize=8,
                label=f'rsog glitch (raw ~{rsog_kt_raw[bad_sog][0]:.0f} kt)')
    if bad_spd.any():
        t_bad = t[bad_spd]
        ax.plot(t_bad, np.full_like(t_bad, mot_lo, dtype=float),
                '+', color='orange', markersize=10,
                label='rspd glitch')
    ax.set_ylim(mot_lo, mot_hi)
    ax.set_ylabel(f'motion kt ({mot_lo:.1f}-{mot_hi:.1f})')
    ax.set_xlabel('t - t_event (s)')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc='best')

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


# ----------------------------------------------------------------------
# Session overview
# ----------------------------------------------------------------------

def plot_session_overview(df: pd.DataFrame, refs: dict,
                          session_id: str, out_path: str,
                          fs: int = 10, decimate_hz: int = 1) -> None:
    stride = max(1, fs // decimate_hz)
    sl = slice(0, len(df), stride)
    t = (np.arange(sl.start, sl.stop, sl.step) - sl.start) / fs / 60.0

    rtwd = df['rtwd'].iloc[sl].values
    ground_twd = refs['rts_ground']['twd'].to_numpy()[sl]
    water_twd = refs['rts_water']['twd'].to_numpy()[sl]
    center = _circ_median_deg(rtwd)
    if not np.isfinite(center):
        center = _circ_median_deg(ground_twd)
    rtwd_u = _unwrap_to_center(rtwd, center)
    ground_u = _unwrap_to_center(ground_twd, center)
    water_u = _unwrap_to_center(water_twd, center)

    rtws_kt = df['rtws'].iloc[sl].values * G.MS_2_KNOTS
    ground_tws_kt = refs['rts_ground']['tws'].to_numpy()[sl]
    water_tws_kt = refs['rts_water']['tws'].to_numpy()[sl]
    aws_kt = refs['est_df']['aws'].iloc[sl].values
    sog_kt = refs['est_df']['sog'].iloc[sl].values
    hdg = df['rhdg'].iloc[sl].values

    dur_hours = (len(df) / fs) / 3600.0
    title = (f"Session overview: {session_id.replace('.parquet', '')} "
             f"({dur_hours:.2f} h, 1 Hz)")

    fig, axes = plt.subplots(3, 1, sharex=True, figsize=(14, 6.5))

    # Panel 1: TWD.
    ax = axes[0]
    ax.plot(t, rtwd_u, color=RTWD_COLOR, linewidth=0.7, alpha=0.9, zorder=2,
            label='rtwd')
    ax.plot(t, water_u, color=WATER_COLOR, linewidth=0.9, alpha=0.9, zorder=3,
            label='water-ref (B&G-style)')
    ax.plot(t, ground_u, color=GROUND_COLOR, linewidth=1.1, alpha=0.9, zorder=5,
            label='ground-ref (proposed)')
    ax.set_ylabel('TWD (deg true)')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='best', fontsize=8, ncol=3)
    ax.set_title(title, fontsize=10)

    # Panel 2: AWS + TWS.
    ax = axes[1]
    ax.plot(t, aws_kt, color='0.55', linewidth=0.7, label='aws (apparent)')
    ax.plot(t, rtws_kt, color=RTWD_COLOR, linewidth=0.8, label='rtws')
    ax.plot(t, water_tws_kt, color=WATER_COLOR, linewidth=0.8,
            label='water-ref')
    ax.plot(t, ground_tws_kt, color=GROUND_COLOR, linewidth=1.0,
            label='ground-ref')
    ax.set_ylabel('Wind speed (kt)')
    ax.grid(True, alpha=0.3)
    ax.legend(loc='best', fontsize=8, ncol=2)

    # Panel 3: SOG (left) + HDG (right).
    ax = axes[2]
    ax.plot(t, sog_kt, color='C3', linewidth=0.8, label='SOG')
    ax.set_ylabel('SOG (kt)', color='C3')
    ax.tick_params(axis='y', labelcolor='C3')
    ax.grid(True, alpha=0.3)
    ax2 = ax.twinx()
    ax2.plot(t, hdg, color=GROUND_COLOR, linewidth=0.6, alpha=0.7, label='HDG')
    ax2.set_ylabel('HDG (deg mag)', color=GROUND_COLOR)
    ax2.tick_params(axis='y', labelcolor=GROUND_COLOR)
    ax.set_xlabel('time (minutes from session start)')

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)


# ----------------------------------------------------------------------
# Current-drag-shear figure
# ----------------------------------------------------------------------

def _load_rsog_rspd_only(parquet_path: str
                         ) -> Optional[tuple[np.ndarray, np.ndarray]]:
    try:
        df = pd.read_parquet(parquet_path,
                             columns=['sog', 'speed_water_referenced'])
    except Exception as exc:
        print(f"  skip {os.path.basename(parquet_path)}: {exc}",
              file=sys.stderr)
        return None
    return (np.asarray(df['sog'], dtype=float),
            np.asarray(df['speed_water_referenced'], dtype=float))


def pick_shear_session(sessions: list[str], logs_dir: str,
                       min_sog_ms: float = 0.5,
                       ) -> tuple[str, float]:
    """Return (filename, median_|rsog-rspd|_kt) with the largest median
    water/ground speed gap among samples that are finite and have
    |rsog| > min_sog_ms.
    """
    best = ('', -1.0)
    for sid in sessions:
        path = os.path.join(logs_dir, sid)
        pair = _load_rsog_rspd_only(path)
        if pair is None:
            continue
        rsog, rspd = pair
        mask = (np.isfinite(rsog) & np.isfinite(rspd)
                & (np.abs(rsog) > min_sog_ms))
        if mask.sum() < 50:
            continue
        d = np.abs(rsog[mask] - rspd[mask])
        med_kt = float(np.median(d)) * G.MS_2_KNOTS
        if med_kt > best[1]:
            best = (sid, med_kt)
    return best


def _choose_shear_window(df: pd.DataFrame, maneuvers: pd.DataFrame,
                         session_id: str, fs: int,
                         max_window_s: float = 900.0,
                         min_tacks: int = 3,
                         ) -> tuple[int, int, str]:
    rows = maneuvers[(maneuvers['session'] == session_id)
                     & (maneuvers['kind'] == 'tack')].copy()
    if rows.empty or len(rows) < min_tacks:
        return 0, len(df), (f"whole session (no >= {min_tacks} tacks)")
    mids = np.sort(rows['mid_idx'].astype(int).values)
    max_n = int(max_window_s * fs)
    best = None
    for i in range(len(mids)):
        for j in range(i + min_tacks - 1, len(mids)):
            span = mids[j] - mids[i]
            if span > max_n:
                break
            count = j - i + 1
            key = (count, -span)
            if best is None or key > best[0]:
                best = (key, mids[i], mids[j])
    if best is None:
        return 0, len(df), (f"whole session (tacks > {max_window_s:.0f}s apart)")
    _, first_mid, last_mid = best
    pad_n = int(60 * fs)
    start = max(0, int(first_mid) - pad_n)
    stop = min(len(df), int(last_mid) + pad_n)
    n_tacks = int(best[0][0])
    dur_s = (stop - start) / fs
    desc = f"{n_tacks} tacks over {dur_s:.0f} s (idx {start}..{stop})"
    return start, stop, desc


def plot_current_drag_shear(df: pd.DataFrame, refs: dict,
                            maneuvers: pd.DataFrame,
                            session_id: str,
                            median_shear_kt: float,
                            out_path: str,
                            ) -> dict:
    fs = int(G.SAMPLES_PER_SECOND)
    start, stop, desc = _choose_shear_window(df, maneuvers, session_id, fs)
    sl = slice(start, stop)
    n = stop - start
    if n <= 10:
        print(f"[narrative] shear window too small; skipping",
              file=sys.stderr)
        return {}

    t = np.arange(n) / fs / 60.0  # minutes
    rts_g = refs['rts_ground']
    rts_w = refs['rts_water']
    est_df = refs['est_df']

    rtwd = df['rtwd'].iloc[sl].values
    ground_twd = rts_g['twd'].to_numpy()[sl]
    water_twd = rts_w['twd'].to_numpy()[sl]
    ground_tws_kt = rts_g['tws'].to_numpy()[sl]
    center = _circ_median_deg(ground_twd)
    if not np.isfinite(center):
        center = _circ_median_deg(rtwd)
    rtwd_u = _unwrap_to_center(rtwd, center)
    ground_u = _unwrap_to_center(ground_twd, center)
    water_u = _unwrap_to_center(water_twd, center)
    twd_lo, twd_hi = _pick_ylim(
        [rtwd_u, water_u, ground_u],
        min_span=8.0, pad_frac=0.15, pad_min=2.0)

    # Current vector C = V_g - V_w in knots, earth frame (N, E).
    sog_kt = est_df['sog'].iloc[sl].values   # cleaned
    spd_kt = est_df['spd'].iloc[sl].values
    cog_rad = np.deg2rad(df['rcog'].iloc[sl].values)
    hdg_true_rad = np.deg2rad(
        (df['rhdg'].iloc[sl].values + G.MAGNETIC_VARIATION) % 360.0)
    c_n = sog_kt * np.cos(cog_rad) - spd_kt * np.cos(hdg_true_rad)
    c_e = sog_kt * np.sin(cog_rad) - spd_kt * np.sin(hdg_true_rad)
    c_mag_kt = np.hypot(c_n, c_e)
    c_dir_deg = np.rad2deg(np.arctan2(c_e, c_n)) % 360.0

    # 10-sample rolling median.
    c_mag_kt_sm = pd.Series(c_mag_kt).rolling(
        10, center=True, min_periods=1).median().values
    cs = np.cos(np.deg2rad(c_dir_deg))
    sn = np.sin(np.deg2rad(c_dir_deg))
    cs_sm = pd.Series(cs).rolling(10, center=True, min_periods=1).median().values
    sn_sm = pd.Series(sn).rolling(10, center=True, min_periods=1).median().values
    c_dir_deg_sm = np.rad2deg(np.arctan2(sn_sm, cs_sm)) % 360.0

    cawa = _wrap_signed_deg(df['cawa'].iloc[sl].values)

    # Empirical bias: water-ref - ground-ref, signed.
    bias_emp_deg = _wrap_signed_deg(water_u - ground_u)

    # First-order predicted bias (math review §7.1):
    #   ΔTWD = (180/π) * (|C| / TWS) * sin(C_dir − TWD_ground)  [deg]
    tws_safe = np.where((ground_tws_kt > 0.5) & np.isfinite(ground_tws_kt),
                        ground_tws_kt, np.nan)
    bias_pred_deg = np.rad2deg(
        (c_mag_kt_sm / tws_safe)
        * np.sin(np.deg2rad(c_dir_deg_sm) - np.deg2rad(ground_twd))
    )

    diff_mask = np.isfinite(bias_emp_deg) & np.isfinite(bias_pred_deg)
    pred_err = np.abs(bias_emp_deg[diff_mask] - bias_pred_deg[diff_mask])
    mean_abs_diff = float(np.mean(pred_err)) if pred_err.size else float('nan')
    p2p = (float(np.nanmax(bias_emp_deg) - np.nanmin(bias_emp_deg))
           if np.isfinite(bias_emp_deg).any() else float('nan'))

    # --- Layout ---
    fig, axes = plt.subplots(4, 1, sharex=True, figsize=(7.0, 11.0))
    fig.subplots_adjust(hspace=0.28)

    cal = refs['cal']
    calib_brief = (f"hdg_off {cal.heading_offset_deg:+.2f}°, "
                   f"awa_off {cal.awa_offset_deg:+.2f}°, "
                   f"awa_sc {cal.awa_scale_upwind:.2f}, "
                   f"stw_sc {cal.stw_scale:.2f}")
    title = (f"Current-drag shear — {session_id.replace('.parquet', '')}, "
             f"|C|_median={median_shear_kt:.2f} kt  ({calib_brief})")
    subtitle = f"window: {desc}"
    axes[0].set_title(title + "\n" + subtitle, fontsize=8)

    # Panel 1: TWD.
    ax = axes[0]
    ax.plot(t, rtwd_u, color=RTWD_COLOR, linewidth=0.9, zorder=2,
            label='rtwd (on-boat)')
    ax.plot(t, water_u, color=WATER_COLOR, linewidth=1.4, zorder=3,
            label='water-ref (B&G-style)')
    ax.plot(t, ground_u, color=GROUND_COLOR, linewidth=2.2, zorder=5,
            label='ground-ref (proposed)')
    ax.set_ylim(twd_lo, twd_hi)
    ax.set_ylabel(f'TWD deg true ({twd_lo:.0f}-{twd_hi:.0f})')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc='best', ncol=1, framealpha=0.85)

    # Panel 2: Current.
    ax = axes[1]
    cm_lo, cm_hi = _pick_ylim([c_mag_kt_sm], min_span=0.5,
                              pad_frac=0.15, pad_min=0.1)
    ax.plot(t, c_mag_kt_sm, color='C3', linewidth=1.2,
            label='|C| (kt, smoothed)')
    ax.set_ylim(cm_lo, cm_hi)
    ax.set_ylabel(f'|C| kt ({cm_lo:.2f}-{cm_hi:.2f})', color='C3')
    ax.tick_params(axis='y', labelcolor='C3')
    ax.grid(True, alpha=0.3)
    ax2 = ax.twinx()
    ax2.plot(t, c_dir_deg_sm, color='C4', linewidth=0.9,
             alpha=0.9, label='C_dir (deg true, flows toward)')
    ax2.set_ylabel('C_dir (deg true)', color='C4')
    ax2.tick_params(axis='y', labelcolor='C4')
    lns1, lbs1 = ax.get_legend_handles_labels()
    lns2, lbs2 = ax2.get_legend_handles_labels()
    ax.legend(lns1 + lns2, lbs1 + lbs2, fontsize=7, loc='best')

    # Panel 3: AWA (tack markers).
    ax = axes[2]
    awa_lo, awa_hi = _pick_ylim([cawa], min_span=60.0,
                                pad_frac=0.10, pad_min=5.0)
    ax.plot(t, cawa, color='0.2', linewidth=0.8, label='cawa')
    ax.axhline(0.0, color='k', linestyle=':', linewidth=0.6, alpha=0.5)
    ax.set_ylim(awa_lo, awa_hi)
    ax.set_ylabel(f'AWA deg ({awa_lo:.0f}-{awa_hi:.0f})')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc='best')

    # Panel 4: bias empirical vs first-order prediction.
    ax = axes[3]
    b_lo, b_hi = _pick_ylim([bias_emp_deg, bias_pred_deg],
                            min_span=8.0, pad_frac=0.15, pad_min=2.0)
    ax.plot(t, bias_emp_deg, color=WATER_COLOR, linewidth=1.2,
            label='empirical: water_ref - ground_ref')
    ax.plot(t, bias_pred_deg, color='C4', linewidth=1.0, alpha=0.9,
            linestyle='--',
            label=r'predicted: (180/$\pi$)$\cdot$(|C|/TWS)$\cdot$sin(C_dir - TWD)')
    ax.axhline(0.0, color='k', linestyle=':', linewidth=0.6, alpha=0.5)
    ax.set_ylim(b_lo, b_hi)
    ax.set_ylabel(f'TWD bias deg ({b_lo:.0f}-{b_hi:.0f})')
    ax.set_xlabel('time (minutes from window start)')
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7, loc='best')

    fig.tight_layout()
    fig.savefig(out_path, dpi=120)
    plt.close(fig)

    return {
        'window_desc': desc,
        'bias_p2p_deg': p2p,
        'bias_pred_mean_abs_diff_deg': mean_abs_diff,
    }


# ----------------------------------------------------------------------
# Event selection (unchanged from prior revision)
# ----------------------------------------------------------------------

def _list_session_parquets(logs_dir: str, pattern: str = '2026-*.parquet'
                           ) -> list[str]:
    """List parquet filenames in ``logs_dir`` matching ``pattern``.

    Replaces the old ``session_summary.csv`` → ``session_id`` lookup
    now that the audit CSVs are gone.
    """
    paths = sorted(glob.glob(os.path.join(logs_dir, pattern)))
    return [os.path.basename(p) for p in paths]


def _detect_session_maneuvers(df: pd.DataFrame, refs: dict,
                              fs: int, session_id: str,
                              ) -> pd.DataFrame:
    """Detect tacks/gybes on-the-fly for a single session's DataFrame.

    Replaces the legacy ``maneuvers.csv`` read. Uses
    :func:`Analyze.true_wind_estimator.detect_tacks` for the AWA sign-
    change event list, then classifies each event (tack if |AWA_mid|
    < 90° else gybe) and computes the summary stats the plotting
    helpers expect.

    Returned columns: ``session, kind, mid_idx, hdg_change_deg,
    mid_awa_abs_deg, rtws_mid, stwd_peak_abs``. All values are floats
    or ints; NaNs where sensors are missing.
    """
    det_df = pd.DataFrame({
        'awa': _wrap_signed_deg(df['rawa'].to_numpy()),
        'hdg': df['rhdg'].to_numpy(dtype=float),
    })
    mids = detect_tacks(det_df, fs_hz=float(fs))
    if len(mids) == 0:
        return pd.DataFrame(columns=[
            'session', 'kind', 'mid_idx', 'hdg_change_deg',
            'mid_awa_abs_deg', 'rtws_mid', 'stwd_peak_abs',
        ])

    n = len(df)
    rawa = _wrap_signed_deg(df['rawa'].to_numpy())
    rhdg_unwrap = np.rad2deg(np.unwrap(np.deg2rad(df['rhdg'].to_numpy(dtype=float))))
    rtwd = df['rtwd'].to_numpy(dtype=float)
    rtws_kt = df['rtws'].to_numpy(dtype=float) * G.MS_2_KNOTS
    water_twd = refs['rts_water']['twd'].to_numpy()

    peak_win = int(30 * fs)   # ±30 s for stwd_peak_abs
    hdg_win = int(10 * fs)    # ±10 s for pre/post heading difference
    awa_win = int(5 * fs)     # ±5 s median |AWA| at midpoint

    rows = []
    for m in mids:
        m = int(m)
        # Midpoint |AWA| (classifies tack vs gybe)
        i0 = max(0, m - awa_win); i1 = min(n, m + awa_win)
        mid_awa = float(np.nanmedian(np.abs(rawa[i0:i1])))
        # Heading change (unwrapped)
        pre_i = max(0, m - hdg_win); post_i = min(n - 1, m + hdg_win)
        hdg_change = abs(float(rhdg_unwrap[post_i] - rhdg_unwrap[pre_i]))
        # Peak |water_ref − rtwd| in ±30 s
        w0 = max(0, m - peak_win); w1 = min(n, m + peak_win)
        err = _wrap_signed_deg(water_twd[w0:w1] - rtwd[w0:w1])
        err = err[np.isfinite(err)]
        stwd_peak = float(np.nanmax(np.abs(err))) if err.size else float('nan')
        kind = 'tack' if mid_awa < 90.0 else 'gybe'
        rtws_mid = float(rtws_kt[m]) if np.isfinite(rtws_kt[m]) else float('nan')
        rows.append({
            'session': session_id,
            'kind': kind,
            'mid_idx': m,
            'hdg_change_deg': hdg_change,
            'mid_awa_abs_deg': mid_awa,
            'rtws_mid': rtws_mid,
            'stwd_peak_abs': stwd_peak,
        })
    return pd.DataFrame(rows)


def _detect_session_steady_legs(df: pd.DataFrame, refs: dict,
                                fs: int, session_id: str,
                                min_dur_s: float = 30.0,
                                turn_rate_thresh_dps: float = 2.0,
                                ) -> pd.DataFrame:
    """Detect steady-heading windows on-the-fly.

    Replaces the legacy ``steady_legs.csv`` read. Returns windows
    where the 5-sample-smoothed |turn rate| stays below
    ``turn_rate_thresh_dps`` for at least ``min_dur_s`` seconds. RMS
    is computed as ``sangle(water_ref_rts.twd − rtwd)`` in each window.

    Returned columns: ``session, start_idx, end_idx, duration_s,
    stwd_minus_rtwd_rms``.
    """
    hdg = df['rhdg'].to_numpy(dtype=float)
    rhdg_unwrap = np.rad2deg(np.unwrap(np.deg2rad(hdg)))
    turn_rate = np.gradient(rhdg_unwrap) * fs  # deg/s
    kernel = np.ones(5) / 5.0
    turn_rate_sm = np.convolve(turn_rate, kernel, mode='same')
    steady_mask = np.abs(turn_rate_sm) < turn_rate_thresh_dps

    rtwd = df['rtwd'].to_numpy(dtype=float)
    water_twd = refs['rts_water']['twd'].to_numpy()
    min_n = int(min_dur_s * fs)
    n = len(steady_mask)

    rows = []
    i = 0
    while i < n:
        if not steady_mask[i]:
            i += 1
            continue
        j = i
        while j < n and steady_mask[j]:
            j += 1
        if j - i >= min_n:
            err = _wrap_signed_deg(water_twd[i:j] - rtwd[i:j])
            err = err[np.isfinite(err)]
            rms = float(np.sqrt(np.mean(err * err))) if err.size else float('nan')
            rows.append({
                'session': session_id,
                'start_idx': i,
                'end_idx': j,
                'duration_s': (j - i) / float(fs),
                'stwd_minus_rtwd_rms': rms,
            })
        i = j
    return pd.DataFrame(rows, columns=[
        'session', 'start_idx', 'end_idx', 'duration_s',
        'stwd_minus_rtwd_rms',
    ])


def select_clean_tack_70p(maneuvers: pd.DataFrame, session: str,
                          max_hdg_change: float = 130.0,
                          ) -> Optional[dict]:
    """70th-percentile tack by stwd_peak_abs, with <130° hdg change."""
    rows = maneuvers[(maneuvers['session'] == session)
                     & (maneuvers['kind'] == 'tack')
                     & (maneuvers['hdg_change_deg'].astype(float)
                        < max_hdg_change)].copy()
    if rows.empty:
        return None
    rows['stwd_peak_abs'] = rows['stwd_peak_abs'].astype(float)
    rows['hdg_change_deg'] = rows['hdg_change_deg'].astype(float)
    rows = rows.sort_values('stwd_peak_abs').reset_index(drop=True)
    target_idx = int(round(0.70 * (len(rows) - 1)))
    for i in range(target_idx, len(rows)):
        hc = float(rows['hdg_change_deg'].iloc[i])
        if 50.0 <= hc <= 100.0:
            r = rows.iloc[i]
            return {'mid_idx': int(r['mid_idx']),
                    'hdg_change_deg': hc,
                    'mid_awa_abs_deg': float(r['mid_awa_abs_deg']),
                    'stwd_peak_abs': float(r['stwd_peak_abs']),
                    'rank_used': i, 'n_candidates': len(rows)}
    r = rows.iloc[target_idx]
    return {'mid_idx': int(r['mid_idx']),
            'hdg_change_deg': float(r['hdg_change_deg']),
            'mid_awa_abs_deg': float(r['mid_awa_abs_deg']),
            'stwd_peak_abs': float(r['stwd_peak_abs']),
            'rank_used': target_idx, 'n_candidates': len(rows)}


def select_clean_gybe(maneuvers: pd.DataFrame,
                      session: str) -> Optional[dict]:
    rows = maneuvers[(maneuvers['session'] == session)
                     & (maneuvers['kind'] == 'gybe')].copy()
    if rows.empty:
        return None
    rows['stwd_peak_abs'] = rows['stwd_peak_abs'].astype(float)
    rows = rows.sort_values('stwd_peak_abs')
    r = rows.iloc[0]
    return {'mid_idx': int(r['mid_idx']),
            'hdg_change_deg': float(r['hdg_change_deg']),
            'mid_awa_abs_deg': float(r['mid_awa_abs_deg']),
            'stwd_peak_abs': float(r['stwd_peak_abs'])}


def select_steady_leg(steady_legs: pd.DataFrame, session: str,
                      min_dur_s: float = 60.0) -> Optional[dict]:
    rows = steady_legs[(steady_legs['session'] == session)
                       & (steady_legs['duration_s'].astype(float)
                          >= min_dur_s)].copy()
    if rows.empty:
        return None
    rows['stwd_minus_rtwd_rms'] = rows['stwd_minus_rtwd_rms'].astype(float)
    rows = rows.sort_values('stwd_minus_rtwd_rms',
                            key=lambda s: np.abs(s))
    r = rows.iloc[0]
    return {'start_idx': int(r['start_idx']),
            'end_idx': int(r['end_idx']),
            'duration_s': float(r['duration_s']),
            'rms': float(r['stwd_minus_rtwd_rms'])}


def find_rsog_glitch(df: pd.DataFrame, thresh_kt: float = 40.0
                     ) -> Optional[int]:
    sog_kt = np.abs(df['rsog'].values) * G.MS_2_KNOTS
    idxs = np.where(sog_kt > thresh_kt)[0]
    if idxs.size == 0:
        return None
    return int(idxs[0])


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--logs-dir', default=LOGS_DIR)
    parser.add_argument('--output-dir', default=OUTPUT_DIR)
    args = parser.parse_args()

    os.makedirs(args.output_dir, exist_ok=True)
    t_start = time.time()

    # Session list for shear-session picker (replaces the legacy
    # ``session_summary.csv`` read from the deleted ``Plots/stwd_audit``).
    fs_hz_int = int(G.SAMPLES_PER_SECOND)
    sessions = _list_session_parquets(args.logs_dir)
    print(f"[narrative] scanning {len(sessions)} 2026 parquets "
          f"in {args.logs_dir}", flush=True)
    print(f"[narrative] scanning {len(sessions)} sessions for max "
          f"median |rsog-rspd|", flush=True)
    t0 = time.time()
    shear_session, shear_med_kt = pick_shear_session(sessions, args.logs_dir)
    print(f"  picked {shear_session}  median={shear_med_kt:.3f} kt  "
          f"({time.time()-t0:.1f} s)", flush=True)

    # --- Load clean session ---
    print(f"[narrative] loading {CLEAN_SESSION}", flush=True)
    df_clean = race_logs.read_log_file(
        CLEAN_SESSION, skip_dock_only=True, discard_columns=False,
        rename_columns=True, trim=True, process=True,
        path=args.logs_dir)
    if df_clean is None:
        print(f"[narrative] failed to load {CLEAN_SESSION}",
              file=sys.stderr)
        return 1

    print(f"[narrative] building refs for {CLEAN_SESSION}", flush=True)
    refs_clean = build_session_refs(df_clean, CLEAN_SESSION)

    # Detect maneuvers / steady legs for this session on the fly.
    maneuvers_clean = _detect_session_maneuvers(
        df_clean, refs_clean, fs=fs_hz_int, session_id=CLEAN_SESSION)
    steady_clean = _detect_session_steady_legs(
        df_clean, refs_clean, fs=fs_hz_int, session_id=CLEAN_SESSION)
    print(f"[narrative] {CLEAN_SESSION}: detected "
          f"{(maneuvers_clean['kind'] == 'tack').sum()} tacks, "
          f"{(maneuvers_clean['kind'] == 'gybe').sum()} gybes, "
          f"{len(steady_clean)} steady legs >= 30 s", flush=True)

    # --- Figure 1: tack_clean.png ---
    tack_pick = select_clean_tack_70p(maneuvers_clean, CLEAN_SESSION)
    if tack_pick is None:
        print(f"[narrative] no clean tack found", file=sys.stderr)
    else:
        ts = _session_local_ts(df_clean, tack_pick['mid_idx'])
        mid = tack_pick['mid_idx']
        title = (f"2026-04-19 — tack @ {ts} (70p pick "
                 f"{tack_pick['rank_used']+1}/{tack_pick['n_candidates']}), "
                 f"AWA mid {tack_pick['mid_awa_abs_deg']:.0f}°, "
                 f"d_hdg {tack_pick['hdg_change_deg']:.0f}°")
        out = os.path.join(args.output_dir, 'tack_clean.png')
        plot_event_figure(df_clean, refs_clean, mid,
                          window_s=90.0, title=title, out_path=out)
        print(f"  wrote {out}  (tack mid_idx={mid})", flush=True)

    # --- Figure 2: gybe_clean.png ---
    gybe = select_clean_gybe(maneuvers_clean, CLEAN_SESSION)
    if gybe is None:
        print(f"[narrative] no gybe on {CLEAN_SESSION}", file=sys.stderr)
    else:
        ts = _session_local_ts(df_clean, gybe['mid_idx'])
        title = (f"2026-04-19 — gybe @ {ts}, "
                 f"AWA mid {gybe['mid_awa_abs_deg']:.0f}°, "
                 f"d_hdg {gybe['hdg_change_deg']:.0f}°")
        out = os.path.join(args.output_dir, 'gybe_clean.png')
        plot_event_figure(df_clean, refs_clean, gybe['mid_idx'],
                          window_s=90.0, title=title, out_path=out)
        print(f"  wrote {out}  (gybe mid_idx={gybe['mid_idx']})", flush=True)

    # --- Figure 3: steady_leg.png (4-panel) ---
    steady = select_steady_leg(steady_clean, CLEAN_SESSION, min_dur_s=60.0)
    if steady is None:
        print(f"[narrative] no steady leg >=60s on {CLEAN_SESSION}",
              file=sys.stderr)
    else:
        mid = (steady['start_idx'] + steady['end_idx']) // 2
        half_s = steady['duration_s'] / 2.0
        ts = _session_local_ts(df_clean, mid)
        title = (f"2026-04-19 — steady leg @ {ts}, "
                 f"dur {steady['duration_s']:.0f} s, "
                 f"baseline RMS {abs(steady['rms']):.2f}°")
        out = os.path.join(args.output_dir, 'steady_leg.png')
        plot_event_figure(df_clean, refs_clean, mid,
                          window_s=half_s, title=title, out_path=out,
                          four_panel=True)
        print(f"  wrote {out}  (steady mid_idx={mid}, "
              f"half_s={half_s:.1f})", flush=True)

    # --- Figure 5: session_overview.png ---
    out = os.path.join(args.output_dir, 'session_overview.png')
    plot_session_overview(df_clean, refs_clean,
                          session_id=CLEAN_SESSION, out_path=out)
    print(f"  wrote {out}", flush=True)

    # --- Full-session quant: sangle differences vs rtwd ---
    rtwd_full = np.asarray(df_clean['rtwd'].values, dtype=float)
    water_twd_full = refs_clean['rts_water']['twd'].to_numpy()
    ground_twd_full = refs_clean['rts_ground']['twd'].to_numpy()
    d_water = _wrap_signed_deg(water_twd_full - rtwd_full)
    d_ground = _wrap_signed_deg(ground_twd_full - rtwd_full)
    water_vs_rtwd_circ = _circmean_deg_signed(d_water)
    water_vs_rtwd_rms = _rms(d_water)
    water_vs_rtwd_p95 = _p95_abs(d_water)
    ground_vs_rtwd_circ = _circmean_deg_signed(d_ground)
    ground_vs_rtwd_rms = _rms(d_ground)
    ground_vs_rtwd_p95 = _p95_abs(d_ground)
    print(f"[narrative] 2026-04-19 full session:", flush=True)
    print(f"  sangle(water_ref_rts - rtwd):  "
          f"circmean {water_vs_rtwd_circ:+.3f}°  "
          f"RMS {water_vs_rtwd_rms:.3f}°  "
          f"95p |·| {water_vs_rtwd_p95:.3f}°", flush=True)
    print(f"  sangle(ground_ref_rts - rtwd): "
          f"circmean {ground_vs_rtwd_circ:+.3f}°  "
          f"RMS {ground_vs_rtwd_rms:.3f}°  "
          f"95p |·| {ground_vs_rtwd_p95:.3f}°", flush=True)

    clean_cal = refs_clean['cal']
    clean_glitches = (refs_clean['stats']['rsog_glitches'],
                      refs_clean['stats']['rspd_glitches'])

    # --- Figure 6: current_drag_shear.png ---
    shear_stats = {}
    shear_cal = None
    shear_glitches = (0, 0)
    if shear_session == CLEAN_SESSION:
        shear_out = os.path.join(args.output_dir, 'current_drag_shear.png')
        shear_stats = plot_current_drag_shear(
            df_clean, refs_clean, maneuvers_clean,
            session_id=shear_session, median_shear_kt=shear_med_kt,
            out_path=shear_out)
        shear_cal = refs_clean['cal']
        shear_glitches = clean_glitches
        print(f"  wrote {shear_out}", flush=True)
    else:
        print(f"[narrative] loading shear session {shear_session}",
              flush=True)
        df_shear = race_logs.read_log_file(
            shear_session, skip_dock_only=True, discard_columns=False,
            rename_columns=True, trim=True, process=True,
            path=args.logs_dir)
        if df_shear is not None:
            print(f"[narrative] building refs for {shear_session}",
                  flush=True)
            refs_shear = build_session_refs(df_shear, shear_session)
            maneuvers_shear = _detect_session_maneuvers(
                df_shear, refs_shear, fs=fs_hz_int,
                session_id=shear_session)
            print(f"[narrative] {shear_session}: detected "
                  f"{(maneuvers_shear['kind'] == 'tack').sum()} tacks, "
                  f"{(maneuvers_shear['kind'] == 'gybe').sum()} gybes",
                  flush=True)
            shear_out = os.path.join(args.output_dir,
                                     'current_drag_shear.png')
            shear_stats = plot_current_drag_shear(
                df_shear, refs_shear, maneuvers_shear,
                session_id=shear_session,
                median_shear_kt=shear_med_kt,
                out_path=shear_out)
            shear_cal = refs_shear['cal']
            shear_glitches = (refs_shear['stats']['rsog_glitches'],
                              refs_shear['stats']['rspd_glitches'])
            print(f"  wrote {shear_out}", flush=True)
            del df_shear, refs_shear
        else:
            print(f"[narrative] failed to load shear session "
                  f"{shear_session}", file=sys.stderr)

    # Free clean session before loading glitch session.
    del df_clean, refs_clean

    # --- Load glitch session ---
    print(f"[narrative] loading {GLITCH_SESSION}", flush=True)
    df_glitch = race_logs.read_log_file(
        GLITCH_SESSION, skip_dock_only=True, discard_columns=False,
        rename_columns=True, trim=True, process=True,
        path=args.logs_dir)
    if df_glitch is None:
        print(f"[narrative] failed to load {GLITCH_SESSION}",
              file=sys.stderr)
        return 1

    print(f"[narrative] building refs for {GLITCH_SESSION}", flush=True)
    refs_glitch = build_session_refs(df_glitch, GLITCH_SESSION)
    glitch_cal = refs_glitch['cal']
    glitch_glitches = (refs_glitch['stats']['rsog_glitches'],
                       refs_glitch['stats']['rspd_glitches'])

    # --- Figure 4: rsog_glitch_recovery.png ---
    glitch_idx = find_rsog_glitch(df_glitch, thresh_kt=40.0)
    if glitch_idx is None:
        print(f"[narrative] no rsog glitch found on {GLITCH_SESSION}",
              file=sys.stderr)
    else:
        ts = _session_local_ts(df_glitch, glitch_idx)
        sog_val_kt = float(df_glitch.rsog.iloc[glitch_idx]) * G.MS_2_KNOTS
        title = (f"2025-08-02 — rsog glitch @ {ts} "
                 f"(raw rsog={sog_val_kt:.0f} kt → pre-cleaned by interp)")
        out = os.path.join(args.output_dir, 'rsog_glitch_recovery.png')
        plot_event_figure(df_glitch, refs_glitch, glitch_idx,
                          window_s=60.0, title=title, out_path=out)
        print(f"  wrote {out}  (glitch idx={glitch_idx})", flush=True)

    t_end = time.time()

    # --- Final handback ---
    print()
    print("[narrative] === HANDBACK SUMMARY ===", flush=True)
    print(f"  wall-clock: {t_end - t_start:.1f} s", flush=True)

    def _fsize(name: str) -> str:
        p = os.path.join(args.output_dir, name)
        if not os.path.exists(p):
            return f"{name}: MISSING"
        return f"{name}: {os.path.getsize(p):,} bytes"

    print("  file sizes:")
    for fn in ['tack_clean.png', 'gybe_clean.png', 'steady_leg.png',
               'rsog_glitch_recovery.png', 'session_overview.png',
               'current_drag_shear.png']:
        print(f"    {_fsize(fn)}", flush=True)

    def _print_cal(label: str, cal: Optional[Calibration]) -> None:
        if cal is None:
            print(f"  {label}: (no calibration)", flush=True)
            return
        print(f"  {label}: "
              f"hdg_off={cal.heading_offset_deg:+.3f}°  "
              f"awa_off={cal.awa_offset_deg:+.3f}°  "
              f"awa_sc_up={cal.awa_scale_upwind:.3f}  "
              f"awa_sc_dn={cal.awa_scale_downwind:.3f}  "
              f"aws_sc_up={cal.aws_scale_upwind:.3f}  "
              f"aws_sc_dn={cal.aws_scale_downwind:.3f}  "
              f"stw_sc={cal.stw_scale:.3f}", flush=True)

    _print_cal(f"calibration {CLEAN_SESSION}", clean_cal)
    _print_cal(f"calibration {GLITCH_SESSION}", glitch_cal)
    _print_cal(f"calibration {shear_session}", shear_cal)

    print(f"  pre-cleaned glitches  (rsog, rspd):")
    print(f"    {CLEAN_SESSION}: {clean_glitches}", flush=True)
    print(f"    {GLITCH_SESSION}: {glitch_glitches}", flush=True)
    if shear_session != CLEAN_SESSION:
        print(f"    {shear_session}: {shear_glitches}", flush=True)

    print(f"  2026-04-19 full session:", flush=True)
    print(f"    water_ref - rtwd   circmean {water_vs_rtwd_circ:+.3f}°  "
          f"RMS {water_vs_rtwd_rms:.3f}°  "
          f"95p |·| {water_vs_rtwd_p95:.3f}°", flush=True)
    print(f"    ground_ref - rtwd  circmean {ground_vs_rtwd_circ:+.3f}°  "
          f"RMS {ground_vs_rtwd_rms:.3f}°  "
          f"95p |·| {ground_vs_rtwd_p95:.3f}°", flush=True)

    if shear_stats:
        print(f"  shear session: {shear_session}  "
              f"|C|_median = {shear_med_kt:.3f} kt", flush=True)
        print(f"    window:               "
              f"{shear_stats.get('window_desc', '?')}", flush=True)
        print(f"    bias peak-to-peak:    "
              f"{shear_stats.get('bias_p2p_deg', float('nan')):.2f}°",
              flush=True)
        print(f"    pred vs emp mean|diff|:  "
              f"{shear_stats.get('bias_pred_mean_abs_diff_deg', float('nan')):.3f}°",
              flush=True)

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
