import brotli
import json
import shutil
import subprocess
import os
import struct
import sys
import tempfile


from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import List, Optional
import pandas as pd
import numpy as np

import telemetry_parser

import re
import pytz

import numpy as np

from Libraries.log_helper import LOG

import Libraries.utils as utils
from Libraries import card_report

import Libraries.race_logs as race_logs
import Libraries.sailview_log_v1 as sailview_log_v1
import Libraries.sailview_log_v2 as sailview_log_v2
from Libraries.global_variables import G

import Libraries.geometry as geo

# Initialize for Seattle.
# LOG.set_logging_level('DEBUG')

MIN_VIDEO_SIZE = 333 * 10**6

# Compressed mp4s shorter than this are accidental taps or failed
# captures (camera came on briefly, then went off). They have no
# .slog because BB sync rejects clips too short to cross-correlate
# gyro vs heading, so they'd appear in the picker as a clickable
# but instrument-less stub. `clean_too_short_videos` moves them out
# of VIDEO_DIR before `create_videos_json` runs.
MIN_VIDEO_DURATION_S = 30.0

VIDEO_DIR = "/Users/viola/Data/Insta360/VID"
# Intermediates and raw camera dumps live one level down so VIDEO_DIR
# itself stays web-targets-only (.mp4, .log, .slog, .poses, .m4a,
# videos.json). RAW_DIR holds .insv (raw fisheye), .lrv (low-res
# preview), .exif / .ffprobe / .telemetry / .imu / .gps (metadata
# sidecars), .bak (rewrite backups), and .log.delay (per-clip cached
# alignment state). Anything in RAW_DIR is internal — never uploaded.
# EXTRAS_DIR holds local-only safety files (voice_audio's
# `<name>.camera.m4a` original-audio backups, mid-mux `.tmp.mp4`
# scratch files) plus any stray non-VID files (test/taps/mojibake)
# the operator wants to keep around but never ship to S3.
# Both are created lazily by the first function that writes to them
# (no import-time disk side effects).
RAW_DIR = os.path.join(VIDEO_DIR, "raw")
EXTRAS_DIR = os.path.join(VIDEO_DIR, "extras")
SMALL_DIR = "/Users/viola/Data/Insta360/TooSmall"
BACKUP_DIR = "/Users/viola/Data/Insta360/Backup"

VIDEO_MATCH = "^VID.*insv$"
LEFT_VIDEO_MATCH = "^VID.*_00_.*.insv$"
RIGHT_VIDEO_MATCH = "^VID.*_10_.*.insv$"

INSTA360_CARD_DIR = "/Volumes/Insta360/DCIM/Camera01/"

PANDAS_DIRECTORY = '/Users/viola/Data/SailingData/PandasLogs'
AWS_PROFILE = os.environ.get("AWS_PROFILE", "regatta")


##################################################################################################
# The first part of the file about video processing.

def _verify_and_wipe_insta360_card():
    """Verify card against RAW_DIR, then wipe + unmount.

    On verify failure: log the ERROR and, if running interactively
    (stdin is a TTY), prompt the operator to wipe anyway. The pipelined
    copy worker only copies .insv, so .lrv proxies are never kept; we
    pass `ignore_suffixes=(".lrv",)` so a clean run no longer flags all
    28 of them and prompts every time. Any genuine .insv mismatch (or a
    stray sidecar like Thumb / fileinfo_list) still trips the gate and
    surfaces the prompt. In non-interactive runs the gate stays strict —
    refuse and leave the card mounted.

    Returns True iff the card was wiped.
    """
    ok, problems = utils.verify_card_copy_complete(
        INSTA360_CARD_DIR, RAW_DIR, ignore_suffixes=(".lrv",)
    )
    if not ok:
        LOG.logger.error(
            f"Refusing to wipe Insta360 card: {len(problems)} file(s) "
            f"unverified. Card left mounted. First few: {problems[:5]}"
        )
        if not sys.stdin.isatty():
            card_report.record("Insta360", mounted=True, wiped=False)
            card_report.set_note("Insta360", f"mounted, {len(problems)} unverified file(s) — REFUSED to wipe; card left mounted")
            card_report.add_problem("Insta360", f"{len(problems)} unverified file(s); card left mounted, not wiped/ejected")
            return False
        print(f"\n  Card has {len(problems)} unverified file(s):", flush=True)
        for p in problems[:10]:
            print(f"    {p}")
        if len(problems) > 10:
            print(f"    ... and {len(problems) - 10} more")
        try:
            ans = input("Wipe Insta360 card anyway? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False
        if ans != "y":
            card_report.record("Insta360", mounted=True, wiped=False)
            card_report.set_note("Insta360", f"mounted, {len(problems)} unverified file(s) — REFUSED to wipe; card left mounted")
            card_report.add_problem("Insta360", f"{len(problems)} unverified file(s); card left mounted, not wiped/ejected")
            return False
        LOG.logger.info(
            "Operator approved wipe despite unverified file(s)"
        )
    else:
        LOG.logger.info("Card verified against local, cleaning SD card")

    utils.fsync_tree(RAW_DIR)
    cmd = f"rm -rf {INSTA360_CARD_DIR}*"
    LOG.logger.info(cmd)
    subprocess.run(cmd, shell=True, check=True)
    # Eject by mounted volume path (not a bare name): INSTA360_CARD_DIR is
    # "/Volumes/Insta360/DCIM/Camera01/", so the volume root is /Volumes/<name>.
    volume_path = os.path.join("/Volumes", INSTA360_CARD_DIR.split("/Volumes/", 1)[1].split("/", 1)[0])
    ejected = utils.eject_card(volume_path)
    card_report.record("Insta360", mounted=True, wiped=True, ejected=ejected)
    if ejected:
        card_report.set_note("Insta360", "mounted, copied + verified, wiped, ejected OK")
    else:
        card_report.set_note("Insta360", "mounted, copied + verified, wiped, EJECT FAILED — remove manually")
        card_report.add_problem("Insta360", "eject failed; unmount/remove the card manually")
    return True


def copy_video_from_card():
    if not os.path.exists(INSTA360_CARD_DIR):
        # Soft-fail: matches process_videos_pipelined's behavior so a re-run
        # without the card stays loud-but-not-fatal (visible in run summary).
        LOG.logger.warning(
            f"NO_INPUT: Insta360 card not mounted at {INSTA360_CARD_DIR} — "
            "skipping copy; downstream uses existing VIDEO_DIR contents"
        )
        card_report.record("Insta360", mounted=False)
        card_report.set_note("Insta360", "not mounted — processed existing local files only")
        return
    LOG.logger.info(f"Copying from {INSTA360_CARD_DIR}")
    os.makedirs(RAW_DIR, exist_ok=True)
    raw_dir = RAW_DIR + "/"
    # Exclude macOS per-volume metadata so Spotlight's unreadable index dir
    # (.Spotlight-V100) doesn't trip rsync with exit 23.
    excludes = "--exclude '.Spotlight-V100' --exclude '.fseventsd' --exclude '.Trashes' --exclude '.DS_Store'"
    # rsync -av Camera01/ /Users/viola/Data/Insta360/VID/raw/
    # Card holds .insv + .lrv + occasional sidecars; all are intermediates
    # and belong in RAW_DIR. Web-targets are produced downstream.
    cmd = f"rsync -av {excludes} {INSTA360_CARD_DIR} {raw_dir}"
    LOG.logger.info(cmd)
    subprocess.run(cmd, shell=True, check=True)
    subprocess.run(cmd, shell=True, check=True)
    # Belt-and-braces gate: re-list the card and assert every file has a
    # same-size local sibling. rsync's exit code alone has bitten us
    # before (a paired _10_ half went missing while rsync still reported
    # success on its sibling _00_ half). On verify failure the helper
    # logs ERROR and, when interactive, prompts to wipe anyway.
    _verify_and_wipe_insta360_card()

def clean_small_files(re_match, min_size=MIN_VIDEO_SIZE, dry_run = True):
    """
    Move all video files in the directory that are small.
    The 4K by 30fps video is 10Gig per 30 mins.  So 1 min is 333M.  Anything less than this is likely
    just a mistaken capture.
    """
    for file in os.listdir(RAW_DIR):
        if re.match(VIDEO_MATCH, file):
            path = os.path.join(RAW_DIR, file)
            file_size = os.path.getsize(path)
            if file_size < min_size:
                LOG.logger.info(f"Small file {path} of size {file_size/10**6:.1f}M")
                LOG.logger.debug(f"Small file {path} of size {file_size/10**6:.1f}M")
                new_file = os.path.join(SMALL_DIR, os.path.basename(file))
                if not dry_run:
                    os.rename(file, new_file)
                LOG.logger.debug(f"Moved {file} to {new_file}")

def _ffprobe_duration_s(mp4_path: str) -> Optional[float]:
    """Return mp4 duration in seconds via ffprobe, or None on failure."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-of", "csv=p=0",
             "-show_entries", "format=duration", mp4_path],
            stderr=subprocess.DEVNULL,
        )
        return float(out.strip())
    except (subprocess.CalledProcessError, ValueError) as e:
        LOG.logger.debug(f"ffprobe duration failed for {mp4_path}: {e}")
        return None


# Sidecar extensions that travel with a clip's basename. Kept in one
# place so the move below stays in sync with create_videos_json.
_SIDECAR_EXTS = (".poses", ".log", ".slog", ".m4a", ".camera.m4a")

# Too-short clips are quarantined here. The compressed .mp4 + web sidecars
# go to SMALL_DIR; the raw .insv + extraction sidecars go to SMALL_DIR/raw/
# so the next run's compress/pose passes (which list RAW_DIR) no longer see
# them — otherwise they re-derive the mp4/.poses every run only to have the
# duration gate sweep them away again, an infinite rework loop.
RAW_SMALL_DIR = os.path.join(SMALL_DIR, "raw")


def _evict_raw_inputs(basename, dry_run=False):
    """Move a too-short clip's raw inputs out of RAW_DIR into SMALL_DIR/raw/.

    Covers the clip's own raw sidecars (.insv, .imu, .ffprobe, .telemetry,
    .exif, plus any .bak / .log.delay strays) AND the paired _10_ right-lens
    files, by matching every RAW_DIR entry that starts with the _00_ basename
    stem or its _10_ counterpart followed by a dot. Returns the count moved.
    """
    if not os.path.isdir(RAW_DIR):
        return 0
    stems = {basename, re.sub(r"_00_", "_10_", basename)}
    moved = 0
    for fn in os.listdir(RAW_DIR):
        if not any(fn.startswith(stem + ".") for stem in stems):
            continue
        src = os.path.join(RAW_DIR, fn)
        dst = os.path.join(RAW_SMALL_DIR, fn)
        if dry_run:
            LOG.logger.debug(f"[dry-run] would move {src} -> {dst}")
        else:
            os.makedirs(RAW_SMALL_DIR, exist_ok=True)
            os.rename(src, dst)
            moved += 1
    return moved


def clean_too_short_videos(video_dir: str = VIDEO_DIR,
                           min_dur_s: float = MIN_VIDEO_DURATION_S,
                           dry_run: bool = False) -> list[str]:
    """Move compressed mp4s shorter than `min_dur_s` (and their sidecars)
    from `video_dir` to SMALL_DIR. Parallel to `clean_small_files`, which
    operates on raw .insv size; this catches clips that survived raw
    compression but came out too short to be useful in the player.

    Pre-screens by file size (>50 MB is definitely long enough at any
    typical Insta360 bitrate) so we don't ffprobe every clip on a big
    VIDEO_DIR. Returns the list of moved basenames.
    """
    os.makedirs(SMALL_DIR, exist_ok=True)
    moved: list[str] = []
    # Match VID_* (real) and VIA_* (synthetic audio-only) compressed
    # outputs. We never want to sweep a VIA_ we just built, so the
    # synthesizer shares this exact threshold: voice_audio's
    # _synthesize_for_uncovered_gaps uses MIN_VIDEO_DURATION_S as its
    # minimum gap, so any VIA_ that reaches this gate is >= min_dur_s and
    # survives. A sub-threshold VIA_ here can only be a pre-fix leftover.
    for name in sorted(os.listdir(video_dir)):
        if not re.match(r"^V[A-Z]+_.*\.mp4$", name):
            continue
        path = os.path.join(video_dir, name)
        try:
            if os.path.getsize(path) > 50 * 10**6:
                continue
        except OSError:
            continue
        dur = _ffprobe_duration_s(path)
        if dur is None or dur >= min_dur_s:
            continue
        basename = os.path.splitext(name)[0]
        for ext in (".mp4",) + _SIDECAR_EXTS:
            src = os.path.join(video_dir, basename + ext)
            if not os.path.exists(src):
                continue
            dst = os.path.join(SMALL_DIR, basename + ext)
            if dry_run:
                LOG.logger.info(f"[dry-run] would move {src} -> {dst}")
            else:
                os.rename(src, dst)
                LOG.logger.info(
                    f"clean_too_short_videos: moved {basename + ext} -> "
                    f"{SMALL_DIR} (dur={dur:.1f}s)")
        # Also evict the raw inputs (.insv/.imu/.ffprobe/.telemetry/.exif and
        # the paired _10_ lens). Without this the compress + pose passes find
        # the raws again next run and re-derive the mp4/.poses we just swept,
        # forever — the idempotency bug this function is meant to prevent.
        n_raw = _evict_raw_inputs(basename, dry_run=dry_run)
        if n_raw:
            LOG.logger.info(
                f"clean_too_short_videos: evicted {n_raw} raw input file(s) "
                f"for {basename} -> {RAW_SMALL_DIR}")
        moved.append(basename)
    return moved


def remove_orphan_metadata(dry_run = True):
    """
    In some cases there may be metadata files without the corresponding video files.  This will move
    them to a backup directory.
    """
    for file in os.listdir(RAW_DIR):
        if not re.match(VIDEO_MATCH, file):
            # remove the extension
            base = os.path.splitext(file)[0]
            metadata_path = os.path.join(RAW_DIR, file)
            video_path = os.path.join(RAW_DIR, base + ".insv")
            if not os.path.exists(video_path):
                backup_path = os.path.join(BACKUP_DIR, file)
                if not dry_run:
                    os.rename(metadata_path, backup_path)
                LOG.logger.debug(f"Moved {file} to {backup_path}")


def find_solo_video_files():
    """
    For higher resolutions and framerate, the insta360 camera records two files for each video,
    one for the left and one for the right (really front and back).  This function will find
    the files that are missing the right side.  These can be handled on their own.
    """
    solo_files = []
    for file in os.listdir(RAW_DIR):
        if re.match(LEFT_VIDEO_MATCH, file):
            # match _00_ and replace with _10_
            right_file = re.sub("_00_", "_10_", file)
            if not os.path.exists(os.path.join(RAW_DIR, right_file)):
                solo_files.append(file)
    return solo_files


def _run_compress(cmd, label):
    """Run an encode command, capturing its (very verbose) ffmpeg/HandBrake
    banner output instead of streaming it to the console. The captured text
    is logged at DEBUG on success and surfaced (WARNING) only when the
    command fails, so a steady-state run stays quiet but a real failure is
    still diagnosable. Returns the CompletedProcess.
    """
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    output = (result.stdout or "") + (result.stderr or "")
    if result.returncode != 0:
        LOG.logger.warning(
            f"{label} failed (exit {result.returncode}); output follows:\n"
            f"{output.strip()}")
    elif output.strip():
        LOG.logger.debug(f"{label} output:\n{output.strip()}")
    return result


def handbrake_recompress(video):
    input_file = os.path.join(RAW_DIR, video)
    new_file = os.path.splitext(video)[0] + ".mp4"
    output_file = os.path.join(VIDEO_DIR, new_file)
    if os.path.exists(output_file):
        LOG.logger.debug(f"Skipping {new_file} already exists")
        return
    # Atomic write: stage to a hidden .tmp in VIDEO_DIR (leading dot keeps
    # it outside the VID_*.mp4 upload glob), then os.replace on success so
    # SIGINT mid-encode never leaves a truncated mp4 the existence-only
    # skip-guard would re-use.
    tmp_file = os.path.join(VIDEO_DIR, f".tmp_{os.getpid()}_{new_file}")
    # Set the bitrate to 10,000 kbps.
    cmd = f"HandBrakeCLI --input {input_file} --output {tmp_file} --encoder vt_h265 --vb 10000 --encoder-preset fast"
    LOG.logger.debug(cmd)
    LOG.logger.info(f"Compressing {new_file}")
    try:
        result = _run_compress(cmd, f"HandBrake {new_file}")
        if (result.returncode == 0
                and os.path.exists(tmp_file)
                and os.path.getsize(tmp_file) > 0):
            os.replace(tmp_file, output_file)
    finally:
        if os.path.exists(tmp_file):
            try:
                os.unlink(tmp_file)
            except FileNotFoundError:
                pass


def find_paired_files():
    """
    Find all the files that have separate front and back videos.
    """
    paired_files = []
    for file in os.listdir(RAW_DIR):
        if re.match(LEFT_VIDEO_MATCH, file):
            # match _00_ and replace with _10_
            right_file = re.sub("_00_", "_10_", file)
            if os.path.exists(os.path.join(RAW_DIR, right_file)):
                paired_files.append((file, right_file))
    return paired_files

def ffmpeg_recompress(video_pair):
    left_file = os.path.join(RAW_DIR, video_pair[0])
    right_file = os.path.join(RAW_DIR, video_pair[1])
    new_file = os.path.splitext(video_pair[0])[0] + ".mp4"
    output_file = os.path.join(VIDEO_DIR, new_file)
    if os.path.exists(output_file):
        LOG.logger.debug(f"Skipping {new_file} already exists")
        return
    # Atomic write: stage to a hidden .tmp in VIDEO_DIR, then os.replace on
    # success. -y so ffmpeg overwrites any prior tmp without prompting.
    tmp_file = os.path.join(VIDEO_DIR, f".tmp_{os.getpid()}_{new_file}")
    # /opt/homebrew/bin/ffmpeg -i VID_20240422_200015_00_005.insv -i VID_20240422_200015_10_005.insv -filter_complex "[0:v]scale=-2:1920[v0];[1:v]scale=-2:1920[v1];[v0][v1]hstack[v]" -map "[v]" -map 0:a -c:v hevc_videotoolbox -qmax 40 -b:v 10M -ac 2 output2.mp4
    cmd = f"/opt/homebrew/bin/ffmpeg -y -i {left_file} -i {right_file} -filter_complex \"[0:v]scale=-2:1920[v0];[1:v]scale=-2:1920[v1];[v0][v1]hstack[v]\" -map \"[v]\" -map 0:a -c:v hevc_videotoolbox -qmax 40 -b:v 10M -ac 2 {tmp_file}"
    LOG.logger.debug(cmd)
    LOG.logger.info(f"Compressing {new_file} (paired)")
    try:
        result = _run_compress(cmd, f"ffmpeg {new_file}")
        if (result.returncode == 0
                and os.path.exists(tmp_file)
                and os.path.getsize(tmp_file) > 0):
            os.replace(tmp_file, output_file)
    finally:
        if os.path.exists(tmp_file):
            try:
                os.unlink(tmp_file)
            except FileNotFoundError:
                pass


def compress_all_videos():
    solo_files = find_solo_video_files()
    LOG.logger.debug(f"Compressing {len(solo_files)} solo files")
    for video in solo_files:
        handbrake_recompress(video)

    paired_files = find_paired_files()
    LOG.logger.debug(f"Compressing {len(paired_files)} paired files")
    for pair in paired_files:
        ffmpeg_recompress(pair)


def extract_telemetry(video_file):
    tp = telemetry_parser.Parser(video_file)
    telemetry = tp.telemetry()
    LOG.logger.info(f'Model {tp.model}')
    return telemetry[0]['Default']['Metadata']

def extract_video_metadata():
    """
    Find all the matching files in a directory use exiftool and ffprobe to extract metadata.  Store
    in files with .exif and .ffprobe extensions.
    """
    def mv(from_file, to_file):
        try:
            # copy so that it works across devices
            shutil.copyfile(from_file, to_file)
            os.remove(from_file)
        except Exception as e:
            LOG.logger.debug(f"Failed to move {from_file} to {to_file}")
            LOG.logger.debug(e)

    # Atomic helpers: write to .tmp then os.replace so SIGINT can't leave a
    # truncated sidecar the existence-only skip-guards below would re-use.
    def _atomic_write_bytes(path, data):
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    def _atomic_write_json(path, obj):
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(obj, f, indent=4)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except FileNotFoundError:
                pass
            raise

    for file in os.listdir(RAW_DIR):
        if re.match("^VID.*_00_.*insv$", file):
            path = os.path.join(RAW_DIR, file)
            basename = os.path.splitext(file)[0]
            exif_file = os.path.join(RAW_DIR, basename + ".exif")
            ffprobe_file = os.path.join(RAW_DIR, basename + ".ffprobe")
            telemetry_file = os.path.join(RAW_DIR, basename + ".telemetry")
            LOG.logger.info(f"Checking {basename}")
            if not os.path.exists(exif_file):
                LOG.logger.debug(f"running exif {basename}")
                # exiftool -m -ee -api largefilesupport -json ${filename} > "${basename}.exif"
                exec = "/Users/viola/Bin/exiftool"
                cmd = [exec, "-m", "-ee", "-api", "largefilesupport", "-json", path]
                LOG.logger.debug(" ".join(cmd))
                result = subprocess.run(cmd, capture_output=True)
                _atomic_write_bytes(exif_file, result.stdout)
                mv("/tmp/insta360.imu", os.path.join(RAW_DIR, basename + ".imu"))
                mv("/tmp/insta360.gps", os.path.join(RAW_DIR, basename + ".gps"))
            if not os.path.exists(ffprobe_file):
                # ffprobe -v quiet -show_format -show_streams -print_format json input.mp4
                LOG.logger.debug(f"running ffprobe {basename}")
                result = subprocess.run(["ffprobe", "-v", "quiet", "-show_format", "-show_streams", "-print_format", "json", path], capture_output=True)
                _atomic_write_bytes(ffprobe_file, result.stdout)
            if not os.path.exists(telemetry_file):
                LOG.logger.debug(f"running telemetry {basename}")
                telemetry = extract_telemetry(path)
                _atomic_write_json(telemetry_file, telemetry)

##################################################################################################
# Read the IMU data and estimate camera poses.


def read_binary_imu_data(file_path, dlen=20):
    """
    Read the raw IMU data that is dumped by my hacked version of Exiftool.  Essentially this is just
    the binary data stored by Insta360.  (Dumping in binary 100x faster than dumping the same data
    as text.)
    """
    # Set the data type based on the description
    if dlen == 56:
        record_dtype = np.dtype([
            ('timestamp', 'u8'), 
            ('data', '6d')  # 6 doubles after the timestamp
        ])
    else:
        record_dtype = np.dtype([
            ('timestamp', 'u8'), 
            ('data', '6H')  # 6 unsigned shorts after the timestamp
        ])
    
    # Read the entire file into a structured array
    data = np.fromfile(file_path, dtype=record_dtype)

    # Convert timestamps to milliseconds
    timestamps = data['timestamp'] / 1000.0

    # Handle data conversion if not doubles
    if dlen != 56:
        scaled_data = (data['data'].astype(np.float32) - 0x8000) / 1000.0
    else:
        scaled_data = data['data']

    # Process each component as needed
    accelerometers = scaled_data[:, 0:3]
    angular_velocities = scaled_data[:, 3:6]

    return timestamps, accelerometers, angular_velocities

# Some messy details to handle coordinate system conversion.  I worked long and hard on determining
# the axes and orientations of the Insta360 camera.  Even after doing all that, it still seems like
# one of the signs is not quite right.  
def rearrage_imu_data(accel, gyro):
    # These transforms are critical!
    new_accel = accel * [1, -1, 1]
    # you can play with the gyros like this!
    # ngg = ggg * [1, 0, 0]
    new_gyro = gyro * [1, -1, 1]
    return new_accel, new_gyro


def read_json_file(file):
    with open(file) as f:
        data = json.load(f)
    return data


# Trim the IMU data to match the video length and compute the camera poses.

# Time on the Insta360 is really annoying.  The videos seem to have 15 second gaps, and their are
# multiple time stamps: 1) the file name, 2) the file system creation time, 3) the exiftool time.

# The IMU data comes with "timestamps" that are in milliseconds, but they do not appear to start
# from the first video frame.  I think they may start at the beginning of the multi-file video.  But
# even in a single video file there are more IMU timestamps than video frames.

# The current plan is to assume that the last IMU timestamp aligns with the **end** of the
# video.  The first is then the last minus the video duration.


# def read_insta_imu_data(video_basename):
def read_insta_imu_data(video_basename, shift=0):
    """
    Read, trim, and rearrage the IMU data.

    The IMU data is longer than the video.  The telemetry_parser is a hack that can read the
    embedded Insta360 metadata.  In that data is a field called "first_frame_timestamp" which
    contains the magic.  https://github.com/AdrianEddy/telemetry-parser and "pip install telemetry-parser"
    """
    dlen = 20  # or the other data length as per your format
    ffprobe_file = os.path.join(RAW_DIR, video_basename + '.ffprobe')
    imu_file = os.path.join(RAW_DIR, video_basename + '.imu')
    telemetry_file = os.path.join(RAW_DIR, video_basename + '.telemetry')

    if not os.path.exists(ffprobe_file):
        LOG.logger.warning(f"Missing ffprobe file {ffprobe_file}")
        return
    
    if not os.path.exists(imu_file):
        LOG.logger.warning(f"Missing imu file {imu_file}")
        return
    
    if not os.path.exists(telemetry_file):
        LOG.logger.warning(f"Missing telemetry file {telemetry_file}")
        return
    
    imu_times, accel, gyro = read_binary_imu_data(imu_file, dlen)
    ffm = read_json_file(ffprobe_file)
    telemetry = read_json_file(telemetry_file)

    video_duration = float(ffm['format']['duration'])
    first_frame_time = float(telemetry['first_frame_timestamp'])/1000.0
    LOG.logger.debug(f"FFprobe duration: {video_duration} seconds")
    
    index = np.searchsorted(imu_times, first_frame_time, side='left')

    LOG.logger.debug(f"IMU start: {imu_times[0]}, IMU end: {imu_times[-1]}")
    LOG.logger.debug(f"First frame time: {first_frame_time}")

    LOG.logger.debug(f"Index: {index}")
    LOG.logger.debug(f"Time: {imu_times[index]}")

    imu_times = imu_times[index:]
    imu_times = (imu_times - imu_times[0]) / 1000.0
    accel = accel[index:]
    gyro = gyro[index:]

    accel, gyro = rearrage_imu_data(accel, gyro)
    return imu_times, accel, gyro


# Pose file format v2: 32-byte header + int16 payload.
#   bytes  0..3  : magic b'PS02'
#   bytes  4..7  : flags uint32 LE (bit 0 = delta-encoded)
#   bytes  8..11 : rows  uint32 LE
#   bytes 12..15 : cols  uint32 LE (always 4)
#   bytes 16..19 : msec_per_sample uint32 LE
#   bytes 20..23 : multiplier      uint32 LE
#   bytes 24..31 : reserved (zero)
# Legacy v1 format was 4 × uint64 LE = (rows, cols, msec, multiplier) then int16 payload.
# Readers detect format by the first 4 bytes: 'PS02' => v2, else => v1.
POSE_MAGIC_V2 = b'PS02'
POSE_FLAG_DELTA = 1

def write_pose_file(file_name, poses, msec_per_sample=33, multiplier=32767):
    """
    Take a series of poses stored as quaternions and write them to a binary file
    using format v2 (delta-encoded int16 quaternions). The on-disk file is
    uncompressed; brotli compression happens at upload time in
    Scripts/race_upload.py::_upload_poses for transport.
    """
    # select every nth pose
    poses = poses[::msec_per_sample] * multiplier
    # permute the columns of the quaternion to line things up with the web viewer.  Moving to the
    # WebGL standard, where x is right, y is up, and z is backward, satisfying the right hand rule.
    # The camera is down, back, and right.
    poses = (poses[:, [2, 0, 1, 3]] * np.array([1, -1, 1, 1])).astype(np.int16)
    rows, cols = poses.shape
    # Delta-encode: row 0 absolute, subsequent rows as differences. int16 modular
    # arithmetic is its own inverse so the reader can cumsum back without overflow care.
    deltas = poses.copy()
    deltas[1:] = poses[1:] - poses[:-1]
    LOG.logger.debug(f"Shape of poses: {poses.shape}")
    # Atomic write: stage to a hidden .tmp in the same dir, then os.replace.
    # try/except BaseException so SIGINT mid-write removes the .tmp instead
    # of leaving a stranded scratch file alongside the (possibly stale) dst.
    fd, tmp = tempfile.mkstemp(
        dir=os.path.dirname(file_name),
        prefix="." + os.path.basename(file_name) + ".tmp_",
    )
    try:
        with os.fdopen(fd, 'wb') as f:
            LOG.logger.debug(f'Writing {rows} poses to {file_name}')
            f.write(POSE_MAGIC_V2)
            hdr = np.array([POSE_FLAG_DELTA, rows, cols, msec_per_sample, multiplier],
                           dtype=np.uint32)
            f.write(hdr.tobytes())
            f.write(b'\x00' * 8)  # 8 reserved bytes
            f.write(deltas.tobytes())
        os.replace(tmp, file_name)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise

def compute_video_poses(video_basename, recompute = False, shift=0):
    """
    Compute the poses (orientation) of a video based on IMU data.
    Args:
        video_basename (str): The base name of the video file.

    Compute the poses (orientation) of a video based on IMU data.

    Args:
        video_basename (str): The base name of the video file.
    """
    pose_file = os.path.join(VIDEO_DIR, video_basename + '.poses')

    if not recompute and os.path.exists(pose_file):
        LOG.logger.info(f"Skipping: {video_basename} already exists, skipping.")
        return

    LOG.logger.info(f"Creating poses for {video_basename}")
    imu = read_insta_imu_data(video_basename, shift=shift)
    if imu is None:
        # Missing .ffprobe / .imu / .telemetry sidecar. Callers (e.g.
        # regen_poses.py) can catch and decide whether to re-extract and
        # retry. Raising here is clearer than the NoneType unpack error.
        raise FileNotFoundError(
            f"IMU sidecar files missing for {video_basename}; "
            f"run extract_video_metadata() to regenerate them")
    imu_times, accel, gyro = imu
    q_initial = geo.q_identity()
    q_poses = geo.estimate_pose(q_initial, accel, gyro, accel_gain=0.0005, gyro_gain=0.9)

    write_pose_file(pose_file, q_poses)


def compute_all_pose_files(recompute=False):
    """
    Compute pose files for videos in the video directory.
    .imu sidecars live in RAW_DIR alongside the .insv they were extracted from.
    """
    for file in os.listdir(RAW_DIR):
        if re.match("^VID.*imu$", file):
            basename = os.path.splitext(file)[0]
            LOG.logger.info(f"Processing {basename}")
            compute_video_poses(basename, recompute=recompute)

##################################################################################################
# Read the boat brain logs, sync to the video, and the write out log files for the web viewer.


def read_parquet_files():
    """
    Read all the parquet files in the directory and concatenate them into a single dataframe.
    """
    dfs = []
    for file in sorted(os.listdir(PANDAS_DIRECTORY)):
        if re.match("^.*.parquet$", file):
            LOG.logger.info(f"Reading {file}")
            result = race_logs.read_log_file(os.path.join(PANDAS_DIRECTORY, file))
            if result is not None:
                dfs.append(result)
    return pd.concat(dfs)

def log_extract_slice(df, dt_start, dt_end):
    sdf = df[(df['row_times'] >= dt_start) & (df['row_times'] <= dt_end)]
    if len(sdf) == 0:
        # Empty windows are routine when probing across historical date ranges
        # in find_starts; demoted from WARNING to keep the race_upload run
        # summary readable.
        LOG.logger.debug(f"No data found between {dt_start} and {dt_end}")
    cdf = sailview_log_v1.df_convert(sdf)
    cdf.row_seconds = cdf.row_seconds - cdf.row_seconds.iloc[0]
    return cdf, sdf

def extract_video_logs(video_basename, boatbrain_df, offset=30):
    dlen = 20  # or the other data length as per your format
    ffprobe_file = os.path.join(RAW_DIR, video_basename + '.ffprobe')

    # if not os.path.exists(ffprobe_file):
    #     LOG.logger.warning(f"Missing ffprobe file {ffprobe_file}")
    #     return
    
    ffm = read_json_file(ffprobe_file)

    creation_time = ffm['format']['tags']['creation_time']
    creation_dt = datetime.strptime(creation_time, "%Y-%m-%dT%H:%M:%S.%fZ")
    # convert to pacific time
    creation_dt = pytz.timezone('US/Pacific').localize(creation_dt)
    LOG.logger.debug(f"Creation time: {creation_dt}")

    video_duration = float(ffm['format']['duration'])
    LOG.logger.debug(f"Duration: {video_duration} seconds")

    video_start = creation_dt - pd.Timedelta(seconds=offset)
    video_end = video_start + pd.Timedelta(seconds=video_duration+2*offset)
    LOG.logger.debug(f"Video start: {video_start}, Video end: {video_end}")

    cdf, _ = log_extract_slice(boatbrain_df, video_start, video_end)
    return cdf, video_start, video_end, pd.Timedelta(seconds=video_duration)


# A trick to sync the video and log data!

# The BB data is on GPS time (very accurate).  The Insta360 video has several problems.  The camera
# time seems inaccurate, and the various timestamps appear to be inconsistent (perhpas 15 seconds)?
#
# We have a trick up our sleeves.  The gyro and the compass **both** measure the rotation of the
# camera.  Using this joint information we can solve for the best sync.


def sync_video_to_bb(video_df, gyro, max_delay=None):
    """
    Synchronize video data with BoatBrain (BB) data by correlating heading changes.
    
    This function uses the fact that both the video's compass heading and the gyroscope 
    measure rotation around the vertical axis. By correlating the rate of change of heading
    with the gyroscope data, we can find the time offset between the two signals.

    Args:
        video_df (pd.DataFrame): DataFrame containing video metadata, must have 'hdg' column
                                with compass heading in degrees
        gyro (np.ndarray): Raw gyroscope data array, sampled at 1000Hz
                          Assumes first column is rotation around vertical axis
        max_delay (float, optional): Maximum allowed delay in seconds. If None, searches
                                    the full correlation range. If specified, limits search
                                    to [0, max_delay] seconds to prevent false matches.

    Returns:
        tuple:
            float: Time delay in seconds between video and BB data
            np.ndarray: Normalized heading rate of change signal
            np.ndarray: Normalized gyroscope signal (downsampled to 10Hz)
            
    How the correlation works:
    1. We compute the rate of change of heading (dhdg) from the video data
    2. We downsample the gyro data from 1000Hz to 10Hz to match video sampling rate
    3. Both signals are normalized to zero mean and unit standard deviation
    4. np.correlate(ngg, nhdg, mode='valid') computes the cross-correlation:
       - This slides the gyro signal (ngg) across the heading signal (nhdg)
       - At each position, it computes the dot product between overlapping samples
       - Higher correlation values indicate better alignment between the signals
    5. np.argmax(match) finds the position where correlation is maximum
    6. This position represents the number of samples the gyro signal needs to be shifted
       to align with the heading signal
    7. delay/10 converts from sample index to seconds (since we're at 10Hz)
    
    The returned delay tells us how much to shift the video data forward in time
    to align with the BoatBrain data. If delay=5.0, the video starts 5 seconds
    before the corresponding BoatBrain data.
    """
    # Compute the derivative of the hdg, that is the rate of change of the heading.
    dhdg = np.diff(video_df.hdg, prepend=video_df.hdg.iloc[0])
    # Handle wraparound at 360 degrees
    dhdg[dhdg > 180] -= 360
    dhdg[dhdg < -180] += 360

    def normalize(signal):
        """Normalize signal to zero mean and unit standard deviation"""
        return (signal - np.mean(signal)) / np.std(signal)

    # Downsample the gyro data to 10Hz (from 1000Hz).  Axis 1 is the vertical axis.
    sgg = gyro[::100, 0]  # Take every 100th sample
    xgg = np.linspace(0, sgg.shape[0]/10, len(sgg))

    # Normalize both signals for correlation
    ngg = normalize(sgg)
    nhdg = normalize(dhdg)

    # Find the delay that maximizes correlation between signals
    # np.correlate slides ngg across nhdg, computing dot product at each position
    # Higher values indicate better alignment between the two signals
    match = np.correlate(nhdg, 1*ngg, mode='valid')
    
    if max_delay is not None:
        # Limit the search range to [0, max_delay] seconds
        max_samples = int(max_delay * 10)  # Convert seconds to samples at 10Hz
        search_range = min(max_samples, len(match))
        delay = np.argmax(match[:search_range])
        LOG.logger.debug(f"Limited correlation search to {max_delay}s ({search_range} samples)")
    else:
        delay = np.argmax(match)

    return delay/10, nhdg, ngg

# DEPRECATED: Use align_video() and apply_sync_logs() instead.
# This function uses a capped max_delay search which can miss the true alignment.
def extract_bb_logs_for_video(basename, big_df, offset=30, shift=0, recompute=False):
    """DEPRECATED: Use align_video() and apply_sync_logs() instead."""
    import warnings
    warnings.warn(
        "extract_bb_logs_for_video is deprecated. Use align_video() and apply_sync_logs() instead.",
        DeprecationWarning, stacklevel=2
    )
    log_file = os.path.join(VIDEO_DIR, basename + '.log')

    if not recompute and os.path.exists(log_file):
        LOG.logger.info(f"Skipping {basename} already exists.")
        return

    video_df, video_start, video_end, video_duration = extract_video_logs(basename, big_df, offset=offset)
    LOG.logger.info(f"Video start: {video_start}, Video end: {video_end}")

    time_stamps, accel, gyro = read_insta_imu_data(basename, shift=shift)
    delay, nhdg, ngg = sync_video_to_bb(video_df, gyro, max_delay=offset)    

    LOG.logger.info(f"Offset: {offset}, Delay: {delay}, Delta: {offset - delay}")

    # slice_start, slice_end = video_start + pd.Timedelta(seconds=delay) , video_end
    slice_start = video_start + pd.Timedelta(seconds=delay)
    slice_end = slice_start + video_duration
    video_df, tdf = log_extract_slice(big_df, slice_start, slice_end)

    LOG.logger.info(f"Video start: {slice_start}, Video end: {slice_end}")    

    sailview_log_v1.save_log_to_binary(video_df, log_file)

    return video_df, log_file, nhdg, ngg

# DEPRECATED: Use sync_videos_to_bb() instead.
# This function uses a capped max_delay search which can miss the true alignment.
def extract_all_bb_logs(big_df, offset=30, recompute=False):
    """DEPRECATED: Use sync_videos_to_bb() instead."""
    import warnings
    warnings.warn(
        "extract_all_bb_logs is deprecated. Use sync_videos_to_bb() instead.",
        DeprecationWarning, stacklevel=2
    )
    for file in os.listdir(RAW_DIR):
        if re.match("^VID.*_00_.*insv$", file):
            basename = os.path.splitext(file)[0]
            LOG.logger.info(f"Processing {basename}")
            try:
                extract_bb_logs_for_video(basename, big_df, offset=offset, recompute=recompute)
            except Exception as e:
                LOG.logger.error(f"Failed to process {basename}: {e}")


##################################################################################################
# Setup web viewer files.

# Used as the friendly name for the video.
# Prefers the corrected creation_time from the .ffprobe sidecar (which may have
# been fixed for camera clock errors).  Falls back to parsing the filename.
def extract_date_from_video(video_name):
    basename = os.path.splitext(video_name)[0]
    ffprobe_file = os.path.join(RAW_DIR, basename + '.ffprobe')
    if os.path.exists(ffprobe_file):
        try:
            ffm = read_json_file(ffprobe_file)
            ct = ffm['format']['tags']['creation_time']
            dt = datetime.strptime(ct, "%Y-%m-%dT%H:%M:%S.%fZ")
            local_dt = pytz.timezone('US/Pacific').localize(dt)
            return local_dt.strftime("%Y/%m/%d %H:%M:%S")
        except Exception:
            pass
    parts = video_name.split('_')
    date = parts[1]
    time = parts[2]
    dt = datetime.strptime(date + time, "%Y%m%d%H%M%S")
    local_dt = pytz.timezone('US/Pacific').localize(dt)
    return local_dt.strftime("%Y/%m/%d %H:%M:%S")

def _read_slog_start_unix_ms(slog_path: str):
    """Return GPS-aligned start_unix_ms from a v2 .slog header, or None.

    The v2 header is `<IHHIIdfH` (30 bytes); start_unix_ms is the float64
    at offset 16. See Libraries/sailview_log_v2.py for the full layout.
    """
    try:
        with open(slog_path, 'rb') as f:
            head = f.read(24)
        if len(head) != 24:
            return None
        return float(struct.unpack('<d', head[16:24])[0])
    except (OSError, struct.error):
        return None


# Create a json file that acts as an index for the web viewer.  It lists all videos
# and their available metadata files.
def create_videos_json():
    # Tidy up any accidental-tap / failed-capture clips (e.g. a 4-second
    # mp4 that survived compression but is too short to be useful)
    # before listing. Moved to SMALL_DIR so they drop out of the picker
    # and the upload glob; the local file is preserved for forensics.
    clean_too_short_videos()
    # ^(VID|VIA)_.*mp4$ — VID_* are real Insta360 clips, VIA_* are
    # synthesized audio-only placeholders for races where the camera
    # wasn't running (see Libraries/blank_video.py).
    video_files = [file for file in os.listdir(VIDEO_DIR)
                   if re.match(r"^(VID|VIA)_.*mp4$", file)]
    json_rows = []
    for file in sorted(video_files):
        row = {}
        basename = os.path.splitext(file)[0]
        pose_file = basename + '.poses'
        log_file = basename + '.log'
        slog_file = basename + '.slog'
        row['SourceFile'] = file
        if basename.startswith("VIA_"):
            row['AudioOnly'] = True
            # VIA_ clips are 2:1 equirect black frames with an "Audio
            # Only" overlay at the center; using fisheye projection
            # warps the text onto a curved hemisphere and renders it
            # illegibly. equirect lays the source flat ahead, mildly
            # stretched but readable. See Libraries/blank_video.py.
            row['projection'] = 'equirect'
        if os.path.exists(os.path.join(VIDEO_DIR, pose_file)):
            row['PoseFile'] = pose_file
        if os.path.exists(os.path.join(VIDEO_DIR, log_file)):
            row['LogFile'] = log_file
        slog_path = os.path.join(VIDEO_DIR, slog_file)
        if os.path.exists(slog_path):
            row['SlogFile'] = slog_file
            start_ms = _read_slog_start_unix_ms(slog_path)
            if start_ms is not None and start_ms > 0:
                # GPS-aligned wall-clock anchor for this clip's timeline,
                # computed by sync_video_to_bb (heading-vs-gyro
                # cross-correlation). Camera EXIF CreateDate (below) drifts
                # ~tens of seconds vs GPS; web clients should prefer
                # StartUnixMs for UTC↔video-offset math.
                row['StartUnixMs'] = start_ms
        row['CreateDate'] = extract_date_from_video(basename)
        json_rows.append(row)

    # Self-repair: splice in VIRB-era equirect entries that live only on
    # S3. The 2020-2023 V*.mp4 corpus (>1 TiB) isn't duplicated locally,
    # but its records must remain in videos.json or the frontend can't
    # find them. Pulled fresh from S3 every call, so a prior clobber heals
    # itself on the next race_upload — no manual restore step.
    local_sources = {r['SourceFile'] for r in json_rows}
    json_rows.extend(_legacy_virb_records(local_sources))

    # Sort by CreateDate ascending and (re-)assign ids. Post-VIRB-migration
    # invariant: VIRB (2020) lands at id=0 and the latest Insta360 clip at
    # the tail. Sort key normalizes both EXIF colon-form ('2020:04:02 ...')
    # and modern slash-form ('2026/04/29 ...') — same transform as the
    # frontend's normalizeCreateDate() in Src/src/js/viewer360.js.
    json_rows.sort(key=lambda r: _normalize_create_date(r.get('CreateDate')))
    for i, row in enumerate(json_rows):
        row['id'] = i

    with open(os.path.join(VIDEO_DIR, 'videos.json'), 'w') as f:
        # save in a pretty format
        videos = {}
        videos['videos'] = json_rows
        videos['columns'] = sailview_log_v1.column_dict()
        json.dump(videos, f, indent=4)
    return json_rows


def _normalize_create_date(cd: Optional[str]) -> str:
    """Python twin of Src/src/js/viewer360.js::normalizeCreateDate.

    Maps both '2026/04/08 19:13:48' (modern Insta360 slash-form) and
    '2020:04:02 20:35:56' (EXIF colon-form, VIRB-era manifest) to
    '2026-04-08 19:13:48' so the two coexist in a lexicographic sort.
    """
    if not cd or len(cd) < 10:
        return cd or ''
    return cd[:10].replace('/', '-').replace(':', '-') + cd[10:]


# Caches for _legacy_virb_records — race_upload calls create_videos_json
# 2-3 times per run; we want one s5cmd ls and one manifest fetch, not three.
_LEGACY_VIRB_S3_KEYS: Optional[set] = None
_LEGACY_VIRB_MANIFEST: Optional[dict] = None
_LEGACY_VIRB_VIDEO_RE = re.compile(r"^V\d+\.mp4$")
_LEGACY_MANIFEST_S3_URL = "s3://regattaanalysis/www/Logs/video_poses_logs.json"


def _legacy_virb_records(local_sourcefiles: set) -> list:
    """Return videos.json-shape records for VIRB-era V*.mp4 clips on S3.

    These records live only on S3; the 1.1 TiB legacy MP4 corpus isn't
    duplicated locally. Called on every create_videos_json() pass so the
    output is self-repairing — any prior splice loss heals on the next
    race_upload run.

    Failure modes (no AWS access, manifest gone, etc.) log a warning and
    return [] so an offline race_upload still produces a usable
    fisheye-only videos.json. The next online run repairs it.
    """
    global _LEGACY_VIRB_S3_KEYS, _LEGACY_VIRB_MANIFEST
    try:
        if _LEGACY_VIRB_S3_KEYS is None:
            _LEGACY_VIRB_S3_KEYS = _list_s3_objects(INSTA360_S3_PREFIX)
        if _LEGACY_VIRB_MANIFEST is None:
            _LEGACY_VIRB_MANIFEST = _fetch_legacy_virb_manifest()
    except Exception as e:
        LOG.logger.warning(f"legacy VIRB splice skipped: {e}")
        return []

    # Manifest is keyed by the original uppercase '.MP4' SourceFile; the
    # migration server-side copied those to lowercase '.mp4' under
    # Insta360/. Build a basename -> CreateDate lookup once.
    create_by_basename = {}
    for v in _LEGACY_VIRB_MANIFEST.get('videos', []):
        sf = v.get('SourceFile') or ''
        cd = v.get('CreateDate')
        if sf.endswith('.MP4') and cd:
            create_by_basename[sf[:-4]] = cd

    rows = []
    for key in _LEGACY_VIRB_S3_KEYS:
        if not _LEGACY_VIRB_VIDEO_RE.match(key):
            continue
        if key in local_sourcefiles:
            # Local-on-disk wins; keep this record in the locally-built list.
            continue
        base = key[:-4]
        cd = create_by_basename.get(base)
        if not cd:
            # Without a CreateDate the picker can't place the clip; skip
            # rather than emit a broken row.
            continue
        row = {
            'SourceFile': key,
            'CreateDate': cd,
            'projection': 'equirect',
        }
        if (base + '.poses') in _LEGACY_VIRB_S3_KEYS:
            row['PoseFile'] = base + '.poses'
        if (base + '.log') in _LEGACY_VIRB_S3_KEYS:
            row['LogFile'] = base + '.log'
        if (base + '.slog') in _LEGACY_VIRB_S3_KEYS:
            row['SlogFile'] = base + '.slog'
        rows.append(row)
    return rows


def _fetch_legacy_virb_manifest(
    local_path: str = "/tmp/video_poses_logs.json",
) -> dict:
    """Fetch the preserved legacy VIRB manifest from S3.

    Intentionally kept after the 2026-05-02 prefix cleanup
    (`docs/VIRB_MIGRATION.md` §Cleanup) so post-hoc splices like
    _legacy_virb_records can still find CreateDate values. ~1.2 MiB.
    """
    if not os.path.exists(local_path):
        cmd = ["aws", "s3", "cp", _LEGACY_MANIFEST_S3_URL, local_path]
        env = os.environ.copy()
        env["AWS_PROFILE"] = AWS_PROFILE
        result = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if result.returncode != 0:
            raise RuntimeError(
                f"fetch legacy VIRB manifest failed: {result.stderr.strip()}"
            )
    with open(local_path) as f:
        return json.load(f)


##################################################################################################
# Pipelined video processing - copy, compress, and upload run concurrently
##################################################################################################

from concurrent.futures import ThreadPoolExecutor, as_completed
from queue import Queue
from threading import Thread, Event
import time

def _copy_single_file(src_path, dst_dir):
    """Copy a single file from SD card to local directory.

    Raises IOError (after deleting the partial) if shutil.copy2 returns
    a short file — defense in depth against silent truncation. The card
    wipe gate also re-verifies sizes by re-listing the card, but failing
    loudly here gives a per-file error message instead of a bare
    "size mismatch" log line.
    """
    filename = os.path.basename(src_path)
    dst_path = os.path.join(dst_dir, filename)
    src_size = os.path.getsize(src_path)
    if os.path.exists(dst_path) and os.path.getsize(dst_path) == src_size:
        LOG.logger.debug(f"Skipping {filename}, already exists with same size")
        return filename, True
    LOG.logger.info(f"Copying {filename}")
    shutil.copy2(src_path, dst_path)
    dst_size = os.path.getsize(dst_path)
    if dst_size != src_size:
        try:
            os.unlink(dst_path)
        except FileNotFoundError:
            pass
        raise IOError(
            f"short copy {filename}: src={src_size} dst={dst_size}"
        )
    return filename, True

def _get_output_filename(video_file):
    """Get the output mp4 filename for a given input file."""
    return os.path.splitext(video_file)[0] + ".mp4"

def _compression_needed(video_file):
    """Check if compression is needed for this file."""
    output_file = os.path.join(VIDEO_DIR, _get_output_filename(video_file))
    return not os.path.exists(output_file)

def _upload_single_file(local_path, s3_prefix="s3://regattaanalysis/www/Insta360/"):
    """Upload a single file to S3 using s5cmd."""
    filename = os.path.basename(local_path)
    s3_path = s3_prefix + filename
    cmd = f"AWS_PROFILE={AWS_PROFILE} s5cmd cp {local_path} {s3_path}"
    LOG.logger.info(f"Uploading {filename}")
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        LOG.logger.error(f"Upload failed for {filename}: {result.stderr}")
        return filename, False
    return filename, True

def process_videos_pipelined(delete_after_compress=False, skip_upload=False):
    """
    Process videos with pipelined copy/compress/upload operations.
    
    This runs copy, compress, and upload concurrently:
    - As files are copied from SD card, they're queued for compression
    - As files are compressed, they're queued for upload
    - All three stages run in parallel using different resources
    
    Args:
        delete_after_compress: If True, delete raw .insv files after successful compression
        skip_upload: If True, skip the upload stage
    """
    card_mounted = os.path.exists(INSTA360_CARD_DIR)
    if not card_mounted:
        LOG.logger.warning(
            f"NO_INPUT: Insta360 card not mounted at {INSTA360_CARD_DIR} — "
            "processing existing local files only"
        )
        files_to_copy = []
    else:
        files_to_copy = [f for f in os.listdir(INSTA360_CARD_DIR)
                        if re.match(VIDEO_MATCH, f)]
        LOG.logger.info(f"Found {len(files_to_copy)} files on SD card")
    
    # Queues for pipeline stages
    compress_queue = Queue()
    upload_queue = Queue()
    stop_event = Event()
    
    # Track copied files to detect when pairs are ready
    copied_files = set()
    copied_files_lock = __import__('threading').Lock()
    # Primary filenames already handed to the compress queue. Both the
    # mid-copy queuer and the end-of-copy sweep run on the copy_worker
    # thread, so a plain set (no lock) dedups them against each other and
    # prevents a clip from being compressed twice.
    queued_primaries = set()
    
    # Stats tracking
    stats = {
        'copied': 0, 'compressed': 0, 'uploaded': 0,
        'copy_errors': 0, 'compress_errors': 0, 'upload_errors': 0
    }
    
    def copy_worker():
        """Copy files from SD card and queue for compression."""
        os.makedirs(RAW_DIR, exist_ok=True)
        for filename in files_to_copy:
            src_path = os.path.join(INSTA360_CARD_DIR, filename)
            try:
                _, success = _copy_single_file(src_path, RAW_DIR)
                if success:
                    stats['copied'] += 1
                    with copied_files_lock:
                        copied_files.add(filename)
                        _queue_ready_for_compression(filename, copied_files, compress_queue)
            except Exception as e:
                LOG.logger.error(f"Copy failed for {filename}: {e}")
                stats['copy_errors'] += 1
        
        # Also queue any existing files that need compression
        _queue_existing_files_for_compression(compress_queue)
        compress_queue.put(None)  # Signal end of copy stage
    
    def _enqueue(queue, kind, primary, secondary):
        """Put a compression job on the queue at most once per primary file."""
        if primary in queued_primaries:
            return
        queued_primaries.add(primary)
        queue.put((kind, primary, secondary))
        LOG.logger.debug(f"Queued {kind} for compression: {primary}")

    def _queue_ready_for_compression(new_file, all_copied, queue):
        """Queue a clip for compression as soon as it's known-ready.

        A `_00_` clip is ambiguous: in dual-*file* mode it's the left half
        of a pair whose `_10_` partner is copied separately; in single-file
        dual-fisheye mode it's a complete frame with no partner. Disambiguate
        against the card manifest (`files_to_copy`): if no `_10_` partner is
        on the card and none has been copied, the clip is solo and can stream
        into compression now instead of waiting for the end-of-copy sweep.
        """
        if re.match(LEFT_VIDEO_MATCH, new_file):
            right_file = re.sub("_00_", "_10_", new_file)
            right_present = (right_file in all_copied
                             or os.path.exists(os.path.join(RAW_DIR, right_file)))
            if right_present:
                if _compression_needed(new_file):
                    _enqueue(queue, 'pair', new_file, right_file)
            elif right_file not in files_to_copy:
                # No partner on the card -> genuine single-file dual-fisheye.
                if _compression_needed(new_file):
                    _enqueue(queue, 'solo', new_file, None)
            # else: partner is on the card but not copied yet; wait for it.
        elif re.match(RIGHT_VIDEO_MATCH, new_file):
            left_file = re.sub("_10_", "_00_", new_file)
            left_present = (left_file in all_copied
                            or os.path.exists(os.path.join(RAW_DIR, left_file)))
            if left_present:
                if _compression_needed(left_file):
                    _enqueue(queue, 'pair', left_file, new_file)
        else:
            # Solo file (no _00_/_10_ lens marker at all).
            if _compression_needed(new_file):
                _enqueue(queue, 'solo', new_file, None)
    
    def _queue_existing_files_for_compression(queue):
        """End-of-copy safety sweep for clips not already streamed above."""
        # Check for solo files
        for video in find_solo_video_files():
            if _compression_needed(video):
                _enqueue(queue, 'solo', video, None)
        # Check for paired files
        for left, right in find_paired_files():
            if _compression_needed(left):
                _enqueue(queue, 'pair', left, right)
    
    def compress_worker():
        """Compress videos and queue for upload."""
        while True:
            item = compress_queue.get()
            if item is None:
                break
            
            file_type, file1, file2 = item
            try:
                if file_type == 'solo':
                    handbrake_recompress(file1)
                    output_file = _get_output_filename(file1)
                    raw_files = [file1]
                else:
                    ffmpeg_recompress((file1, file2))
                    output_file = _get_output_filename(file1)
                    raw_files = [file1, file2]
                
                stats['compressed'] += 1
                output_path = os.path.join(VIDEO_DIR, output_file)
                
                if os.path.exists(output_path):
                    if not skip_upload:
                        upload_queue.put(output_path)
                    
                    if delete_after_compress:
                        for raw in raw_files:
                            raw_path = os.path.join(RAW_DIR, raw)
                            if os.path.exists(raw_path):
                                os.remove(raw_path)
                                LOG.logger.info(f"Deleted raw file: {raw}")
            except Exception as e:
                LOG.logger.error(f"Compression failed for {file1}: {e}")
                stats['compress_errors'] += 1
        
        upload_queue.put(None)  # Signal end of compress stage
    
    def upload_worker():
        """Upload compressed videos to S3."""
        if skip_upload:
            # Drain the queue
            while upload_queue.get() is not None:
                pass
            return
            
        while True:
            item = upload_queue.get()
            if item is None:
                break
            try:
                _, success = _upload_single_file(item)
                if success:
                    stats['uploaded'] += 1
                else:
                    stats['upload_errors'] += 1
            except Exception as e:
                LOG.logger.error(f"Upload failed for {item}: {e}")
                stats['upload_errors'] += 1
    
    # Start pipeline threads
    LOG.logger.info("Starting pipelined video processing")
    start_time = time.time()
    
    copy_thread = Thread(target=copy_worker, name="CopyWorker")
    compress_thread = Thread(target=compress_worker, name="CompressWorker")
    upload_thread = Thread(target=upload_worker, name="UploadWorker")
    
    copy_thread.start()
    compress_thread.start()
    upload_thread.start()
    
    # Wait for all stages to complete
    copy_thread.join()
    compress_thread.join()
    upload_thread.join()
    
    # Clean up SD card after successful copy. Two gates must hold:
    #   1. No per-file copy exception was logged (stats['copy_errors'] == 0).
    #   2. Every file currently on the card has a same-size local sibling
    #      under RAW_DIR. We re-list the card here rather than trusting
    #      the in-memory bookkeeping so a file that appeared after our
    #      initial listdir, a silently-truncated copy, or any bookkeeping
    #      bug causes a refusal-to-wipe instead of data loss. If anything
    #      is off, the card is left mounted and a loud ERROR is logged;
    #      re-run race_upload to retry the copy.
    if not card_mounted:
        card_report.record("Insta360", mounted=False)
        card_report.set_note("Insta360", "not mounted — processed existing local files only")
    elif not files_to_copy:
        card_report.record("Insta360", mounted=True, files_found=0)
        card_report.set_note("Insta360", "mounted, 0 new files — already ingested; left mounted (not ejected)")
    else:
        card_report.record("Insta360", mounted=True, files_found=len(files_to_copy),
                           copied=stats['copied'], copy_errors=stats['copy_errors'])
        if stats['copy_errors'] != 0:
            LOG.logger.error(
                f"Refusing to wipe Insta360 card: "
                f"{stats['copy_errors']} copy error(s) during this run. "
                f"Card left mounted; re-run race_upload to retry."
            )
            card_report.set_note("Insta360", f"mounted, {len(files_to_copy)} files, COPY FAILED ({stats['copy_errors']} errors) — card left mounted")
            card_report.add_problem("Insta360", f"copy failed ({stats['copy_errors']} errors); card left mounted, re-run to retry")
        else:
            _verify_and_wipe_insta360_card()
    
    elapsed = time.time() - start_time
    LOG.logger.info(f"Pipeline complete in {elapsed:.1f}s - "
                   f"Copied: {stats['copied']}, Compressed: {stats['compressed']}, "
                   f"Uploaded: {stats['uploaded']}, "
                   f"Errors: {stats['copy_errors']}/{stats['compress_errors']}/{stats['upload_errors']}")
    
    return stats


##################################################################################################
# Video-to-BoatBrain Synchronization
#
# These functions handle the full sync pipeline:
# 1. Coarse clock offset detection (handles camera clock errors)
# 2. Metadata correction (.ffprobe / .exif files)
# 3. Fine per-video alignment (uncapped correlation search)
# 4. Validation and reporting
# 5. Writing .log files
##################################################################################################

TIMEZONE = "US/Pacific"
BOUNDARY_FRACTION = 0.05
WEAK_RATIO = 4.0
CLOCK_ERROR_THRESHOLD = 30.0  # seconds -- correct metadata if offset exceeds this
MAX_EXISTING_LOG_START_DELTA_SECONDS = 60.0
SEARCH_STAGES = [60, 1800, 7200, 21600]  # Widening search stages (seconds)


@dataclass
class VideoInfo:
    """Metadata for a video file."""
    basename: str
    ffprobe_creation_time: Optional[datetime] = None
    duration_seconds: float = 0.0

    def filename_time(self) -> datetime:
        parts = self.basename.split("_")
        dt = datetime.strptime(parts[1] + parts[2], "%Y%m%d%H%M%S")
        return pytz.timezone(TIMEZONE).localize(dt)


@dataclass
class SyncReport:
    """Result of aligning a single video."""
    basename: str
    delay: float = 0.0
    corr_peak: float = 0.0
    corr_mean: float = 0.0
    bb_rows: int = 0
    status: str = "PENDING"
    error: str = ""


def list_videos_for_sync(date: Optional[str] = None, video: Optional[str] = None) -> List[str]:
    """List video basenames matching date or specific video name.

    Sources from RAW_DIR where .insv lives; the basename is what
    downstream sync needs (it'll then look for .imu/.ffprobe in RAW_DIR
    and .log in VIDEO_DIR).
    """
    results = []
    for f in sorted(os.listdir(RAW_DIR)):
        if not re.match(r"^VID.*_00_.*\.insv$", f):
            continue
        basename = os.path.splitext(f)[0]
        if video and basename != video:
            continue
        if date and not f.startswith(f"VID_{date}_"):
            continue
        results.append(basename)
    return results


def read_video_info(basename: str) -> VideoInfo:
    """Read video metadata from .ffprobe sidecar file."""
    vi = VideoInfo(basename=basename)
    ffprobe_path = os.path.join(RAW_DIR, basename + ".ffprobe")
    if os.path.exists(ffprobe_path):
        ffm = read_json_file(ffprobe_path)
        ct = ffm["format"]["tags"]["creation_time"]
        vi.ffprobe_creation_time = pytz.timezone(TIMEZONE).localize(
            datetime.strptime(ct, "%Y-%m-%dT%H:%M:%S.%fZ")
        )
        vi.duration_seconds = float(ffm["format"]["duration"])
    return vi


def read_log_row_times_bounds(basename: str):
    """Return the first/last row_times in a clip .log as localized datetimes."""
    log_file = os.path.join(VIDEO_DIR, basename + ".log")
    if not os.path.exists(log_file):
        return None, None

    with open(log_file, "rb") as f:
        buf = f.read()

    rows, cols, _, multiplier = struct.unpack("<4i", buf[:16])
    if rows <= 0 or multiplier == 0:
        raise ValueError(f"Invalid log header for {log_file}")

    offset = 16
    length = 8 * cols
    max_vals = struct.unpack("<" + "d" * cols, buf[offset:offset + length])
    offset += length
    min_vals = struct.unpack("<" + "d" * cols, buf[offset:offset + length])
    offset += length

    row_times_idx = sailview_log_v1.column_dict()["row_times"]
    if row_times_idx >= cols:
        raise ValueError(f"{log_file} missing row_times column")

    col_length = 2 * rows
    col_offset = offset + col_length * row_times_idx
    first_raw = struct.unpack_from("<H", buf, col_offset)[0]
    last_raw = struct.unpack_from("<H", buf, col_offset + 2 * (rows - 1))[0]

    scale = (max_vals[row_times_idx] - min_vals[row_times_idx]) / float(multiplier)
    first_ms = first_raw * scale + min_vals[row_times_idx]
    last_ms = last_raw * scale + min_vals[row_times_idx]

    tz = pytz.timezone(TIMEZONE)
    first_dt = datetime.fromtimestamp(first_ms / 1000.0, tz=pytz.utc).astimezone(tz)
    last_dt = datetime.fromtimestamp(last_ms / 1000.0, tz=pytz.utc).astimezone(tz)
    return first_dt, last_dt


def _delay_sidecar_path(basename: str) -> str:
    # .log.delay is alignment-cache state for sync_videos_to_bb; it tracks
    # the validated delay that produced the corresponding .log. Internal
    # bookkeeping, never uploaded — lives in RAW_DIR.
    return os.path.join(RAW_DIR, basename + ".log.delay")


def _parquets_for_video_date(basename: str) -> List[str]:
    """Return parquet paths whose filename date matches the video's date.

    Video basename: VID_YYYYMMDD_HHMMSS_*.  Parquet basename:
    YYYY-MM-DDTHH_MM.parquet.  Multiple parquets per day are possible
    (logger restarted mid-day) so return the full list — sidecar
    invalidation considers all of them.
    """
    parts = basename.split("_")
    if len(parts) < 2 or len(parts[1]) != 8:
        return []
    date_iso = f"{parts[1][:4]}-{parts[1][4:6]}-{parts[1][6:8]}"
    pattern = os.path.join(PANDAS_DIRECTORY, f"{date_iso}T*.parquet")
    import glob as _glob
    return sorted(_glob.glob(pattern))


def _parquet_signature(basename: str) -> dict:
    """Map parquet basename → mtime for every parquet covering the video's date."""
    return {
        os.path.basename(p): os.path.getmtime(p)
        for p in _parquets_for_video_date(basename)
    }


def _read_cached_state(basename: str) -> Optional[dict]:
    """Read the .log.delay sidecar.

    Returns ``{"delay": float, "parquets": {name: mtime, ...}}`` or
    ``None`` if missing/unparseable. Accepts the legacy single-float
    format (delay only, no parquet signature) and returns it with an
    empty parquets dict so the caller treats it as "needs lazy
    migration" without losing the cached delay.
    """
    p = _delay_sidecar_path(basename)
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            raw = f.read().strip()
        if raw.startswith("{"):
            data = json.loads(raw)
            return {
                "delay": float(data["delay"]),
                "parquets": {k: float(v) for k, v in (data.get("parquets") or {}).items()},
            }
        # Legacy format: bare float written before parquet-mtime tracking.
        return {"delay": float(raw), "parquets": {}}
    except (OSError, ValueError, KeyError, json.JSONDecodeError):
        return None


def _read_cached_delay(basename: str) -> Optional[float]:
    """Convenience: just the delay component of the cached state."""
    state = _read_cached_state(basename)
    return None if state is None else state["delay"]


def _write_cached_state(basename: str, delay_s: float) -> None:
    """Persist alignment delay + parquet mtime signature.

    The delay alone keeps existing_log_is_valid stable across runs; the
    parquet signature lets it correctly invalidate when the upstream BB
    parquet has been rewritten (e.g. via recompute_logs.py).
    """
    state = {
        "delay": round(delay_s, 3),
        "parquets": _parquet_signature(basename),
    }
    with open(_delay_sidecar_path(basename), "w") as f:
        json.dump(state, f)


def existing_log_is_valid(
    basename: str,
    max_start_delta_seconds: float = MAX_EXISTING_LOG_START_DELTA_SECONDS,
) -> bool:
    """Return True when an existing clip .log is present and plausibly aligned.

    Three independent staleness signals — any of them invalidates:
      1) .log first-row time drifted from (CreateDate + cached_delay) by
         more than `max_start_delta_seconds` (someone rewrote the .log
         out-of-band, or the .ffprobe creation_time was corrected).
      2) Any parquet covering the video's date has an mtime newer than
         what the sidecar recorded (BB log was reprocessed; downstream
         alignment should be redone).
      3) A parquet that the sidecar tracked is now missing, or a new
         parquet for that date appeared (logger added/restarted).

    Missing sidecar (legacy .log on disk) → back-fill from the observed
    start and current parquet mtimes. Trusts the previously-written log
    as ground truth so the historical resync loop is broken on the next
    run; subsequent upstream changes are then caught by the signals
    above.
    """
    log_file = os.path.join(VIDEO_DIR, basename + ".log")
    if not os.path.exists(log_file):
        return False

    try:
        create_date = extract_date_from_video(basename)
        expected_start = pytz.timezone(TIMEZONE).localize(
            datetime.strptime(create_date, "%Y/%m/%d %H:%M:%S")
        )
        first_dt, _ = read_log_row_times_bounds(basename)
        if first_dt is None:
            return False
        observed_delay = (first_dt - expected_start).total_seconds()

        state = _read_cached_state(basename)
        if state is None:
            _write_cached_state(basename, observed_delay)
            return True

        # Signal 1: delay drift.
        delta_s = abs(observed_delay - state["delay"])
        if delta_s > max_start_delta_seconds:
            LOG.logger.warning(
                f"Existing log {basename}.log starts {observed_delay:.1f}s "
                f"from CreateDate (cached delay={state['delay']:.1f}s, "
                f"drift={delta_s:.1f}s); forcing resync."
            )
            return False

        # Signal 2/3: upstream parquet identity.
        cached_pq = state["parquets"]
        current_pq = _parquet_signature(basename)
        if not cached_pq:
            # Legacy sidecar with no parquet info — back-fill so future
            # runs catch upstream changes. Don't invalidate just because
            # we lacked tracking before.
            _write_cached_state(basename, state["delay"])
            return True
        if cached_pq != current_pq:
            added = sorted(set(current_pq) - set(cached_pq))
            removed = sorted(set(cached_pq) - set(current_pq))
            mutated = sorted(
                k for k in (set(cached_pq) & set(current_pq))
                if cached_pq[k] != current_pq[k]
            )
            LOG.logger.warning(
                f"Upstream parquet(s) changed for {basename}: "
                f"added={added} removed={removed} mtime_changed={mutated}; "
                f"forcing resync."
            )
            return False
        return True
    except Exception as e:
        LOG.logger.warning(f"Could not validate {basename}.log ({e}); forcing resync.")
        return False


def _normalize_signal(signal):
    """Normalize signal to zero mean, unit variance."""
    s = np.std(signal)
    if s == 0:
        return signal
    return (signal - np.mean(signal)) / s


##################################################################################################
# Tack-based synchronization
#
# Tacks and gybes are ~90-degree heading changes in 10-20 seconds — distinctive enough to
# match unambiguously between BB heading and video gyro, even across a 5-hour clock offset.
##################################################################################################

TACK_WINDOW_SECONDS = 15
TACK_STEADY_SECONDS = 30
TACK_STEADY_STD_MAX = 12     # degrees — heading must be this stable before/after
TACK_MIN_TURN_DEGREES = 60
TACK_MIN_GAP_SECONDS = 30
TACK_MATCH_TOLERANCE_SECONDS = 15


def detect_tacks_in_heading(heading, sample_rate=10,
                            window_seconds=TACK_WINDOW_SECONDS,
                            steady_seconds=TACK_STEADY_SECONDS,
                            steady_std_max=TACK_STEADY_STD_MAX,
                            min_turn=TACK_MIN_TURN_DEGREES,
                            min_gap_seconds=TACK_MIN_GAP_SECONDS):
    """Detect tack/gybe events in a heading time series.

    Unwraps heading to avoid 0/360 artifacts, requires steady sailing before
    and after each turn (inspired by find_hdg_manuevers in find_tacks.py).

    Args:
        heading: 1-D array of heading in degrees (0-360 or already unwrapped).
        sample_rate: samples per second (10 for BB data).
        window_seconds: sliding window for measuring heading change.
        steady_seconds: window for steady-heading detection.
        steady_std_max: max rolling std (degrees) to count as steady.
        min_turn: minimum absolute heading change to count as a tack.
        min_gap_seconds: suppress duplicate detections closer than this.

    Returns:
        List of (sample_index, heading_change_degrees) tuples.
    """
    heading = np.asarray(heading, dtype=float)
    if len(heading) < sample_rate * 10:
        return []

    # Unwrap to remove 0/360 discontinuities
    unwrapped = np.degrees(np.unwrap(np.radians(heading)))

    steady_win = int(steady_seconds * sample_rate)
    hdg_series = pd.Series(unwrapped)

    # Rolling std to find periods of steady heading
    rolling_std = hdg_series.rolling(window=steady_win, center=True, min_periods=steady_win // 2).std()
    is_steady = (rolling_std < steady_std_max).values

    # Smoothed heading for computing delta across a maneuver
    smooth_win = int(steady_seconds * sample_rate)
    smoothed = hdg_series.rolling(window=smooth_win, center=True, min_periods=smooth_win // 2).mean().values

    # Heading change: compare smoothed heading window_seconds apart
    delta_samples = int(window_seconds * sample_rate * 3)
    delta = np.zeros(len(smoothed))
    half = delta_samples // 2
    delta[half:-half] = smoothed[delta_samples:] - smoothed[:-delta_samples]

    # A maneuver requires: was steady, is NOT steady now, will be steady again
    maneuver_buf = int(min_gap_seconds * sample_rate // 2)
    was_steady = pd.Series(is_steady.astype(float)).rolling(
        window=maneuver_buf, center=False, min_periods=1).max().values > 0.5
    will_steady = pd.Series(is_steady[::-1].astype(float)).rolling(
        window=maneuver_buf, center=False, min_periods=1).max().values[::-1] > 0.5

    is_maneuver = was_steady & ~is_steady & will_steady & (np.abs(delta) > min_turn)

    candidates = np.where(is_maneuver)[0]
    if len(candidates) == 0:
        return []

    # Cluster nearby candidates and pick the peak of each cluster
    min_gap = int(min_gap_seconds * sample_rate)
    abs_delta = np.abs(delta)
    tacks = []
    cluster_start = candidates[0]
    cluster_best_idx = candidates[0]
    cluster_best_val = abs_delta[candidates[0]]

    for i in range(1, len(candidates)):
        if candidates[i] - candidates[i-1] > min_gap:
            tacks.append((cluster_best_idx, float(delta[cluster_best_idx])))
            cluster_start = candidates[i]
            cluster_best_idx = candidates[i]
            cluster_best_val = abs_delta[candidates[i]]
        elif abs_delta[candidates[i]] > cluster_best_val:
            cluster_best_idx = candidates[i]
            cluster_best_val = abs_delta[candidates[i]]

    tacks.append((cluster_best_idx, float(delta[cluster_best_idx])))
    return tacks


def detect_tacks_in_gyro(gyro, sample_rate=1000, **kwargs):
    """Detect tack/gybe events in raw video gyro data.

    Integrates the z-axis rotation rate to reconstruct heading, then detects tacks.

    Args:
        gyro: (N, 3) array at sample_rate Hz. Column 0 is yaw (vertical axis).
        sample_rate: gyro sample rate (typically 1000 Hz for Insta360).
        **kwargs: forwarded to detect_tacks_in_heading.

    Returns:
        List of (time_seconds_from_video_start, heading_change_degrees) tuples.
    """
    dt = 1.0 / sample_rate
    yaw_rate = gyro[:, 0]

    cumulative_heading_deg = np.degrees(np.cumsum(yaw_rate) * dt)

    # Downsample to 10 Hz
    target_rate = 10
    step = sample_rate // target_rate
    downsampled = cumulative_heading_deg[::step]

    # Already unwrapped (cumulative integration), so detection works directly
    tacks = detect_tacks_in_heading(downsampled, sample_rate=target_rate, **kwargs)

    result = []
    for sample_idx, change in tacks:
        time_s = sample_idx / target_rate
        result.append((time_s, change))
    return result


def match_tack_sequences(bb_tacks_times, video_tack_offsets, tolerance=TACK_MATCH_TOLERANCE_SECONDS):
    """Find the time offset that best aligns video tack times to BB tack times.

    Uses a voting approach: each (bb_tack, video_tack) pair implies a candidate
    offset.  The offset that is consistent with the most pairs wins.

    Args:
        bb_tacks_times: array of BB tack times in seconds (e.g. unix timestamps or
                        seconds-from-session-start — just needs to be consistent).
        video_tack_offsets: array of video tack times in seconds from video start.
        tolerance: maximum seconds between matched tacks.

    Returns:
        (best_offset, match_count) where best_offset is the value to ADD to
        each video_tack_offset to get the BB time.  match_count is how many
        tacks agreed on this offset.  Returns (0.0, 0) if no match found.
    """
    bb_times = np.asarray(bb_tacks_times, dtype=float)
    vid_times = np.asarray(video_tack_offsets, dtype=float)

    if len(bb_times) == 0 or len(vid_times) == 0:
        return 0.0, 0

    # Every (bb, vid) pair implies offset = bb - vid
    candidate_offsets = []
    for bt in bb_times:
        for vt in vid_times:
            candidate_offsets.append(bt - vt)

    best_offset = 0.0
    best_count = 0

    for offset in candidate_offsets:
        shifted = vid_times + offset
        count = 0
        for sv in shifted:
            if np.any(np.abs(bb_times - sv) <= tolerance):
                count += 1
        if count > best_count:
            best_count = count
            best_offset = offset

    return float(best_offset), int(best_count)


def detect_clock_offset_by_tacks(videos: List[str], big_df,
                                  max_offset_hours: float = 5.0) -> Optional[float]:
    """Detect camera clock offset by matching tack events between video gyro and BB heading.

    Finds a video with tacks, matches them against the day's BB heading data,
    and returns the clock offset.

    Args:
        videos: list of video basenames (should be from the same day).
        big_df: combined BB dataframe with row_times and hdg columns.
        max_offset_hours: maximum clock drift to search.

    Returns:
        Clock offset in seconds (add to creation_time to get true time),
        or None if matching failed.
    """
    if not videos:
        return None

    # Determine the day from the first video
    vi0 = read_video_info(videos[0])
    if vi0.ffprobe_creation_time is None:
        return None
    video_date = vi0.ffprobe_creation_time.date()

    # Extract the full day's BB heading data
    day_start = pytz.timezone(TIMEZONE).localize(datetime.combine(video_date, datetime.min.time()))
    day_end = day_start + timedelta(days=1)
    try:
        day_df, _ = log_extract_slice(big_df, day_start, day_end)
    except Exception:
        # Routine when probing across historical date ranges; demoted from
        # WARNING to keep the race_upload run summary readable.
        LOG.logger.debug("No BB data for this day")
        return None

    if len(day_df) < 1000:
        LOG.logger.warning(f"Only {len(day_df)} BB rows for this day")
        return None

    bb_heading = np.asarray(day_df.hdg)
    bb_tacks = detect_tacks_in_heading(bb_heading, sample_rate=10)

    # row_times is in JavaScript epoch milliseconds after df_convert
    bb_row_times_sec = np.asarray(day_df.row_times, dtype=float) / 1000.0

    print(f"  BB tacks for {video_date}: {len(bb_tacks)} detected")
    for idx, change in bb_tacks:
        if idx < len(bb_row_times_sec):
            t = pd.to_datetime(bb_row_times_sec[idx], unit="s", utc=True).tz_convert(TIMEZONE)
            print(f"    {t.strftime('%H:%M:%S')}  {change:+.0f}°")

    if len(bb_tacks) < 2:
        print("  Not enough BB tacks for matching")
        return None

    bb_tack_times = np.array([bb_row_times_sec[min(idx, len(bb_row_times_sec)-1)]
                              for idx, _ in bb_tacks])

    # Score each video by tack count, pick the best candidate
    best_video = None
    best_tacks = []
    best_count = 0

    for basename in videos:
        try:
            imu = read_insta_imu_data(basename)
            if imu is None:
                continue
            _, _, gyro = imu
            vtacks = detect_tacks_in_gyro(gyro)
            if len(vtacks) > best_count:
                best_count = len(vtacks)
                best_tacks = vtacks
                best_video = basename
        except Exception as e:
            LOG.logger.debug(f"Skipping {basename}: {e}")
            continue

    if best_count < 2:
        print(f"  No video with >= 2 tacks (best: {best_count})")
        return None

    print(f"  Best candidate: {best_video} with {best_count} tacks")
    for t, change in best_tacks:
        print(f"    {t:.0f}s from start  {change:+.0f}°")

    video_tack_times = np.array([t for t, _ in best_tacks])

    # The offset we find maps video-internal seconds to BB absolute seconds.
    # We need to express this as a camera clock correction.
    offset_abs, match_count = match_tack_sequences(bb_tack_times, video_tack_times)

    if match_count < 2:
        print(f"  Tack matching failed (only {match_count} matches)")
        return None

    # offset_abs = bb_time - video_internal_time
    # The video's creation_time (camera clock) maps 0s -> creation_time
    # True start = offset_abs (in unix seconds)
    # clock_offset = true_start - creation_time
    vi = read_video_info(best_video)
    creation_unix = vi.ffprobe_creation_time.timestamp()
    clock_offset = offset_abs - creation_unix

    true_start = pd.to_datetime(int(offset_abs), unit="s", utc=True).tz_convert(TIMEZONE)
    print(f"  Tack match: {match_count}/{best_count} tacks, offset={clock_offset:+.0f}s ({clock_offset/3600:+.2f}h)")
    print(f"  Video true start: {true_start.strftime('%H:%M:%S')}")

    return clock_offset


def detect_clock_offset(videos: List[str], big_df) -> float:
    """Correlation-based clock offset detection (fallback when tack matching fails).

    Uses gyro-heading cross-correlation with widening search windows.
    Returns the offset in seconds (add to creation_time to get true time).
    Returns 0.0 if the clock appears correct.
    """

    # --- Fallback: widening correlation search ---
    # Pick a representative video (prefer one with good duration)
    probe_video = videos[len(videos) // 2]
    vi = read_video_info(probe_video)

    if vi.ffprobe_creation_time is None:
        LOG.logger.warning(f"No ffprobe for {probe_video}, cannot detect clock offset")
        return 0.0

    creation_time = vi.ffprobe_creation_time
    duration = vi.duration_seconds

    print(f"  Probe video: {probe_video}")
    print(f"  Camera creation_time: {creation_time.strftime('%H:%M:%S')}, duration: {duration:.0f}s")

    # Load IMU for the probe video
    imu = read_insta_imu_data(probe_video)
    if imu is None:
        LOG.logger.error(f"No IMU data for {probe_video}")
        return 0.0
    _, _, gyro = imu
    sgg = gyro[::100, 0]
    ngg = _normalize_signal(sgg)

    # Search with progressively wider windows, starting narrow.
    # The narrow window (60s) handles the case where the clock is nearly correct.
    for window in SEARCH_STAGES:
        search_start = creation_time - timedelta(seconds=window)
        search_end = creation_time + timedelta(seconds=duration + window)
        try:
            cdf, _ = log_extract_slice(big_df, search_start, search_end)
        except Exception:
            continue

        if len(cdf) < 300:
            print(f"  Window ±{window}s: only {len(cdf)} BB rows, widening...")
            continue

        print(f"  Window ±{window}s: {len(cdf)} BB rows, running correlation...")

        dhdg = np.diff(cdf.hdg, prepend=cdf.hdg.iloc[0])
        dhdg[dhdg > 180] -= 360
        dhdg[dhdg < -180] += 360

        nhdg = _normalize_signal(dhdg)
        match = np.correlate(nhdg, 1 * ngg, mode="valid")

        if len(match) == 0:
            continue

        peak_idx = int(np.argmax(match))
        peak_val = float(match[peak_idx])
        mean_val = float(np.mean(np.abs(match)))
        ratio = peak_val / mean_val if mean_val > 0 else 0

        if ratio < WEAK_RATIO:
            print(f"  Weak correlation (ratio={ratio:.1f}), widening...")
            continue

        delay_seconds = peak_idx / 10.0

        # BB slice starts at search_start, but actual data may start later
        bb_start_time = pd.to_datetime(
            int(cdf.row_times.iloc[0]), unit="ms", utc=True
        ).tz_convert(TIMEZONE)
        video_true_start = bb_start_time + timedelta(seconds=delay_seconds)
        clock_offset = (video_true_start - creation_time).total_seconds()

        print(f"  Found alignment: video starts at {video_true_start.strftime('%H:%M:%S')} (ratio={ratio:.1f})")
        print(f"  Camera clock offset: {clock_offset:+.1f}s ({clock_offset/3600:+.2f}h)")
        return clock_offset

    print("  WARNING: Could not find BB overlap at any search width")
    return 0.0


def correct_metadata(videos: List[str], clock_offset_seconds: float, dry_run: bool = True) -> None:
    """Shift creation_time in .ffprobe and CreateDate in .exif by the given offset."""
    offset = timedelta(seconds=clock_offset_seconds)

    for basename in videos:
        ffprobe_path = os.path.join(RAW_DIR, basename + ".ffprobe")
        exif_path = os.path.join(RAW_DIR, basename + ".exif")

        # --- .ffprobe ---
        if os.path.exists(ffprobe_path):
            ffm = read_json_file(ffprobe_path)
            old_ct = ffm["format"]["tags"]["creation_time"]
            old_dt = datetime.strptime(old_ct, "%Y-%m-%dT%H:%M:%S.%fZ")
            new_dt = old_dt + offset
            new_ct = new_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

            if dry_run:
                print(f"  {basename}.ffprobe: {old_ct} -> {new_ct}")
            else:
                bak = ffprobe_path + ".bak"
                if not os.path.exists(bak):
                    shutil.copy2(ffprobe_path, bak)
                ffm["format"]["tags"]["creation_time"] = new_ct
                for stream in ffm.get("streams", []):
                    if "tags" in stream and "creation_time" in stream["tags"]:
                        s_old = stream["tags"]["creation_time"]
                        s_dt = datetime.strptime(s_old, "%Y-%m-%dT%H:%M:%S.%fZ")
                        stream["tags"]["creation_time"] = (s_dt + offset).strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ"
                        )
                # Atomic write: stage to .tmp then os.replace. .bak above is
                # a separate safety net (pre-correction snapshot), not an
                # interrupt-safety mechanism.
                tmp = ffprobe_path + ".tmp"
                try:
                    with open(tmp, "w") as f:
                        json.dump(ffm, f, indent=4)
                    os.replace(tmp, ffprobe_path)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except FileNotFoundError:
                        pass
                    raise
                LOG.logger.info(f"Corrected {ffprobe_path}")

        # --- .exif ---
        if os.path.exists(exif_path):
            exif = read_json_file(exif_path)
            rec = exif[0] if isinstance(exif, list) else exif
            if "CreateDate" in rec:
                old_cd = rec["CreateDate"]
                old_edt = datetime.strptime(old_cd, "%Y:%m:%d %H:%M:%S")
                new_edt = old_edt + offset
                new_cd = new_edt.strftime("%Y:%m:%d %H:%M:%S")

                if dry_run:
                    print(f"  {basename}.exif:    {old_cd} -> {new_cd}")
                else:
                    bak = exif_path + ".bak"
                    if not os.path.exists(bak):
                        shutil.copy2(exif_path, bak)
                    rec["CreateDate"] = new_cd
                    # Atomic write: stage to .tmp then os.replace.
                    tmp = exif_path + ".tmp"
                    try:
                        with open(tmp, "w") as f:
                            json.dump(exif, f, indent=4)
                        os.replace(tmp, exif_path)
                    except BaseException:
                        try:
                            os.unlink(tmp)
                        except FileNotFoundError:
                            pass
                        raise
                    LOG.logger.info(f"Corrected {exif_path}")


def align_video(basename: str, big_df, offset: int = 30, recompute: bool = False) -> SyncReport:
    """Run uncapped cross-correlation to find the best alignment for a single video."""
    log_file = os.path.join(VIDEO_DIR, basename + ".log")
    if not recompute and existing_log_is_valid(basename):
        return SyncReport(basename, status="EXISTING")
    try:
        video_df, video_start, _, video_duration = extract_video_logs(
            basename, big_df, offset=offset
        )
    except Exception as e:
        return SyncReport(basename, status="ERROR", error=str(e))

    if len(video_df) < 300:
        return SyncReport(
            basename, bb_rows=len(video_df), status="NO_DATA",
            error=f"Only {len(video_df)} BB rows",
        )

    try:
        imu = read_insta_imu_data(basename)
        if imu is None:
            return SyncReport(basename, bb_rows=len(video_df), status="NO_IMU")
        _, _, gyro = imu
    except Exception as e:
        return SyncReport(basename, bb_rows=len(video_df), status="ERROR", error=str(e))

    dhdg = np.diff(video_df.hdg, prepend=video_df.hdg.iloc[0])
    dhdg[dhdg > 180] -= 360
    dhdg[dhdg < -180] += 360

    sgg = gyro[::100, 0]
    ngg = _normalize_signal(sgg)
    nhdg = _normalize_signal(dhdg)
    match = np.correlate(nhdg, 1 * ngg, mode="valid")

    if len(match) == 0:
        return SyncReport(basename, bb_rows=len(video_df), status="NO_OVERLAP")

    peak_idx = int(np.argmax(match))
    peak_val = float(match[peak_idx])
    corr_mean = float(np.mean(np.abs(match)))
    delay = peak_idx / 10.0

    max_meaningful = (len(nhdg) - len(ngg)) / 10.0
    ratio = peak_val / corr_mean if corr_mean > 0 else 0

    if ratio < WEAK_RATIO:
        status = "WEAK"
    elif delay > max_meaningful * (1 - BOUNDARY_FRACTION):
        status = "BOUNDARY"
    else:
        status = "GOOD"

    return SyncReport(
        basename=basename, delay=delay, corr_peak=peak_val,
        corr_mean=corr_mean, bb_rows=len(video_df), status=status,
    )


def print_sync_report(reports: List[SyncReport], offset: int = 30) -> None:
    """Print a formatted sync report.  Shows the net fine adjustment (delay - offset)."""
    hdr = f"{'VIDEO':<45s} {'FINE':>7s} {'CORR':>8s} {'P/M':>6s} {'ROWS':>6s} {'STATUS'}"
    print(hdr)
    print("-" * len(hdr))
    for r in reports:
        if r.status == "EXISTING":
            continue
        elif r.status in ("ERROR", "NO_DATA", "NO_IMU", "NO_OVERLAP"):
            print(f"{r.basename:<45s} {'':>7s} {'':>8s} {'':>6s} {r.bb_rows:>6d} {r.status} {r.error}")
        else:
            ratio = r.corr_peak / r.corr_mean if r.corr_mean > 0 else 0
            fine = r.delay - offset
            print(
                f"{r.basename:<45s} {fine:>+7.1f} {r.corr_peak:>8.0f} "
                f"{ratio:>6.1f} {r.bb_rows:>6d} {r.status}"
            )


def apply_sync_logs(reports: List[SyncReport], big_df, offset: int = 30) -> None:
    """Write .log files using the validated delays."""
    for r in reports:
        if r.status not in ("GOOD", "WEAK"):
            LOG.logger.warning(f"Skipping {r.basename} (status={r.status})")
            continue
        try:
            _, video_start, _, video_duration = extract_video_logs(
                r.basename, big_df, offset=offset
            )
            slice_start = video_start + pd.Timedelta(seconds=r.delay)
            slice_end = slice_start + video_duration
            sliced_df, raw_slice = log_extract_slice(big_df, slice_start, slice_end)

            if len(sliced_df) < 10:
                LOG.logger.warning(f"Skipping {r.basename}: only {len(sliced_df)} rows in slice")
                continue

            log_file = os.path.join(VIDEO_DIR, r.basename + ".log")
            sailview_log_v1.save_log_to_binary(sliced_df, log_file)
            # Record the on-disk start offset (first_row_time - CreateDate)
            # plus the upstream parquet mtimes, so existing_log_is_valid
            # stays stable across re-runs and invalidates correctly when
            # the BB parquet is rewritten. r.delay is shifted by the
            # BB-window padding `offset`, since extract_video_logs
            # returns video_start = creation_dt - offset and
            # slice_start = video_start + r.delay.
            _write_cached_state(r.basename, r.delay - offset)
            LOG.logger.info(f"Wrote {log_file} ({len(sliced_df)} rows, delay={r.delay:.1f}s)")

            # v2 .slog — consumes the RAW (pre-df_convert) parquet slice
            # because V2_COLUMNS pulls from r* columns and applies its own
            # knots conversion. See SLOG_FORMAT.md for the format spec.
            slog_file = os.path.join(VIDEO_DIR, r.basename + ".slog")
            try:
                stats = sailview_log_v2.write_slog(
                    raw_slice, slog_file, proj4=getattr(G, "PROJ4", ""))
                LOG.logger.info(
                    f"Wrote {slog_file} ({stats['file_size']} B, "
                    f"{stats['num_cols']} cols"
                    + (f", fallback={','.join(stats['fallback_columns'])}"
                       if stats['fallback_columns'] else "")
                    + ")")
            except Exception as e:
                LOG.logger.error(f"Failed to write .slog for {r.basename}: {e}")
        except Exception as e:
            LOG.logger.error(f"Failed {r.basename}: {e}")


def sync_videos_to_bb(
    videos: List[str],
    big_df,
    dry_run: bool = False,
    offset: int = 30,
    force_recompute_existing: bool = False,
) -> List[SyncReport]:
    """Full sync pipeline: detect clock offset, correct metadata, align videos, write logs.
    
    This is the main entry point for video synchronization, called from race_upload.py.
    All videos must be from the same date — mixing dates leads to false clock detection.
    
    Args:
        videos: List of video basenames to sync (must all share the same date)
        big_df: Combined BoatBrain log dataframe
        dry_run: If True, don't write any files
        offset: BB window padding in seconds (default 30)
        force_recompute_existing: Re-align even when a .log already exists.
    
    Returns:
        List of SyncReport objects with alignment results
    """
    if not videos:
        LOG.logger.info("No videos to sync")
        return []

    # Safety check: all videos must be from the same date.
    dates = set()
    for v in videos:
        parts = v.split("_")
        if len(parts) >= 2:
            dates.add(parts[1])  # e.g. "20260411"
    if len(dates) > 1:
        raise ValueError(
            f"sync_videos_to_bb received videos from {len(dates)} different dates: "
            f"{sorted(dates)}. Pass only one date at a time to avoid cross-date "
            f"clock detection errors."
        )
    
    print(f"\n=== Video-to-BoatBrain Synchronization ({len(videos)} videos) ===\n")
    
    # Stage 1: Coarse clock offset detection (tack-based, with correlation fallback)
    print("Stage 1: Clock offset detection")
    tack_offset = detect_clock_offset_by_tacks(videos, big_df)
    tack_based = tack_offset is not None

    if tack_based:
        clock_offset = tack_offset
    else:
        print("  Tack detection did not succeed, falling back to correlation...")
        clock_offset = detect_clock_offset(videos, big_df)

    # Stage 2: Metadata correction (if needed)
    if abs(clock_offset) > CLOCK_ERROR_THRESHOLD:
        print(f"\nStage 2: Metadata correction ({clock_offset:+.0f}s)")
        if dry_run:
            correct_metadata(videos, clock_offset, dry_run=True)
            print("  (dry run -- no files modified)")
        else:
            correct_metadata(videos, clock_offset, dry_run=False)
            print("  Metadata corrected (originals backed up as .bak)")
    else:
        print(f"  Clock offset {clock_offset:+.1f}s within threshold, no correction needed")

    # Narrow fine offset when tack-based detection succeeded (already precise)
    if tack_based:
        fine_offset = 300
    elif abs(clock_offset) <= CLOCK_ERROR_THRESHOLD:
        fine_offset = offset
    else:
        fine_offset = max(offset, 60)
    
    # Stage 3: Fine alignment
    print(f"\nStage 3: Fine alignment (offset={fine_offset}s)")
    if abs(clock_offset) > CLOCK_ERROR_THRESHOLD and dry_run:
        print("  Skipping fine alignment until metadata is corrected")
        return []
    
    recompute = force_recompute_existing or abs(clock_offset) > CLOCK_ERROR_THRESHOLD
    reports = [align_video(b, big_df, fine_offset, recompute=recompute) for b in videos]
    print_sync_report(reports, offset=fine_offset)
    
    existing = sum(1 for r in reports if r.status == "EXISTING")
    good = sum(1 for r in reports if r.status == "GOOD")
    boundary = sum(1 for r in reports if r.status == "BOUNDARY")
    weak = sum(1 for r in reports if r.status == "WEAK")
    errors = sum(1 for r in reports if r.status in ("ERROR", "NO_DATA", "NO_IMU", "NO_OVERLAP"))
    
    print(f"\nSummary: {good} GOOD, {weak} WEAK, {boundary} BOUNDARY, {errors} ERROR, {existing} EXISTING")
    
    if boundary > 0:
        print(f"\n** {boundary} video(s) at correlation boundary")
        LOG.logger.warning(f"{boundary} videos at correlation boundary - sync may be unreliable")
    
    # Stage 4: Apply (write .log files)
    if not dry_run:
        print(f"\nStage 4: Writing .log files")
        apply_sync_logs(reports, big_df, fine_offset)
        if boundary > 0:
            print(f"  WARNING: {boundary} video(s) at correlation boundary were skipped")
    else:
        print("\n  (dry run -- no .log files written)")
    
    return reports


##################################################################################################
# Upload-set integrity check.
#
# Invariant after the WEB_DIR collapse: every uploadable file under VIDEO_DIR has a
# matching object in s3://regattaanalysis/www/Insta360/. Run after the three upload
# subcalls (`_run_upload_command`, `_upload_slogs`, `_upload_poses`) finish so a
# missed file never lingers locally without an S3 counterpart.

UPLOAD_EXTENSIONS = (".mp4", ".log", ".m4a", ".slog", ".poses")
INSTA360_S3_PREFIX = "s3://regattaanalysis/www/Insta360/"


def _list_s3_objects(s3_prefix: str = INSTA360_S3_PREFIX) -> set:
    """Return the set of object basenames under `s3_prefix` via one s5cmd ls call.

    s5cmd ls with a wildcard prints one line per object. The basename is the
    last whitespace-separated field on each line. ~1500 keys today, ~0.5 s.
    """
    cmd = f"AWS_PROFILE={AWS_PROFILE} s5cmd ls '{s3_prefix}*'"
    result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(
            f"s5cmd ls failed (exit {result.returncode}): {result.stderr.strip()}"
        )
    keys = set()
    for line in result.stdout.splitlines():
        parts = line.split()
        if not parts:
            continue
        last = parts[-1]
        # Skip directory markers ("DIR" lines end with '/').
        if last.endswith("/"):
            continue
        keys.add(last)
    return keys


# Sidecar safety files that match the VID_*.<ext> patterns but must never
# ship. Mirror of the s5cmd --exclude flags in race_upload.py / sync_videos.py.
NON_UPLOADABLE_SUFFIXES = (".camera.m4a", ".tmp.mp4")


def assert_uploadable_set(video_dir: str = VIDEO_DIR,
                          s3_prefix: str = INSTA360_S3_PREFIX) -> None:
    """Assert every local uploadable VID_* file in `video_dir` has an S3 counterpart.

    Raises RuntimeError if any local file is missing on S3. The set of
    `uploadable` extensions matches the union of the three upload subcalls
    (bulk include-list + `.slog` + `.poses`) plus `videos.json`. Sidecar
    safety files (`*.camera.m4a`, `*.tmp.mp4`) are explicitly excluded —
    they live in `video_dir` for local-only safety reasons and must not
    be expected on S3.
    """
    local = {
        f for f in os.listdir(video_dir)
        if (f.startswith("VID") or f.startswith("VIA"))
        and f.endswith(UPLOAD_EXTENSIONS)
        and not f.endswith(NON_UPLOADABLE_SUFFIXES)
    }
    if os.path.exists(os.path.join(video_dir, "videos.json")):
        local.add("videos.json")
    remote = _list_s3_objects(s3_prefix)
    missing = sorted(local - remote)
    if missing:
        raise RuntimeError(
            f"After upload, {len(missing)} local file(s) absent on S3 "
            f"under {s3_prefix}. First few: {missing[:5]}"
        )
    LOG.logger.info(
        f"assert_uploadable_set: {len(local)} local file(s) all present on S3"
    )
