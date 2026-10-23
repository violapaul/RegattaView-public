#!/usr/bin/env python
"""
Build a per-day AIS viewer payload.

Consumes the per-date AIS extracts produced by Scripts/extract_ais.py and the
matching ego-boat parquet logs, then writes a compact JSON file under
Analyze/AISDays/ for the web UI.

Unlike the per-start slicer, inclusion is based on closest approach over the
whole logged day. Once a boat qualifies, its whole in-window AIS track is kept
so the viewer can scrub through the rest of the race even when boats spread out.

Usage:
    conda run -n sailing python Analyze/slice_ais_day.py --date 2026-05-09
"""
import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from Libraries.global_variables import G  # noqa: F401  (imported for side effects / consistency)
from Libraries import race_logs, sailview_log_v1

AIS_LOGS_DIR = "/Users/viola/Data/SailingData/AISLogs"
PANDAS_LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
ANALYZE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR_DEFAULT = os.path.join(ANALYZE_DIR, "AISDays")
FLEETS_CONFIG_DEFAULT = os.path.join(ANALYZE_DIR, "ais_fleets.yaml")

PROXIMITY_M = 600.0
EGO_DECIMATE_S = 5.0
DEFAULT_EGO_MMSI = "338204092"
# Sample rate for the per-day .log served to tracks.html. 1 Hz keeps the file
# small (~120 KB raw for an 8 h day) while still giving smooth sparklines in
# the default 120 s window. The parquet is ~10 Hz native, so we just iloc-step.
INSTRUMENT_HZ_DEFAULT = 1.0
PARQUET_NATIVE_HZ = 10.0


def normalized_name(name):
    """Normalize vessel names for exact alias matching."""
    return "".join(ch for ch in (name or "").lower() if ch.isalnum())


def load_fleet_config(path=FLEETS_CONFIG_DEFAULT):
    """Load fleet matching config. Missing config is allowed."""
    if not path or not os.path.exists(path):
        return {"ego": {"name": "Our boat", "mmsi": DEFAULT_EGO_MMSI}, "fleets": []}
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    data.setdefault("ego", {"name": "Our boat", "mmsi": DEFAULT_EGO_MMSI})
    data.setdefault("fleets", [])
    return data


def build_alias_index(fleet_config):
    """Map normalized AIS vessel aliases to configured fleet boats."""
    alias_index = {}
    for fleet in fleet_config.get("fleets", []):
        fleet_id = fleet.get("id")
        fleet_label = fleet.get("label") or fleet_id
        for boat in fleet.get("boats", []):
            canonical = boat.get("name") or ""
            aliases = set(boat.get("aliases") or [])
            if canonical:
                aliases.add(canonical)
            for alias in aliases:
                key = normalized_name(alias)
                if not key:
                    continue
                alias_index.setdefault(key, []).append({
                    "fleet_id": fleet_id,
                    "fleet_label": fleet_label,
                    "name": canonical,
                    "alias": alias,
                    "sail_numbers": [str(x) for x in (boat.get("sail_numbers") or [])],
                })
    return alias_index


def tag_candidate_boat(boat, alias_index):
    """Attach fleet tags to a candidate boat when its AIS name exactly matches an alias."""
    matches = alias_index.get(normalized_name(boat.get("name")), [])
    if not matches:
        return boat
    # Prefer the first configured match; ambiguous aliases can be split later by
    # adding more specific AIS aliases or MMSI rules to the config.
    match = matches[0]
    tagged = dict(boat)
    tagged["race_name"] = match["name"]
    tagged["sail_numbers"] = match["sail_numbers"]
    tagged["fleet_tags"] = sorted(x for x in {match["fleet_id"], *(tagged.get("fleet_tags") or [])} if x)
    tagged["match_reason"] = f"alias:{match['alias']}"
    tagged["match_confidence"] = "exact_alias"
    return tagged


def utc_dates_for_span(t_min, t_max):
    """Return YYYY-MM-DD UTC dates touched by a unix-time span."""
    start = datetime.fromtimestamp(t_min, tz=timezone.utc).date()
    end = datetime.fromtimestamp(t_max, tz=timezone.utc).date()
    days = (end - start).days
    return [(start + timedelta(days=i)).isoformat() for i in range(days + 1)]


def load_ais_blobs_for_span(date_str, ais_dir, t_min, t_max):
    """Load and merge AIS extracts for every UTC file-date touching the ego span."""
    dates = utc_dates_for_span(t_min, t_max)
    # Keep the requested local date first for diagnostics, even if the UTC span
    # helper changes in the future.
    dates = sorted(set([date_str, *dates]))
    merged = {"boats": {}, "summary": {"total_positions": 0}, "source_files": []}
    missing = []
    for date in dates:
        path = os.path.join(ais_dir, f"{date}_ais.json")
        if not os.path.exists(path):
            missing.append(path)
            continue
        with open(path) as f:
            blob = json.load(f)
        merged["source_files"].append(path)
        merged["summary"]["total_positions"] += (blob.get("summary") or {}).get("total_positions") or 0
        for mmsi, boat in (blob.get("boats") or {}).items():
            entry = merged["boats"].setdefault(mmsi, {
                "mmsi": boat.get("mmsi") or mmsi,
                "name": "",
                "callsign": "",
                "ship_type": None,
                "track": [],
            })
            if boat.get("name"):
                entry["name"] = boat["name"]
            if boat.get("callsign"):
                entry["callsign"] = boat["callsign"]
            if boat.get("ship_type") is not None:
                entry["ship_type"] = boat["ship_type"]
            entry["track"].extend(boat.get("track") or [])
    if not merged["source_files"]:
        raise FileNotFoundError(f"No AIS extracts found for UTC dates {dates}; first missing path: {missing[0] if missing else '<none>'}")
    for boat in merged["boats"].values():
        boat["track"].sort(key=lambda p: p["ts"])
    merged["summary"]["total_boats"] = len(merged["boats"])
    return merged


def find_ego_logs_for_date(date_str, logs_dir=PANDAS_LOGS_DIR):
    """Return ego log files whose basename starts with date_str."""
    if not os.path.isdir(logs_dir):
        return []
    suffixes = (".parquet", ".pd.gz")
    return [
        os.path.join(logs_dir, name)
        for name in sorted(os.listdir(logs_dir))
        if name.startswith(date_str) and name.endswith(suffixes)
    ]


def find_ego_dates(logs_dir=PANDAS_LOGS_DIR, since=None):
    """Return local dates that have processed ego logs."""
    if not os.path.isdir(logs_dir):
        return []
    dates = set()
    for name in os.listdir(logs_dir):
        if not (name.endswith(".parquet") or name.endswith(".pd.gz")):
            continue
        date = name[:10]
        if len(date) == 10 and date[4] == "-" and date[7] == "-":
            if since is None or date >= since:
                dates.add(date)
    return sorted(dates)


def read_ego_log(path):
    """Read only the columns needed for map matching.

    Returns an empty frame on schema mismatch (older `.pd.gz` files predate
    the `unixtime` column) so the caller can skip the day cleanly via the
    existing 'empty ego' ValueError path.
    """
    cols = ["unixtime", "latitude", "longitude"]
    if path.endswith(".parquet"):
        try:
            return pd.read_parquet(path, columns=cols)
        except (ValueError, KeyError):
            return pd.DataFrame(columns=cols)
    try:
        df = pd.read_pickle(path)
    except Exception:
        return pd.DataFrame(columns=cols)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        return pd.DataFrame(columns=cols)
    return df[cols]


def load_ego_tracks(paths):
    """Load, merge, and sort one or more ego logs for a date."""
    frames = [read_ego_log(path) for path in paths]
    if not frames:
        return pd.DataFrame(columns=["unixtime", "latitude", "longitude"])
    df = pd.concat(frames, ignore_index=True)
    df = df.dropna(subset=["unixtime", "latitude", "longitude"])
    df = df[
        df["latitude"].between(-90, 90)
        & df["longitude"].between(-180, 180)
    ]
    df = df.sort_values("unixtime").drop_duplicates(subset=["unixtime"])
    return df.reset_index(drop=True)


def distance_m(lat, lon, ego_lat, ego_lon, lat_ref):
    """Flat-earth vector distance in meters around lat_ref."""
    earth_radius_m = 6371000.0
    cos_ref = math.cos(math.radians(lat_ref))
    dx = np.radians(lon - ego_lon) * earth_radius_m * cos_ref
    dy = np.radians(lat - ego_lat) * earth_radius_m
    return np.hypot(dx, dy)


def normalize_track(track, t_min, t_max):
    """Return sorted AIS points within the ego time span as numpy arrays.

    Returns (ts, lat, lon, sog) — SOG is in m/s as emitted by
    extract_ais.py (PGN 129039 raw units). Missing SOG comes through as NaN
    so callers can decide whether to forward it as a JSON null.
    """
    def _sog(p):
        v = p.get("sog")
        try:
            return float("nan") if v is None else float(v)
        except (TypeError, ValueError):
            return float("nan")

    points = [
        (float(p["ts"]), float(p["lat"]), float(p["lon"]), _sog(p))
        for p in (track or [])
        if t_min <= float(p.get("ts", float("nan"))) <= t_max
           and -90 <= float(p.get("lat", float("nan"))) <= 90
           and -180 <= float(p.get("lon", float("nan"))) <= 180
    ]
    if not points:
        return None
    points.sort(key=lambda p: p[0])
    arr = np.array(points, dtype=np.float64)
    return arr[:, 0], arr[:, 1], arr[:, 2], arr[:, 3]


def compact_track(ts, lat, lon, sog_kt=None):
    """Compact JSON form used by tracks.html.

    Without `sog_kt`: legacy 3-tuples `[lat, lon, ts]` (kept for the ego
    decimated track, whose speed lives in the per-day .log).
    With `sog_kt`: 4-tuples `[lat, lon, ts, sog_kt_or_null]` for AIS boats,
    so the fleet panel can read each boat's broadcast speed. NaN/missing
    SOG comes through as JSON `null`.
    """
    if sog_kt is None:
        return [
            [round(float(la), 6), round(float(lo), 6), round(float(t), 1)]
            for t, la, lo in zip(ts, lat, lon)
        ]
    out = []
    for t, la, lo, s in zip(ts, lat, lon, sog_kt):
        s_out = None if (s is None or (isinstance(s, float) and np.isnan(s))) else round(float(s), 2)
        out.append([round(float(la), 6), round(float(lo), 6), round(float(t), 1), s_out])
    return out


def decimate_ego_track(ego_ut, ego_lat, ego_lon, step_s):
    """Time-decimate the ego track for display while preserving endpoints."""
    if len(ego_ut) == 0:
        return []
    keep = [0]
    last_t = float(ego_ut[0])
    for i in range(1, len(ego_ut) - 1):
        t = float(ego_ut[i])
        if t - last_t >= step_s:
            keep.append(i)
            last_t = t
    if keep[-1] != len(ego_ut) - 1:
        keep.append(len(ego_ut) - 1)
    keep = np.array(keep, dtype=np.int64)
    return compact_track(ego_ut[keep], ego_lat[keep], ego_lon[keep])


def build_day_log(ego_paths, out_log_path, hz=INSTRUMENT_HZ_DEFAULT):
    """Concatenate per-session processed parquet logs into a per-day .log binary.

    Reuses the same pipeline sailview's per-video .log uses:
        race_logs.read_log_file()    -> rename, smooth, true-wind estimation
        sailview_log_v1.df_convert() -> unit conversion, map projection, COLUMN_LIST order
        sailview_log_v1.save_log_to_binary() -> uint16 column-major payload

    Sessions are concatenated end-to-end; gaps between sessions show up as
    non-uniform row_times in the resulting file. The browser side binary-searches
    on row_times rather than using msec_per_sample for indexing, so gaps render
    as 'stale' samples that the UI can mask with INSTR_MAX_GAP_S in tracks.js.

    Returns the row count written, or 0 if no usable sessions.
    """
    frames = []
    # Native parquet rate is ~10 Hz; iloc-step gives a clean downsample.
    step = max(1, int(round(PARQUET_NATIVE_HZ / hz)))
    for path in ego_paths:
        basename = os.path.basename(path)
        directory = os.path.dirname(path)
        # skip_dock_only=False so dockside-only days still produce a .log;
        # tracks.html still needs to show the ego at the marina.
        df = race_logs.read_log_file(
            basename, skip_dock_only=False, path=directory,
        )
        if df is None or len(df) == 0:
            continue
        frames.append(df.iloc[::step].copy())
    if not frames:
        return 0
    day_df = pd.concat(frames, ignore_index=True)
    day_df = day_df.sort_values("row_times").reset_index(drop=True)
    cdf = sailview_log_v1.df_convert(day_df)
    # row_seconds is per-session in the source; rebase so the .log's row_seconds
    # is monotonically increasing from zero across the whole day.
    cdf["row_seconds"] = cdf["row_seconds"] - cdf["row_seconds"].iloc[0]
    sailview_log_v1.save_log_to_binary(
        cdf, out_log_path,
        msec_per_sample=int(round(1000.0 / hz)),
    )
    return len(cdf)


def slice_ais_day(date_str, ais_dir=AIS_LOGS_DIR, logs_dir=PANDAS_LOGS_DIR,
                  out_dir=OUT_DIR_DEFAULT, proximity_m=PROXIMITY_M,
                  ego_decimate_s=EGO_DECIMATE_S,
                  fleet_config_path=FLEETS_CONFIG_DEFAULT,
                  instruments_hz=INSTRUMENT_HZ_DEFAULT):
    ego_paths = find_ego_logs_for_date(date_str, logs_dir)
    if not ego_paths:
        raise FileNotFoundError(f"No ego logs found for {date_str} in {logs_dir}")

    ego = load_ego_tracks(ego_paths)
    if ego.empty:
        raise ValueError(f"No usable ego samples for {date_str}")

    ego_ut = ego["unixtime"].to_numpy(dtype=np.float64)
    ego_lat = ego["latitude"].to_numpy(dtype=np.float64)
    ego_lon = ego["longitude"].to_numpy(dtype=np.float64)
    t_min = float(ego_ut[0])
    t_max = float(ego_ut[-1])
    lat_ref = float(np.nanmean(ego_lat))

    ais_blob = load_ais_blobs_for_span(date_str, ais_dir, t_min, t_max)

    fleet_config = load_fleet_config(fleet_config_path)
    alias_index = build_alias_index(fleet_config)

    candidate_boats = []
    skipped_no_overlap = 0
    for mmsi, boat in (ais_blob.get("boats") or {}).items():
        normalized = normalize_track(boat.get("track"), t_min, t_max)
        if normalized is None:
            skipped_no_overlap += 1
            continue
        ts, lat, lon, sog_mps = normalized
        ego_lat_at_ts = np.interp(ts, ego_ut, ego_lat)
        ego_lon_at_ts = np.interp(ts, ego_ut, ego_lon)
        distances = distance_m(lat, lon, ego_lat_at_ts, ego_lon_at_ts, lat_ref)
        min_idx = int(np.argmin(distances))
        min_dist = float(distances[min_idx])
        # Convert broadcast SOG from m/s (AIS PGN 129039 raw units, see
        # extract_ais.py::decode_ais_position) to knots for the JSON consumer.
        sog_kt = sog_mps * G.MS_2_KNOTS
        candidate_boats.append(tag_candidate_boat({
            "mmsi": boat.get("mmsi") or mmsi,
            "name": boat.get("name") or "",
            "callsign": boat.get("callsign") or "",
            "ship_type": boat.get("ship_type"),
            "min_dist_m": round(min_dist, 1),
            "closest_approach_ts": round(float(ts[min_idx]), 1),
            "track": compact_track(ts, lat, lon, sog_kt),
        }, alias_index))

    # Keep any boat that came within proximity_m of ego, OR that matched a
    # configured fleet (e.g. a J/105 in ais_fleets.yaml). Fleet members are
    # curated boats of interest, so they're always retained regardless of
    # closest approach — a fleet boat that raced the far side of the course
    # (Moose Unknown, sail #89, closest ~900 m on 2026-05-30) would otherwise
    # be silently dropped by the proximity cut despite being on the AIS list.
    boats = [boat for boat in candidate_boats
             if boat["min_dist_m"] < proximity_m or boat.get("fleet_tags")]

    boats.sort(key=lambda b: (b["min_dist_m"], (b.get("name") or "").upper(), str(b["mmsi"])))

    ego_track = decimate_ego_track(ego_ut, ego_lat, ego_lon, ego_decimate_s)

    os.makedirs(out_dir, exist_ok=True)
    # Per-day instrument log (.log) consumed by tracks.html's instrument
    # overlay. Side-by-side with the JSON so CloudFront caches both under
    # the same path prefix.
    log_basename = f"{date_str}.log"
    log_path = os.path.join(out_dir, log_basename)
    log_rows = build_day_log(ego_paths, log_path, hz=instruments_hz)
    if log_rows <= 0 and os.path.exists(log_path):
        # Don't leave an empty stub behind if every session was rejected.
        try:
            os.remove(log_path)
        except OSError:
            pass

    output = {
        "date": date_str,
        "generated_at": datetime.now().isoformat(),
        "source_ais": ais_blob["source_files"],
        "source_logs": [os.path.basename(path) for path in ego_paths],
        "proximity_m": proximity_m,
        "fleet_config": os.path.basename(fleet_config_path) if fleet_config_path else None,
        "ego": fleet_config.get("ego") or {"name": "Our boat", "mmsi": DEFAULT_EGO_MMSI},
        "time_start": round(t_min, 1),
        "time_end": round(t_max, 1),
        "summary": {
            "source_boats": len(ais_blob.get("boats") or {}),
            "candidate_boats": len(candidate_boats),
            "boats_kept": len(boats),
            "fleet_tagged": sum(1 for boat in boats if boat.get("fleet_tags")),
            "skipped_no_overlap": skipped_no_overlap,
            "source_positions": (ais_blob.get("summary") or {}).get("total_positions"),
            "kept_positions": sum(len(b["track"]) for b in boats),
            "ego_points": len(ego),
            "ego_points_decimated": len(ego_track),
            "log_rows": log_rows,
            "log_hz": instruments_hz if log_rows > 0 else None,
        },
        "ego_track": ego_track,
        "boats": boats,
    }
    if log_rows > 0:
        # `log_file` is resolved relative to the JSON's directory by the viewer;
        # `columns` mirrors the channel-index map that videos.json carries for
        # sailview, so the existing log_load.js reader works unchanged.
        output["log_file"] = log_basename
        output["columns"] = sailview_log_v1.column_dict()

    out_path = os.path.join(out_dir, f"{date_str}.json")
    with open(out_path, "w") as f:
        json.dump(output, f, separators=(",", ":"))
    return out_path, output


def index_entry_from_payload(payload, file_name):
    """Small summary consumed by tracks.html index mode."""
    summary = payload.get("summary") or {}
    return {
        "date": payload.get("date"),
        "file": file_name,
        "time_start": payload.get("time_start"),
        "time_end": payload.get("time_end"),
        "source_logs": payload.get("source_logs") or [],
        "source_ais": [os.path.basename(p) for p in (payload.get("source_ais") or [])],
        "boats_kept": summary.get("boats_kept", 0),
        "fleet_tagged": summary.get("fleet_tagged", 0),
        "kept_positions": summary.get("kept_positions", 0),
        "ego_points_decimated": summary.get("ego_points_decimated", 0),
        "has_log": bool(payload.get("log_file")),
    }


def write_index(out_dir=OUT_DIR_DEFAULT):
    """Write Analyze/AISDays/index.json from existing day payloads."""
    days = []
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir)):
            if name == "index.json" or not name.endswith(".json"):
                continue
            path = os.path.join(out_dir, name)
            try:
                with open(path) as f:
                    payload = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("date"):
                days.append(index_entry_from_payload(payload, name))
    days.sort(key=lambda d: d["date"], reverse=True)
    index = {
        "generated_at": datetime.now().isoformat(),
        "days": days,
    }
    os.makedirs(out_dir, exist_ok=True)
    index_path = os.path.join(out_dir, "index.json")
    with open(index_path, "w") as f:
        json.dump(index, f, separators=(",", ":"))
    return index_path, index


def generate_days(dates, ais_dir=AIS_LOGS_DIR, logs_dir=PANDAS_LOGS_DIR,
                  out_dir=OUT_DIR_DEFAULT, proximity_m=PROXIMITY_M,
                  ego_decimate_s=EGO_DECIMATE_S,
                  fleet_config_path=FLEETS_CONFIG_DEFAULT,
                  instruments_hz=INSTRUMENT_HZ_DEFAULT):
    """Generate payloads for dates, skipping days without AIS or nearby boats."""
    written = []
    skipped = []
    for date_str in dates:
        try:
            out_path, payload = slice_ais_day(
                date_str,
                ais_dir=ais_dir,
                logs_dir=logs_dir,
                out_dir=out_dir,
                proximity_m=proximity_m,
                ego_decimate_s=ego_decimate_s,
                fleet_config_path=fleet_config_path,
                instruments_hz=instruments_hz,
            )
        except (FileNotFoundError, ValueError) as exc:
            skipped.append((date_str, str(exc)))
            continue
        if payload["summary"]["boats_kept"] <= 0:
            try:
                os.remove(out_path)
            except OSError:
                pass
            # Also drop the orphan .log we just produced for this date.
            log_orphan = os.path.join(out_dir, f"{date_str}.log")
            if os.path.exists(log_orphan):
                try:
                    os.remove(log_orphan)
                except OSError:
                    pass
            skipped.append((date_str, "no nearby AIS boats"))
            continue
        written.append((out_path, payload))
    index_path, index = write_index(out_dir)
    return written, skipped, index_path, index


def main():
    parser = argparse.ArgumentParser(description="Build a per-day AIS viewer payload")
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument("--date", "-d", help="Date to slice (YYYY-MM-DD)")
    scope.add_argument("--days", type=int, help="Generate local sailing days from the last N days")
    scope.add_argument("--since", help="Generate local sailing days on dates >= YYYY-MM-DD")
    parser.add_argument("--ais-dir", default=AIS_LOGS_DIR)
    parser.add_argument("--logs-dir", default=PANDAS_LOGS_DIR)
    parser.add_argument("--out-dir", default=OUT_DIR_DEFAULT)
    parser.add_argument("--proximity-m", type=float, default=PROXIMITY_M,
                        help=f"Keep boats with daily closest approach below this many meters (default {PROXIMITY_M:g})")
    parser.add_argument("--ego-decimate-s", type=float, default=EGO_DECIMATE_S,
                        help=f"Ego display-track decimation interval in seconds (default {EGO_DECIMATE_S:g})")
    parser.add_argument("--fleet-config", default=FLEETS_CONFIG_DEFAULT,
                        help=f"Fleet alias YAML (default {FLEETS_CONFIG_DEFAULT})")
    parser.add_argument("--instruments-hz", type=float, default=INSTRUMENT_HZ_DEFAULT,
                        help=f"Sample rate (Hz) of the per-day .log emitted alongside the JSON (default {INSTRUMENT_HZ_DEFAULT:g}). 0 disables the .log.")
    parser.add_argument("--force", action="store_true",
                        help="Accepted for pipeline compatibility; day payloads are regenerated when selected.")
    args = parser.parse_args()

    if args.date:
        dates = [args.date]
    elif args.days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")
        dates = find_ego_dates(args.logs_dir, since=cutoff)
    else:
        dates = find_ego_dates(args.logs_dir, since=args.since)

    written, skipped, index_path, index = generate_days(
        dates,
        ais_dir=args.ais_dir,
        logs_dir=args.logs_dir,
        out_dir=args.out_dir,
        proximity_m=args.proximity_m,
        ego_decimate_s=args.ego_decimate_s,
        fleet_config_path=args.fleet_config,
        instruments_hz=args.instruments_hz,
    )
    for out_path, output in written:
        print(
            f"Wrote {out_path}: {output['summary']['boats_kept']} boats, "
            f"{output['summary']['fleet_tagged']} tagged, "
            f"{output['summary']['kept_positions']} AIS positions"
        )
    for date_str, reason in skipped:
        print(f"Skipped {date_str}: {reason}")
    print(f"Wrote {index_path}: {len(index['days'])} days")


if __name__ == "__main__":
    main()
