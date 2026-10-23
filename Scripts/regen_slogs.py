#!/usr/bin/env python
"""Offline driver: regenerate v2 `.slog` files for a given date.

Finds existing `.log` files under /Users/viola/www/Insta360/ that match the
date, reads their stored `row_times` range to identify each video's
parquet slice, and writes a `.slog` next to each `.log` using the Stage-D
v2 writer (`Libraries/sailview_log_v2`).

No deploy: this only creates local files. S3 upload is a separate step.

Usage:
    conda run -n sailing --no-capture-output \
        python Scripts/regen_slogs.py --date 2026-04-19
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from Libraries import race_logs
from Libraries import sailview_log_v1
from Libraries import sailview_log_v2
from Libraries.global_variables import G


INSTA360_DIR = '/Users/viola/www/Insta360'
PANDAS_LOGS_DIR = '/Users/viola/Data/SailingData/PandasLogs'


def find_parquet_for_date(date_str: str) -> str:
    matches = sorted(
        glob.glob(os.path.join(PANDAS_LOGS_DIR, f'{date_str}T*.parquet')))
    if not matches:
        raise FileNotFoundError(
            f'No parquet for {date_str} in {PANDAS_LOGS_DIR}')
    return matches[0]


def find_logs_for_date(date_str: str) -> list[str]:
    ymd = date_str.replace('-', '')
    return sorted(glob.glob(os.path.join(INSTA360_DIR, f'VID_{ymd}_*.log')))


def time_range_from_log(log_path: str) -> tuple[pd.Timestamp, pd.Timestamp]:
    """Decode the stored row_times column (the one whose max > 1e12 ms-epoch)
    and return its start/end bounds as tz-aware UTC Timestamps.

    Some historical `.log` files have `inf` or `nan` in their max/min values
    (bad data that slipped through the v1 writer). Skip those and fall back
    to the next candidate; raise if none are usable.
    """
    parsed = sailview_log_v1.read_log_v1(log_path)
    for i in range(parsed['num_cols']):
        mx = parsed['max_values'][i]
        mn = parsed['min_values'][i]
        if not (np.isfinite(mx) and np.isfinite(mn)):
            continue
        if mx > 1e12:
            return (pd.Timestamp(mn, unit='ms', tz='UTC'),
                    pd.Timestamp(mx, unit='ms', tz='UTC'))
    raise ValueError(f'{log_path}: no usable row_times-like column '
                     '(finite max > 1e12 ms-epoch)')


def _slice_parquet(df: pd.DataFrame, t_start: pd.Timestamp,
                   t_end: pd.Timestamp) -> pd.DataFrame:
    rt = df['row_times']
    # The parquet's row_times is tz-aware (local or UTC); pandas handles the
    # comparison across zones.
    return df[(rt >= t_start) & (rt <= t_end)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True, help='YYYY-MM-DD')
    ap.add_argument('--dry-run', action='store_true',
                    help='Inspect; do not write .slog files')
    ap.add_argument('--parquet', default=None,
                    help='Override parquet path (otherwise discovered by date)')
    ap.add_argument('--insta360-dir', default=INSTA360_DIR,
                    help='Directory containing the VID_*.log files '
                    '(default: /Users/viola/www/Insta360)')
    args = ap.parse_args()

    parquet = args.parquet or find_parquet_for_date(args.date)
    print(f'Parquet: {parquet}')
    df = race_logs.read_log_file(parquet)
    print(f'  loaded {len(df)} rows')

    # Allow the dir to be overridden for tests / out-of-tree use
    ymd = args.date.replace('-', '')
    log_files = sorted(glob.glob(
        os.path.join(args.insta360_dir, f'VID_{ymd}_*.log')))
    if not log_files:
        print(f'No .log files for {args.date} under {INSTA360_DIR}',
              file=sys.stderr)
        sys.exit(2)
    print(f'Found {len(log_files)} video .log files')

    total_log = 0
    total_slog = 0
    skipped = 0
    for log_path in log_files:
        fname = os.path.basename(log_path)
        try:
            t_start, t_end = time_range_from_log(log_path)
        except ValueError as e:
            print(f'  WARN {fname}: {e}')
            skipped += 1
            continue
        sdf = _slice_parquet(df, t_start, t_end)
        if len(sdf) < 2:
            print(f'  WARN {fname}: parquet has no rows in the log window '
                  f'{t_start.isoformat()} .. {t_end.isoformat()}')
            skipped += 1
            continue
        slog_path = log_path[:-4] + '.slog'
        if args.dry_run:
            print(f'  DRY {fname} -> {os.path.basename(slog_path)} '
                  f'({len(sdf)} rows)')
            continue
        try:
            stats = sailview_log_v2.write_slog(
                sdf, slog_path, proj4=getattr(G, 'PROJ4', ''))
        except Exception as e:
            print(f'  FAIL {fname}: {e}')
            skipped += 1
            continue
        log_size = os.path.getsize(log_path)
        slog_size = stats['file_size']
        total_log += log_size
        total_slog += slog_size
        fb = ','.join(stats['fallback_columns']) or '-'
        print(f'  {fname[:-4]}: '
              f'rows={len(sdf):>6d}  '
              f'.log={log_size:>7d}B  '
              f'.slog={slog_size:>7d}B  '
              f'ratio={slog_size / log_size * 100:5.1f}%  '
              f'fallback={fb}')

    if total_log:
        print(f'\nTotals over {len(log_files) - skipped} clip(s): '
              f'.log={total_log / 1024:.1f} KB, '
              f'.slog={total_slog / 1024:.1f} KB, '
              f'ratio={total_slog / total_log * 100:.1f}% '
              f'(saved {(total_log - total_slog) / 1024:.1f} KB uncompressed)')
    if skipped:
        print(f'{skipped} clip(s) skipped')


if __name__ == '__main__':
    main()
