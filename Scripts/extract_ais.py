#!/usr/bin/env /Users/viola/miniforge3/envs/sailing/bin/python
"""
Extract AIS data from BoatBrain log files.

This script reads raw BB log files, extracts AIS messages (PGNs 129039, 129809, 129810),
and outputs a JSON file with per-boat tracks grouped by MMSI.

AIS data is stored separately from the main parquet pipeline because:
- AIS has multiple boats (can't flatten to single-row-per-timestep)
- AIS updates at ~0.03Hz per boat vs instruments at 10Hz
- Different data structure needed (keyed by MMSI)

Implementation note: canboat v5.0.3 `analyzer -json` drops PGN 129039 records with
"field Reserved print routine did not print anything" error. To work around this we
decode 129039 payloads directly from the actisense-serial CSV stream and only rely on
the analyzer for static data (129809/129810 — name, callsign, ship type).

Frame timestamps for 129039 are reconstructed from PGN 126992 (System Time) anchors in
the same stream, because the actisense-serial CSV column-0 timestamp is wall-clock at
read time, not capture time.

Usage:
    python extract_ais.py --date 2026-04-11
    python extract_ais.py --date 2026-04-11 --output /path/to/output.json
"""

import os
import sys
import json
import struct
import argparse
import subprocess
from datetime import datetime
from collections import defaultdict

import numpy as np
import ujson

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Libraries.log_helper import LOG
from Libraries.global_variables import G

# AIS PGN definitions
AIS_POSITION_PGN = 129039  # AIS Class B Position Report
AIS_STATIC_A_PGN = 129809  # AIS Class B static data (msg 24 Part A)
AIS_STATIC_B_PGN = 129810  # AIS Class B static data (msg 24 Part B)
SYSTEM_TIME_PGN = 126992   # used as in-stream time anchor

STATIC_AIS_PGNS = {AIS_STATIC_A_PGN, AIS_STATIC_B_PGN}

BBLOG_DIR = "/Users/viola/Data/BBLog/"
AIS_OUTPUT_DIR = "/Users/viola/Data/SailingData/AISLogs/"

# Our own vessel's Class-B MMSI. Suppressed from output so the ego boat doesn't
# show up as a "nearby competitor". Override via --ego-mmsi if the transponder
# ever changes.
DEFAULT_EGO_MMSI = "338204092"


def find_logs_for_date(date_str, log_dir=BBLOG_DIR):
    """List BB log files whose filename starts with date_str (YYYY-MM-DD)."""
    files = sorted(os.listdir(log_dir))
    return [
        os.path.join(log_dir, f) for f in files
        if f.startswith(date_str) and f.endswith("Z.log")
    ]


def parse_csv_line(line):
    """Parse an actisense-serial CSV line. Returns (pgn, src, payload_bytes) or None."""
    parts = line.rstrip("\n").split(",")
    if len(parts) < 7:
        return None
    try:
        pgn = int(parts[2])
        src = int(parts[3])
        length = int(parts[5])
    except ValueError:
        return None
    data = parts[6:6 + length]
    if len(data) != length:
        return None
    try:
        payload = bytes(int(b, 16) for b in data)
    except ValueError:
        return None
    return pgn, src, payload


def decode_system_time(payload):
    """Return unix timestamp from a PGN 126992 payload, or None."""
    if len(payload) < 8:
        return None
    days = struct.unpack_from("<H", payload, 2)[0]
    secs_1e4 = struct.unpack_from("<I", payload, 4)[0]
    if days == 0xFFFF or secs_1e4 == 0xFFFFFFFF:
        return None
    # Sanity bracket: early 2020s through mid-2030s.
    if not (18000 < days < 25000):
        return None
    return days * 86400.0 + secs_1e4 * 1e-4


def decode_ais_position(payload):
    """Return dict(mmsi, lat, lon, cog, sog) or None for AIS Class B position (PGN 129039)."""
    if len(payload) < 18:
        return None
    msg_id = payload[0] & 0x3F
    if msg_id != 18:  # Class B position report
        return None
    mmsi = struct.unpack_from("<I", payload, 1)[0]
    lon_i = struct.unpack_from("<i", payload, 5)[0]
    lat_i = struct.unpack_from("<i", payload, 9)[0]
    cog_u = struct.unpack_from("<H", payload, 14)[0]
    sog_u = struct.unpack_from("<H", payload, 16)[0]
    if lon_i == 0x7FFFFFFF or lat_i == 0x7FFFFFFF:
        return None
    lat = lat_i * 1e-7
    lon = lon_i * 1e-7
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return None
    cog = (cog_u * 1e-4 * 180.0 / np.pi) if cog_u != 0xFFFF else None  # degrees
    sog = (sog_u * 1e-2) if sog_u != 0xFFFF else None                  # m/s
    return {
        'mmsi': str(mmsi),
        'lat': lat,
        'lon': lon,
        'cog': cog,
        'sog': sog,
    }


def interpolate_frame_times(anchors, frames):
    """Interpolate unix timestamps for each frame by CSV line index.

    anchors: list of (line_idx, unixtime)
    frames:  list of (line_idx, decoded_dict)
    Returns: list of dicts with 'ts' field added.
    """
    if not anchors or not frames:
        return []
    anchor_idx = np.array([a[0] for a in anchors], dtype=np.int64)
    anchor_ut = np.array([a[1] for a in anchors], dtype=np.float64)
    order = np.argsort(anchor_idx)
    anchor_idx = anchor_idx[order]
    anchor_ut = anchor_ut[order]

    out = []
    for idx, d in frames:
        pos = np.searchsorted(anchor_idx, idx)
        if pos == 0:
            ut = float(anchor_ut[0])
        elif pos >= len(anchor_idx):
            ut = float(anchor_ut[-1])
        else:
            i0, i1 = anchor_idx[pos - 1], anchor_idx[pos]
            t0, t1 = anchor_ut[pos - 1], anchor_ut[pos]
            if i1 == i0:
                ut = float(t0)
            else:
                ut = float(t0 + (t1 - t0) * (idx - i0) / (i1 - i0))
        rec = dict(d)
        rec['ts'] = ut
        out.append(rec)
    return out


def decode_positions_from_csv(csv_lines):
    """Walk the actisense-serial CSV stream, decoding 126992 anchors and 129039 positions.

    Returns: list of position records with 'ts' (unix seconds).
    """
    anchors = []
    frames = []
    for idx, line in enumerate(csv_lines):
        parsed = parse_csv_line(line)
        if parsed is None:
            continue
        pgn, _src, payload = parsed
        if pgn == SYSTEM_TIME_PGN:
            ut = decode_system_time(payload)
            if ut is not None:
                anchors.append((idx, ut))
        elif pgn == AIS_POSITION_PGN:
            d = decode_ais_position(payload)
            if d is not None:
                frames.append((idx, d))
    return interpolate_frame_times(anchors, frames)


def decode_static_via_analyzer(csv_bytes):
    """Pipe the CSV stream through `analyzer -json` and extract 129809/129810 static data.

    We still use canboat here because the static PGNs are not affected by the 129039
    analyzer bug and their payloads carry variable-length strings that are a pain to
    decode by hand.

    Returns: dict keyed by MMSI with partial metadata (name/callsign/ship_type).
    """
    proc = subprocess.run(
        ["analyzer", "-json"],
        input=csv_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    out = {}
    for line in proc.stdout.splitlines():
        try:
            rec = ujson.loads(line)
        except Exception:
            continue
        pgn = rec.get('pgn')
        if pgn not in STATIC_AIS_PGNS:
            continue
        fields = rec.get('fields') or {}
        mmsi = str(fields.get('User ID') or '').strip()
        if not mmsi:
            continue
        entry = out.setdefault(mmsi, {})
        if pgn == AIS_STATIC_A_PGN:
            name = (fields.get('Name') or '').strip()
            if name:
                entry['name'] = name
        else:  # 129810
            callsign = (fields.get('Callsign') or '').strip()
            if callsign:
                entry['callsign'] = callsign
            ship_type = fields.get('Type of ship')
            if ship_type is not None:
                entry['ship_type'] = ship_type
    return out


def process_log(log_path, boats_data, ego_mmsi):
    """Single-pass extraction from a BB log. Merges results into boats_data.

    boats_data: dict keyed by MMSI → {mmsi, name, callsign, ship_type, track: [...]}
    """
    LOG.logger.info(f"Processing: {os.path.basename(log_path)}")
    # -r tells actisense-serial to read until EOF. Without it a short default timeout
    # can fire mid-file and silently truncate the output (cf. Libraries/bb_log.py:136).
    proc = subprocess.run(
        ["actisense-serial", "-r", log_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    csv_bytes = proc.stdout
    if not csv_bytes:
        LOG.logger.warning(f"  actisense-serial returned no data for {log_path}")
        return

    csv_lines = csv_bytes.decode('utf-8', errors='replace').splitlines()
    positions = decode_positions_from_csv(csv_lines)
    static = decode_static_via_analyzer(csv_bytes)

    pos_count = 0
    for p in positions:
        mmsi = p['mmsi']
        if mmsi == ego_mmsi:
            continue
        boat = boats_data.setdefault(mmsi, {
            'mmsi': mmsi, 'name': '', 'callsign': '', 'ship_type': None, 'track': []
        })
        boat['track'].append({
            'ts': p['ts'],
            'lat': p['lat'],
            'lon': p['lon'],
            'cog': p['cog'],
            'sog': p['sog'],
        })
        pos_count += 1

    static_count = 0
    for mmsi, info in static.items():
        if mmsi == ego_mmsi:
            continue
        boat = boats_data.setdefault(mmsi, {
            'mmsi': mmsi, 'name': '', 'callsign': '', 'ship_type': None, 'track': []
        })
        if info.get('name'):
            boat['name'] = info['name']
        if info.get('callsign'):
            boat['callsign'] = info['callsign']
        if info.get('ship_type') is not None:
            boat['ship_type'] = info['ship_type']
        static_count += 1

    LOG.logger.info(f"  Found {pos_count} positions, {static_count} static records")


def _source_log_signature(log_paths):
    """Map raw-log basename → mtime; lets callers detect upstream churn."""
    return {os.path.basename(p): os.path.getmtime(p) for p in log_paths}


def extract_ais_for_date(date_str, output_path=None, ego_mmsi=DEFAULT_EGO_MMSI):
    """Extract all AIS data for a given date."""
    log_files = find_logs_for_date(date_str)
    if not log_files:
        LOG.logger.error(f"No log files found for {date_str}")
        return None

    LOG.logger.info(f"Found {len(log_files)} log files for {date_str}")

    boats_data = {}
    for log_path in log_files:
        process_log(log_path, boats_data, ego_mmsi)

    source_signature = _source_log_signature(log_files)

    if not boats_data:
        LOG.logger.warning(f"No AIS data found for {date_str}")
        # Write a sentinel JSON so the date isn't reprocessed every run by
        # callers that gate on os.path.exists. Empty `boats: {}` is a
        # normal data-driven outcome, not a script failure. Delete the
        # file to force a re-extract.
        if output_path is None:
            os.makedirs(AIS_OUTPUT_DIR, exist_ok=True)
            output_path = os.path.join(AIS_OUTPUT_DIR, f"{date_str}_ais.json")
        sentinel = {
            'race_date': date_str,
            'extracted_at': datetime.now().isoformat(),
            'ego_mmsi_suppressed': ego_mmsi,
            'summary': {'total_boats': 0, 'total_positions': 0, 'boats_with_names': 0},
            # Records the BB-log fileset at extract time so a caller can
            # invalidate this sentinel when the upstream logs change.
            'source_logs': source_signature,
            'boats': {},
        }
        with open(output_path, 'w') as f:
            json.dump(sentinel, f, indent=2)
        LOG.logger.info(f"Wrote empty-AIS sentinel to: {output_path}")
        # Empty-string return preserves the "no AIS pings" signal for any
        # caller that distinguishes it from "no log files" (None).
        return ""

    # Sort each boat's track by timestamp
    for mmsi in boats_data:
        boats_data[mmsi]['track'].sort(key=lambda x: x['ts'])

    total_boats = len(boats_data)
    total_positions = sum(len(b['track']) for b in boats_data.values())
    boats_with_names = sum(1 for b in boats_data.values() if b['name'])
    LOG.logger.info(
        f"Summary: {total_boats} boats, {total_positions} positions, {boats_with_names} with names"
    )

    output = {
        'race_date': date_str,
        'extracted_at': datetime.now().isoformat(),
        'ego_mmsi_suppressed': ego_mmsi,
        'summary': {
            'total_boats': total_boats,
            'total_positions': total_positions,
            'boats_with_names': boats_with_names,
        },
        # Records the BB-log fileset at extract time so callers can
        # invalidate this output when upstream logs change.
        'source_logs': source_signature,
        'boats': boats_data,
    }

    if output_path is None:
        os.makedirs(AIS_OUTPUT_DIR, exist_ok=True)
        output_path = os.path.join(AIS_OUTPUT_DIR, f"{date_str}_ais.json")

    with open(output_path, 'w') as f:
        json.dump(output, f, indent=2)

    LOG.logger.info(f"Wrote AIS data to: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(
        description="Extract AIS data from BoatBrain log files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--date", "-d", required=True, help="Date to extract (YYYY-MM-DD format)")
    parser.add_argument("--output", "-o", help="Output JSON file path (default: AISLogs/DATE_ais.json)")
    parser.add_argument("--ego-mmsi", default=DEFAULT_EGO_MMSI,
                        help=f"Suppress this MMSI (our boat's transponder). Default: {DEFAULT_EGO_MMSI}")
    parser.add_argument("--log", default="info", help="Logging level (debug, info, warning, error)")

    args = parser.parse_args()
    LOG.set_logging_level(args.log)

    try:
        datetime.strptime(args.date, "%Y-%m-%d")
    except ValueError:
        LOG.logger.error(f"Invalid date format: {args.date}. Use YYYY-MM-DD")
        sys.exit(1)

    result = extract_ais_for_date(args.date, args.output, ego_mmsi=args.ego_mmsi)
    if result is None:
        # Real error: the date had no log files at all.
        print(f"\nNo log files found for {args.date}")
        sys.exit(1)
    if result == "":
        # Logs exist but had no AIS pings — exit 0 so the race_upload pipeline
        # doesn't flag this as an error every run.
        print(f"\nNo AIS data found for {args.date}")
        return
    print(f"\nAIS data extracted to: {result}")


if __name__ == "__main__":
    main()
