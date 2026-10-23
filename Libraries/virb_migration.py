"""One-shot migration of VIRB-era 360 video into the modern Insta360/* layout.

Reads `s3://regattaanalysis/www/Virb/Small/*.MP4`, the legacy
`s3://regattaanalysis/www/Logs/video_poses_logs.json` manifest, and the legacy
`s3://regattaanalysis/www/Poses/*.bin` 5-col Float64 pose files. Writes
`s3://regattaanalysis/www/Insta360/<basename>.{mp4,poses,log}` and a record
suitable for splicing into `videos.json` (with `projection: 'equirect'`).

Default mode is dry-run: enumerates work and validates locally, but performs
no S3 mutation. Pass `dry_run=False` for actual writes.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import struct
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Optional

import arrow
import numpy as np
import pandas as pd

from Libraries.global_variables import G
from Libraries.log_helper import LOG
from Libraries import insta360, race_logs, sailview_log_v1


S3_BUCKET = "regattaanalysis"
S3_PREFIX = f"s3://{S3_BUCKET}/www"
LEGACY_VIRB_PREFIX = f"{S3_PREFIX}/Virb/Small/"
LEGACY_POSES_PREFIX = f"{S3_PREFIX}/Poses/"
LEGACY_MANIFEST_KEY = f"{S3_PREFIX}/Logs/video_poses_logs.json"
INSTA360_PREFIX = f"{S3_PREFIX}/Insta360/"
PANDAS_LOGS_DIR = "/Users/viola/Data/SailingData/PandasLogs"
AWS_PROFILE = os.environ.get("AWS_PROFILE", "regatta")

# 60s tolerance reused from the recovered legacy find_pose_files_for_videos behavior.
POSE_MATCH_TOLERANCE_SEC = 60


@dataclass
class VirbClip:
    basename: str               # 'V1070447'
    s3_mp4_url: str             # 's3://.../Virb/Small/V1070447.MP4'
    create_date: Optional[str]  # '2020:12:05 08:50:49' if known
    pose_key: Optional[str]     # '2020-12-05T08_50_23.566.bin' if matched
    pose_unix: Optional[float]  # pose start time in unix seconds
    log_pd_gz: Optional[str]    # local '<date>.pd.gz' if available


def _run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _s3_list(prefix: str) -> list[str]:
    res = _run(["aws", "s3", "ls", prefix])
    out = []
    for line in res.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            out.append(parts[-1])
    return out


def fetch_legacy_manifest(local_path: str = "/tmp/video_poses_logs.json") -> dict:
    if not os.path.exists(local_path):
        _run(["aws", "s3", "cp", LEGACY_MANIFEST_KEY, local_path])
    with open(local_path) as f:
        return json.load(f)


def parquet_basename(file: str) -> Optional[str]:
    m = re.match(r"(^.*)\.pd\.gz$", file)
    return m.group(1) if m else None


def _parquet_index() -> list[dict]:
    """Sorted list of {LogFile, ts_ms} for every local .pd.gz parquet."""
    out = []
    for file in sorted(os.listdir(PANDAS_LOGS_DIR)):
        base = parquet_basename(file)
        if base is None:
            continue
        try:
            date = arrow.get(base, "YYYY-MM-DD_HH:mm", tzinfo="US/Pacific")
        except arrow.parser.ParserError:
            continue
        out.append({"LogFile": file, "ts_ms": date.timestamp()})
    return sorted(out, key=lambda r: r["ts_ms"])


def _find_log_for_video(parquet_idx: list[dict], video_ts: float,
                        max_hours: float = 12.0) -> Optional[str]:
    """Pick the local parquet whose start <= video start within `max_hours`."""
    if not parquet_idx:
        return None
    times = [r["ts_ms"] for r in parquet_idx]
    idx = bisect.bisect_left(times, video_ts)
    if idx == 0:
        return None
    cand = parquet_idx[idx - 1]
    if (video_ts - cand["ts_ms"]) <= max_hours * 3600:
        return cand["LogFile"]
    return None


def _video_unix(video_record: dict) -> Optional[float]:
    cd = video_record.get("CreateDate")
    if not cd:
        return None
    try:
        return arrow.get(cd, "YYYY:MM:DD HH:mm:ss", tzinfo="US/Pacific").timestamp()
    except arrow.parser.ParserError:
        return None


def _pose_unix_from_filename(pose_file: str) -> Optional[float]:
    base = os.path.splitext(pose_file)[0]
    try:
        return arrow.get(base, "YYYY-MM-DDTHH_mm_ss.S", tzinfo="US/Pacific").timestamp()
    except arrow.parser.ParserError:
        return None


def enumerate_virb_videos(manifest: Optional[dict] = None,
                          s3_mp4s: Optional[list[str]] = None) -> list[VirbClip]:
    """Build the canonical clip list (MP4s from S3, timestamps from the legacy manifest)."""
    if manifest is None:
        manifest = fetch_legacy_manifest()
    if s3_mp4s is None:
        s3_mp4s = _s3_list(LEGACY_VIRB_PREFIX)

    by_source = {v["SourceFile"]: v for v in manifest.get("videos", []) if v.get("SourceFile")}
    parquet_idx = _parquet_index()

    clips = []
    for mp4 in s3_mp4s:
        if not mp4.lower().endswith(".mp4"):
            continue
        basename = os.path.splitext(mp4)[0]
        record = by_source.get(mp4)
        create_date = record.get("CreateDate") if record else None
        pose_key = record.get("PoseFile") if record else None
        # Manifest's video.ts is pose-derived when matched, else None.
        if record and "ts" in record:
            video_ts = float(record["ts"])
        else:
            video_ts = _video_unix({"CreateDate": create_date}) if create_date else None
        pose_unix = _pose_unix_from_filename(pose_key) if pose_key else None

        log_pd_gz = None
        if video_ts is not None:
            log_pd_gz = _find_log_for_video(parquet_idx, video_ts)

        clips.append(VirbClip(
            basename=basename,
            s3_mp4_url=LEGACY_VIRB_PREFIX + mp4,
            create_date=create_date,
            pose_key=pose_key,
            pose_unix=pose_unix,
            log_pd_gz=log_pd_gz,
        ))
    return clips


def parse_legacy_pose_file(local_path: str) -> tuple[np.ndarray, np.ndarray, float]:
    """Decode legacy 2xuint64 header + (rows*5) Float64 payload -> (ts_ms, quats_xyzw, duration_s).

    Empirical column order in the legacy file is (t, qw, qx, qy, qz) — the scalar
    sits in col 1 (mean ~0.92, std 0.005 across a real recording). Returned quats
    are reordered to xyzw so write_pose_file's [2,0,1,3] permutation works.
    """
    with open(local_path, "rb") as f:
        data = f.read()
    if len(data) < 16:
        raise ValueError(f"{local_path}: too short for legacy pose header")
    nrows, ncols = struct.unpack("<qq", data[:16])
    if ncols != 5:
        raise ValueError(f"{local_path}: expected 5 cols (t, qw, qx, qy, qz), got {ncols}")
    payload = data[16:]
    expected = nrows * ncols * 8
    if len(payload) != expected:
        raise ValueError(
            f"{local_path}: payload size mismatch — expected {expected} bytes, got {len(payload)}")
    arr = np.frombuffer(payload, dtype="<f8").reshape(nrows, ncols)
    ts_ms = arr[:, 0].copy()
    quats = arr[:, [2, 3, 4, 1]].copy()
    duration_s = float(ts_ms[-1] - ts_ms[0]) / 1000.0 if nrows >= 2 else 0.0
    return ts_ms, quats, duration_s


def _write_ps02_xyzw(out_path: str, quats_xyzw: np.ndarray, ts_ms: np.ndarray,
                     msec_per_sample: int = 33, multiplier: int = 32767) -> None:
    # Mirror of insta360.write_pose_file but without the Insta360-specific
    # [2,0,1,3] / [1,-1,1,1] permutation: the caller has already produced
    # WebGL-convention xyzw quats. Resamples by timestamp so the output's
    # claimed msec_per_sample matches reality regardless of the input rate.
    target = np.arange(float(ts_ms[0]), float(ts_ms[-1]) + 1e-6, msec_per_sample)
    idx = np.searchsorted(ts_ms, target)
    idx = np.clip(idx, 0, len(ts_ms) - 1)
    poses = (quats_xyzw[idx] * multiplier).astype(np.int16)
    rows, cols = poses.shape
    deltas = poses.copy()
    deltas[1:] = poses[1:] - poses[:-1]
    with open(out_path, "wb") as f:
        f.write(insta360.POSE_MAGIC_V2)
        hdr = np.array([insta360.POSE_FLAG_DELTA, rows, cols, msec_per_sample, multiplier],
                       dtype=np.uint32)
        f.write(hdr.tobytes())
        f.write(b"\x00" * 8)
        f.write(deltas.tobytes())


def _virb_to_webgl(quats_xyzw: np.ndarray) -> np.ndarray:
    """Conjugate by R(180° about (0, 1, -1)/√2): q_webgl = (-qx, -qz, -qy, qw).

    The legacy stabilizer (`/Users/viola/www/virb_pose/virb_pose.cpp`) writes
    qest as body-from-world in the FRD-style world frame the C++ code defines:
        k_hat = (0,0,1) is gravity-DOWN (constraint `k_est points down`),
        j_hat = (0,1,0) is FORWARD     (constraint `j_est points to j_flat`),
        i_hat = j × k                  (LEFT, by right-hand rule).
    WebGL is X=right, Y=up, Z=back. Mapping the bases gives a 180° rotation
    about (0, 1, -1)/√2 — i.e., the swap (qy ↔ qz) with sign flips on qx, qy, qz.
    """
    out = np.empty_like(quats_xyzw)
    out[:, 0] = -quats_xyzw[:, 0]   # -qx
    out[:, 1] = -quats_xyzw[:, 2]   # -qz
    out[:, 2] = -quats_xyzw[:, 1]   # -qy
    out[:, 3] =  quats_xyzw[:, 3]   #  qw
    return out


def transcode_legacy_pose(local_legacy_pose: str, out_path: str,
                          msec_per_sample: int = 33) -> dict:
    """Transcode a legacy 5-col Float64 pose .bin into a modern PS02 v2 .poses."""
    ts_ms, quats, duration_s = parse_legacy_pose_file(local_legacy_pose)
    norms = np.linalg.norm(quats, axis=1)
    bad = np.abs(norms - 1.0) > 1e-3
    if bad.any():
        nz = norms > 1e-9
        quats[nz] = quats[nz] / norms[nz, None]
    quats_webgl = _virb_to_webgl(quats)
    _write_ps02_xyzw(out_path, quats_webgl, ts_ms, msec_per_sample=msec_per_sample)
    rows_out = int(np.ceil((ts_ms[-1] - ts_ms[0]) / msec_per_sample)) + 1
    dt_ms_med = float(np.median(np.diff(ts_ms))) if len(ts_ms) >= 2 else 0.0
    return dict(
        rows_in=int(quats.shape[0]),
        rows_out=rows_out,
        duration_s=float(duration_s),
        mean_norm=float(norms.mean()),
        bad_norm_count=int(bad.sum()),
        input_dt_ms=dt_ms_med,
    )


def derive_log_from_parquet(parquet_filename: str, video_unix: float,
                            duration_s: float, out_path: str) -> dict:
    """Slice local `<date>.pd.gz` by [video_unix, video_unix+duration_s] and write a v1 .log."""
    df = race_logs.read_log_file(parquet_filename, path=PANDAS_LOGS_DIR)
    if df is None:
        raise FileNotFoundError(f"parquet {parquet_filename} not found or empty")
    start = pd.Timestamp(video_unix, unit="s", tz="UTC").tz_convert("US/Pacific")
    end = start + pd.Timedelta(seconds=duration_s)
    sdf = df[(df.row_times >= start) & (df.row_times <= end)]
    if len(sdf) <= 5:
        raise ValueError(f"insufficient parquet rows in window {start}..{end}: {len(sdf)}")
    cdf = sailview_log_v1.df_convert(sdf)
    cdf.row_seconds = cdf.row_seconds - cdf.row_seconds.iloc[0]
    sailview_log_v1.write_log_v1(cdf, out_path)
    return dict(rows=int(cdf.shape[0]), cols=int(cdf.shape[1]),
                duration_s=float(duration_s))


def _s3_object_exists(s3_url: str) -> bool:
    res = _run(["aws", "s3", "ls", s3_url], check=False)
    return res.returncode == 0 and bool(res.stdout.strip())


def _s5cmd(args: list[str]) -> None:
    env = os.environ.copy()
    env["AWS_PROFILE"] = AWS_PROFILE
    res = subprocess.run(["s5cmd"] + args, capture_output=True, text=True, env=env)
    if res.returncode != 0:
        raise RuntimeError(f"s5cmd {args} failed: {res.stderr.strip()}")


def migrate_one(clip: VirbClip, dry_run: bool = True,
                tmp_dir: Optional[str] = None,
                no_mp4_copy: bool = False) -> dict:
    """Migrate one clip; returns a videos.json-shape record with an _actions trail.

    When `no_mp4_copy=True` the MP4 server-side copy is skipped — useful when a
    bulk `s5cmd run` is handling the MP4 transfer separately and this pass is
    only generating the .poses / .log sidecars.
    """
    target_basename = clip.basename
    out_record = {
        "id": None,
        "SourceFile": f"{target_basename}.mp4",
        "basename": target_basename,
        "CreateDate": clip.create_date,
        "projection": "equirect",
    }
    actions = []

    if not dry_run and tmp_dir is None:
        tmp_dir = tempfile.mkdtemp(prefix="virb_mig_")
    elif tmp_dir is None:
        tmp_dir = "/tmp"

    mp4_target = f"{INSTA360_PREFIX}{target_basename}.mp4"
    if no_mp4_copy:
        actions.append({"step": "copy_mp4", "from": clip.s3_mp4_url, "to": mp4_target,
                        "status": "skipped_by_flag"})
    else:
        actions.append({"step": "copy_mp4", "from": clip.s3_mp4_url, "to": mp4_target})
        if dry_run:
            actions[-1]["status"] = "would_copy"
        elif _s3_object_exists(mp4_target):
            actions[-1]["status"] = "skipped_exists"
        else:
            _s5cmd(["cp", clip.s3_mp4_url, mp4_target])
            actions[-1]["status"] = "copied"

    if clip.pose_key:
        legacy_pose_url = f"{LEGACY_POSES_PREFIX}{clip.pose_key}"
        local_legacy = os.path.join(tmp_dir, f"{target_basename}.legacy.bin")
        local_modern = os.path.join(tmp_dir, f"{target_basename}.poses")
        poses_target = f"{INSTA360_PREFIX}{target_basename}.poses"

        action = {"step": "transcode_pose", "from": legacy_pose_url, "to": poses_target}
        try:
            if dry_run and not os.path.exists(local_legacy):
                action["status"] = "would_transcode"
            else:
                if not os.path.exists(local_legacy):
                    _run(["aws", "s3", "cp", legacy_pose_url, local_legacy])
                stats = transcode_legacy_pose(local_legacy, local_modern)
                action["stats"] = stats
                if not dry_run:
                    _s5cmd(["cp", local_modern, poses_target])
                    action["status"] = "uploaded"
                else:
                    action["status"] = "transcoded_local_only"
                out_record["PoseFile"] = f"{target_basename}.poses"
        except Exception as e:
            action["status"] = f"error: {e}"
        actions.append(action)

    if clip.log_pd_gz and clip.pose_unix is not None:
        local_log = os.path.join(tmp_dir, f"{target_basename}.log")
        log_target = f"{INSTA360_PREFIX}{target_basename}.log"
        # Prefer pose-derived duration when we have the legacy pose locally.
        duration_s = None
        legacy_local = os.path.join(tmp_dir, f"{target_basename}.legacy.bin")
        if os.path.exists(legacy_local):
            try:
                _, _, duration_s = parse_legacy_pose_file(legacy_local)
            except ValueError:
                duration_s = None
        if duration_s is None:
            duration_s = 1800.0  # 30-minute VIRB chunks are the camera default
        action = {"step": "derive_log", "from": clip.log_pd_gz, "to": log_target,
                  "duration_s": duration_s}
        try:
            if dry_run:
                action["status"] = "would_derive"
            else:
                stats = derive_log_from_parquet(clip.log_pd_gz, clip.pose_unix,
                                                duration_s, local_log)
                action["stats"] = stats
                _s5cmd(["cp", local_log, log_target])
                action["status"] = "uploaded"
                out_record["LogFile"] = f"{target_basename}.log"
        except Exception as e:
            action["status"] = f"error: {e}"
        actions.append(action)

    out_record["_actions"] = actions
    return out_record


def migrate_all(clips: Optional[list[VirbClip]] = None, dry_run: bool = True,
                limit: Optional[int] = None,
                basenames: Optional[set[str]] = None,
                no_mp4_copy: bool = False) -> list[dict]:
    if clips is None:
        clips = enumerate_virb_videos()
    if basenames:
        clips = [c for c in clips if c.basename in basenames]
    if limit:
        clips = clips[:limit]

    G.init_seattle()  # required for race_logs / df_convert (uses G.MAP, G.MS_2_KNOTS)
    out = []
    for clip in clips:
        LOG.logger.info(f"migrate_one {clip.basename} (dry_run={dry_run})")
        out.append(migrate_one(clip, dry_run=dry_run, no_mp4_copy=no_mp4_copy))
    return out
