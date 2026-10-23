#!/usr/bin/env python
"""Evaluate the start detector against ``events_edits.json`` ground truth.

With no confirmed labels (the state at Phase D launch), the harness operates
in **precision-only** mode:
  - reports total predictions per day;
  - counts predictions that match a previously ``discarded`` label (known FP
    that the detector still emits);
  - counts ``discarded`` labels that have no matching prediction (known FP
    that the new detector successfully suppressed — a desirable outcome).

Once confirmed labels accumulate it automatically adds precision / recall / F1
reporting, per-day and overall.

Also supports sweeping the Phase C composite weights by recomputing
``score_v2`` from stored per-candidate features — no need to rerun
``find_starts.py``.
"""
import argparse
import itertools
import json
import os
import sys
from collections import defaultdict
from typing import Iterable, List, Optional

import numpy as np
import pandas as pd


MATCH_TOLERANCE_S = 15
DEFAULT_THRESHOLD = 0.20
NMS_WINDOW_S = 180

# Weight schema matching composite_score in find_starts.py.
DEFAULT_WEIGHTS = {
    'upwind':    0.30,
    'near_line': 0.20,
    'cross_dir': 0.15,
    'loiter':    0.15,
    'tortuous':  0.10,
    'legacy':    0.10,
    'transit_penalty': -0.30,
}


# ---------------------------------------------------------------------------
# Ground truth + predictions
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

def _parse_id_to_unix(sid: str) -> Optional[float]:
    """Recover a timestamp from a start_id of form ``YYYY-MM-DDTHH_MM`` (UTC).

    ``find_starts.py`` derives the id by ``pd.Timestamp(..., tz='UTC').strftime``,
    so inverting that gives the minute-precision label time. Good enough for
    ±15 s event matching.
    """
    try:
        ts = pd.Timestamp(sid.replace('_', ':'), tz='UTC')
        return float(ts.timestamp())
    except Exception:
        return None


def _event_time(event: dict) -> float:
    for key in ('t_key_unix', 'start_time_unix'):
        if event.get(key) is not None:
            return float(event[key])
    if event.get('t_start_unix') is not None:
        return float(event['t_start_unix']) + 300.0
    raise KeyError(f"event {event.get('id', '<unknown>')} has no key timestamp")


def _load_events(events_path: str) -> list[dict]:
    with open(events_path) as f:
        return json.load(f).get('events', [])


def _load_detail(events_dir: Optional[str], event_id: str) -> dict:
    if not events_dir:
        return {}
    path = os.path.join(events_dir, f'{event_id}.json')
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        return json.load(f)


def load_ground_truth(edits_path: str, events_path: str) -> pd.DataFrame:
    """Load labeled events (status in {confirmed, discarded}).

    Labels are keyed by event id. Prefer the timestamp from ``events_index.json``,
    but fall back to parsing the id for labels whose candidate was re-detected
    at a slightly different time after a detector change.
    """
    with open(edits_path) as f:
        edits = json.load(f).get('edits', {})
    events = {e['id']: e for e in _load_events(events_path)}

    rows = []
    orphans_parsed = 0
    orphans_dropped = 0
    for sid, e in edits.items():
        if e.get('status') not in ('confirmed', 'discarded'):
            continue
        s = events.get(sid)
        if s is not None:
            ts = _event_time(s)
            file_ = s.get('file', '')
            date_ = s.get('date', '')
        else:
            parsed = _parse_id_to_unix(sid)
            if parsed is None:
                orphans_dropped += 1
                continue
            orphans_parsed += 1
            ts = parsed
            file_ = ''
            date_ = sid[:10]
        rows.append({
            'id': sid,
            'status': e['status'],
            'start_time_unix': ts,
            'file': file_,
            'date': date_,
        })
    if orphans_parsed:
        print(f"note: {orphans_parsed} labels matched by id-parsed timestamp "
              f"(candidate re-detected at a slightly different time)")
    if orphans_dropped:
        print(f"warning: {orphans_dropped} labels with unparseable start_id dropped")
    return pd.DataFrame(rows)


def load_predictions(events_path: str, events_dir: Optional[str] = None) -> pd.DataFrame:
    """Start candidates present in ``events_index.json``.

    Per-candidate features live in ``Analyze/Events/<id>.json``; load them when
    available so the weight sweep can still rescore current event files.
    """
    starts = [e for e in _load_events(events_path) if e.get('type') == 'start']
    rows = []
    for s in starts:
        detail = _load_detail(events_dir, s['id'])
        summary = s.get('summary') or {}
        features = detail.get('features') or s.get('features') or {}
        rows.append({
            'id': s['id'],
            'start_time_unix': _event_time(s),
            'file': s.get('file', ''),
            'date': s.get('date', ''),
            'score_v2': float(summary.get('score_v2', s.get('confidence', 0.0))),
            'score_legacy': float(summary.get('score_legacy', 0.0)),
            'line_present': detail.get('start_line_inferred') is not None,
            **_feature_columns(features),
        })
    return pd.DataFrame(rows)


_FEATURE_COLS = (
    'post60_best_single_tack_upwind_m',
    'd_line_at_cross_m',
    'cross_dir_sign',
    'time_within_30m_prestart_s',
    'prestart_tortuosity',
    'pre_frac', 'post_frac',
    'approach_hdg_settled',
)


def _feature_columns(feats: dict) -> dict:
    out = {}
    for k in _FEATURE_COLS:
        v = feats.get(k)
        out[k] = float(v) if isinstance(v, (int, float)) and v is not None else np.nan
    return out


# ---------------------------------------------------------------------------
# Rescoring (sweep support)
# ---------------------------------------------------------------------------

def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-np.clip(x, -30, 30)))


def rescore(preds: pd.DataFrame, weights: dict) -> pd.Series:
    """Recompute ``score_v2`` for a set of weights without touching the JSON.
    Mirrors ``find_starts.composite_score`` but vectorized on the DataFrame.
    """
    w = {**DEFAULT_WEIGHTS, **weights}
    line = preds['line_present'].astype(bool)

    upwind = _sigmoid(preds['post60_best_single_tack_upwind_m'].fillna(0) / 150.0 - 0.5)
    near_line = _sigmoid(-preds['d_line_at_cross_m'].abs().fillna(1e6) / 40.0 + 1.0)
    cross_dir = (preds['cross_dir_sign'] == 1.0).astype(float)
    loiter = _sigmoid(preds['time_within_30m_prestart_s'].fillna(0) / 60.0 - 0.5)
    tortuous = (preds['prestart_tortuosity'].fillna(0) > 1.8).astype(float)
    legacy = (preds['post_frac'].fillna(0) - preds['pre_frac'].fillna(0)).clip(lower=0.0)
    settled_pen = ((preds['approach_hdg_settled'].fillna(0) > 0.5)
                   & (preds['post60_best_single_tack_upwind_m'].fillna(0) < 50.0)).astype(float)

    # Zero out line-dependent terms when no line is present; keep weights fixed
    # (the detector redistributes them; for a sweep it is simpler to just let
    # those events score a bit lower — they still participate in NMS).
    near_line = near_line.where(line, 0.0)
    cross_dir = cross_dir.where(line, 0.0)
    loiter = loiter.where(line, 0.0)

    score = (
        w['upwind'] * upwind
        + w['near_line'] * near_line
        + w['cross_dir'] * cross_dir
        + w['loiter'] * loiter
        + w['tortuous'] * tortuous
        + w['legacy'] * legacy
        + w['transit_penalty'] * settled_pen
    ).clip(lower=0.0, upper=1.0)
    return score


def apply_threshold_and_nms(
    preds: pd.DataFrame,
    score_col: str,
    threshold: float,
    nms_window_s: int,
) -> pd.DataFrame:
    """Filter by threshold, then greedy highest-first NMS per file."""
    kept = preds[preds[score_col] >= threshold].copy()
    kept = kept.sort_values(score_col, ascending=False)
    out_idx = []
    for file_name, grp in kept.groupby('file', sort=False):
        times = []
        for idx, row in grp.iterrows():
            if all(abs(row['start_time_unix'] - t) >= nms_window_s for t in times):
                out_idx.append(idx)
                times.append(row['start_time_unix'])
    return preds.loc[out_idx].sort_values('start_time_unix').reset_index(drop=True)


# ---------------------------------------------------------------------------
# Event matching
# ---------------------------------------------------------------------------

def match_events(
    pred_ts: Iterable[float],
    gt_ts: Iterable[float],
    tol_s: int = MATCH_TOLERANCE_S,
) -> dict:
    """Greedy 1:1 match by temporal proximity within ``tol_s`` seconds.

    Returns ``{'matches': [(pred_i, gt_j), ...], 'fp_pred': [i], 'fn_gt': [j]}``.
    """
    pred_ts = list(pred_ts)
    gt_ts = list(gt_ts)
    pairs = []
    for i, pt in enumerate(pred_ts):
        for j, gt in enumerate(gt_ts):
            dt = abs(pt - gt)
            if dt <= tol_s:
                pairs.append((dt, i, j))
    pairs.sort()
    used_p, used_g, matches = set(), set(), []
    for _, i, j in pairs:
        if i in used_p or j in used_g:
            continue
        matches.append((i, j))
        used_p.add(i); used_g.add(j)
    fp = [i for i in range(len(pred_ts)) if i not in used_p]
    fn = [j for j in range(len(gt_ts)) if j not in used_g]
    return {'matches': matches, 'fp_pred': fp, 'fn_gt': fn}


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def _metrics_for_day(
    pred: pd.DataFrame,
    gt: pd.DataFrame,
    tol_s: int,
) -> dict:
    confirmed = gt[gt['status'] == 'confirmed']
    discarded = gt[gt['status'] == 'discarded']
    pred_ts = pred['start_time_unix'].tolist()

    conf_match = match_events(pred_ts, confirmed['start_time_unix'].tolist(), tol_s)
    disc_match = match_events(pred_ts, discarded['start_time_unix'].tolist(), tol_s)

    tp = len(conf_match['matches'])
    fn = len(conf_match['fn_gt'])
    known_fp_preserved = len(disc_match['matches'])
    known_fp_suppressed = len(disc_match['fn_gt'])
    # "New" predictions: didn't match any labeled event (confirmed or discarded).
    matched_pred_conf = {i for i, _ in conf_match['matches']}
    matched_pred_disc = {i for i, _ in disc_match['matches']}
    unknown_preds = [i for i in range(len(pred_ts))
                     if i not in matched_pred_conf and i not in matched_pred_disc]
    return {
        'preds': len(pred_ts),
        'confirmed_labels': len(confirmed),
        'discarded_labels': len(discarded),
        'tp': tp,
        'fn': fn,
        'known_fp_preserved': known_fp_preserved,
        'known_fp_suppressed': known_fp_suppressed,
        'unknown_preds': len(unknown_preds),
    }


def _aggregate_pr(rows: List[dict]) -> dict:
    tp = sum(r['tp'] for r in rows)
    fn = sum(r['fn'] for r in rows)
    fp = sum(r['known_fp_preserved'] + r['unknown_preds'] for r in rows)
    confirmed = sum(r['confirmed_labels'] for r in rows)
    precision = tp / (tp + fp) if (tp + fp) else float('nan')
    recall = tp / (tp + fn) if (tp + fn) else float('nan')
    f1 = (2 * precision * recall / (precision + recall)
          if (precision + recall and np.isfinite(precision) and np.isfinite(recall))
          else float('nan'))
    return {
        'tp': tp, 'fp': fp, 'fn': fn,
        'precision': precision, 'recall': recall, 'f1': f1,
        'confirmed_labels': confirmed,
    }


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def evaluate(
    preds: pd.DataFrame,
    gt: pd.DataFrame,
    tol_s: int = MATCH_TOLERANCE_S,
) -> dict:
    """Return per-day and overall metrics.

    Splitting is by ``date`` (YYYY-MM-DD). Predictions and GT are joined on
    that key so venue geometry doesn't leak across days.
    """
    days = sorted(set(preds['date']).union(set(gt['date'])))
    per_day = []
    for day in days:
        p = preds[preds['date'] == day]
        g = gt[gt['date'] == day]
        row = {'date': day or '(unknown)'}
        row.update(_metrics_for_day(p, g, tol_s))
        per_day.append(row)
    overall = _aggregate_pr(per_day)
    return {'per_day': per_day, 'overall': overall}


def report_markdown(result: dict, header: Optional[str] = None) -> str:
    lines = []
    if header:
        lines.append(f"## {header}")
        lines.append("")
    ov = result['overall']
    has_confirms = ov['confirmed_labels'] > 0
    if not has_confirms:
        lines.append("_Precision-only mode: 0 `confirmed` labels available. "
                     "Reporting prediction counts and known-FP behavior only._")
    else:
        lines.append(f"**Overall:** precision {ov['precision']:.3f} · "
                     f"recall {ov['recall']:.3f} · F1 {ov['f1']:.3f}  "
                     f"(TP {ov['tp']}, FP {ov['fp']}, FN {ov['fn']}, "
                     f"confirms {ov['confirmed_labels']})")
    lines.append("")
    lines.append("| Date | Preds | Conf | Disc | TP | FN | Known-FP kept | Known-FP supp | New preds |")
    lines.append("| :--- | ---:  | ---: | ---: | ---:| ---:| ---:          | ---:          | ---:      |")
    for r in result['per_day']:
        lines.append(f"| {r['date']} | {r['preds']} | {r['confirmed_labels']} | "
                     f"{r['discarded_labels']} | {r['tp']} | {r['fn']} | "
                     f"{r['known_fp_preserved']} | {r['known_fp_suppressed']} | "
                     f"{r['unknown_preds']} |")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sweep
# ---------------------------------------------------------------------------

def sweep(
    preds: pd.DataFrame,
    gt: pd.DataFrame,
    weight_grid: dict,
    threshold: float = DEFAULT_THRESHOLD,
    nms_window_s: int = NMS_WINDOW_S,
    tol_s: int = MATCH_TOLERANCE_S,
) -> pd.DataFrame:
    """Grid-sweep composite weights. Returns one row per config."""
    keys = list(weight_grid.keys())
    rows = []
    for values in itertools.product(*[weight_grid[k] for k in keys]):
        weights = dict(zip(keys, values))
        preds = preds.copy()
        preds['score_swept'] = rescore(preds, weights)
        kept = apply_threshold_and_nms(preds, 'score_swept', threshold, nms_window_s)
        res = evaluate(kept, gt, tol_s=tol_s)
        ov = res['overall']
        row = {**weights,
               'preds': len(kept),
               'tp': ov['tp'], 'fp': ov['fp'], 'fn': ov['fn'],
               'precision': ov['precision'], 'recall': ov['recall'], 'f1': ov['f1']}
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--events', default=os.path.join(REPO_ROOT, 'Analyze/events_index.json'))
    parser.add_argument('--events-dir', default=os.path.join(REPO_ROOT, 'Analyze/Events'))
    parser.add_argument('--edits', default=os.path.join(REPO_ROOT, 'Analyze/events_edits.json'))
    parser.add_argument('--tol', type=int, default=MATCH_TOLERANCE_S,
                        help='event-match tolerance in seconds (default 15)')
    parser.add_argument('--threshold', type=float, default=DEFAULT_THRESHOLD,
                        help='score_v2 threshold below which predictions are dropped')
    parser.add_argument('--sweep', action='store_true',
                        help='sweep composite weights (coarse ±50%% grid)')
    parser.add_argument('--out', default=None, help='write markdown report to this path')
    args = parser.parse_args()

    gt = load_ground_truth(args.edits, args.events)
    preds = load_predictions(args.events, args.events_dir)
    preds = apply_threshold_and_nms(preds, 'score_v2', args.threshold, NMS_WINDOW_S)

    baseline = evaluate(preds, gt, tol_s=args.tol)
    out = report_markdown(baseline, header=f"Baseline (threshold={args.threshold})")

    if args.sweep:
        # Coarse ±50% grid on the three highest-weight terms; each row is a
        # full evaluation so keep the grid small.
        grid = {
            'upwind':    [0.20, 0.30, 0.45],
            'near_line': [0.10, 0.20, 0.30],
            'cross_dir': [0.08, 0.15, 0.22],
        }
        sw = sweep(preds, gt, grid, threshold=args.threshold, nms_window_s=NMS_WINDOW_S, tol_s=args.tol)
        out += "\n## Sweep (best precision first; F1 used only when confirms exist)\n\n"
        sort_key = 'f1' if not sw['f1'].isna().all() else 'precision'
        sw = sw.sort_values(sort_key, ascending=False, na_position='last').head(10)
        out += sw.round(3).to_markdown(index=False) + "\n"

    print(out)
    if args.out:
        with open(args.out, 'w') as f:
            f.write(out)
        print(f"\nWrote {args.out}")


if __name__ == '__main__':
    main()
