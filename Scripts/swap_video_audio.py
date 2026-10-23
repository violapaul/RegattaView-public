#!/usr/bin/env python
"""Swap mic-on-person audio into a compressed Insta360 video.

The pipeline already aligns each compressed video against the BoatBrain log and
stores the absolute UTC anchor of frame 0 as `start_unix_ms` (float64, byte
offset 16) in the sibling `.slog`. We trust that anchor and only need to
anchor the audio file in absolute UTC (from ffprobe metadata or a manual
override), compute a constant signed offset, and ask ffmpeg to mux the audio
into a copy of the video without re-encoding the video stream.

Usage:
    conda run -n sailing --no-capture-output \\
        python Scripts/swap_video_audio.py \\
            --audio "~/Downloads/Seaview Ave NW.m4a" \\
            --date 20260427 \\
            --dry-run

See the plan at .claude/plans/we-have-the-option-warm-plum.md for design
notes. Hard rules:
  - `-c:v copy` is mandatory; never re-encode 4K video for an audio swap.
  - Backup camera audio to `<name>.camera.m4a` once before overwriting.
  - `os.replace` the tmp output onto the original (atomic on same fs).
  - `--dry-run` touches nothing on disk and never invokes ffmpeg with -y.
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import struct
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Repo root on sys.path so `from Libraries import insta360` works when invoked
# as `python Scripts/swap_video_audio.py` from the repo root.
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

LOG = logging.getLogger(__name__)


# ---- Constants (mirror Scripts/race_upload.py and Libraries/insta360.py) ----

# Hardcoded (not imported from Libraries.insta360) to keep this script
# scipy/pandas-free — same rationale as SLOG_HEADER_STRUCT below. Must
# stay in sync with Libraries.insta360.VIDEO_DIR. After the WEB_DIR
# collapse there is no separate Insta360/ staging dir — source video,
# .slog, and .poses all live under VID/.
INSTA360_LOCAL_DIR = "/Users/viola/Data/Insta360/VID"
# Local-only safety files (`.camera.m4a` original-audio backups,
# `.tmp.mp4` mid-mux scratch) live one level down so the upload allow-list
# never trips on them. Hardcoded (not imported from Libraries.insta360) to
# keep this script scipy/pandas-free — must stay in sync with
# Libraries.insta360.EXTRAS_DIR.
EXTRAS_DIR = os.path.join(INSTA360_LOCAL_DIR, "extras")
INSTA360_S3_PREFIX = "s3://regattaanalysis/www/Insta360/"

# Mirror of sailview_log_v2.HEADER_FIXED_STRUCT (30 bytes).
# Inlined to avoid pulling scipy/pandas via the Libraries.sailview_log_v2 import.
SLOG_HEADER_STRUCT = struct.Struct('<IHHIIdfH')
SLOG_MAGIC = 0x474F4C53  # 'SLOG' little-endian


# ---- Data classes -----------------------------------------------------------

@dataclass
class VideoMatch:
    path: str
    start_utc: datetime
    duration_s: float
    offset_s: float          # audio_start - video_start
    overlap_s: float         # length of [V_start, V_end] ∩ [A_start, A_end]
    skip_reason: Optional[str] = None  # set if we should skip


# ---- ffprobe / ffmpeg helpers -----------------------------------------------

def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing stdout/stderr as text. On failure, log
    stderr before re-raising — ffmpeg/ffprobe errors are otherwise opaque."""
    LOG.debug("$ %s", " ".join(cmd))
    try:
        return subprocess.run(cmd, check=check, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        LOG.error("Command failed: %s", " ".join(cmd))
        if e.stderr:
            LOG.error("stderr:\n%s", e.stderr)
        raise


def ffprobe_duration_s(path: str) -> float:
    out = _run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "csv=p=0",
        path,
    ]).stdout.strip()
    if not out:
        raise RuntimeError(f"ffprobe returned no duration for {path}")
    return float(out)


def parse_iso_utc(s: str) -> datetime:
    """Parse an ISO 8601 string into a tz-aware UTC datetime.

    Trailing 'Z' or an explicit '+HH:MM' offset is honored as-is. A naive
    timestamp (no tzinfo) is interpreted as **local wall-clock** time using
    the system timezone, then converted to UTC — this makes both
    `--audio-start-utc` and sidecar `<name>.start.txt` convenient on a
    Seattle dev machine without manually computing UTC offsets.
    """
    s = s.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        # Python interprets a naive datetime as system-local on .astimezone().
        return dt.astimezone(timezone.utc)
    return dt.astimezone(timezone.utc)


def probe_audio_anchor(audio_path: str) -> tuple[Optional[datetime], float]:
    """Return (start_utc_or_None, duration_s) for the audio file.

    Tries `com.apple.quicktime.creationdate` first (iOS Voice Memos), then
    `creation_time`. Both are documented as UTC. If neither tag is present,
    returns (None, duration) so the caller can fall back to mtime - duration.
    """
    out = _run([
        "ffprobe", "-v", "error",
        "-show_entries",
        "format_tags=creation_time,com.apple.quicktime.creationdate",
        "-show_entries", "format=duration",
        "-of", "json",
        audio_path,
    ]).stdout
    info = json.loads(out) if out.strip() else {}
    fmt = info.get("format", {}) or {}
    tags = fmt.get("tags", {}) or {}
    duration_s = float(fmt.get("duration", "0") or 0.0)

    # Apple's QuickTime tag wins when both are present; iOS Voice Memos sets it
    # explicitly and `creation_time` is sometimes a re-mux time.
    for key in ("com.apple.quicktime.creationdate", "creation_time"):
        val = tags.get(key)
        if val:
            try:
                return parse_iso_utc(val), duration_s
            except ValueError:
                LOG.warning("Could not parse %s=%r as ISO 8601", key, val)
    return None, duration_s


def resolve_audio_start(
    audio_path: str,
    duration_s: float,
    manual_start_utc: Optional[datetime],
) -> datetime:
    """Pick the absolute UTC anchor for sample 0 of the audio file.

    Priority: --audio-start-utc > QuickTime/creation_time > mtime - duration.
    The mtime fallback is loud because it depends on filesystem behavior and is
    easy to get wrong (e.g. file copied → mtime is the copy time).
    """
    if manual_start_utc is not None:
        LOG.info("Audio start: %s (manual --audio-start-utc)", manual_start_utc.isoformat())
        # Note both probed and manual values when probe yields a value, so a
        # disagreement is visible.
        probed, _ = probe_audio_anchor(audio_path)
        if probed is not None:
            delta = (manual_start_utc - probed).total_seconds()
            LOG.info("  (probe said %s; delta = %+.3f s)", probed.isoformat(), delta)
        return manual_start_utc

    probed, _ = probe_audio_anchor(audio_path)
    if probed is not None:
        LOG.info("Audio start: %s (from metadata)", probed.isoformat())
        return probed

    mtime = datetime.fromtimestamp(os.path.getmtime(audio_path), tz=timezone.utc)
    fallback = mtime - timedelta(seconds=duration_s)
    LOG.warning(
        "No creation_time / com.apple.quicktime.creationdate tag on %s; "
        "falling back to mtime - duration = %s. "
        "Consider --audio-start-utc.",
        audio_path, fallback.isoformat(),
    )
    return fallback


# ---- .slog reader -----------------------------------------------------------

def read_slog_start_unix_ms(slog_path: str) -> Optional[float]:
    """Return float `start_unix_ms` from the slog header, or None if the file
    is missing, has the wrong magic, or the field is zero (no BB sync yet)."""
    try:
        with open(slog_path, "rb") as f:
            buf = f.read(SLOG_HEADER_STRUCT.size)
    except FileNotFoundError:
        return None
    if len(buf) < SLOG_HEADER_STRUCT.size:
        return None
    magic, _ver, _, _, _, start_ms, _, _ = SLOG_HEADER_STRUCT.unpack(buf)
    if magic != SLOG_MAGIC or start_ms == 0.0:
        return None
    return start_ms


# ---- Video discovery & matching --------------------------------------------

def discover_videos(args) -> list[str]:
    """Resolve --video/--date into a list of mp4 paths."""
    if args.video:
        return [os.path.abspath(p) for p in args.video]
    pattern = os.path.join(INSTA360_LOCAL_DIR, f"VID_{args.date}_*.mp4")
    matches = sorted(glob.glob(pattern))
    if not matches:
        raise SystemExit(f"No videos matched {pattern}")
    return matches


def match_video(
    video_path: str,
    audio_start: datetime,
    audio_duration_s: float,
) -> VideoMatch:
    """Read sibling `.slog` for the video's wall-clock anchor and compute
    the offset and overlap against the audio span."""
    slog_path = os.path.splitext(video_path)[0] + ".slog"
    start_ms = read_slog_start_unix_ms(slog_path)
    if start_ms is None:
        return VideoMatch(
            path=video_path,
            start_utc=datetime.fromtimestamp(0, tz=timezone.utc),
            duration_s=0.0,
            offset_s=0.0,
            overlap_s=0.0,
            skip_reason="no .slog or pre-BB-sync (re-run BB sync first)",
        )
    video_start = datetime.fromtimestamp(start_ms / 1000.0, tz=timezone.utc)
    try:
        duration_s = ffprobe_duration_s(video_path)
    except Exception as e:
        return VideoMatch(
            path=video_path, start_utc=video_start, duration_s=0.0,
            offset_s=0.0, overlap_s=0.0,
            skip_reason=f"ffprobe failed: {e}",
        )
    video_end = video_start + timedelta(seconds=duration_s)
    audio_end = audio_start + timedelta(seconds=audio_duration_s)
    overlap_start = max(video_start, audio_start)
    overlap_end = min(video_end, audio_end)
    overlap_s = max(0.0, (overlap_end - overlap_start).total_seconds())
    offset_s = (audio_start - video_start).total_seconds()
    return VideoMatch(
        path=video_path,
        start_utc=video_start,
        duration_s=duration_s,
        offset_s=offset_s,
        overlap_s=overlap_s,
    )


def print_match_table(matches: list[VideoMatch]) -> None:
    """Print a one-line-per-video summary before doing any work."""
    header = f"{'video':<48} {'V_start_UTC':<26} {'dur_s':>8} {'offset_s':>11} {'overlap_s':>10}  action"
    print(header)
    print("-" * len(header))
    for m in matches:
        name = os.path.basename(m.path)
        v_start = m.start_utc.isoformat() if m.start_utc.timestamp() > 0 else "-"
        if m.skip_reason:
            action = f"SKIP ({m.skip_reason})"
        elif m.overlap_s <= 0.0:
            action = "SKIP (no overlap)"
        else:
            action = "MUX"
        print(f"{name:<48} {v_start:<26} {m.duration_s:>8.2f} {m.offset_s:>+11.3f} {m.overlap_s:>10.2f}  {action}")


# ---- ffmpeg invocation ------------------------------------------------------

def build_mux_cmd(
    video_path: str,
    audio_path: str,
    out_path: str,
    offset_s: float,
    bitrate: str,
) -> list[str]:
    """Construct the ffmpeg argv that muxes `audio_path` into `video_path` at
    a constant absolute offset, writing `out_path`.

    Sign convention: `offset_s = audio_start_utc - video_start_utc`.
      offset_s > 0  → audio starts AFTER video; pad audio head with silence
                       by delaying audio input with `-itsoffset`.
      offset_s < 0  → audio started BEFORE video; seek into audio with `-ss`
                       so audio sample 0 in the muxed stream lines up with
                       video frame 0.
    """
    if offset_s >= 0:
        # Delay audio relative to video. -itsoffset shifts the audio input's
        # timestamps by +offset_s before muxing; the missing head is filled
        # with silence in the AAC encoder when -shortest is on the video side.
        return [
            "ffmpeg", "-y",
            "-i", video_path,
            "-itsoffset", f"{offset_s:.6f}",
            "-i", audio_path,
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", bitrate,
            "-shortest",
            "-movflags", "+faststart",
            out_path,
        ]
    # Audio started before video; seek `|offset_s|` into the audio so the
    # audio's effective sample 0 in the mux matches video frame 0. Reverse
    # input order so the -ss applies cleanly to the audio.
    return [
        "ffmpeg", "-y",
        "-ss", f"{-offset_s:.6f}",
        "-i", audio_path,
        "-i", video_path,
        "-map", "1:v", "-map", "0:a",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", bitrate,
        "-shortest",
        "-movflags", "+faststart",
        out_path,
    ]


def build_camera_backup_cmd(video_path: str, backup_path: str) -> list[str]:
    """Extract the original camera audio track to `<name>.camera.m4a`."""
    return [
        "ffmpeg", "-y",
        "-i", video_path,
        "-map", "0:a",
        "-c:a", "copy",
        backup_path,
    ]


# ---- per-video driver -------------------------------------------------------

def process_one_video(
    match: VideoMatch,
    audio_path: str,
    args,
) -> bool:
    """Returns True on success, False on failure or skip."""
    if match.skip_reason:
        LOG.warning("Skipping %s: %s", os.path.basename(match.path), match.skip_reason)
        return False
    if match.overlap_s <= 0.0:
        LOG.warning("Skipping %s: audio span does not overlap video", os.path.basename(match.path))
        return False

    if match.overlap_s + 0.5 < match.duration_s:
        LOG.warning(
            "%s: audio covers only %.1f / %.1f s of video; tail will be silent",
            os.path.basename(match.path), match.overlap_s, match.duration_s,
        )

    video_path = match.path
    base, _ = os.path.splitext(video_path)
    # Safety + scratch files live in EXTRAS_DIR so they never trip the
    # upload allow-list on the main video dir. Cross-dir os.replace below
    # is fine — same filesystem, atomic on POSIX.
    os.makedirs(EXTRAS_DIR, exist_ok=True)
    name = os.path.basename(base)
    tmp_path = os.path.join(EXTRAS_DIR, name + ".tmp.mp4")
    backup_path = os.path.join(EXTRAS_DIR, name + ".camera.m4a")
    offset_s = args.audio_offset_s if args.audio_offset_s is not None else match.offset_s

    if args.audio_offset_s is not None and abs(args.audio_offset_s - match.offset_s) > 0.001:
        LOG.info(
            "%s: --audio-offset-s=%+.3f overrides computed offset %+.3f (delta %+.3f)",
            os.path.basename(video_path), args.audio_offset_s, match.offset_s,
            args.audio_offset_s - match.offset_s,
        )

    mux_cmd = build_mux_cmd(video_path, audio_path, tmp_path, offset_s, args.bitrate)

    # Backup is taken from the *current* video; if the camera audio has
    # already been replaced once, skip to avoid clobbering the real backup.
    need_backup = (not args.no_camera_backup) and (not os.path.exists(backup_path))
    backup_cmd = build_camera_backup_cmd(video_path, backup_path) if need_backup else None

    LOG.info("Plan for %s:", os.path.basename(video_path))
    if backup_cmd:
        LOG.info("  backup: %s", " ".join(backup_cmd))
    else:
        if args.no_camera_backup:
            LOG.info("  backup: (disabled by --no-camera-backup)")
        else:
            LOG.info("  backup: %s already exists, keeping it", os.path.basename(backup_path))
    LOG.info("  mux:    %s", " ".join(mux_cmd))
    LOG.info("  replace: %s -> %s", os.path.basename(tmp_path), os.path.basename(video_path))

    if args.dry_run:
        return True

    if not args.yes:
        # Sanity: the plan recommends rejecting clearly-wrong audio anchors
        # (e.g. parsed start far in the future or > 60 days old). We surface
        # the offset here so the operator has one last chance to bail.
        try:
            resp = input(f"Overwrite {os.path.basename(video_path)}? [y/N] ").strip().lower()
        except EOFError:
            resp = ""
        if resp not in ("y", "yes"):
            LOG.warning("Aborted by user; %s untouched", os.path.basename(video_path))
            return False

    if backup_cmd:
        try:
            _run(backup_cmd)
            LOG.info("Wrote camera-audio backup: %s", backup_path)
        except subprocess.CalledProcessError:
            LOG.error("Camera-audio backup failed; aborting before overwrite")
            return False

    try:
        _run(mux_cmd)
    except subprocess.CalledProcessError:
        LOG.error(
            "ffmpeg mux failed; leaving %s for inspection, %s untouched",
            tmp_path, os.path.basename(video_path),
        )
        return False

    # Atomic replace on the same filesystem; an interrupted run never leaves
    # the original half-written.
    os.replace(tmp_path, video_path)
    LOG.info("Replaced %s in place", os.path.basename(video_path))

    if not args.no_upload:
        # Imported lazily so --no-upload paths don't drag in pandas/scipy
        # from Libraries.insta360 transitively.
        from Libraries import insta360  # noqa: WPS433
        filename, ok = insta360._upload_single_file(video_path, s3_prefix=INSTA360_S3_PREFIX)
        if not ok:
            LOG.error("Upload of %s failed", filename)
            return False
        LOG.info("Uploaded %s", filename)

    return True


# ---- CLI --------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Swap mic-on-person audio into Insta360 mp4(s).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--audio", required=True,
                   help="Audio file (.m4a / .wav / .mp3) to mux in.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--video", action="append", default=None,
                   help="Path to a single mp4 (repeat for several).")
    g.add_argument("--date", default=None,
                   help=f"YYYYMMDD; scans {INSTA360_LOCAL_DIR} for VID_<date>_*.mp4.")
    p.add_argument("--audio-start-utc", default=None,
                   help="Manual override of the audio's start (ISO 8601). "
                        "Trailing 'Z' or '+HH:MM' = explicit; naive = system local.")
    p.add_argument("--audio-offset-s", type=float, default=None,
                   help="Manual signed offset in seconds (audio_start - video_start). "
                        "Skips the metadata-derived math.")
    p.add_argument("--bitrate", default="128k",
                   help="AAC bitrate for the muxed audio (default: 128k).")
    p.add_argument("--no-upload", action="store_true",
                   help="Skip the s5cmd re-upload to S3.")
    p.add_argument("--no-camera-backup", action="store_true",
                   help="Do not write <name>.camera.m4a before overwriting.")
    p.add_argument("--dry-run", action="store_true",
                   help="Print the plan and ffmpeg commands but touch nothing.")
    p.add_argument("--yes", action="store_true",
                   help="Skip the interactive overwrite confirmation.")
    p.add_argument("--verbose", "-v", action="store_true",
                   help="Verbose logging.")
    args = p.parse_args(argv)

    args.audio = os.path.abspath(os.path.expanduser(args.audio))
    if not os.path.isfile(args.audio):
        p.error(f"Audio file not found: {args.audio}")

    if args.audio_start_utc is not None:
        try:
            args.audio_start_utc_dt = parse_iso_utc(args.audio_start_utc)
        except ValueError as e:
            p.error(f"--audio-start-utc not parseable: {e}")
    else:
        args.audio_start_utc_dt = None

    return args


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Discover videos.
    videos = discover_videos(args)
    LOG.info("Found %d video(s)", len(videos))

    # Audio anchor + duration.
    _, audio_duration_s = probe_audio_anchor(args.audio)
    if audio_duration_s <= 0.0:
        try:
            audio_duration_s = ffprobe_duration_s(args.audio)
        except Exception as e:
            raise SystemExit(f"Could not determine audio duration: {e}")
    audio_start = resolve_audio_start(
        args.audio, audio_duration_s, args.audio_start_utc_dt,
    )

    # Sanity: span should be within ~60 days of now unless --yes.
    now = datetime.now(timezone.utc)
    audio_end = audio_start + timedelta(seconds=audio_duration_s)
    if (audio_end < now - timedelta(days=60)
            or audio_start > now + timedelta(days=1)) and not args.yes:
        raise SystemExit(
            f"Audio span [{audio_start} .. {audio_end}] is far from now; "
            f"refusing without --yes. (Pass --audio-start-utc to override.)"
        )
    LOG.info("Audio: %s ; duration %.2f s ; end %s",
             audio_start.isoformat(), audio_duration_s, audio_end.isoformat())

    # Match each video.
    matches = [match_video(v, audio_start, audio_duration_s) for v in videos]
    print_match_table(matches)

    # Process.
    any_failed = False
    n_done = 0
    for m in matches:
        ok = process_one_video(m, args.audio, args)
        if ok:
            n_done += 1
        elif not (m.skip_reason or m.overlap_s <= 0.0):
            any_failed = True

    LOG.info("Done. %d processed, %d total candidates.", n_done, len(matches))
    return 1 if any_failed else 0


if __name__ == "__main__":
    sys.exit(main())
