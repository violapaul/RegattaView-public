"""Synthesize a `VIA_*` ("Video, Audio-only") placeholder clip for an audio
drop that has no (or only partial) real-video coverage.

Output set, all in ``Libraries.insta360.VIDEO_DIR``:
  - ``VIA_<YYYYMMDD>_<HHMMSS>_99_NNN.mp4``   tiny black H.264, video-only
  - ``VIA_<YYYYMMDD>_<HHMMSS>_99_NNN.poses`` identity quats (v2 PS02 format)
  - ``VIA_<YYYYMMDD>_<HHMMSS>_99_NNN.log``   v1 per-clip BB-log slice
  - ``VIA_<YYYYMMDD>_<HHMMSS>_99_NNN.slog``  v2 per-clip BB-log slice

Date/time in the basename is US/Pacific local, matching the VID_*
convention from Insta360 EXIF.

The audio mux is NOT done here — the caller (``Libraries.voice_audio``)
re-enters the existing ``swap_video_audio.process_one_video`` path
against the synthesized mp4 once this module has produced the video +
slog (which carries the wall-clock anchor that match_video reads).
"""
from __future__ import annotations

import os
import struct
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Optional

import numpy as np
import pandas as pd
import pytz

from Libraries import insta360
from Libraries import sailview_log_v1, sailview_log_v2
from Libraries.global_variables import G
from Libraries.log_helper import LOG as _RegattaLogger

LOG = _RegattaLogger.logger

PACIFIC = pytz.timezone("US/Pacific")

# A high-numbered channel id keeps VIA_ basenames parseable by
# `insta360.extract_date_from_video` (which splits on '_' and takes
# parts[1]+[2]) while staying clear of any real Insta360 channel id
# (00 = left, 10 = right).
_VIA_CHANNEL = "99"

# Identity-quat encoding shortcut
# --------------------------------
# `insta360.write_pose_file` permutes columns [qx, qy, qz, qw] -> [qz, qx,
# qy, qw] and flips sign on qx. For the identity quat (0, 0, 0, 1) the
# permutation+flip is a no-op, so the on-disk identity row is just
# (0, 0, 0, multiplier).
_POSES_MULTIPLIER = 32767
_POSES_MSEC_PER_SAMPLE = 33  # match compute_video_poses default
_POSES_MAGIC = b"PS02"
_POSES_FLAG_DELTA = 1

_FFMPEG_FONT_CANDIDATES = (
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
)

# Lazily-loaded full BB parquet df. Loading every parquet (~years of data)
# is slow, but the audio-only path is rare; we cache for the rest of the
# process so multiple drops in one run reuse the load.
_BIG_DF: Optional[pd.DataFrame] = None


def _get_big_df() -> pd.DataFrame:
    global _BIG_DF
    if _BIG_DF is None:
        LOG.info("blank_video: loading BB parquet corpus (one-time)")
        _BIG_DF = insta360.read_parquet_files()
    return _BIG_DF


def _pacific_basename(start_utc: datetime, video_dir: str) -> str:
    """Return an unused VIA_* basename. Bumps the trailing serial on
    collision so two drops at the same Pacific second don't overwrite."""
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    local = start_utc.astimezone(PACIFIC)
    stem = f"VIA_{local.strftime('%Y%m%d_%H%M%S')}_{_VIA_CHANNEL}"
    for n in range(1, 1000):
        candidate = f"{stem}_{n:03d}"
        if not os.path.exists(os.path.join(video_dir, candidate + ".mp4")):
            return candidate
    raise RuntimeError(
        f"Could not allocate VIA_ basename for {start_utc.isoformat()}: "
        f"1000 serials already taken in {video_dir}")


def _resolve_font_file() -> Optional[str]:
    for p in _FFMPEG_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return None


def _create_mp4(out_path: str, duration_s: float) -> None:
    """Encode a tiny black H.264 equirect video of exactly `duration_s`
    with an "Audio Only" text overlay plus a silent stereo AAC track.

    Frame is 2:1 (equirect) so the videos.json `projection: 'equirect'`
    tag on VIA_ rows is honored by viewer360 — text drawn at the
    frame center lands directly ahead at the equator in the spherical
    projection, mildly stretched but readable. A 1:1 fisheye source
    instead warps the text onto a curved hemisphere and renders it
    illegibly.

    The silent audio track has to exist (even at 32 kbps) so the
    caller's swap_video_audio.process_one_video flow finds something to
    back up via `ffmpeg -map 0:a -c:a copy ... .camera.m4a` before the
    real mux overwrites it. Without the silent track, that backup step
    errors out and the audio mux is skipped, leaving the clip mute.
    """
    tmp = out_path + ".tmp.mp4"
    # Equirect frame: 1024x512 (2:1). Text at frame center lands on the
    # equator directly ahead. Two stacked lines: a heading and a smaller
    # subtitle below.
    width, height = 1024, 512
    title = "Audio Only"
    subtitle = "(no video captured for this race)"
    title_size = 40
    subtitle_size = 22
    gap = 6  # px between baselines of title and subtitle
    font = _resolve_font_file()
    if font:
        # Anchor pair so subtitle sits directly below the title with a
        # small gap; both centered horizontally.
        title_filter = (
            f"drawtext=text='{title}':fontfile={font}:"
            f"fontcolor=white:fontsize={title_size}:"
            f"x=(w-text_w)/2:y=(h/2)-text_h-{gap}"
        )
        subtitle_filter = (
            f"drawtext=text='{subtitle}':fontfile={font}:"
            f"fontcolor=white:fontsize={subtitle_size}:"
            f"x=(w-text_w)/2:y=(h/2)+{gap}"
        )
        drawtext = f"{title_filter},{subtitle_filter}"
    else:
        # ffmpeg's drawtext can default-resolve a font on macOS, but if
        # neither candidate file is present we skip the overlay rather
        # than fail the whole synthesis — the black-only fallback is
        # ugly but functional.
        LOG.warning("blank_video: no font for drawtext; emitting plain black")
        drawtext = None

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=black:s={width}x{height}:r=1:d={duration_s:.3f}",
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
    ]
    if drawtext:
        cmd += ["-vf", drawtext]
    cmd += [
        "-t", f"{duration_s:.3f}",
        "-map", "0:v",
        "-map", "1:a",
        "-c:v", "libx264",
        "-pix_fmt", "yuv420p",
        "-tune", "stillimage",
        # crf=18 is visually near-lossless. Default crf=23 softened the
        # white-on-black edges enough that text rendered as grey on the
        # WebGL-sampled sphere texture.
        "-crf", "18",
        "-c:a", "aac",
        "-b:a", "32k",
        "-shortest",
        "-movflags", "+faststart",
        tmp,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    except subprocess.CalledProcessError as e:
        # Surface ffmpeg's complaint — usually a font path issue.
        raise RuntimeError(
            f"ffmpeg failed for {out_path}: {e.stderr.decode(errors='replace')[-500:]}"
        ) from e
    os.replace(tmp, out_path)


def _create_poses(out_path: str, duration_s: float) -> None:
    """Write an identity-quaternion v2 poses file matching the standard
    `compute_video_poses` on-disk layout (delta-encoded int16). For
    identity, post-permute row = (0, 0, 0, multiplier) so deltas are
    (0, 0, 0, multiplier) at row 0 and (0, 0, 0, 0) thereafter."""
    rows = max(2, int(np.ceil(duration_s * 1000.0 / _POSES_MSEC_PER_SAMPLE)))
    deltas = np.zeros((rows, 4), dtype=np.int16)
    deltas[0, 3] = _POSES_MULTIPLIER

    tmp = out_path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(_POSES_MAGIC)
        hdr = np.array(
            [_POSES_FLAG_DELTA, rows, 4, _POSES_MSEC_PER_SAMPLE, _POSES_MULTIPLIER],
            dtype=np.uint32,
        )
        f.write(hdr.tobytes())
        f.write(b"\x00" * 8)  # reserved
        f.write(deltas.tobytes())
    os.replace(tmp, out_path)


def _create_logs(basename: str,
                 start_utc: datetime,
                 duration_s: float,
                 video_dir: str) -> bool:
    """Slice the BB log to [start_utc, start_utc + duration_s] and write
    both the v1 `.log` and v2 `.slog` for this clip. Returns True iff at
    least the v1 log was written (i.e. BB data covered the span).

    The v2 `.slog` is the file `voice_audio._scan_videos_for_audio_span`
    reads to time-align future audio drops, so it MUST carry a correct
    `start_unix_ms` header. `sailview_log_v2.write_slog` derives this
    from `df['row_times'].iloc[0]` which lands at-or-just-after start_utc
    given the slice filter `row_times >= dt_start`.
    """
    if start_utc.tzinfo is None:
        start_utc = start_utc.replace(tzinfo=timezone.utc)
    dt_start = pd.Timestamp(start_utc)
    dt_end = pd.Timestamp(start_utc + timedelta(seconds=duration_s))

    big_df = _get_big_df()
    cdf, raw_slice = insta360.log_extract_slice(big_df, dt_start, dt_end)
    if len(cdf) < 10 or len(raw_slice) < 2:
        LOG.warning(
            "blank_video: BB log has %d rows in [%s, %s] — skipping .log/.slog "
            "for %s (instrument panel will be hidden on this clip)",
            len(cdf), dt_start.isoformat(), dt_end.isoformat(), basename)
        return False

    log_file = os.path.join(video_dir, basename + ".log")
    slog_file = os.path.join(video_dir, basename + ".slog")

    sailview_log_v1.save_log_to_binary(cdf, log_file)

    try:
        stats = sailview_log_v2.write_slog(
            raw_slice, slog_file, proj4=getattr(G, "PROJ4", ""))
        LOG.info(
            "blank_video: wrote %s (%d B, %d cols, start_unix_ms=%d)",
            slog_file, stats["file_size"], stats["num_cols"],
            int(stats["start_unix_ms"]))
    except Exception as e:
        LOG.error("blank_video: write_slog failed for %s: %s", basename, e)
        # .log alone is still useful — the frontend instrument panel will
        # display via the v1 fallback. But without the slog header,
        # _scan_videos_for_audio_span on a future run cannot find this
        # clip, so it could be re-synthesized. applied.json keying on
        # audio basename+mtime keeps the user-visible behaviour stable;
        # this is just a duplication risk if the manifest is wiped.
        return True

    return True


def create_for_audio(audio_path: str,
                     start_utc: datetime,
                     duration_s: float,
                     *,
                     video_dir: Optional[str] = None) -> str:
    """Synthesize a VIA_* clip covering [start_utc, start_utc + duration_s].

    Returns the new clip's basename (no extension). The caller is
    responsible for muxing `audio_path` into the resulting mp4 via the
    standard swap_video_audio.process_one_video flow.
    """
    if video_dir is None:
        video_dir = insta360.VIDEO_DIR
    os.makedirs(video_dir, exist_ok=True)

    basename = _pacific_basename(start_utc, video_dir)
    LOG.info(
        "blank_video: synthesizing %s for audio %s (%.1fs starting %s)",
        basename, os.path.basename(audio_path), duration_s,
        start_utc.isoformat())

    mp4_path = os.path.join(video_dir, basename + ".mp4")
    poses_path = os.path.join(video_dir, basename + ".poses")

    _create_mp4(mp4_path, duration_s)
    _create_poses(poses_path, duration_s)
    _create_logs(basename, start_utc, duration_s, video_dir)

    return basename
