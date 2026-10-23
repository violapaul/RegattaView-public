#!/usr/bin/env python
"""
Per-start AIS slicer.

Consumes the per-date AIS extracts produced by Scripts/extract_ais.py together with
Analyze/events_index.json, and writes a small per-start JSON file under Analyze/AIS/
that the review UI lazy-loads when a user selects a start event.

Each per-start file contains only the boats that were within PROXIMITY_M of ego at
any point during the start window, with tracks trimmed to the window. This keeps the
eager events index compact.

Usage:
    python slice_ais_starts.py --days 30
    python slice_ais_starts.py --start-id 2026-04-19T18_47 --force
"""
import os
import sys
import json
import math
import argparse
from datetime import datetime, timedelta, timezone
from collections import defaultdict

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Libraries.global_variables import G  # noqa: F401  (imported for side effects / consistency)

AIS_LOGS_DIR = "/Users/viola/Data/SailingData/AISLogs"
PANDAS_LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
ANALYZE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR_DEFAULT = os.path.join(ANALYZE_DIR, "AIS")

# These mirror the review.js scrubber range (START_WINDOW_PRE_SEC / POST_SEC).
WINDOW_PRE_S = 300
WINDOW_POST_S = 600

# Boats whose closest approach to ego in the window exceeds this distance are
# dropped so the map doesn't drown in irrelevant traffic.
PROXIMITY_M = 500.0


def ll_to_meters(lat, lon, lat0, lon0):
    """Flat-earth projection in meters around (lat0, lon0). Good to ~cm over <1 km."""
    R = 6371000.0
    dlat = math.radians(lat - lat0)
    dlon = math.radians(lon - lon0)
    x = dlon * R * math.cos(math.radians(lat0))
    y = dlat * R
    return x, y


def load_ego_window(parquet_path, t0, t1):
    df = pd.read_parquet(parquet_path, columns=['unixtime', 'latitude', 'longitude'])
    df = df.dropna(subset=['unixtime', 'latitude', 'longitude'])
    m = (df['unixtime'] >= t0) & (df['unixtime'] <= t1)
    return df.loc[m].reset_index(drop=True)


def ego_interp(ego_ut, ego_lat, ego_lon, t):
    if t < ego_ut[0] or t > ego_ut[-1]:
        return None
    i = int(np.searchsorted(ego_ut, t))
    if i <= 0:
        return float(ego_lat[0]), float(ego_lon[0])
    if i >= len(ego_ut):
        return float(ego_lat[-1]), float(ego_lon[-1])
    t0, t1 = ego_ut[i - 1], ego_ut[i]
    if t1 == t0:
        return float(ego_lat[i - 1]), float(ego_lon[i - 1])
    f = (t - t0) / (t1 - t0)
    return (
        float(ego_lat[i - 1] * (1 - f) + ego_lat[i] * f),
        float(ego_lon[i - 1] * (1 - f) + ego_lon[i] * f),
    )


def load_start_events(events_path):
    """Load current event-index entries that represent race starts."""
    with open(events_path) as f:
        events_blob = json.load(f)
    events = events_blob.get('events', [])
    return [e for e in events if e.get('kind') == 'start']


def event_key_time(event):
    """Return the event's canonical timestamp, with old-field fallbacks."""
    if event.get('t_key_unix') is not None:
        return float(event['t_key_unix'])
    if event.get('start_time_unix') is not None:
        return float(event['start_time_unix'])
    if event.get('t_start_unix') is not None:
        return float(event['t_start_unix']) + WINDOW_PRE_S
    raise KeyError(f"event {event.get('id', '<unknown>')} has no key timestamp")


def slice_ais_for_start(start, ais_date_blob, parquet_path):
    gun = event_key_time(start)
    t0 = gun - WINDOW_PRE_S
    t1 = gun + WINDOW_POST_S

    if not os.path.exists(parquet_path):
        return None, f"parquet missing: {parquet_path}"
    ego = load_ego_window(parquet_path, t0 - 60, t1 + 60)
    if ego.empty:
        return None, "no ego samples in window"

    ego_ut = ego['unixtime'].to_numpy(dtype=np.float64)
    ego_lat = ego['latitude'].to_numpy(dtype=np.float64)
    ego_lon = ego['longitude'].to_numpy(dtype=np.float64)
    lat0 = float(ego_lat.mean())
    lon0 = float(ego_lon.mean())

    out_boats = []
    for mmsi, boat in (ais_date_blob.get('boats') or {}).items():
        track = boat.get('track') or []
        win = [p for p in track if t0 <= p['ts'] <= t1]
        if not win:
            continue

        min_d = float('inf')
        for p in win:
            ep = ego_interp(ego_ut, ego_lat, ego_lon, p['ts'])
            if ep is None:
                continue
            ex, ey = ll_to_meters(ep[0], ep[1], lat0, lon0)
            bx, by = ll_to_meters(p['lat'], p['lon'], lat0, lon0)
            d = math.hypot(bx - ex, by - ey)
            if d < min_d:
                min_d = d
        if min_d == float('inf') or min_d > PROXIMITY_M:
            continue

        out_boats.append({
            'mmsi': boat.get('mmsi') or mmsi,
            'name': boat.get('name') or '',
            'callsign': boat.get('callsign') or '',
            'ship_type': boat.get('ship_type'),
            'min_dist_m': round(min_d, 1),
            'track': [[round(p['lat'], 6), round(p['lon'], 6), round(p['ts'], 1)] for p in win],
        })

    out_boats.sort(key=lambda b: b['min_dist_m'])
    return {
        'event_id': start['id'],
        'start_id': start['id'],
        'start_time_unix': gun,
        'window_pre_s': WINDOW_PRE_S,
        'window_post_s': WINDOW_POST_S,
        'boats': out_boats,
    }, None


def main():
    parser = argparse.ArgumentParser(description='Per-start AIS slicer')
    parser.add_argument('--events', default=os.path.join(ANALYZE_DIR, 'events_index.json'),
                        help='Path to current events_index.json')
    parser.add_argument('--ais-dir', default=AIS_LOGS_DIR)
    parser.add_argument('--logs-dir', default=PANDAS_LOGS_DIR)
    parser.add_argument('--out-dir', default=OUT_DIR_DEFAULT)
    parser.add_argument('--days', type=int, default=None, help='Only process starts from the last N days')
    parser.add_argument('--since', default=None, help='Only process starts on dates >= YYYY-MM-DD')
    parser.add_argument('--start-id', default=None, help='Only process this specific start_id')
    parser.add_argument('--force', action='store_true', help='Regenerate per-start files even if present')
    args = parser.parse_args()

    since = args.since
    if args.days and not since:
        since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime('%Y-%m-%d')

    starts = load_start_events(args.events)

    if args.start_id:
        starts = [s for s in starts if s.get('id') == args.start_id]
    elif since:
        starts = [s for s in starts if s.get('date', '') >= since]

    os.makedirs(args.out_dir, exist_ok=True)

    by_date = defaultdict(list)
    for s in starts:
        by_date[s['date']].append(s)

    written = 0
    skipped = 0
    missing_ais = 0
    errors = 0
    for date_str in sorted(by_date.keys()):
        ais_path = os.path.join(args.ais_dir, f"{date_str}_ais.json")
        if not os.path.exists(ais_path):
            missing_ais += len(by_date[date_str])
            continue
        with open(ais_path) as f:
            ais_blob = json.load(f)
        for s in by_date[date_str]:
            out_path = os.path.join(args.out_dir, f"{s['id']}.json")
            if os.path.exists(out_path) and not args.force:
                skipped += 1
                continue
            parquet_path = os.path.join(args.logs_dir, s['file'])
            result, err = slice_ais_for_start(s, ais_blob, parquet_path)
            if result is None:
                print(f"  {s['id']}: {err}")
                errors += 1
                continue
            with open(out_path, 'w') as f:
                json.dump(result, f)
            print(f"  {s['id']}: {len(result['boats'])} boats")
            written += 1

    print(
        f"\nWrote {written} per-start AIS files, "
        f"skipped {skipped} existing, "
        f"missing AIS extract for {missing_ais} starts, "
        f"errors on {errors}"
    )


if __name__ == '__main__':
    main()
