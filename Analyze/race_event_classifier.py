"""Deterministic race-grammar classification for upwind transitions."""
from dataclasses import dataclass
import math
from typing import Optional

import numpy as np
import pandas as pd

from Libraries.global_variables import G
from Libraries import process as p
from Libraries.event_detectors import HeadingManeuverDetector

from Analyze import polar
from Analyze.sailing_state import (
    LONG_LEG_STATES,
    STATE_DOWNWIND,
    STATE_REACH,
    STATE_STOPPED,
    STATE_UPWIND,
    add_sailing_state,
)
from Analyze.start_line import StartLine


SUBTYPE_PROBABLE_START = 'probable_start'
SUBTYPE_PROBABLE_LEEWARD_ROUNDING = 'probable_leeward_rounding'
SUBTYPE_PROBABLE_WINDWARD_ROUNDING = 'probable_windward_rounding'
SUBTYPE_AMBIGUOUS_ROUNDING = 'ambiguous_rounding'
SUBTYPE_PROBABLE_DELIVERY = 'probable_delivery'
SUBTYPE_AMBIGUOUS_START = 'ambiguous_start'
SUBTYPE_AMBIGUOUS_UPWIND = 'ambiguous_upwind_transition'


@dataclass(frozen=True)
class GrammarThresholds:
    post10_upwind_frac: float = 0.75
    post10_mean_upwind_vmg_kts: float = 2.5
    post10_net_upwind_m: float = 300.0
    pre10_reach_frac: float = 0.45
    pre10_tortuosity: float = 3.0
    pre10_path_m: float = 1000.0
    pre10_chord_m: float = 800.0
    pre10_downwind_frac: float = 0.75
    leeward_tortuosity_max: float = 1.8
    previous_downwind_leg_duration_s: float = 240.0
    long_leg_duration_s: float = 180.0
    long_leg_progress_m: float = 400.0
    post10_polar_vmg_ratio: float = 0.40
    pre10_upwind_frac: float = 0.70
    windward_tortuosity_max: float = 1.8
    previous_upwind_leg_duration_s: float = 240.0
    post10_offwind_frac: float = 0.55
    next_long_leg_duration_s: float = 240.0


def _safe_float(v, default=float('nan')) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        return default
    return v if np.isfinite(v) else default


def _clip01(v: float) -> float:
    if not np.isfinite(v):
        return 0.0
    return float(max(0.0, min(1.0, v)))


def _bool_score(parts) -> float:
    vals = [1.0 if p else 0.0 for p in parts]
    return float(sum(vals) / len(vals)) if vals else 0.0


def _path_chord(x: np.ndarray, y: np.ndarray) -> tuple:
    mask = np.isfinite(x) & np.isfinite(y)
    if mask.sum() < 2:
        return float('nan'), float('nan')
    x = x[mask]
    y = y[mask]
    return (
        float(np.sum(np.hypot(np.diff(x), np.diff(y)))),
        float(np.hypot(x[-1] - x[0], y[-1] - y[0])),
    )


def _state_frac(states: np.ndarray, state: str) -> float:
    if len(states) == 0:
        return float('nan')
    return float(np.mean(states == state))


def _heading_reversal_count(hdg: np.ndarray) -> int:
    if len(hdg) < 3:
        return 0
    unwrapped = p.unwrap_d(hdg)
    # Compare 5-s means to avoid counting sample jitter.
    win = min(50, max(3, len(unwrapped) // 20))
    if len(unwrapped) <= win * 2:
        return 0
    smooth = pd.Series(unwrapped).rolling(win, center=True, min_periods=win // 2).mean().to_numpy()
    delta = np.diff(smooth)
    finite = np.isfinite(delta)
    delta = delta[finite]
    turn = np.sign(np.where(np.abs(delta) > 0.05, delta, 0.0))
    turn = turn[turn != 0.0]
    if len(turn) < 2:
        return 0
    return int(np.sum(turn[1:] != turn[:-1]))


def _maneuver_counts(df: pd.DataFrame, lo: int, hi: int, fs: int) -> dict:
    if hi - lo < fs * 60 or 'hdg' not in df.columns:
        return {'tack_count': 0, 'gybe_count': 0, 'first_tack_time_s': float('nan')}
    det = HeadingManeuverDetector(min_turn=55, steady_std_max=12,
                                   steady_seconds=20, min_gap_seconds=20,
                                   sample_rate=fs)
    hdg = df['hdg'].to_numpy(dtype=float)[lo:hi]
    awa = df['awa'].to_numpy(dtype=float)[lo:hi] if 'awa' in df.columns else None
    maneuvers = det._find_maneuvers(hdg)
    tack_count = 0
    gybe_count = 0
    first_tack_time_s = float('nan')
    for local_idx, heading_change in maneuvers:
        if awa is not None and local_idx < len(awa):
            is_tack = abs(float(awa[local_idx])) < 90.0
        else:
            is_tack = abs(float(heading_change)) < 120.0
        if is_tack:
            tack_count += 1
            if not np.isfinite(first_tack_time_s):
                first_tack_time_s = float(local_idx) / fs
        else:
            gybe_count += 1
    return {
        'tack_count': int(tack_count),
        'gybe_count': int(gybe_count),
        'first_tack_time_s': first_tack_time_s,
    }


def _polar_features(seg: pd.DataFrame, prefix: str) -> dict:
    if len(seg) == 0 or 'twa' not in seg.columns or 'tws' not in seg.columns:
        return {
            f'{prefix}_tws_median_kts': float('nan'),
            f'{prefix}_polar_domain_frac': 0.0,
            f'{prefix}_polar_vmgmax_kts': float('nan'),
            f'{prefix}_polar_vmg_at_twa_kts': float('nan'),
            f'{prefix}_observed_to_polar_vmg_ratio': float('nan'),
        }
    twa = np.abs(p.angle_diff_d(seg['twa'].to_numpy(dtype=float), 0.0))
    # race_logs wind/speed columns are m/s; polars are in knots.
    tws_kts = seg['tws'].to_numpy(dtype=float) * G.MS_2_KNOTS
    observed_vmg = seg['upwind_vmg_kts'].to_numpy(dtype=float)
    valid = polar.in_upwind_domain(twa, tws_kts)
    vmgmax = polar.upwind_vmgmax_kt(tws_kts)
    vmg_at_twa = polar.upwind_vmg_kt(twa, tws_kts)
    valid_vmgmax = np.isfinite(vmgmax)
    polar_ref = float(np.nanmedian(vmgmax[valid_vmgmax])) if np.any(valid_vmgmax) else float('nan')
    observed = float(np.nanmean(observed_vmg)) if len(observed_vmg) else float('nan')
    ratio = observed / polar_ref if np.isfinite(observed) and np.isfinite(polar_ref) and polar_ref > 0 else float('nan')
    return {
        f'{prefix}_tws_median_kts': float(np.nanmedian(tws_kts)),
        f'{prefix}_polar_domain_frac': float(np.mean(valid)) if len(valid) else 0.0,
        f'{prefix}_polar_vmgmax_kts': polar_ref,
        f'{prefix}_polar_vmg_at_twa_kts': float(np.nanmean(vmg_at_twa[valid])) if np.any(valid) else float('nan'),
        f'{prefix}_observed_to_polar_vmg_ratio': ratio,
    }


class RaceEventContext:
    """Reusable per-day context for classifying upwind-transition candidates."""

    def __init__(
        self,
        df: pd.DataFrame,
        fs: int = G.SAMPLES_PER_SECOND,
        thresholds: GrammarThresholds = GrammarThresholds(),
    ):
        self.fs = fs
        self.thresholds = thresholds
        self.df, self.segments = add_sailing_state(df, fs=fs)

    def _window(self, idx: int, seconds_before: int = 0, seconds_after: int = 0) -> tuple:
        lo = max(0, int(idx) - seconds_before * self.fs)
        hi = min(len(self.df), int(idx) + seconds_after * self.fs)
        return lo, hi

    def _window_metrics(self, idx: int, prefix: str, seconds_before: int = 0, seconds_after: int = 0) -> dict:
        lo, hi = self._window(idx, seconds_before=seconds_before, seconds_after=seconds_after)
        seg = self.df.iloc[lo:hi]
        states = seg['sailing_state'].to_numpy(dtype=object)
        x = seg['x_m'].to_numpy(dtype=float)
        y = seg['y_m'].to_numpy(dtype=float)
        u = seg['wind_u_m'].to_numpy(dtype=float)
        v = seg['wind_v_m'].to_numpy(dtype=float)
        path, chord = _path_chord(x, y)
        hdg = seg['hdg'].to_numpy(dtype=float) if 'hdg' in seg.columns else np.array([])
        maneuvers = _maneuver_counts(self.df, lo, hi, self.fs)
        out = {
            f'{prefix}_state_frac_upwind': _state_frac(states, STATE_UPWIND),
            f'{prefix}_state_frac_downwind': _state_frac(states, STATE_DOWNWIND),
            f'{prefix}_state_frac_reach': _state_frac(states, STATE_REACH),
            f'{prefix}_state_frac_stopped': _state_frac(states, STATE_STOPPED),
            f'{prefix}_path_m': path,
            f'{prefix}_chord_m': chord,
            f'{prefix}_tortuosity': float(path / max(chord, 1.0)) if np.isfinite(path) and np.isfinite(chord) else float('nan'),
            f'{prefix}_net_upwind_m': float(u[-1] - u[0]) if len(u) else float('nan'),
            f'{prefix}_net_crosswind_m': float(v[-1] - v[0]) if len(v) else float('nan'),
            f'{prefix}_mean_upwind_vmg_kts': float(np.nanmean(seg['upwind_vmg_kts'])) if len(seg) else float('nan'),
            f'{prefix}_mean_sog_kts': float(np.nanmean(seg['sog'].to_numpy(dtype=float) * G.MS_2_KNOTS)) if len(seg) and 'sog' in seg.columns else float('nan'),
            f'{prefix}_abs_turn_deg': float(np.nansum(np.abs(np.diff(p.unwrap_d(hdg))))) if len(hdg) > 1 else 0.0,
            f'{prefix}_heading_reversal_count': _heading_reversal_count(hdg),
            f'{prefix}_tack_count': maneuvers['tack_count'],
            f'{prefix}_gybe_count': maneuvers['gybe_count'],
            f'{prefix}_first_tack_time_s': maneuvers['first_tack_time_s'],
        }
        out.update(_polar_features(seg, prefix))
        # Short aliases used by the first-pass rule text.
        if prefix in ('pre5', 'pre10', 'post5', 'post10'):
            for state in (STATE_UPWIND, STATE_DOWNWIND, STATE_REACH, STATE_STOPPED):
                out[f'{prefix}_{state}_frac'] = out[f'{prefix}_state_frac_{state}']
        return out

    def _long_leg_before(self, idx: int, state: Optional[str] = None) -> Optional[pd.Series]:
        candidates = self.segments[self.segments['end_idx'] <= idx].copy()
        candidates = candidates[candidates['state'].isin(LONG_LEG_STATES)]
        if state is not None:
            candidates = candidates[candidates['state'] == state]
        candidates = candidates[
            (candidates['duration_s'] >= self.thresholds.long_leg_duration_s)
            & (np.abs(candidates['net_upwind_m']) >= self.thresholds.long_leg_progress_m)
        ]
        if candidates.empty:
            return None
        return candidates.sort_values('end_idx').iloc[-1]

    def _long_leg_after(self, idx: int) -> Optional[pd.Series]:
        candidates = self.segments[self.segments['start_idx'] >= idx].copy()
        candidates = candidates[candidates['state'].isin(LONG_LEG_STATES)]
        candidates = candidates[
            (candidates['duration_s'] >= self.thresholds.long_leg_duration_s)
            & (np.abs(candidates['net_upwind_m']) >= self.thresholds.long_leg_progress_m)
        ]
        if candidates.empty:
            return None
        return candidates.sort_values('start_idx').iloc[0]

    def _sequence_features(self, idx: int) -> dict:
        prev = self._long_leg_before(idx)
        prev_down = self._long_leg_before(idx, state=STATE_DOWNWIND)
        prev_up = self._long_leg_before(idx, state=STATE_UPWIND)
        next_leg = self._long_leg_after(idx)
        next_offwind = self.segments[
            (self.segments['start_idx'] >= idx)
            & (~self.segments['state'].isin((STATE_UPWIND, STATE_STOPPED)))
        ]
        next_offwind_idx = int(next_offwind.iloc[0]['start_idx']) if not next_offwind.empty else None
        out = {
            'previous_long_leg_type': prev['state'] if prev is not None else None,
            'previous_long_leg_duration_s': _safe_float(prev['duration_s']) if prev is not None else float('nan'),
            'previous_long_leg_net_upwind_m': _safe_float(prev['net_upwind_m']) if prev is not None else float('nan'),
            'previous_downwind_leg_duration_s': _safe_float(prev_down['duration_s']) if prev_down is not None else float('nan'),
            'previous_upwind_leg_duration_s': _safe_float(prev_up['duration_s']) if prev_up is not None else float('nan'),
            'time_since_previous_downwind_leg_end_s': float((idx - int(prev_down['end_idx'])) / self.fs) if prev_down is not None else float('nan'),
            'time_since_previous_upwind_transition_s': float((idx - int(prev['end_idx'])) / self.fs) if prev is not None else float('nan'),
            'next_long_leg_type': next_leg['state'] if next_leg is not None else None,
            'next_long_leg_duration_s': _safe_float(next_leg['duration_s']) if next_leg is not None else float('nan'),
            'time_to_next_windward_rounding_s': float((int(next_leg['start_idx']) - idx) / self.fs) if next_leg is not None and next_leg['state'] == STATE_DOWNWIND else float('nan'),
            'time_to_next_offwind_segment_s': float((next_offwind_idx - idx) / self.fs) if next_offwind_idx is not None else float('nan'),
        }
        return out

    def _geometry_features(self, idx: int, line: Optional[StartLine]) -> dict:
        row = self.df.iloc[int(idx)]
        out = {
            'candidate_u': _safe_float(row.get('wind_u_m')),
            'candidate_v': _safe_float(row.get('wind_v_m')),
            'distance_to_recurrent_start_zone_m': float('nan'),
            'distance_to_recurrent_leeward_zone_m': float('nan'),
            'signed_line_crossing_distance_m': float('nan'),
            'line_crossing_direction': 0.0,
        }
        if line is not None and np.isfinite(row.get('x_m', np.nan)) and np.isfinite(row.get('y_m', np.nan)):
            xy = np.array([float(row['x_m']), float(row['y_m'])])
            out['signed_line_crossing_distance_m'] = float(line.signed_distance(xy))
            a = max(0, idx - self.fs)
            b = min(len(self.df) - 1, idx + self.fs)
            if b > a:
                xy_a = self.df[['x_m', 'y_m']].iloc[a].to_numpy(dtype=float)
                xy_b = self.df[['x_m', 'y_m']].iloc[b].to_numpy(dtype=float)
                slope = float(line.signed_distance(xy_b) - line.signed_distance(xy_a))
                out['line_crossing_direction'] = 1.0 if slope > 0 else (-1.0 if slope < 0 else 0.0)
        return out

    def classify_candidate(self, idx: int, line: Optional[StartLine] = None) -> dict:
        idx = int(idx)
        feats = {}
        feats.update(self._window_metrics(idx, 'pre5', seconds_before=5 * 60))
        feats.update(self._window_metrics(idx, 'pre10', seconds_before=10 * 60))
        feats.update(self._window_metrics(idx, 'post5', seconds_after=5 * 60))
        feats.update(self._window_metrics(idx, 'post10', seconds_after=10 * 60))
        feats.update(self._sequence_features(idx))
        feats.update(self._geometry_features(idx, line))

        t = self.thresholds
        post_parts = (
            feats['post10_upwind_frac'] >= t.post10_upwind_frac,
            feats['post10_mean_upwind_vmg_kts'] >= t.post10_mean_upwind_vmg_kts,
            feats['post10_net_upwind_m'] >= t.post10_net_upwind_m,
            (
                feats.get('post10_observed_to_polar_vmg_ratio', float('nan')) >= t.post10_polar_vmg_ratio
                and feats.get('post10_polar_domain_frac', 0.0) >= 0.20
            ),
        )
        pre_parts = (
            feats['pre10_reach_frac'] >= t.pre10_reach_frac,
            feats['pre10_tortuosity'] >= t.pre10_tortuosity,
            feats['pre10_path_m'] >= t.pre10_path_m,
            feats['pre10_chord_m'] <= t.pre10_chord_m,
        )
        prestart_box_score = _bool_score(pre_parts)
        post_beat_score = _bool_score(post_parts)
        no_prestart_since_downwind = prestart_box_score < 0.75
        leeward_parts = (
            feats['pre10_downwind_frac'] >= t.pre10_downwind_frac,
            feats['pre10_tortuosity'] <= t.leeward_tortuosity_max,
            feats['previous_downwind_leg_duration_s'] >= t.previous_downwind_leg_duration_s,
            no_prestart_since_downwind,
        )
        leeward_rounding_score = _bool_score(leeward_parts)

        delivery_score = _bool_score((
            post_beat_score < 0.67,
            feats['pre10_tortuosity'] < 2.0,
            feats['pre10_state_frac_stopped'] > 0.2 or feats['pre10_mean_sog_kts'] < 2.0,
        ))
        line_score = _clip01(1.0 - abs(_safe_float(feats['signed_line_crossing_distance_m'], 999.0)) / 150.0)
        sequence_score = _bool_score((
            feats.get('previous_long_leg_type') != STATE_DOWNWIND,
            feats.get('next_long_leg_type') in (STATE_UPWIND, None),
        ))

        post_beat_is_good = post_beat_score >= 0.50
        prestart_box_is_good = prestart_box_score >= 0.75
        weak_prestart_box = prestart_box_score >= 0.50
        direct_leeward_rounding = leeward_rounding_score >= 0.75
        strong_direct_leeward_rounding = leeward_rounding_score >= 0.75
        delivery_like = delivery_score >= 0.67 and not post_beat_is_good

        # Hard gate for leeward classification: a real leeward rounding is
        # followed by a sustained upwind leg. Without one, the candidate is
        # a brief head-up-then-quit, not a rounding into a beat.
        leeward_sequence_real = (
            feats.get('next_long_leg_type') == STATE_UPWIND
            and _safe_float(feats.get('next_long_leg_duration_s'), 0.0)
                >= t.next_long_leg_duration_s
        )

        if post_beat_is_good and prestart_box_is_good and not direct_leeward_rounding:
            subtype = SUBTYPE_PROBABLE_START
        elif post_beat_is_good and direct_leeward_rounding and leeward_sequence_real:
            subtype = SUBTYPE_PROBABLE_LEEWARD_ROUNDING
        elif post_beat_is_good and weak_prestart_box and not strong_direct_leeward_rounding:
            subtype = SUBTYPE_AMBIGUOUS_START
        elif delivery_like:
            subtype = SUBTYPE_PROBABLE_DELIVERY
        else:
            subtype = SUBTYPE_AMBIGUOUS_UPWIND

        start_confidence = _clip01(
            0.45 * post_beat_score
            + 0.40 * prestart_box_score
            + 0.10 * line_score
            + 0.05 * sequence_score
            - 0.45 * leeward_rounding_score
            - 0.20 * delivery_score
        )
        rounding_confidence = _clip01(0.55 * leeward_rounding_score + 0.35 * post_beat_score)
        if subtype in (SUBTYPE_PROBABLE_START, SUBTYPE_AMBIGUOUS_START):
            review_priority = 'high'
        elif subtype == SUBTYPE_AMBIGUOUS_UPWIND:
            review_priority = 'medium'
        else:
            review_priority = 'low'

        scores = {
            'prestart_box_score': round(prestart_box_score, 3),
            'leeward_rounding_score': round(leeward_rounding_score, 3),
            'delivery_score': round(delivery_score, 3),
            'post_beat_score': round(post_beat_score, 3),
            'line_score': round(line_score, 3),
            'sequence_score': round(sequence_score, 3),
        }
        feats.update(scores)
        feats['post10_leg_quality'] = scores['post_beat_score']
        return {
            'subtype': subtype,
            'start_confidence': round(start_confidence, 3),
            'rounding_confidence': round(rounding_confidence, 3),
            'review_priority': review_priority,
            'features': feats,
            'scores': scores,
        }

    def classify_windward_candidate(self, idx: int) -> dict:
        idx = int(idx)
        feats = {}
        feats.update(self._window_metrics(idx, 'pre10', seconds_before=10 * 60))
        feats.update(self._window_metrics(idx, 'post10', seconds_after=10 * 60))
        feats.update(self._sequence_features(idx))

        t = self.thresholds
        post_offwind_frac = (
            _safe_float(feats.get('post10_state_frac_downwind'), 0.0)
            + _safe_float(feats.get('post10_state_frac_reach'), 0.0)
        )
        feats['post10_offwind_frac_observed'] = round(post_offwind_frac, 3)

        # Hard gate: a real windward rounding sits between a sustained
        # upwind leg and a sustained downwind leg. Without both, the boat
        # only briefly bore away (or only briefly sailed upwind) — not a
        # mark rounding.
        gate_real_sequence = (
            _safe_float(feats.get('previous_upwind_leg_duration_s'), 0.0)
                >= t.previous_upwind_leg_duration_s
            and feats.get('next_long_leg_type') == STATE_DOWNWIND
            and _safe_float(feats.get('next_long_leg_duration_s'), 0.0)
                >= t.next_long_leg_duration_s
        )
        if not gate_real_sequence:
            return {
                'subtype': None,
                'start_confidence': 0.0,
                'rounding_confidence': 0.0,
                'review_priority': 'low',
                'features': feats,
                'scores': {
                    'windward_rounding_score': 0.0,
                    'post10_offwind_frac_observed': round(post_offwind_frac, 3),
                },
            }

        windward_parts = (
            feats['pre10_upwind_frac'] >= t.pre10_upwind_frac,
            feats['pre10_tortuosity'] <= t.windward_tortuosity_max,
            post_offwind_frac >= t.post10_offwind_frac,
        )
        windward_rounding_score = _bool_score(windward_parts)

        if windward_rounding_score >= 0.67:
            subtype = SUBTYPE_PROBABLE_WINDWARD_ROUNDING
            review_priority = 'low'
        elif windward_rounding_score >= 0.34:
            subtype = SUBTYPE_AMBIGUOUS_ROUNDING
            review_priority = 'medium'
        else:
            subtype = None
            review_priority = 'low'

        rounding_confidence = _clip01(
            0.7 * windward_rounding_score + 0.3 * _clip01(post_offwind_frac)
        )

        scores = {
            'windward_rounding_score': round(windward_rounding_score, 3),
            'post10_offwind_frac_observed': round(post_offwind_frac, 3),
        }
        feats.update(scores)
        return {
            'subtype': subtype,
            'start_confidence': 0.0,
            'rounding_confidence': round(rounding_confidence, 3),
            'review_priority': review_priority,
            'features': feats,
            'scores': scores,
        }
