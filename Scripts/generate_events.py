#!/usr/bin/env python3
"""
Generate playlists.json from video .log files using pluggable event detectors.

Reads the binary .log files produced by the video sync pipeline, runs each
registered EventDetector, groups events into auto-playlists by date and type,
and writes playlists.json to the web directory.

Usage:
    python generate_events.py                    # generate only
    python generate_events.py --upload           # generate and upload to S3
    python generate_events.py --web-dir /path    # override web directory
"""

import argparse
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from Libraries.event_detectors import HeadingManeuverDetector
import Libraries.sailview_log_v1 as sailview_log_v1


# After the WEB_DIR collapse, .log / .slog / .poses live alongside .mp4
# in VIDEO_DIR. The argparse flag still calls this --web-dir for backwards
# compat, but the default now points at VIDEO_DIR.
WEB_DIR = "/Users/viola/Data/Insta360/VID"
AWS_PROFILE = os.environ.get("AWS_PROFILE", "regatta")
S3_PREFIX = "s3://regattaanalysis/www/Insta360/"


def read_binary_log(log_path):
    """Decode a binary .log file into a dict of column_name -> numpy array.

    Format (matching sailview_log_v1.save_log_to_binary):
        Header:  4 x int32  (rows, cols, msec_per_sample, multiplier)
        MaxVals: cols x float64
        MinVals: cols x float64
        Data:    column-major, each column is rows x uint16
    """
    with open(log_path, 'rb') as f:
        raw = f.read()

    offset = 0
    header = np.frombuffer(raw, dtype=np.int32, count=4, offset=offset)
    rows, cols, msec_per_sample, multiplier = header
    offset += 4 * 4

    max_values = np.frombuffer(raw, dtype=np.float64, count=cols, offset=offset)
    offset += 8 * cols

    min_values = np.frombuffer(raw, dtype=np.float64, count=cols, offset=offset)
    offset += 8 * cols

    columns = sailview_log_v1.COLUMN_LIST
    data = {}
    col_bytes = 2 * rows
    for i in range(cols):
        col_data = np.frombuffer(raw, dtype=np.uint16, count=rows,
                                 offset=offset + col_bytes * i)
        col_name = columns[i] if i < len(columns) else f'col_{i}'
        mn, mx = min_values[i], max_values[i]
        rng = mx - mn
        scale = rng / multiplier if multiplier else 1.0
        scaled = col_data.astype(np.float64) * scale + mn
        data[col_name] = scaled

    return data, msec_per_sample


def extract_date_from_video_name(video_name):
    """Extract a date string like '2026/03/15' from a video basename."""
    parts = video_name.split('_')
    if len(parts) >= 2:
        try:
            dt = datetime.strptime(parts[1], "%Y%m%d")
            return dt.strftime("%Y/%m/%d")
        except ValueError:
            pass
    return "unknown"


def build_auto_playlists(events):
    """Group events by date and type into auto-generated playlists."""
    by_date = defaultdict(list)
    for ev in events:
        by_date[ev['date']].append(ev)

    playlists = []
    all_tacks = []
    all_gybes = []

    for date in sorted(by_date.keys(), reverse=True):
        date_events = by_date[date]
        date_short = datetime.strptime(date, "%Y/%m/%d").strftime("%b %d")

        tacks = [e for e in date_events if e['type'] == 'tack']
        gybes = [e for e in date_events if e['type'] == 'gybe']
        maneuvers = tacks + gybes
        maneuvers.sort(key=lambda e: (e['video'], e['time']))

        all_tacks.extend(tacks)
        all_gybes.extend(gybes)

        date_id = date.replace('/', '-')

        if tacks:
            playlists.append({
                'id': f'{date_id}-tacks',
                'title': f'{date_short} - All Tacks ({len(tacks)})',
                'date': date,
                'source': 'auto',
                'padding': 10,
                'events': [e['id'] for e in tacks],
            })

        if gybes:
            playlists.append({
                'id': f'{date_id}-gybes',
                'title': f'{date_short} - All Gybes ({len(gybes)})',
                'date': date,
                'source': 'auto',
                'padding': 10,
                'events': [e['id'] for e in gybes],
            })

        if len(maneuvers) > 0:
            playlists.append({
                'id': f'{date_id}-maneuvers',
                'title': f'{date_short} - All Maneuvers ({len(maneuvers)})',
                'date': date,
                'source': 'auto',
                'padding': 10,
                'events': [e['id'] for e in maneuvers],
            })

    all_tacks.sort(key=lambda e: (e['date'], e['video'], e['time']))
    all_gybes.sort(key=lambda e: (e['date'], e['video'], e['time']))

    if all_tacks:
        playlists.append({
            'id': 'all-tacks',
            'title': f'All Tacks ({len(all_tacks)})',
            'date': '',
            'source': 'auto',
            'padding': 10,
            'events': [e['id'] for e in all_tacks],
        })

    if all_gybes:
        playlists.append({
            'id': 'all-gybes',
            'title': f'All Gybes ({len(all_gybes)})',
            'date': '',
            'source': 'auto',
            'padding': 10,
            'events': [e['id'] for e in all_gybes],
        })

    return playlists


def generate(web_dir):
    """Run detection across all .log files and return events + playlists."""
    detectors = [
        HeadingManeuverDetector(),
    ]

    columns = sailview_log_v1.column_dict()
    all_events = []

    log_files = sorted(f for f in os.listdir(web_dir)
                       if re.match(r'^VID.*\.log$', f))

    print(f"Found {len(log_files)} log files in {web_dir}")

    for log_file in log_files:
        video_name = os.path.splitext(log_file)[0]
        log_path = os.path.join(web_dir, log_file)
        date = extract_date_from_video_name(video_name)

        try:
            log_data, _ = read_binary_log(log_path)
        except Exception as e:
            print(f"  SKIP {video_name}: {e}")
            continue

        for detector in detectors:
            events = detector.detect(video_name, log_data, columns, date)
            if events:
                print(f"  {video_name}: {len(events)} events from {detector.__class__.__name__}")
                all_events.extend(events)

    playlists = build_auto_playlists(all_events)

    print(f"\nTotal: {len(all_events)} events, {len(playlists)} playlists")
    return all_events, playlists


def main():
    parser = argparse.ArgumentParser(description='Generate playlists.json from video logs')
    parser.add_argument('--web-dir', default=WEB_DIR, help='Directory with .log files')
    parser.add_argument('--upload', action='store_true', help='Upload to S3 after generating')
    parser.add_argument('--output', default=None, help='Output path (default: <web-dir>/playlists.json)')
    args = parser.parse_args()

    events, playlists = generate(args.web_dir)

    output = {
        'version': 1,
        'events': events,
        'playlists': playlists,
    }

    out_path = args.output or os.path.join(args.web_dir, 'playlists.json')
    with open(out_path, 'w') as f:
        json.dump(output, f, indent=2)
    print(f"Wrote {out_path}")

    if args.upload:
        s3_path = S3_PREFIX + 'playlists.json'
        cmd = f'AWS_PROFILE={AWS_PROFILE} s5cmd cp {out_path} {s3_path}'
        print(f"Uploading: {cmd}")
        subprocess.run(cmd, shell=True, check=True)
        print("Upload complete.")


if __name__ == '__main__':
    main()
