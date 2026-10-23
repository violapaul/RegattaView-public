"""Primitive sailing-state segmentation for race grammar detection.

This module deliberately works from canonical columns produced by
``Libraries.race_logs.add_navigation_derived_channels`` so downstream event
logic does not reinterpret instrument frames or units.
"""
from dataclasses import dataclass
from typing import Iterable, Tuple

import numpy as np
import pandas as pd

from Libraries.global_variables import G
from Libraries import race_logs


STATE_UPWIND = 'upwind'
STATE_DOWNWIND = 'downwind'
STATE_REACH = 'reach'
STATE_STOPPED = 'stopped'
STATE_UNKNOWN = 'maneuvering_unknown'

LONG_LEG_STATES = {STATE_UPWIND, STATE_DOWNWIND}


@dataclass(frozen=True)
class StateConfig:
    """Thresholds for primitive sailing-state segmentation."""
    upwind_rel_deg: float = 70.0
    downwind_rel_deg: float = 135.0
    min_upwind_vmg_kts: float = 1.5
    max_downwind_vmg_kts: float = -1.5
    moving_sog_kts: float = 2.0
    stopped_sog_kts: float = 1.0
    min_state_duration_s: float = 25.0
    merge_gap_s: float = 15.0


def _as_kts(df: pd.DataFrame, column: str) -> np.ndarray:
    kts_col = f'{column}_kts'
    if kts_col in df.columns:
        return df[kts_col].to_numpy(dtype=float)
    return df[column].to_numpy(dtype=float) * G.MS_2_KNOTS


def add_wind_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with wind-frame coordinates ``wind_u_m`` / ``wind_v_m``.

    ``wind_u_m`` increases toward the wind source (up the course).
    ``wind_v_m`` is the crosswind coordinate. Inputs are projected east/north
    meters plus true-frame TWD.
    """
    out = race_logs.add_navigation_derived_channels(df)
    if 'wind_u_m' in out.columns and 'wind_v_m' in out.columns:
        return out

    theta = np.radians(out['twd_true'].to_numpy(dtype=float))
    x = out['x_m'].to_numpy(dtype=float)
    y = out['y_m'].to_numpy(dtype=float)
    upwind_e = np.sin(theta)
    upwind_n = np.cos(theta)
    cross_e = np.cos(theta)
    cross_n = -np.sin(theta)
    out = out.assign(
        wind_u_m=x * upwind_e + y * upwind_n,
        wind_v_m=x * cross_e + y * cross_n,
    )
    return out


def primitive_states(df: pd.DataFrame, config: StateConfig = StateConfig()) -> np.ndarray:
    """Classify each sample into a primitive wind-relative sailing state."""
    rel = np.abs(df['rel_cog_to_twd_deg'].to_numpy(dtype=float))
    vmg = df['upwind_vmg_kts'].to_numpy(dtype=float)
    sog = _as_kts(df, 'sog')

    states = np.full(len(df), STATE_UNKNOWN, dtype=object)
    finite = np.isfinite(rel) & np.isfinite(vmg) & np.isfinite(sog)
    states[finite & (sog < config.stopped_sog_kts)] = STATE_STOPPED
    moving = finite & (sog > config.moving_sog_kts)
    states[moving & (rel < config.upwind_rel_deg) & (vmg > config.min_upwind_vmg_kts)] = STATE_UPWIND
    states[moving & (rel > config.downwind_rel_deg) & (vmg < config.max_downwind_vmg_kts)] = STATE_DOWNWIND
    states[moving & (rel >= config.upwind_rel_deg) & (rel <= config.downwind_rel_deg)] = STATE_REACH
    return states


def _runs(values: Iterable[object]) -> list:
    vals = np.asarray(values, dtype=object)
    if len(vals) == 0:
        return []
    runs = []
    start = 0
    current = vals[0]
    for i in range(1, len(vals)):
        if vals[i] != current:
            runs.append((start, i, current))
            start = i
            current = vals[i]
    runs.append((start, len(vals), current))
    return runs


def smooth_states(
    states: np.ndarray,
    fs: int = G.SAMPLES_PER_SECOND,
    config: StateConfig = StateConfig(),
) -> np.ndarray:
    """Suppress short-lived state changes with simple deterministic hysteresis."""
    out = np.asarray(states, dtype=object).copy()
    min_n = max(1, int(round(config.min_state_duration_s * fs)))
    gap_n = max(1, int(round(config.merge_gap_s * fs)))

    # First pass: short non-stopped blips become unknown unless they bridge
    # identical neighbors.
    for start, end, state in _runs(out):
        length = end - start
        if length >= min_n or state == STATE_STOPPED:
            continue
        prev_state = out[start - 1] if start > 0 else None
        next_state = out[end] if end < len(out) else None
        if prev_state == next_state and prev_state is not None:
            out[start:end] = prev_state
        else:
            out[start:end] = STATE_UNKNOWN

    # Second pass: merge short unknown gaps between identical stable states.
    for start, end, state in _runs(out):
        if state != STATE_UNKNOWN or end - start > gap_n:
            continue
        prev_state = out[start - 1] if start > 0 else None
        next_state = out[end] if end < len(out) else None
        if prev_state == next_state and prev_state not in (None, STATE_UNKNOWN):
            out[start:end] = prev_state
    return out


def add_sailing_state(
    df: pd.DataFrame,
    fs: int = G.SAMPLES_PER_SECOND,
    config: StateConfig = StateConfig(),
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Return ``(sample_df, segments_df)`` with state labels and metrics."""
    out = add_wind_frame(df)
    states = smooth_states(primitive_states(out, config=config), fs=fs, config=config)

    segment_ids = np.full(len(out), -1, dtype=int)
    for seg_id, (start, end, _) in enumerate(_runs(states)):
        segment_ids[start:end] = seg_id
    out = out.assign(sailing_state=states, state_segment_id=segment_ids)
    segments = segment_table(out, fs=fs)
    return out, segments


def _path_chord(x: np.ndarray, y: np.ndarray) -> Tuple[float, float]:
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 2:
        return float('nan'), float('nan')
    x = x[mask]
    y = y[mask]
    path = float(np.sum(np.hypot(np.diff(x), np.diff(y))))
    chord = float(np.hypot(x[-1] - x[0], y[-1] - y[0]))
    return path, chord


def segment_table(df: pd.DataFrame, fs: int = G.SAMPLES_PER_SECOND) -> pd.DataFrame:
    """Build one row per smoothed primitive-state segment."""
    rows = []
    states = df['sailing_state'].to_numpy(dtype=object)
    x = df['x_m'].to_numpy(dtype=float)
    y = df['y_m'].to_numpy(dtype=float)
    u = df['wind_u_m'].to_numpy(dtype=float)
    v = df['wind_v_m'].to_numpy(dtype=float)
    sog_kts = _as_kts(df, 'sog')
    vmg_kts = df['upwind_vmg_kts'].to_numpy(dtype=float)

    for seg_id, (start, end, state) in enumerate(_runs(states)):
        xs = x[start:end]
        ys = y[start:end]
        us = u[start:end]
        vs = v[start:end]
        path, chord = _path_chord(xs, ys)
        rows.append({
            'segment_id': seg_id,
            'state': state,
            'start_idx': int(start),
            'end_idx': int(end),
            'duration_s': float((end - start) / fs),
            'u0': float(us[0]) if len(us) else float('nan'),
            'u1': float(us[-1]) if len(us) else float('nan'),
            'v0': float(vs[0]) if len(vs) else float('nan'),
            'v1': float(vs[-1]) if len(vs) else float('nan'),
            'path_m': path,
            'chord_m': chord,
            'net_upwind_m': float(us[-1] - us[0]) if len(us) else float('nan'),
            'net_crosswind_m': float(vs[-1] - vs[0]) if len(vs) else float('nan'),
            'mean_sog_kts': float(np.nanmean(sog_kts[start:end])),
            'mean_upwind_vmg_kts': float(np.nanmean(vmg_kts[start:end])),
        })
    return pd.DataFrame(rows)
