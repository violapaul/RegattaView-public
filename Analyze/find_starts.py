#!/usr/bin/env python
"""Find starts in the processed parquet logs.

Parquet files under ``--logs-dir`` are globbed directly. Detection works
from the logs themselves; the previous ``tracks.json`` / ``generate_tracks.py``
artifact is gone, replaced by the per-session JSONs emitted here.

Output layout (replaces the monolithic ``starts.json``):
  * ``Analyze/events_index.json`` — small list of event summaries; loaded
    eagerly by review.html / label.html. Each entry carries a flat
    ``kind`` enum (one of ``start, windward_rounding, leeward_rounding,
    tack, gybe, other``).
  * ``Analyze/Events/<id>.json``  — heavy per-event payload (tracks, series,
    features, inferred line). Lazy-fetched on selection.
  * ``Analyze/Sessions/<basename>.json`` — per-parquet decimated GPS track
    used by the review.html detail map's full-session backdrop. Written
    once per parquet inside the same loop as event detection. Lazy-fetched
    by review.js when an event is selected (basename = parquet stem, e.g.
    ``2026-02-28T09_32.json`` for ``2026-02-28T09_32.parquet``).

The split keeps first-paint cost proportional to the index (~MB) instead
of the sum of all detail payloads (~hundreds of MB). The schema is
deliberately generic so future ``find_roundings.py`` / ``find_finishes.py``
can append additional ``kind`` values to the same index without
colliding with starts.
"""
import sys
from pathlib import Path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
import json
import argparse
import concurrent.futures
import glob
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
import numpy as np
import pandas as pd
from Libraries.global_variables import G
from Libraries import race_logs
from Analyze.start_features import (
    FEATURE_SCHEMA_VERSION,
    compute_candidate_features,
    compute_derived_channels,
)
from Analyze.race_event_classifier import RaceEventContext
from Analyze.maneuver_analysis import analyze_maneuvers
from Analyze.start_line import infer_start_line

G.init_peer_gynt()

CLOSE_HAULED_AWA = 40
PRE_WINDOW_S = 120
POST_WINDOW_S = 240
# Phase C: loosened stage-1 thresholds for higher recall. The v2 composite
# score (see ``composite_score``) then filters the enlarged candidate pool.
PRE_THRESHOLD = 0.65
POST_THRESHOLD = 0.60
SAMPLES_PER_SEC = G.SAMPLES_PER_SECOND
TRACK_DECIMATION = 5
# Session-wide track decimation for the per-parquet Sessions/<basename>.json
# emitted alongside events. 10 Hz / 50 = 0.2 Hz, i.e. one point per ~5 s,
# matching the resolution the now-retired generate_tracks.py used to produce.
SESSION_TRACK_DECIMATION = 50
MIN_HDG_STD = 45
MIN_PRE_FRAC = 0.05  # Must have some close-hauled time before (filters junk)
CANDIDATE_STEP_S = 5  # was 10 — tighter grid pairs with loosened thresholds

# Stage-2 settings.
NMS_WINDOW_S = 180     # non-maximum suppression radius on score_v2
SCORE_V2_MIN = 0.20    # candidates below this never reach the events index
MANEUVER_EVENT_LEAD_S = 20

# Index-side preview track. ~60 points across a ~900 s window keeps the
# review.html mini-map readable without inflating events_index.json: the
# full track lives in the per-event detail file under Analyze/Events/.
PREVIEW_TRACK_DECIMATION = 30
EVENTS_INDEX_VERSION = 2
# Flat event-kind enum (replaces the older type / subtype / tags trio).
# review.html filters on this single field; ``other`` covers ambiguous
# starts and ambiguous roundings (default-hidden behind a checkbox).
EVENT_KIND_START = 'start'
EVENT_KIND_WINDWARD_ROUNDING = 'windward_rounding'
EVENT_KIND_LEEWARD_ROUNDING = 'leeward_rounding'
EVENT_KIND_TACK = 'tack'
EVENT_KIND_GYBE = 'gybe'
EVENT_KIND_OTHER = 'other'

REQUIRED_COLS = ('awa', 'hdg', 'spd', 'twa', 'twd', 'latitude', 'longitude')


def _sigmoid(x: float) -> float:
    if not np.isfinite(x):
        return 0.0
    # Numerically stable.
    if x >= 0:
        ex = np.exp(-x)
        return float(1.0 / (1.0 + ex))
    ex = np.exp(x)
    return float(ex / (1.0 + ex))


def composite_score(feats: dict, line_present: bool) -> tuple:
    """Phase C heuristic v2 scorer.

    Returns ``(score, components)`` where ``components`` is a dict of the
    weighted contributions for transparency in the event detail payload. Weights are
    initial guesses; Phase D sweeps them against labels.

    When the start line is not available for a candidate (inference failed
    and no override), the three line-dependent terms are zeroed and the
    remaining weights are renormalized so the overall score stays on the
    same [0, 1] scale.
    """
    def _nz(key, default=0.0):
        v = feats.get(key)
        try:
            v = float(v)
        except (TypeError, ValueError):
            return default
        return v if np.isfinite(v) else default

    # Individual terms (each in [0, 1] before weighting).
    upwind_term  = _sigmoid(_nz('post60_best_single_tack_upwind_m') / 150.0 - 0.5)
    near_line    = _sigmoid(-abs(_nz('d_line_at_cross_m', 1e6)) / 40.0 + 1.0) if line_present else 0.0
    cross_dir    = 1.0 if _nz('cross_dir_sign') == 1.0 else 0.0
    if not line_present:
        cross_dir = 0.0
    loiter       = _sigmoid(_nz('time_within_30m_prestart_s') / 60.0 - 0.5) if line_present else 0.0
    tortuous     = 1.0 if _nz('prestart_tortuosity') > 1.8 else 0.0
    legacy       = max(0.0, _nz('post_frac') - _nz('pre_frac'))
    settled_pen  = 1.0 if (_nz('approach_hdg_settled') > 0.5
                            and _nz('post60_best_single_tack_upwind_m') < 50.0) else 0.0

    # Weights. When the line is present, they sum to 1.0 before subtracting
    # the transit penalty. When absent, we re-normalize line-less terms.
    w_upwind, w_near, w_dir, w_loiter, w_tortuous, w_legacy = (
        0.30, 0.20, 0.15, 0.15, 0.10, 0.10,
    )
    if not line_present:
        # Spread the 0.50 line-dependent budget across the remaining terms
        # in proportion to their native weights (0.30 + 0.10 + 0.10 = 0.50).
        w_upwind, w_tortuous, w_legacy = 0.60, 0.20, 0.20

    components = {
        'upwind':   round(w_upwind   * upwind_term, 4),
        'near_line': round(w_near    * near_line, 4),
        'cross_dir': round(w_dir     * cross_dir, 4),
        'loiter':    round(w_loiter  * loiter, 4),
        'tortuous':  round(w_tortuous * tortuous, 4),
        'legacy':    round(w_legacy  * legacy, 4),
        'transit_penalty': round(-0.30 * settled_pen, 4),
    }
    score = sum(components.values())
    # Clamp to [0, 1] so downstream UI treats it as a confidence-like value.
    score = max(0.0, min(1.0, score))
    return float(score), components


def _apply_nms(starts: list, window_s: int = NMS_WINDOW_S) -> list:
    """Drop any start whose ``score_v2`` is dominated by another within
    ``window_s`` seconds (same file). Greedy highest-first.

    ``starts`` is a list of ``{'index': ..., 'detail': ...}`` records as
    produced by :func:`find_awa_transitions`. Comparisons read fields from
    the index entry so this function stays cheap.
    """
    if len(starts) < 2:
        return starts
    def _score(rec):
        return rec['index'].get('summary', {}).get('score_v2', 0.0)
    starts_sorted = sorted(starts, key=_score, reverse=True)
    kept = []
    for s in starts_sorted:
        if all(abs(s['index']['t_key_unix'] - k['index']['t_key_unix']) >= window_s
               or s['index'].get('file') != k['index'].get('file')
               for k in kept):
            kept.append(s)
    kept.sort(key=lambda s: s['index']['t_key_unix'])
    return kept


def _kind_for_subtype(subtype: str):
    """Map an internal race-grammar subtype string to the flat ``kind`` enum
    written to ``events_index.json``. Returns ``None`` for kinds we no longer
    surface (``probable_delivery`` / fallback ``upwind_transition``); callers
    skip the candidate when ``None`` is returned.
    """
    if subtype == 'probable_start':
        return EVENT_KIND_START
    if subtype == 'ambiguous_start':
        return EVENT_KIND_OTHER
    if subtype == 'probable_leeward_rounding':
        return EVENT_KIND_LEEWARD_ROUNDING
    if subtype == 'probable_windward_rounding':
        return EVENT_KIND_WINDWARD_ROUNDING
    if subtype == 'ambiguous_rounding':
        return EVENT_KIND_OTHER
    if subtype == 'probable_delivery':
        return None
    return None


def _event_id(unix_time: float, event_type: str) -> str:
    d = pd.Timestamp(unix_time, unit='s', tz='UTC')
    return f"{d.strftime('%Y-%m-%dT%H_%M_%S')}-{event_type}"


def _unix_times(df: pd.DataFrame) -> np.ndarray:
    ts = pd.to_datetime(df['timestamp'], utc=True)
    return ts.astype('int64').to_numpy(dtype=np.float64) / 1e9


@dataclass
class EventSession:
    """Per-parquet state shared by all event detectors."""
    file_name: str
    df: pd.DataFrame
    race_context: RaceEventContext
    unix_times: np.ndarray
    lat: np.ndarray
    lon: np.ndarray
    map_lon: np.ndarray
    map_lat: np.ndarray
    awa: np.ndarray
    hdg: np.ndarray
    spd: np.ndarray
    twa: np.ndarray
    twd: np.ndarray
    upwind_vmg: np.ndarray

    @classmethod
    def from_df(cls, df: pd.DataFrame, file_name: str) -> 'EventSession':
        missing = [c for c in REQUIRED_COLS if c not in df.columns]
        if missing:
            raise ValueError(f"{file_name}: missing required columns {missing}")

        df_enriched = compute_derived_channels(df)
        race_context = RaceEventContext(df_enriched, fs=SAMPLES_PER_SEC)
        ready = race_context.df
        return cls(
            file_name=file_name,
            df=ready,
            race_context=race_context,
            unix_times=_unix_times(ready),
            lat=ready['latitude'].values,
            lon=ready['longitude'].values,
            map_lon=ready['x_m'].values,
            map_lat=ready['y_m'].values,
            awa=np.abs(ready['awa'].values),
            hdg=ready['hdg'].values,
            spd=ready['spd'].values * G.MS_2_KNOTS,
            twa=ready['twa'].values,
            twd=ready['twd_true'].values,
            upwind_vmg=ready['upwind_vmg_kts'].values,
        )

    def track_payload(self, window_start_idx: int, window_end_idx: int) -> tuple:
        track_slice = slice(window_start_idx, window_end_idx, TRACK_DECIMATION)
        track = []
        track_m = []
        for la, lo, my, mx, t in zip(
            self.lat[track_slice], self.lon[track_slice],
            self.map_lat[track_slice], self.map_lon[track_slice],
            self.unix_times[track_slice],
        ):
            if not (np.isfinite(la) and np.isfinite(lo) and np.isfinite(my) and np.isfinite(mx)):
                continue
            track.append([round(float(la), 6), round(float(lo), 6), round(float(t), 1)])
            track_m.append([round(float(my), 3), round(float(mx), 3), round(float(t), 1)])
        return track, track_m

    def series_slice(self, col_vals: np.ndarray, window_start_idx: int,
                     window_end_idx: int, event_idx: int, decimation: int = 5) -> list:
        out = []
        for i in range(window_start_idx, window_end_idx, decimation):
            t_s = round((i - event_idx) / SAMPLES_PER_SEC, 1)
            out.append([t_s, round(float(col_vals[i]), 3)])
        return out


def _close_hauled_cumsum(awa: np.ndarray) -> np.ndarray:
    close = np.asarray(awa < CLOSE_HAULED_AWA, dtype=np.int32)
    return np.concatenate(([0], np.cumsum(close)))


def _window_frac(cumsum: np.ndarray, lo: int, hi: int) -> float:
    return float((cumsum[hi] - cumsum[lo]) / max(hi - lo, 1))


def _event_window(idx: int, n: int, pre_s: int = 300, post_s: int = 600) -> tuple:
    return (
        max(0, idx - pre_s * SAMPLES_PER_SEC),
        min(n, idx + post_s * SAMPLES_PER_SEC),
    )


def _detail_series(session: EventSession, window_start_idx: int,
                   window_end_idx: int, event_idx: int) -> dict:
    return {
        'awa_series': session.series_slice(session.awa, window_start_idx, window_end_idx, event_idx),
        'hdg_series': session.series_slice(session.hdg, window_start_idx, window_end_idx, event_idx),
        'speed_series': session.series_slice(session.spd, window_start_idx, window_end_idx, event_idx),
        'vmg_series': session.series_slice(session.upwind_vmg, window_start_idx, window_end_idx, event_idx),
        'twa_series': session.series_slice(session.twa, window_start_idx, window_end_idx, event_idx),
        'twd_series': session.series_slice(session.twd, window_start_idx, window_end_idx, event_idx),
    }


def _finalize_start_record(session: EventSession, rec: dict) -> dict:
    data = rec.pop('_detail')
    start_idx = data['start_idx']
    window_start_idx, window_end_idx = _event_window(start_idx, len(session.df))
    track, track_m = session.track_payload(window_start_idx, window_end_idx)
    tacks, gybes = analyze_maneuvers(
        session.df, window_start_idx, window_end_idx, start_idx,
        fs=SAMPLES_PER_SEC,
    )
    rec['index']['tack_count'] = len(tacks)
    rec['index']['gybe_count'] = len(gybes)
    rec['index']['summary']['tack_count'] = len(tacks)
    rec['index']['summary']['gybe_count'] = len(gybes)
    rec['index']['preview_track'] = track[::PREVIEW_TRACK_DECIMATION]
    rec['detail'] = {
        'id': rec['index']['id'],
        'kind': rec['index']['kind'],
        't_start_unix': rec['index']['t_start_unix'],
        't_end_unix': rec['index']['t_end_unix'],
        't_key_unix': rec['index']['t_key_unix'],
        'trim': [None, None],
        'track': track,
        'track_m': track_m,
        **_detail_series(session, window_start_idx, window_end_idx, start_idx),
        'tacks': tacks,
        'gybes': gybes,
        'features': _round_features(data['feats']),
        'race_grammar_features': _round_features(data['grammar']['features']),
        'race_grammar': {
            'subtype': data['grammar']['subtype'],
            'start_confidence': data['grammar']['start_confidence'],
            'rounding_confidence': data['grammar']['rounding_confidence'],
            'review_priority': data['grammar']['review_priority'],
            'scores': data['grammar']['scores'],
        },
        'feature_schema_version': FEATURE_SCHEMA_VERSION,
        'start_line_inferred': data['line'].to_dict() if data['line'] is not None else None,
        'start_line_source': data['line_source'],
        'score_components': data['score_components'],
    }
    return rec


def find_awa_transitions(session: EventSession):
    awa = session.awa
    hdg = session.hdg
    unix_times = session.unix_times
    pre_samples = PRE_WINDOW_S * SAMPLES_PER_SEC
    post_samples = POST_WINDOW_S * SAMPLES_PER_SEC
    step = SAMPLES_PER_SEC * CANDIDATE_STEP_S

    # Candidate generation: per-sample thresholds + hdg_std filter applied
    # up-front, so clustering's max-score pick isn't biased toward the leading
    # edge of a transition (where pre_window hasn't yet captured the loiter).
    candidates = []
    close_cumsum = _close_hauled_cumsum(awa)
    for i in range(pre_samples, len(awa) - post_samples, step):
        pre_frac = _window_frac(close_cumsum, i - pre_samples, i)
        post_frac = _window_frac(close_cumsum, i, i + post_samples)
        if not (pre_frac < PRE_THRESHOLD and post_frac > POST_THRESHOLD):
            continue
        if pre_frac < MIN_PRE_FRAC:
            continue
        pre_hdg = hdg[i - pre_samples:i]
        if len(pre_hdg) <= 100:
            continue
        hdg_std = np.degrees(np.std(np.unwrap(np.radians(pre_hdg))))
        if hdg_std < MIN_HDG_STD:
            continue
        candidates.append({
            'idx': i, 'time': unix_times[i],
            'pre_frac': pre_frac, 'post_frac': post_frac,
            'score': post_frac - pre_frac,
            'hdg_std': round(hdg_std, 1),
        })

    if not candidates:
        return []

    # Cluster
    clustered = []
    current = [candidates[0]]
    for c in candidates[1:]:
        if c['time'] - current[-1]['time'] < 120:
            current.append(c)
        else:
            clustered.append(max(current, key=lambda x: x['score']))
            current = [c]
    clustered.append(max(current, key=lambda x: x['score']))

    starts = []
    for c in clustered:
        start_time = c['time']
        start_idx = c['idx']
        d = pd.Timestamp(start_time, unit='s', tz='UTC')
        start_id = d.strftime('%Y-%m-%dT%H_%M')

        # Train/infer symmetry: features ALWAYS use the inferred line. At
        # production-inference time on a new race, there is no human label to
        # fall back on — if training features could depend on a user-drawn
        # line, the model would silently expect one at inference and fail.
        # User corrections in events_edits.json are preserved separately as
        # ground truth for a future line-inference training loop; they never
        # flow into the feature row.
        line = infer_start_line(session.df, start_idx)
        line_source = 'inferred' if line is not None else None
        feats = compute_candidate_features(
            session.df, start_idx, line,
            pre_frac=c['pre_frac'], post_frac=c['post_frac'],
        )
        grammar = session.race_context.classify_candidate(start_idx, line=line)
        score_v2, score_components = composite_score(feats, line_present=(line is not None))
        # OCS-return flag combines "crossed toward course side" with
        # reverse_cross_within_90s (a meaningful retreat to prestart side).
        possible_ocs = bool(
            feats.get('reverse_cross_within_90s', 0.0) == 1.0
            and feats.get('cross_dir_sign') == 1.0
        )
        top = sorted(score_components.items(), key=lambda kv: kv[1], reverse=True)[:2]
        notes = (
            f"{grammar['subtype']}: start={grammar['start_confidence']:.2f} "
            f"round={grammar['rounding_confidence']:.2f}; v2: "
            + ", ".join(f"{k}={v:+.2f}" for k, v in top)
        )

        kind = _kind_for_subtype(grammar['subtype'])
        if kind is None:
            continue
        index_entry = {
            'id': start_id,
            'kind': kind,
            # Generic event window: t_start_unix .. t_end_unix bound the full
            # detail payload; t_key_unix is the canonical moment of the event
            # (the gun for starts; rounding instant for marks; line crossing
            # for finishes; None for free-form commented segments).
            't_start_unix': start_time - 300,
            't_end_unix':   start_time + 600,
            't_key_unix':   start_time,
            'date': d.strftime('%Y-%m-%d'),
            'file': session.file_name,
            # race_id groups multiple events (start + roundings + finish) for
            # the same race. Populated by future detectors; left null here.
            'race_id': None,
            'label': None,
            'status': 'pending',
            'notes': notes,
            'confidence': round(score_v2, 3),
            'start_confidence': grammar['start_confidence'],
            'rounding_confidence': grammar['rounding_confidence'],
            'review_priority': grammar['review_priority'],
            'tack_count': 0,
            'gybe_count': 0,
            'summary': {
                'score_v2': round(score_v2, 3),
                'score_legacy': round(c['score'], 3),
                'possible_ocs_return': possible_ocs,
                'race_grammar_subtype': grammar['subtype'],
                'start_confidence': grammar['start_confidence'],
                'rounding_confidence': grammar['rounding_confidence'],
                'review_priority': grammar['review_priority'],
                'tack_count': 0,
                'gybe_count': 0,
                **grammar['scores'],
            },
            'preview_track': [],
        }

        # NMS / score-threshold filters operate on the index entry, since they
        # need ``t_key_unix`` and ``summary.score_v2``.
        starts.append({
            'index': index_entry,
            '_detail': {
                'start_idx': start_idx,
                'line': line,
                'line_source': line_source,
                'feats': feats,
                'grammar': grammar,
                'score_components': score_components,
            },
        })

    # Non-maximum suppression on the v2 score across the whole file.
    starts = _apply_nms(starts)
    # Drop candidates that fall below the minimum score; keeps the events
    # index from bloating with low-information events after the stage-1
    # loosening.
    starts = [s for s in starts
              if s['index']['summary']['score_v2'] >= SCORE_V2_MIN]
    return [_finalize_start_record(session, s) for s in starts]


def _finalize_windward_record(session: EventSession, rec: dict) -> dict:
    data = rec.pop('_detail')
    start_idx = data['start_idx']
    window_start_idx, window_end_idx = _event_window(start_idx, len(session.df))
    track, track_m = session.track_payload(window_start_idx, window_end_idx)
    tacks, gybes = analyze_maneuvers(
        session.df, window_start_idx, window_end_idx, start_idx,
        fs=SAMPLES_PER_SEC,
    )
    rec['index']['tack_count'] = len(tacks)
    rec['index']['gybe_count'] = len(gybes)
    rec['index']['summary']['tack_count'] = len(tacks)
    rec['index']['summary']['gybe_count'] = len(gybes)
    rec['index']['preview_track'] = track[::PREVIEW_TRACK_DECIMATION]
    grammar = data['grammar']
    rec['detail'] = {
        'id': rec['index']['id'],
        'kind': rec['index']['kind'],
        't_start_unix': rec['index']['t_start_unix'],
        't_end_unix': rec['index']['t_end_unix'],
        't_key_unix': rec['index']['t_key_unix'],
        'trim': [None, None],
        'track': track,
        'track_m': track_m,
        **_detail_series(session, window_start_idx, window_end_idx, start_idx),
        'tacks': tacks,
        'gybes': gybes,
        'features': {},
        'race_grammar_features': _round_features(grammar['features']),
        'race_grammar': {
            'subtype': grammar['subtype'],
            'start_confidence': grammar['start_confidence'],
            'rounding_confidence': data['rounding_conf'],
            'review_priority': grammar['review_priority'],
            'scores': grammar['scores'],
        },
        'feature_schema_version': FEATURE_SCHEMA_VERSION,
        'start_line_inferred': None,
        'start_line_source': None,
        'score_components': {},
    }
    return rec


def find_windward_roundings(session: EventSession):
    """Detect transitions OUT of close-hauled (windward mark roundings).

    Mirror of :func:`find_awa_transitions` with inverted threshold semantics:
    pre-window is the upwind beat (high close-hauled fraction), post-window
    is the offwind leg (low close-hauled fraction). The start-tuned
    ``MIN_HDG_STD`` filter is intentionally omitted — a windward approach
    on a layline shows a steady heading (std typically <= 15 deg), so
    requiring high heading variance would reject every real candidate.
    """
    awa = session.awa
    unix_times = session.unix_times
    pre_samples = PRE_WINDOW_S * SAMPLES_PER_SEC
    post_samples = POST_WINDOW_S * SAMPLES_PER_SEC
    step = SAMPLES_PER_SEC * CANDIDATE_STEP_S

    candidates = []
    close_cumsum = _close_hauled_cumsum(awa)
    for i in range(pre_samples, len(awa) - post_samples, step):
        pre_frac = _window_frac(close_cumsum, i - pre_samples, i)
        post_frac = _window_frac(close_cumsum, i, i + post_samples)
        if not (pre_frac > POST_THRESHOLD and post_frac < PRE_THRESHOLD):
            continue
        if pre_frac < 0.50:
            continue
        candidates.append({
            'idx': i, 'time': unix_times[i],
            'pre_frac': pre_frac, 'post_frac': post_frac,
            'score': pre_frac - post_frac,
        })

    if not candidates:
        return []

    clustered = []
    current = [candidates[0]]
    for c in candidates[1:]:
        if c['time'] - current[-1]['time'] < 120:
            current.append(c)
        else:
            clustered.append(max(current, key=lambda x: x['score']))
            current = [c]
    clustered.append(max(current, key=lambda x: x['score']))

    roundings = []
    for c in clustered:
        start_time = c['time']
        start_idx = c['idx']
        d = pd.Timestamp(start_time, unit='s', tz='UTC')
        event_id = d.strftime('%Y-%m-%dT%H_%M') + '-windward'

        grammar = session.race_context.classify_windward_candidate(start_idx)
        if grammar['subtype'] is None:
            continue

        rounding_conf = grammar['rounding_confidence']
        notes = (
            f"{grammar['subtype']}: round={rounding_conf:.2f} "
            f"windward_score={grammar['scores']['windward_rounding_score']:.2f}"
        )

        kind = _kind_for_subtype(grammar['subtype'])
        if kind is None:
            continue
        index_entry = {
            'id': event_id,
            'kind': kind,
            't_start_unix': start_time - 300,
            't_end_unix':   start_time + 600,
            't_key_unix':   start_time,
            'date': d.strftime('%Y-%m-%d'),
            'file': session.file_name,
            'race_id': None,
            'label': None,
            'status': 'pending',
            'notes': notes,
            'confidence': round(rounding_conf, 3),
            'start_confidence': grammar['start_confidence'],
            'rounding_confidence': rounding_conf,
            'review_priority': grammar['review_priority'],
            'tack_count': 0,
            'gybe_count': 0,
            'summary': {
                # NMS reuses _apply_nms which sorts on score_v2; populate
                # it with rounding_confidence so windward roundings can
                # be deduplicated within their own list.
                'score_v2': round(rounding_conf, 3),
                'score_legacy': round(c['score'], 3),
                'race_grammar_subtype': grammar['subtype'],
                'start_confidence': grammar['start_confidence'],
                'rounding_confidence': rounding_conf,
                'review_priority': grammar['review_priority'],
                'tack_count': 0,
                'gybe_count': 0,
                **grammar['scores'],
            },
            'preview_track': [],
        }
        roundings.append({
            'index': index_entry,
            '_detail': {
                'start_idx': start_idx,
                'grammar': grammar,
                'rounding_conf': rounding_conf,
            },
        })

    roundings = _apply_nms(roundings)
    roundings = [r for r in roundings
                 if r['index']['summary']['score_v2'] >= 0.5]
    return [_finalize_windward_record(session, r) for r in roundings]


def find_maneuver_events(session: EventSession):
    """Promote whole-day tack/gybe detections to first-class events."""
    all_tacks, all_gybes = analyze_maneuvers(
        session.df, 0, len(session.df), 0, fs=SAMPLES_PER_SEC,
    )
    rows = [(EVENT_KIND_TACK, e) for e in all_tacks] + [(EVENT_KIND_GYBE, e) for e in all_gybes]
    rows.sort(key=lambda kv: kv[1].get('abs_idx', 0))
    events = []

    for kind, maneuver in rows:
        detected_idx = int(maneuver.get('abs_idx', 0))
        event_idx = max(0, detected_idx - MANEUVER_EVENT_LEAD_S * SAMPLES_PER_SEC)
        if detected_idx <= 0 or detected_idx >= len(session.df):
            continue
        event_time = float(session.unix_times[event_idx])
        d = pd.Timestamp(event_time, unit='s', tz='UTC')
        event_id = _event_id(event_time, kind)
        pre_s, post_s = 60, 120
        window_start_idx = max(0, event_idx - pre_s * SAMPLES_PER_SEC)
        window_end_idx = min(len(session.df), event_idx + post_s * SAMPLES_PER_SEC)
        track, track_m = session.track_payload(window_start_idx, window_end_idx)

        marker = dict(maneuver)
        marker['time_s'] = round((detected_idx - event_idx) / SAMPLES_PER_SEC, 1)
        marker['detected_t_unix'] = round(float(session.unix_times[detected_idx]), 1)
        marker['event_lead_s'] = MANEUVER_EVENT_LEAD_S
        index_entry = {
            'id': event_id,
            'kind': kind,
            't_start_unix': event_time - pre_s,
            't_end_unix': event_time + post_s,
            't_key_unix': event_time,
            'date': d.strftime('%Y-%m-%d'),
            'file': session.file_name,
            'race_id': None,
            'label': None,
            'status': 'pending',
            'notes': f"{kind}: {maneuver.get('direction', '')} {maneuver.get('hdg_change', '')}°",
            'confidence': 1.0,
            'review_priority': 'medium',
            'tack_count': 1 if kind == EVENT_KIND_TACK else 0,
            'gybe_count': 1 if kind == EVENT_KIND_GYBE else 0,
            'summary': {
                'direction': maneuver.get('direction'),
                'hdg_change': maneuver.get('hdg_change'),
                'duration_s': maneuver.get('duration_s'),
                'entry_spd_kts': maneuver.get('entry_spd_kts'),
                'entry_vmg_kts': maneuver.get('entry_vmg_kts'),
                'spd_recovery_s_80': maneuver.get('spd_recovery_s_80'),
                'spd_recovery_s_90': maneuver.get('spd_recovery_s_90'),
                'time_below_80_spd_s': maneuver.get('time_below_80_spd_s'),
                'distance_lost_m': maneuver.get('distance_lost_m'),
                'time_lost_s': maneuver.get('time_lost_s'),
                'min_spd_kts': maneuver.get('min_spd_kts'),
                'max_yaw_rate_deg_s': maneuver.get('max_yaw_rate_deg_s'),
                'twa_symmetry_deg': maneuver.get('twa_symmetry_deg'),
            },
            'preview_track': track[::PREVIEW_TRACK_DECIMATION],
        }
        detail = {
            **index_entry,
            'trim': [None, None],
            'track': track,
            'track_m': track_m,
            **_detail_series(session, window_start_idx, window_end_idx, event_idx),
            'tacks': [marker] if kind == EVENT_KIND_TACK else [],
            'gybes': [marker] if kind == EVENT_KIND_GYBE else [],
        }
        events.append({'index': index_entry, 'detail': detail})
    return events


def _round_features(feats: dict, ndigits: int = 3) -> dict:
    """JSON-friendly rounding; NaN stays as None for valid JSON."""
    out = {}
    for k, v in feats.items():
        if v is None:
            out[k] = None
        elif isinstance(v, float) and not np.isfinite(v):
            out[k] = None
        elif isinstance(v, float):
            out[k] = round(v, ndigits)
        else:
            out[k] = v
    return out


_LOG_DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2})')


def _log_date(filename: str) -> str:
    """Extract the YYYY-MM-DD date prefix from a parquet filename."""
    m = _LOG_DATE_RE.match(os.path.basename(filename))
    return m.group(1) if m else ''


_REPO_ANALYZE = str(Path(__file__).resolve().parent)


def write_session_track(df, file_name: str, sessions_dir: str) -> str:
    """Write a decimated session-wide track to ``Sessions/<basename>.json``.

    The file mirrors what the old ``generate_tracks.py`` used to put in the
    monolithic ``tracks.json`` — same shape, but per-parquet so review.js can
    lazy-load only the session that contains the selected event. Carries
    both lat/lon (``track``) and projected meters (``track_m``); the latter
    feeds the wind-up overlay frame in review.js.
    """
    lat = df['latitude'].values[::SESSION_TRACK_DECIMATION]
    lon = df['longitude'].values[::SESSION_TRACK_DECIMATION]
    if 'x_m' in df.columns and 'y_m' in df.columns:
        map_lon = df['x_m'].values[::SESSION_TRACK_DECIMATION]
        map_lat = df['y_m'].values[::SESSION_TRACK_DECIMATION]
    else:
        map_lon, map_lat = G.MAP(lon, lat)
    ts = df['timestamp'].values[::SESSION_TRACK_DECIMATION]

    times = []
    for t in ts:
        if hasattr(t, 'timestamp'):
            times.append(float(t.timestamp()))
        else:
            times.append(float(pd.Timestamp(t).timestamp()))

    track = []
    track_m = []
    for la, lo, my, mx, t in zip(lat, lon, map_lat, map_lon, times):
        if not (np.isfinite(la) and np.isfinite(lo) and np.isfinite(my) and np.isfinite(mx)):
            continue
        track.append([round(float(la), 6), round(float(lo), 6), round(t, 1)])
        track_m.append([round(float(my), 3), round(float(mx), 3), round(t, 1)])

    payload = {
        'file': file_name,
        'date': _log_date(file_name),
        'points': len(track),
        'track': track,
        'track_m': track_m,
    }
    out_path = os.path.join(sessions_dir, os.path.splitext(file_name)[0] + '.json')
    with open(out_path, 'w') as f:
        json.dump(payload, f)
    return out_path


def process_log_file(file_name: str, logs_dir: str, sessions_dir: str) -> dict:
    """Process one parquet file and return JSON-ready event records.

    This is deliberately top-level so ProcessPoolExecutor can pickle it on
    macOS's spawn-based multiprocessing.
    """
    messages = [f"  {file_name}"]
    try:
        df = race_logs.read_log_file(file_name, path=logs_dir)
        if df is None or len(df) < 6000:
            messages.append("    Skipping (too short)")
            return {'file': file_name, 'records': [], 'messages': messages, 'error': None}

        session = EventSession.from_df(df, file_name)
        # Emit the per-session track first so a partial run still leaves
        # the map backdrop available for any events written below.
        write_session_track(session.df, file_name, sessions_dir)
        file_records = find_awa_transitions(session)
        file_records.extend(find_windward_roundings(session))
        file_records.extend(find_maneuver_events(session))
        for rec in file_records:
            messages.append(f"    Found {rec['index'].get('kind')}: {rec['index']['id']}")
        return {'file': file_name, 'records': file_records, 'messages': messages, 'error': None}
    except Exception as e:
        messages.append(f"    Error: {e}")
        return {'file': file_name, 'records': [], 'messages': messages, 'error': str(e)}


def _process_files(log_files: list, logs_dir: str, sessions_dir: str, jobs: int) -> list:
    if jobs <= 1 or len(log_files) <= 1:
        return [process_log_file(f, logs_dir, sessions_dir) for f in log_files]

    results = []
    with concurrent.futures.ProcessPoolExecutor(max_workers=jobs) as executor:
        futures = [
            executor.submit(process_log_file, f, logs_dir, sessions_dir)
            for f in log_files
        ]
        by_file = {}
        for future in concurrent.futures.as_completed(futures):
            result = future.result()
            by_file[result['file']] = result
        # Preserve input ordering for deterministic progress output and detail
        # write ordering. The final index is sorted by event time below.
        results = [by_file[f] for f in log_files if f in by_file]
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--events',
                        default=os.path.join(_REPO_ANALYZE, 'events_index.json'),
                        help='Path to the events index JSON (formerly starts.json).')
    parser.add_argument('--events-dir',
                        default=os.path.join(_REPO_ANALYZE, 'Events'),
                        help='Directory holding per-event detail JSON files.')
    parser.add_argument('--sessions-dir',
                        default=os.path.join(_REPO_ANALYZE, 'Sessions'),
                        help='Directory holding per-parquet session-wide tracks (replaces tracks.json).')
    parser.add_argument('--logs-dir', default='/Users/viola/Data/SailingData/PandasLogs')
    parser.add_argument('--days', type=int, default=None, help='Only process files from the last N days')
    parser.add_argument('--since', default=None, help='Only process files >= YYYY-MM-DD')
    parser.add_argument('--jobs', type=int, default=0,
                        help='Parallel parquet workers. 0 = auto/use available cores (default); 1 = serial.')
    parser.add_argument('--tracks', default=None,
                        help='Deprecated; ignored. Kept so existing callers do not break.')
    parser.add_argument('--starts', default=None,
                        help='Deprecated; ignored. The monolithic starts.json was '
                             'split into events_index.json + Events/<id>.json.')
    parser.add_argument('--force', action='store_true',
                        help='Reprocess every parquet in scope even if its session '
                             'track is already up to date. Default skips parquets '
                             'whose mtime is older than the staged session JSON.')
    args = parser.parse_args()

    since = args.since
    if args.days and not since:
        since = (datetime.now() - timedelta(days=args.days)).strftime('%Y-%m-%d')

    parquet_paths = sorted(glob.glob(os.path.join(args.logs_dir, '*.parquet')))
    log_files = [os.path.basename(p) for p in parquet_paths]
    if since:
        log_files = [f for f in log_files if _log_date(f) >= since]

    os.makedirs(args.events_dir, exist_ok=True)
    os.makedirs(args.sessions_dir, exist_ok=True)

    # Incremental gate: a parquet is "fresh" when its companion session
    # JSON exists and is at least as new as the parquet. This is the
    # canonical per-file sentinel because write_session_track is the
    # last thing process_log_file writes, so any earlier crash leaves
    # the session JSON missing/older and forces a redo.
    in_scope_files = list(log_files)
    if not args.force:
        fresh = []
        for f in log_files:
            parquet_path = os.path.join(args.logs_dir, f)
            session_path = os.path.join(
                args.sessions_dir, os.path.splitext(f)[0] + '.json')
            if (os.path.exists(session_path)
                    and os.path.getmtime(session_path)
                        >= os.path.getmtime(parquet_path)):
                fresh.append(f)
        log_files = [f for f in log_files if f not in set(fresh)]
        if fresh:
            print(f"Skipping {len(fresh)}/{len(in_scope_files)} parquet files "
                  f"with up-to-date session tracks (--force to override)")

    jobs_arg = int(args.jobs or 0)
    jobs = min(len(log_files), os.cpu_count() or 1) if jobs_arg <= 0 else jobs_arg
    jobs = max(1, jobs)
    print(f"Processing {len(log_files)} parquet files from {args.logs_dir} (jobs={jobs})")

    # Load existing index. Preserve entries for (a) files outside the current
    # scope and (b) other event types — future find_roundings.py / find_finishes.py
    # share this index, and find_starts.py must not stomp their entries.
    # Deliberately NOT loading events_edits.json here: human line/mark edits
    # are supervision for future inference-model training, not inputs to the
    # per-candidate feature row. Keeping that invariant prevents train/infer
    # skew (there are no human labels on a fresh race day).
    try:
        with open(args.events) as f:
            existing = json.load(f)
    except FileNotFoundError:
        existing = {'version': EVENTS_INDEX_VERSION, 'events': []}
    selected_files = set(log_files)
    # Schema-version gate: old indexes carry the legacy type/subtype/tags
    # trio and have no `kind`. Mixing them into the new index would feed
    # review.js entries it cannot filter or render. Drop everything and
    # force a full regeneration when the on-disk version is behind.
    if existing.get('version', 0) < EVENTS_INDEX_VERSION:
        print(f"events_index.json is version {existing.get('version', 0)}; "
              f"current is {EVENTS_INDEX_VERSION}. Dropping all preserved entries; "
              f"re-run with a window that covers every parquet to fully repopulate.")
        preserved = []
    else:
        preserved = [
            e for e in existing.get('events', [])
            if e.get('file') not in selected_files
        ]

    new_records = []  # list of {'index': ..., 'detail': ...}
    for result in _process_files(log_files, args.logs_dir, args.sessions_dir, jobs):
        for message in result['messages']:
            print(message)
        new_records.extend(result['records'])

    if not log_files:
        # Nothing was reprocessed, so the index would be re-written
        # byte-for-byte identically — skip the write to keep its mtime
        # stable for downstream incremental tooling.
        print(f"\nNo parquet files needed processing; events_index.json untouched")
        return

    # Write per-event detail files first, so a partial run leaves a self-
    # consistent on-disk state (any index entry that lands in the index has
    # its detail file already on disk).
    for rec in new_records:
        detail_path = os.path.join(args.events_dir, f"{rec['index']['id']}.json")
        with open(detail_path, 'w') as f:
            json.dump(rec['detail'], f, separators=(',', ':'))

    all_events = preserved + [rec['index'] for rec in new_records]
    # Stable sort: keep by event time so the review.html list renders in
    # chronological order without a client-side sort.
    all_events.sort(key=lambda e: e.get('t_key_unix') or e.get('t_start_unix') or 0)

    with open(args.events, 'w') as f:
        json.dump(
            {'version': EVENTS_INDEX_VERSION, 'events': all_events},
            f, indent=2,
        )

    kind_counts = {}
    for e in all_events:
        kind_counts[e.get('kind', 'unknown')] = kind_counts.get(e.get('kind', 'unknown'), 0) + 1
    print(f"\nWrote {len(all_events)} events {kind_counts} to {args.events}")
    print(f"Wrote {len(new_records)} detail files under {args.events_dir}")

    # One-shot cleanup of the legacy monolithic file. The aws_sync.sh script
    # uses ``aws s3 sync`` without ``--delete``, so the S3 copy of starts.json
    # must be removed by hand once: ``aws s3 rm s3://regattaanalysis/www/Analyze/starts.json``.
    legacy_path = os.path.join(_REPO_ANALYZE, 'starts.json')
    if os.path.exists(legacy_path):
        os.remove(legacy_path)
        print(f"Removed legacy {legacy_path}")


if __name__ == '__main__':
    main()
