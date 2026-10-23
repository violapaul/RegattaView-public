"""Derived channels and per-candidate feature extraction for start detection.

Feature schema is stable and versioned. Downstream consumers (the detector
scorer, the eval harness, and the Phase E training-row builder) all read the
same ``FEATURE_KEYS`` tuple.
"""
from typing import Callable, Optional
import math

import numpy as np
import pandas as pd

from Libraries.global_variables import G
from Libraries import race_logs
from Libraries.event_detectors import HeadingManeuverDetector

from .start_line import StartLine


FEATURE_SCHEMA_VERSION = 1
SAMPLES_PER_SEC = 10

FEATURE_KEYS: tuple = (
    # legacy carry-overs (from the current AWA-based detector)
    'pre_frac', 'post_frac', 'hdg_std',
    # start-line geometry (NaN when line is None)
    'd_line_at_cross_m',
    'cross_dir_sign',
    'cross_speed_kts',
    'd_line_min_prestart_m',
    'time_within_30m_prestart_s',
    'along_line_at_cross_m',
    # prestart kinematics
    'prestart_tortuosity',
    'prestart_tack_count',
    'prestart_rel_cog_std',
    'prestart_low_speed_frac',
    # post-start, tack-invariant
    'post60_upwind_m',
    'post60_best_single_tack_upwind_m',
    'post60_mean_upwind_vmg_kts',
    'post60_first_tack_time_s',
    # transit-pattern detector (helps reject "settled on a heading and drove
    # upwind for a bit" candidates that aren't starts)
    'approach_hdg_settled',
    # OCS-return pattern
    'reverse_cross_within_90s',
)


# ---------------------------------------------------------------------------
# Derived channels
# ---------------------------------------------------------------------------

def compute_derived_channels(df: pd.DataFrame) -> pd.DataFrame:
    """Idempotently add canonical derived channels used by feature extraction.

    The signal-processing contract lives in
    :func:`Libraries.race_logs.add_navigation_derived_channels`, so start
    detection uses the same frame and unit semantics as the rest of the
    project. This wrapper is kept for existing start-analysis call sites.
    """
    return race_logs.add_navigation_derived_channels(df)


# ---------------------------------------------------------------------------
# Per-candidate feature extraction
# ---------------------------------------------------------------------------

_NAN_FEATURES: dict = {k: float('nan') for k in FEATURE_KEYS}


def _window_bounds(idx: int, n: int, pre_s: int, post_s: int, fs: int):
    return max(0, idx - pre_s * fs), min(n, idx + post_s * fs)


def _safe_mean(a: np.ndarray) -> float:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    return float(a.mean()) if len(a) else float('nan')


def _hdg_std(values: np.ndarray) -> float:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) < 2:
        return float('nan')
    return float(np.degrees(np.std(np.unwrap(np.radians(v)))))


_TORTUOSITY_CAP = 20.0


def _tortuosity(x: np.ndarray, y: np.ndarray) -> float:
    """Path length divided by endpoint displacement, capped at a sane bound.

    A figure-eight loiter can return to its start (displacement ~ 0), which
    would drive the ratio to infinity. Capping keeps the feature usable by
    any downstream scorer or classifier.
    """
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 2:
        return float('nan')
    x = x[mask]; y = y[mask]
    dx = np.diff(x); dy = np.diff(y)
    path_len = float(np.sum(np.hypot(dx, dy)))
    disp = float(math.hypot(x[-1] - x[0], y[-1] - y[0]))
    if disp < 1.0 or path_len / max(disp, 1.0) > _TORTUOSITY_CAP:
        return _TORTUOSITY_CAP if path_len > 0 else float('nan')
    return path_len / disp


def _count_tacks(hdg: np.ndarray, fs: int = SAMPLES_PER_SEC) -> int:
    if len(hdg) < fs * 60:
        return 0
    det = HeadingManeuverDetector(min_turn=55, steady_std_max=12,
                                   steady_seconds=20, min_gap_seconds=20,
                                   sample_rate=fs)
    return len(det._find_maneuvers(np.asarray(hdg, dtype=float)))


def _find_line_crossing(
    d_line: np.ndarray,
    cand_rel_idx: int,
    search_radius_s: int = 30,
    fs: int = SAMPLES_PER_SEC,
) -> Optional[int]:
    """Return index of the zero crossing of ``d_line`` nearest ``cand_rel_idx``.

    Searches ± ``search_radius_s`` seconds around the candidate.
    """
    n = len(d_line)
    lo = max(1, cand_rel_idx - search_radius_s * fs)
    hi = min(n, cand_rel_idx + search_radius_s * fs)
    if hi - lo < 2:
        return None
    seg = d_line[lo:hi]
    prev = seg[:-1]
    curr = seg[1:]
    finite = np.isfinite(prev) & np.isfinite(curr)
    signflip = (prev * curr) <= 0
    candidates = np.where(finite & signflip)[0]
    if len(candidates) == 0:
        return None
    # Nearest to candidate in this slice.
    cand_local = cand_rel_idx - lo
    best = candidates[np.argmin(np.abs(candidates - cand_local))]
    return lo + int(best)


def _cumulative_upwind_m(upwind_vmg_kts: np.ndarray, fs: int = SAMPLES_PER_SEC) -> float:
    """Integrate upwind_vmg (knots) over the window, returning meters."""
    v = np.asarray(upwind_vmg_kts, dtype=float)
    v = np.where(np.isfinite(v), v, 0.0)
    # knots * seconds * (m/s per knot) = meters
    return float(v.sum() / fs / G.MS_2_KNOTS)


def _find_maneuvers(hdg: np.ndarray, fs: int = SAMPLES_PER_SEC) -> list:
    if len(hdg) < fs * 60:
        return []
    det = HeadingManeuverDetector(min_turn=55, steady_std_max=12,
                                   steady_seconds=20, min_gap_seconds=20,
                                   sample_rate=fs)
    return det._find_maneuvers(np.asarray(hdg, dtype=float))


def _best_single_tack_upwind(
    upwind_vmg_kts: np.ndarray,
    hdg: np.ndarray,
    fs: int = SAMPLES_PER_SEC,
    maneuvers: Optional[list] = None,
) -> float:
    """Best per-tack cumulative upwind progress (meters) in the window.

    Partitions the window at detected tacks, integrates ``upwind_vmg`` within
    each segment, returns the largest segment total. Falls back to the whole
    window when no tack is detected (the common case for clean starts).
    """
    n = len(upwind_vmg_kts)
    if n == 0:
        return float('nan')
    if maneuvers is None:
        maneuvers = _find_maneuvers(hdg, fs=fs)
    boundaries = [0] + [m[0] for m in maneuvers] + [n]
    best = -float('inf')
    for a, b in zip(boundaries[:-1], boundaries[1:]):
        if b - a < fs * 5:
            continue
        seg_val = _cumulative_upwind_m(upwind_vmg_kts[a:b], fs=fs)
        if seg_val > best:
            best = seg_val
    return best if best != -float('inf') else float('nan')


def _first_tack_time(hdg: np.ndarray, fs: int = SAMPLES_PER_SEC, maneuvers: Optional[list] = None) -> float:
    if maneuvers is None:
        maneuvers = _find_maneuvers(hdg, fs=fs)
    if not maneuvers:
        return float('nan')
    return float(maneuvers[0][0]) / fs


def compute_candidate_features(
    df: pd.DataFrame,
    idx: int,
    line: Optional[StartLine],
    pre_s: int = 180,
    post_s: int = 90,
    fs: int = SAMPLES_PER_SEC,
    pre_frac: Optional[float] = None,
    post_frac: Optional[float] = None,
) -> dict:
    """Compute the per-candidate feature dict.

    ``df`` must have derived channels (call :func:`compute_derived_channels`).
    If ``pre_frac`` / ``post_frac`` are provided by the caller (the detector
    already computes them), they are carried through unchanged; otherwise they
    are computed on the supplied windows.
    """
    feats = dict(_NAN_FEATURES)
    n = len(df)
    lo, hi = _window_bounds(idx, n, pre_s, post_s, fs)
    pre_lo, pre_hi = lo, idx
    post_lo, post_hi = idx, hi

    awa = np.abs(df['awa'].values) if 'awa' in df.columns else None
    hdg = df['hdg'].values if 'hdg' in df.columns else None
    sog = df['sog'].values if 'sog' in df.columns else None
    x_m = df['x_m'].values if 'x_m' in df.columns else None
    y_m = df['y_m'].values if 'y_m' in df.columns else None
    rel_cog = df['rel_cog_to_twd_deg'].values if 'rel_cog_to_twd_deg' in df.columns else (
        df['rel_cog'].values if 'rel_cog' in df.columns else None
    )
    upwind_vmg = df['upwind_vmg_kts'].values if 'upwind_vmg_kts' in df.columns else (
        df['upwind_vmg'].values if 'upwind_vmg' in df.columns else None
    )

    # ---- Legacy carry-overs ------------------------------------------------
    CLOSE_HAULED_AWA = 40.0
    if pre_frac is None and awa is not None and pre_hi > pre_lo:
        feats['pre_frac'] = float(np.mean(awa[pre_lo:pre_hi] < CLOSE_HAULED_AWA))
    elif pre_frac is not None:
        feats['pre_frac'] = float(pre_frac)
    if post_frac is None and awa is not None and post_hi > post_lo:
        feats['post_frac'] = float(np.mean(awa[post_lo:post_hi] < CLOSE_HAULED_AWA))
    elif post_frac is not None:
        feats['post_frac'] = float(post_frac)
    if hdg is not None and pre_hi - pre_lo > 100:
        feats['hdg_std'] = _hdg_std(hdg[pre_lo:pre_hi])

    # ---- Prestart kinematics ----------------------------------------------
    if x_m is not None and y_m is not None and pre_hi - pre_lo > fs * 30:
        feats['prestart_tortuosity'] = _tortuosity(x_m[pre_lo:pre_hi], y_m[pre_lo:pre_hi])
    if hdg is not None:
        feats['prestart_tack_count'] = _count_tacks(hdg[pre_lo:pre_hi], fs=fs)
    if rel_cog is not None and pre_hi - pre_lo > 100:
        feats['prestart_rel_cog_std'] = _hdg_std(rel_cog[pre_lo:pre_hi])
    if sog is not None and pre_hi > pre_lo:
        sog_kts = sog[pre_lo:pre_hi] * G.MS_2_KNOTS
        finite = sog_kts[np.isfinite(sog_kts)]
        if len(finite):
            feats['prestart_low_speed_frac'] = float(np.mean(finite < 2.0))

    # ---- Post-start upwind progress ---------------------------------------
    if upwind_vmg is not None and post_hi > post_lo:
        seg = upwind_vmg[post_lo:post_hi]
        feats['post60_upwind_m'] = _cumulative_upwind_m(seg, fs=fs)
        feats['post60_mean_upwind_vmg_kts'] = _safe_mean(seg)
    post_maneuvers = None
    if hdg is not None and post_hi > post_lo:
        post_maneuvers = _find_maneuvers(hdg[post_lo:post_hi], fs=fs)
    if upwind_vmg is not None and hdg is not None and post_hi > post_lo:
        feats['post60_best_single_tack_upwind_m'] = _best_single_tack_upwind(
            upwind_vmg[post_lo:post_hi], hdg[post_lo:post_hi],
            fs=fs, maneuvers=post_maneuvers,
        )
    if hdg is not None:
        feats['post60_first_tack_time_s'] = _first_tack_time(
            hdg[post_lo:post_hi], fs=fs, maneuvers=post_maneuvers,
        )

    # ---- Approach-settled (transit-pattern) penalty flag ------------------
    # True when the boat has been on a steady heading for the last 60 s of the
    # prestart window — i.e. transiting, not maneuvering near the line.
    if hdg is not None and pre_hi - 60 * fs > pre_lo:
        approach_std = _hdg_std(hdg[pre_hi - 60 * fs:pre_hi])
        feats['approach_hdg_settled'] = float(np.isfinite(approach_std) and approach_std < 8.0)
    else:
        feats['approach_hdg_settled'] = 0.0

    # ---- Line-geometry features -------------------------------------------
    if line is not None and x_m is not None and y_m is not None:
        xy = np.column_stack([x_m[lo:hi], y_m[lo:hi]])
        d_line = line.signed_distance(xy)
        cand_rel = idx - lo

        # Prestart
        if pre_hi > pre_lo:
            pre_d = d_line[:cand_rel]
            if len(pre_d):
                feats['d_line_min_prestart_m'] = float(np.nanmin(np.abs(pre_d)))
                feats['time_within_30m_prestart_s'] = float(
                    np.sum(np.abs(pre_d) < 30.0) / fs
                )

        # Crossing near the candidate. The AWA-regime-change candidate and
        # the actual line crossing are rarely simultaneous — typical offsets
        # are ±30 s, sometimes more on a slow close-hauled approach.
        cross_rel = _find_line_crossing(d_line, cand_rel, search_radius_s=60, fs=fs)
        if cross_rel is not None:
            feats['d_line_at_cross_m'] = float(d_line[cross_rel])
            # Crossing direction: sign of d_line derivative at crossing.
            a = max(0, cross_rel - fs)
            b = min(len(d_line) - 1, cross_rel + fs)
            if b > a and sog is not None:
                slope = d_line[b] - d_line[a]
                feats['cross_dir_sign'] = 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
                dt_s = (b - a) / fs
                cross_speed_m_s = slope / dt_s
                feats['cross_speed_kts'] = float(abs(cross_speed_m_s) * G.MS_2_KNOTS)
            feats['along_line_at_cross_m'] = float(line.along_line(xy[cross_rel]))

        # OCS-return pattern: after a forward crossing, does the boat reverse
        # back to the prestart side? Skip the first few seconds past the
        # crossing (the signed-distance index we found is the last negative
        # sample before the flip) and require a meaningful negative excursion.
        if feats.get('cross_dir_sign') == 1.0 and cross_rel is not None:
            after_lo = cross_rel + 3 * fs
            after_hi = min(len(d_line), cross_rel + 90 * fs)
            after = d_line[after_lo:after_hi] if after_hi > after_lo else np.array([])
            feats['reverse_cross_within_90s'] = float(
                np.any(np.isfinite(after) & (after < -5.0))
            )
        else:
            feats['reverse_cross_within_90s'] = 0.0

    return feats


def compute_features_bulk(
    df: pd.DataFrame,
    candidate_indices: np.ndarray,
    line_resolver: Callable[[int], Optional[StartLine]],
    pre_s: int = 180,
    post_s: int = 90,
    fs: int = SAMPLES_PER_SEC,
) -> pd.DataFrame:
    """Bulk feature extraction, one row per candidate index. Used by the eval
    harness and the Phase E training-row builder."""
    df_enriched = compute_derived_channels(df)
    rows = []
    for idx in np.asarray(candidate_indices, dtype=int):
        line = line_resolver(int(idx))
        feats = compute_candidate_features(
            df_enriched, int(idx), line, pre_s=pre_s, post_s=post_s, fs=fs,
        )
        feats['idx'] = int(idx)
        rows.append(feats)
    cols = ('idx',) + FEATURE_KEYS
    return pd.DataFrame(rows, columns=cols)
