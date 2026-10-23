#!/usr/bin/env python
"""Stage 0 instrumentation: offline audit of bearing-frame assumptions.

Walks every parquet under --logs-dir, processes each through the normal
pipeline (``race_logs.read_log_file``), and writes a JSON report summarizing:

- raw-column presence (heading / cog / zg100_variation / zeus_variation / ...)
- zg100_reference histogram (raw N2K HeadingReference code)
- variation coverage per source (``variation``, ``zg100_variation``,
  ``zeus_variation``) and inter-source agreement
- empirical ``cog - hdg`` circular-mean offset (expected ≈ variation, since
  cog is true and hdg is magnetic)
- ``twd - rtwd`` circular-mean offset — primary check for the
  "TWD is true in the processed frame" hypothesis pinned by
  ``Analyze/twd_reference.md``; expectation ≈ 0. A bias resembling
  variation would mean the plan's Stage-1.5 bug fix assumption is wrong.

No code paths are modified. The output (``Analyze/baseline_frame_audit.json``)
is deterministic byte-for-byte on re-runs (smoke test for Stage 0).
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import argparse
import json
from collections import Counter

import numpy as np
import pandas as pd

from Libraries.global_variables import G
from Libraries import race_logs

G.init_peer_gynt()

DEFAULT_LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
DEFAULT_OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                              "baseline_frame_audit.json")

RAW_COLS_OF_INTEREST = (
    'heading', 'cog', 'variation',
    'zg100_heading', 'zg100_variation', 'zg100_reference',
    'zeus_cog', 'zeus_variation',
    'wind_angle', 'wind_speed',
    'true_wind_angle', 'true_wind_speed',
    'true_north_wind_angle', 'true_north_wind_speed',
)


def _wrap_signed_deg(a):
    """Wrap an angle array into (-180, 180] degrees."""
    a = np.asarray(a, dtype=float)
    return ((a + 180.0) % 360.0) - 180.0


def _circular_mean_deg(values):
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return None
    r = np.deg2rad(v)
    return float(np.rad2deg(np.arctan2(np.mean(np.sin(r)), np.mean(np.cos(r)))))


def _summarize_scalar(arr):
    a = np.asarray(arr, dtype=float)
    a = a[np.isfinite(a)]
    if len(a) == 0:
        return {'count': 0}
    return {
        'count': int(len(a)),
        'mean': round(float(np.mean(a)), 3),
        'median': round(float(np.median(a)), 3),
        'std': round(float(np.std(a)), 3),
        'min': round(float(np.min(a)), 3),
        'max': round(float(np.max(a)), 3),
    }


def _round_or_none(x, digits=3):
    return None if x is None or not np.isfinite(x) else round(float(x), digits)


def _raw_column_report(raw):
    present = sorted(c for c in raw.columns if c in RAW_COLS_OF_INTEREST)

    variation_sources = {}
    for src in ('variation', 'zg100_variation', 'zeus_variation'):
        if src in raw.columns:
            variation_sources[src] = _summarize_scalar(raw[src])
        else:
            variation_sources[src] = None

    inter_agreement = {}
    pairs = (('variation', 'zg100_variation'),
             ('variation', 'zeus_variation'),
             ('zg100_variation', 'zeus_variation'))
    for a, b in pairs:
        key = f'{a}_minus_{b}_deg'
        if a in raw.columns and b in raw.columns:
            both = raw[[a, b]].dropna()
            if len(both):
                diff = _wrap_signed_deg(both[a].values - both[b].values)
                inter_agreement[key] = _summarize_scalar(diff)
            else:
                inter_agreement[key] = {'count': 0}
        else:
            inter_agreement[key] = None

    ref_hist = None
    if 'zg100_reference' in raw.columns:
        s = raw['zg100_reference'].dropna()
        if len(s):
            vc = Counter(str(x) for x in s.values)
            ref_hist = {k: int(v) for k, v in sorted(vc.items())}

    return {
        'raw_columns_present': present,
        'variation_sources': variation_sources,
        'variation_inter_agreement_deg': inter_agreement,
        'zg100_reference_histogram': ref_hist,
    }


def _processed_report(df, session_var_mean):
    """Compute circular-mean offsets on the processed df. df may be None."""
    if df is None:
        return {'status': 'SKIPPED_DOCK_ONLY'}

    cols = set(df.columns)
    moving = df['sog'] > 0.8 if 'sog' in cols else pd.Series([True] * len(df))
    mdf = df[moving]

    rep = {
        'status': 'OK',
        'rows_total': int(len(df)),
        'rows_moving': int(len(mdf)),
        'processed_columns_of_interest': sorted(cols.intersection(
            {'hdg', 'cog', 'twd', 'twa', 'rhdg', 'rcog', 'rtwd', 'rtwa',
             'variation', 'boat_twd', 'boat_twa'})),
        'variation_session_mean_deg': _round_or_none(session_var_mean),
    }

    if 'cog' in cols and 'hdg' in cols and len(mdf):
        d = _wrap_signed_deg(mdf['cog'].values - mdf['hdg'].values)
        rep['cog_minus_hdg_circular_mean_deg'] = _round_or_none(
            _circular_mean_deg(d))
        rep['cog_minus_hdg_samples'] = int(np.isfinite(d).sum())

    if 'twd' in cols and 'rtwd' in cols and len(mdf):
        d = _wrap_signed_deg(mdf['twd'].values - mdf['rtwd'].values)
        finite = d[np.isfinite(d)]
        rep['twd_minus_rtwd_circular_mean_deg'] = _round_or_none(
            _circular_mean_deg(d))
        rep['twd_minus_rtwd_samples'] = int(finite.size)
        if finite.size:
            rep['twd_minus_rtwd_p5_deg'] = round(float(np.percentile(finite, 5)), 3)
            rep['twd_minus_rtwd_p95_deg'] = round(float(np.percentile(finite, 95)), 3)

    return rep


def _flags(row):
    """Emit terse text flags that catch 'plan-contradicting' sessions."""
    flags = []
    proc = row.get('processed') or {}
    if proc.get('status') != 'OK':
        return flags

    var_mean = proc.get('variation_session_mean_deg')
    if var_mean is None or var_mean == 0.0:
        flags.append('NO_VARIATION')

    # cog - hdg should be ≈ variation (hdg is magnetic, cog is true).
    cog_hdg = proc.get('cog_minus_hdg_circular_mean_deg')
    if cog_hdg is not None and var_mean:
        if abs(cog_hdg - var_mean) > 5.0:
            flags.append('COG_HDG_OFFSET_DEVIATES_FROM_VARIATION')

    # twd - rtwd should be ≈ 0 (both true). Catches a buggy assumption.
    twd_rtwd = proc.get('twd_minus_rtwd_circular_mean_deg')
    if twd_rtwd is not None:
        if abs(twd_rtwd) > 3.0:
            flags.append('TWD_RTWD_NONZERO_OFFSET')
        if var_mean and abs(abs(twd_rtwd) - abs(var_mean)) < 2.0:
            # Would suggest twd is in a different frame than rtwd (plan wrong).
            flags.append('TWD_RTWD_OFFSET_RESEMBLES_VARIATION')

    return flags


def _audit_one(parquet_path):
    row = {'file': os.path.basename(parquet_path)}
    try:
        raw = pd.read_parquet(parquet_path)
    except Exception as e:
        row['error'] = f'read_parquet: {e}'
        return row

    row['rows_raw'] = int(len(raw))
    row['raw'] = _raw_column_report(raw)

    # Session-mean variation for downstream flags, taken from the raw parquet
    # so we see the value even if ``discard_columns`` drops alt sources later.
    if 'variation' in raw.columns:
        v = raw['variation'].dropna()
        session_var_mean = float(v.mean()) if len(v) else float('nan')
    else:
        session_var_mean = float('nan')

    try:
        df = race_logs.read_log_file(
            os.path.basename(parquet_path),
            skip_dock_only=True,
            discard_columns=False,
            rename_columns=True,
            trim=True,
            process=True,
            path=os.path.dirname(parquet_path),
        )
    except Exception as e:
        row['processed'] = {'status': 'PROCESS_ERROR', 'error': str(e)}
        row['flags'] = ['PROCESS_ERROR']
        return row

    row['processed'] = _processed_report(df, session_var_mean)
    row['flags'] = _flags(row)
    return row


def _aggregate(sessions):
    ok = [r for r in sessions
          if r.get('processed', {}).get('status') == 'OK']

    def _pick(key, source='processed'):
        return [r[source][key] for r in ok
                if r[source].get(key) is not None]

    agg = {
        'session_count_total': len(sessions),
        'session_count_ok': len(ok),
        'variation_session_mean_deg': _summarize_scalar(
            _pick('variation_session_mean_deg')),
        'cog_minus_hdg_circular_mean_deg': _summarize_scalar(
            _pick('cog_minus_hdg_circular_mean_deg')),
        'twd_minus_rtwd_circular_mean_deg': _summarize_scalar(
            _pick('twd_minus_rtwd_circular_mean_deg')),
    }

    flag_counter = Counter()
    for r in sessions:
        for f in r.get('flags', []) or []:
            flag_counter[f] += 1
    agg['flag_histogram'] = {k: int(v) for k, v in sorted(flag_counter.items())}

    return agg


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--logs-dir', default=DEFAULT_LOGS_DIR)
    parser.add_argument('--output', default=DEFAULT_OUTPUT)
    parser.add_argument('--only', default=None,
                        help='Substring filter on parquet filename.')
    args = parser.parse_args()

    files = sorted(f for f in os.listdir(args.logs_dir)
                   if f.endswith('.parquet'))
    if args.only:
        files = [f for f in files if args.only in f]
    print(f"Scanning {len(files)} parquet(s) in {args.logs_dir}", flush=True)

    sessions = []
    for i, fn in enumerate(files, 1):
        print(f"  [{i}/{len(files)}] {fn}", flush=True)
        sessions.append(_audit_one(os.path.join(args.logs_dir, fn)))

    out = {
        'schema_version': 1,
        'logs_dir': args.logs_dir,
        'sessions': sessions,
        'aggregates': _aggregate(sessions),
    }
    with open(args.output, 'w') as fs:
        json.dump(out, fs, indent=2, sort_keys=False)
    print(f"Wrote {args.output}")


if __name__ == '__main__':
    main()
