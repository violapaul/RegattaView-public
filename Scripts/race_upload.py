#!/usr/bin/env /Users/viola/miniforge3/envs/sailing/bin/python

"""
Race Upload Script

This script processes sailing race data from two sources:
1. Insta360 camera videos - Captures 360-degree video footage of races
2. BoatBrain logs - Contains sensor data (GPS, wind, heading etc.) from the boat

The script performs the following operations:
1. Copies raw data from SD cards
2. Processes and compresses video files
3. Extracts and processes sensor data
4. Syncs video with sensor data
5. Uploads processed data to web server and AWS

The processed data enables race replay and analysis through a web interface.

Usage:
    python race_upload.py [options]

Options:
    --recompute-pose     Force recomputation of camera pose files
    --no-video          Skip video processing steps
    --no-logs           Skip log processing steps
    --no-sync           Skip video-log synchronization
    --no-upload         Skip AWS upload
    --upload-only       Only perform the final upload step (skip all processing)
    --no-pipeline       Disable pipelined processing (run copy/compress/upload sequentially)
    --delete-raw        Delete raw .insv files after successful compression
    --dry-run          Perform a dry run without making changes
"""

# %% imports
import os
import argparse
import logging
import re
import shutil
import subprocess
import sys

# Ensure the repo root is importable so first-party packages (Libraries, Analyze)
# resolve regardless of CWD. Libraries is also reachable via the Scripts/Libraries
# symlink, but Analyze (pulled in transitively via bb_log -> race_logs -> true_wind)
# is not, so the repo root must be on sys.path. Mirrors Scripts/extract_ais.py.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Libraries.log_helper import LOG
LOG.set_logging_level('INFO')


# --- Run-summary instrumentation -----------------------------------------------
# Capture every WARNING/ERROR/CRITICAL record emitted through LOG.logger during
# a run, and print them (plus per-stage pass/fail) at the very end. A silent
# card-not-mounted or a caught CalledProcessError used to scroll past in a river
# of INFO output; with this, you can glance at the last ~15 lines and know.

class _SummaryHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def _install_summary_handler():
    handler = _SummaryHandler()
    LOG.logger.addHandler(handler)
    return handler


def _fmt_record(record):
    loc = f"{record.funcName}:{record.lineno}"
    return f"[{record.levelname:<8}] {loc}  {record.getMessage()}"


def _print_summary(stage_results, handler):
    print("\n=============== RUN SUMMARY ===============", flush=True)
    stage_line = "  ".join(f"{name}:{status}" for name, status in stage_results)
    print(f"stages: {stage_line}")
    n_err = sum(1 for r in handler.records if r.levelno >= logging.ERROR)
    n_warn = sum(1 for r in handler.records if r.levelno == logging.WARNING)
    print(f"{n_err} error(s), {n_warn} warning(s)")
    for r in handler.records:
        print(f"  {_fmt_record(r)}")
    card_lines = card_report.summary_lines()
    if card_lines:
        print()
        for _l in card_lines:
            print(_l)
    print("===========================================", flush=True)
    return n_err


def _stage(stage_results, name, fn, enabled=True):
    if not enabled:
        stage_results.append((name, "skipped"))
        return
    print(f"\n=== stage: {name} ===", flush=True)
    try:
        fn()
        stage_results.append((name, "ok"))
    except Exception as e:
        LOG.logger.exception(f"stage {name} failed: {e}")
        stage_results.append((name, "FAILED"))

from Libraries.global_variables import G
G.init_seattle()  # Initialize global settings for Seattle location

import Libraries.bb_log as bb_log        # BoatBrain log processing
import Libraries.insta360 as insta360    # Insta360 video processing
import Libraries.metadata as metadata     # Metadata handling
from Libraries import card_report         # Per-card end-of-run roundup

AWS_PROFILE = os.environ.get("AWS_PROFILE", "regatta")
# Cache-Control for binary artifacts (videos, .log, .poses, .slog).
# These are write-once-keyed-by-name in the steady state; the rare rewrite
# (voice_audio swap, recompute_logs) is handled by the explicit CloudFront
# invalidation at the end of _do_upload. 1 week TTL keeps re-watches at the
# CF edge cache instead of paying S3 egress every time.
CACHE_CONTROL_BINARY = "public, max-age=604800"
# CloudFront distribution that fronts the regattaanalysis bucket. Override
# with $CF_DIST_ID if running against a non-prod distribution. Same default
# as Scripts/aws_egress_killswitch.sh.
CF_DIST_ID = os.environ.get("CF_DIST_ID", "EPCTF7XCNPLK2")
# VIDEO_DIR holds only the four ship-targets at its top level. Intermediates
# live in VIDEO_DIR/raw/, local-only safety files in VIDEO_DIR/extras/,
# brotli-cached .poses copies in VIDEO_DIR/.poses_br/.
#
# We push each ship-target shape with its own non-recursive `s5cmd sync`
# call. The earlier single-call form (`PATH/*` source plus
# `--include / --exclude` filters) had two failure modes:
#   1. s5cmd would stat broken symlinks at the top level (e.g. test.mp4 ->
#      a now-raw/-resident .insv) and abort with "given object not found"
#      before ever consulting the filters.
#   2. s5cmd would recurse into subdirs (extras/, raw/) despite
#      --exclude 'extras/*' / 'raw/*' — its sync filter semantics with
#      subdir patterns are flaky.
# Per-pattern globs sidestep both: each pattern matches only the files
# at the top level that we actually want, nothing else is enumerated.
#  VID_*.mp4    : compressed video (one per clip).
#  VID_*.log    : per-video binary instrument data (legacy v1 reader).
#  VID_*.m4a    : per-clip audio (only when voice_audio swap is on).
#  videos.json  : index consumed by the web viewer.
#
# Two sidecar safety files match VID_*.m4a / VID_*.mp4 but must never
# ship; voice_audio now writes them to extras/, but the per-pattern
# excludes are kept defensively in case anything lands at top level by
# accident:
#  *.camera.m4a : voice_audio's backup of the original camera audio.
#  *.tmp.mp4    : voice_audio's mid-mux scratch file.
#
# .slog and .poses are pushed by their dedicated helpers below — each
# needs a per-call Content-Encoding header (gzip / br). All upload calls
# carry --cache-control so re-watches hit the CF edge cache.
from Libraries.insta360 import VIDEO_DIR, INSTA360_S3_PREFIX  # noqa: E402

# Each entry is a shell glob *relative to VIDEO_DIR*; s5cmd does the
# matching itself (the path is single-quoted to defer shell expansion).
# VIA_* clips are audio-only synthetic placeholders created by
# Libraries/blank_video.py for races where the camera wasn't running;
# they share the same per-clip sidecar layout as VID_* (mp4 + log + m4a).
# .slog and .poses are handled by _upload_slogs / _upload_poses below,
# whose `*.slog` / `*.poses` globs already cover both prefixes.
BULK_UPLOAD_PATTERNS = (
    "VID_*.mp4",
    "VID_*.log",
    "VID_*.m4a",
    "VIA_*.mp4",
    "VIA_*.log",
    "VIA_*.m4a",
    "videos.json",
)
BULK_UPLOAD_EXCLUDES = (
    "*.camera.m4a",
    "*.tmp.mp4",
)


def _build_bulk_sync_cmd(pattern: str) -> str:
    excludes = " ".join(f"--exclude '{e}'" for e in BULK_UPLOAD_EXCLUDES)
    return (
        f"AWS_PROFILE={AWS_PROFILE} s5cmd sync "
        f"{excludes} "
        f"--cache-control '{CACHE_CONTROL_BINARY}' "
        f"'{VIDEO_DIR}/{pattern}' {INSTA360_S3_PREFIX}"
    )

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SCRIPTS_DIR = os.path.join(REPO_ROOT, "Scripts")
ANALYZE_DIR = os.path.join(REPO_ROOT, "Analyze")
ANALYZE_AIS_DIR = os.path.join(ANALYZE_DIR, "AIS")
ANALYZE_AIS_DAYS_DIR = os.path.join(ANALYZE_DIR, "AISDays")
WEB_DIR = "/Users/viola/www"
WEB_ANALYZE_DIR = os.path.join(WEB_DIR, "Analyze")
WEB_ANALYZE_AIS_DIR = os.path.join(WEB_ANALYZE_DIR, "AIS")
WEB_ANALYZE_AIS_DAYS_DIR = os.path.join(WEB_ANALYZE_DIR, "AISDays")
AIS_LOGS_DIR = "/Users/viola/Data/SailingData/AISLogs"

# S3 destination for the generated review/tracks artifacts under Analyze/
# (events_index.json, Events/, Sessions/, AIS/, AISDays/). The analyze stage
# regenerates these and stages them into WEB_ANALYZE_DIR; _upload_analyze
# below is the push that actually publishes them. Mirrors aws_sync.sh's
# `aws s3 sync Analyze "$S3/www/Analyze"`. Historically only aws_sync.sh (the
# frontend `npm run deploy`) pushed Analyze/, so a plain race_upload.py
# refreshed the data locally yet review.html / tracks.html kept serving the
# previous deploy's copy.
ANALYZE_S3_PREFIX = os.environ.get(
    "ANALYZE_S3_PREFIX", "s3://regattaanalysis/www/Analyze")
BBLOG_DIR_DEFAULT = "/Users/viola/Data/BBLog"
ANALYZE_DAYS_DEFAULT = 30


def run_analyze_pipeline(days: int, dry_run: bool = False) -> None:
    """Regenerate events_index.json + per-event detail files under
    Analyze/Events/ + per-session tracks under Analyze/Sessions/, then stage
    them under WEB_DIR.

    Runs the script through `conda run -n sailing` so the child inherits the
    correct interpreter and dependencies regardless of the shell PATH.

    The per-event edits overlay (events_edits.json) and all Comments/* files
    live only in S3 and are never overwritten here. Exceptions from the analyze
    step are caught so a transient failure doesn't abort the upload step.
    """
    def _run(script, label):
        cmd = ["conda", "run", "-n", "sailing", "python",
               os.path.join(ANALYZE_DIR, script), "--days", str(days)]
        if dry_run:
            print(f"Would run: {' '.join(cmd)}")
            return True
        LOG.logger.info(f"Running {label}")
        try:
            subprocess.run(cmd, cwd=REPO_ROOT, check=True)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            LOG.logger.error(f"{label} failed: {e}")
            return False

    if not _run("find_starts.py", "find_starts.py"):
        LOG.logger.warning("Skipping Analyze/ staging because find_starts.py failed")
        return

    os.makedirs(WEB_ANALYZE_DIR, exist_ok=True)
    src = os.path.join(ANALYZE_DIR, "events_index.json")
    dst = os.path.join(WEB_ANALYZE_DIR, "events_index.json")
    if os.path.exists(src):
        shutil.copyfile(src, dst)
        LOG.logger.info(f"Staged events_index.json -> {dst}")
    else:
        LOG.logger.warning(f"Expected {src} after Analyze run; not found")

    # Per-event detail payloads + per-session tracks — mirror the directories
    # in full so newly-added ids appear under WEB_DIR. dirs_exist_ok avoids a
    # manual rmtree, and copytree mirrors only files that find_starts actually
    # wrote (no orphan cleanup, matching the rest of the staging).
    for sub in ("Events", "Sessions"):
        sub_src = os.path.join(ANALYZE_DIR, sub)
        sub_dst = os.path.join(WEB_ANALYZE_DIR, sub)
        if os.path.isdir(sub_src):
            shutil.copytree(sub_src, sub_dst, dirs_exist_ok=True)
            LOG.logger.info(f"Staged {sub}/ -> {sub_dst}")

    # AIS pipeline: per-date extraction + per-start slices. Run after the
    # events index is fresh so the slicer picks up any new starts. Failures
    # here are non-fatal.
    run_ais_pipeline(days, dry_run=dry_run)


def _ais_extract_is_current(out_path: str, date_str: str) -> bool:
    """Compare the AIS extract's recorded source-log signature against current BB logs.

    Returns False (forces re-extract) when:
      - the JSON cannot be read,
      - the file lacks a `source_logs` block (legacy output predating
        upstream-change tracking — re-extract once to back-fill),
      - any BB log on disk for that date has a different mtime than
        what was recorded, or any log was added/removed.
    """
    try:
        import json as _json
        with open(out_path) as f:
            data = _json.load(f)
    except (OSError, ValueError):
        return False
    cached = data.get('source_logs')
    if not cached:
        return False
    try:
        current = {
            f: os.path.getmtime(os.path.join(BBLOG_DIR_DEFAULT, f))
            for f in os.listdir(BBLOG_DIR_DEFAULT)
            if f.startswith(date_str) and f.endswith("Z.log")
        }
    except OSError:
        return False
    return cached == current


def run_ais_pipeline(days: int, dry_run: bool = False) -> None:
    """Extract AIS for recent BB log dates and slice per-start AIS files.

    Incremental: each per-date AIS extract is skipped when its recorded
    `source_logs` signature matches the current BB log files for that
    date; per-start slices are skipped if the output already exists.
    Delete the AIS output file (or pass --force to the underlying
    scripts) to force a re-run.
    """
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y-%m-%d')

    if not os.path.isdir(BBLOG_DIR_DEFAULT):
        LOG.logger.warning(f"BB log dir missing ({BBLOG_DIR_DEFAULT}); skipping AIS pipeline")
        return

    # Unique log dates in the recent window.
    dates = set()
    for f in os.listdir(BBLOG_DIR_DEFAULT):
        if not f.endswith("Z.log"):
            continue
        date_str = f.split("T", 1)[0]
        if date_str >= cutoff:
            dates.add(date_str)

    os.makedirs(AIS_LOGS_DIR, exist_ok=True)
    for date_str in sorted(dates):
        out_path = os.path.join(AIS_LOGS_DIR, f"{date_str}_ais.json")
        if os.path.exists(out_path) and _ais_extract_is_current(out_path, date_str):
            continue
        cmd = ["conda", "run", "-n", "sailing", "python",
               os.path.join(SCRIPTS_DIR, "extract_ais.py"), "--date", date_str]
        if dry_run:
            print(f"Would run: {' '.join(cmd)}")
            continue
        LOG.logger.info(f"Extracting AIS for {date_str}")
        try:
            subprocess.run(cmd, cwd=REPO_ROOT, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            LOG.logger.error(f"extract_ais.py --date {date_str} failed: {e}")

    # Per-start slicer. Incremental (skips existing output files).
    cmd = ["conda", "run", "-n", "sailing", "python",
           os.path.join(ANALYZE_DIR, "slice_ais_starts.py"), "--days", str(days)]
    if dry_run:
        print(f"Would run: {' '.join(cmd)}")
    else:
        LOG.logger.info("Running slice_ais_starts.py")
        try:
            subprocess.run(cmd, cwd=REPO_ROOT, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            LOG.logger.error(f"slice_ais_starts.py failed: {e}")

    # Per-local-day AIS tracks for tracks.html. This consumes the same UTC-date
    # extracts above, merging adjacent UTC extracts when a local sailing day
    # crosses midnight UTC.
    cmd = ["conda", "run", "-n", "sailing", "python",
           os.path.join(ANALYZE_DIR, "slice_ais_day.py"), "--days", str(days)]
    if dry_run:
        print(f"Would run: {' '.join(cmd)}")
    else:
        LOG.logger.info("Running slice_ais_day.py")
        try:
            subprocess.run(cmd, cwd=REPO_ROOT, check=True)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            LOG.logger.error(f"slice_ais_day.py failed: {e}")

    # Stage Analyze/AIS/ into the web dir for the uploader (aws s3 sync Analyze is
    # recursive, so subdirectories are included automatically once they exist here).
    if os.path.isdir(ANALYZE_AIS_DIR):
        os.makedirs(WEB_ANALYZE_AIS_DIR, exist_ok=True)
        staged = 0
        for fn in os.listdir(ANALYZE_AIS_DIR):
            if not fn.endswith(".json"):
                continue
            src = os.path.join(ANALYZE_AIS_DIR, fn)
            dst = os.path.join(WEB_ANALYZE_AIS_DIR, fn)
            if dry_run:
                continue
            shutil.copyfile(src, dst)
            staged += 1
        if not dry_run:
            LOG.logger.info(f"Staged {staged} per-start AIS files -> {WEB_ANALYZE_AIS_DIR}")

    if os.path.isdir(ANALYZE_AIS_DAYS_DIR):
        if dry_run:
            print(f"Would stage {ANALYZE_AIS_DAYS_DIR} -> {WEB_ANALYZE_AIS_DAYS_DIR}")
        else:
            shutil.copytree(ANALYZE_AIS_DAYS_DIR, WEB_ANALYZE_AIS_DAYS_DIR, dirs_exist_ok=True)
            LOG.logger.info(f"Staged AISDays/ -> {WEB_ANALYZE_AIS_DAYS_DIR}")

def parse_arguments():
    parser = argparse.ArgumentParser(description='Process sailing race data and upload to web server')
    parser.add_argument('--recompute-pose', action='store_true', help='Force recomputation of camera pose files')
    parser.add_argument('--no-video', action='store_true', help='Skip video processing steps')
    parser.add_argument('--no-logs', action='store_true', help='Skip log processing steps')
    parser.add_argument('--force-logs', action='store_true',
                        help='Regenerate parquet logs even if they exist; '
                             'undersized /tmp/*.json caches are detected and rebuilt automatically')
    parser.add_argument('--no-sync', action='store_true', help='Skip video-log synchronization')
    parser.add_argument('--no-upload', action='store_true', help='Skip AWS upload')
    parser.add_argument('--upload-only', action='store_true', help='Only perform the final upload step (skip all processing)')
    parser.add_argument('--no-analyze', action='store_true', help='Skip regenerating Analyze/ events_index.json + Events/ + Sessions/')
    parser.add_argument('--no-voice-audio', action='store_true',
                        help='Skip auto-applying audio drops in /Users/viola/Data/Insta360/Audio/')
    parser.add_argument('--analyze-days', type=int, default=ANALYZE_DAYS_DEFAULT,
                        help=f'Days of log history to feed into Analyze/ scripts (default {ANALYZE_DAYS_DEFAULT})')
    parser.add_argument('--no-pipeline', action='store_true', help='Disable pipelined processing (run copy/compress/upload sequentially)')
    parser.add_argument('--delete-raw', action='store_true', help='Delete raw .insv files after successful compression')
    parser.add_argument('--dry-run', action='store_true', help='Perform a dry run without making changes')
    return parser.parse_args()

_S5CMD_CP_RE = re.compile(r'^\s*cp\s+\S+\s+(s3://\S+)\s*$')
# `aws s3 sync` prints one "upload: <local> to <s3-uri>" line per transferred
# file; unchanged files (skipped by sync's size/mtime check) emit nothing.
_AWS_S3_UPLOAD_RE = re.compile(r'^\s*upload:\s+\S.*\s+to\s+(s3://\S+)\s*$')


def _run_s5cmd_capturing_uploads(cmd: str, dry_run: bool, label: str) -> list[str]:
    """Run an s5cmd command, stream output to the user line-by-line, and
    return the list of S3 keys (bucket-prefix stripped, e.g.
    ``www/Insta360/foo.mp4``) that were actually uploaded. Used to feed
    the per-file CloudFront invalidation.

    s5cmd's default text format prints one ``cp <local> <s3-uri>`` line
    per completed transfer; --if-size-differ skips emit nothing. Output
    is forwarded as each line arrives so multi-GB bulk syncs show
    progress in real time; the same lines are also retained for parsing.
    Parse is best-effort — a parse miss invalidates fewer keys (cache
    stays warm for some objects we just rewrote), which is preferable
    to a parse mistake that issues spurious invalidations.
    """
    if dry_run:
        print(f"Would run: {cmd}")
        return []
    LOG.logger.info(label)
    proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"{label} exited with {proc.returncode}")
    keys: list[str] = []
    for line in captured:
        m = _S5CMD_CP_RE.match(line)
        if not m:
            continue
        rest = m.group(1)[len("s3://"):]
        slash = rest.find("/")
        if slash > 0:
            keys.append(rest[slash + 1:])
    return keys


def _run_upload_command(dry_run: bool) -> list[str]:
    """Run one s5cmd sync per ship-target pattern and return the union of
    uploaded S3 keys.

    Per-pattern globs avoid s5cmd's flaky behavior with top-level broken
    symlinks and subdir recursion under a single ``PATH/*`` source —
    each pattern is non-recursive and matches only the files we
    actually want.

    Patterns that match no local files are skipped silently — s5cmd
    treats an empty glob as a hard ERROR ("no match found"), which
    pollutes the log with a scary line for the routine case where, e.g.,
    no VID_*.m4a sidecars exist.
    """
    import glob as _glob
    keys: list[str] = []
    for pattern in BULK_UPLOAD_PATTERNS:
        local_path = os.path.join(VIDEO_DIR, pattern)
        if any(ch in pattern for ch in "*?["):
            if not _glob.glob(local_path):
                LOG.logger.info(
                    f"Bulk sync ({pattern}): no local matches, skipping")
                continue
        elif not os.path.exists(local_path):
            LOG.logger.info(
                f"Bulk sync ({pattern}): {local_path} not present, skipping")
            continue
        cmd = _build_bulk_sync_cmd(pattern)
        keys += _run_s5cmd_capturing_uploads(
            cmd, dry_run, f"Bulk sync ({pattern})")
    return keys


POSES_BROTLI_CACHE_DIR = os.path.join(VIDEO_DIR, ".poses_br")


def _upload_poses(dry_run: bool) -> list[str]:
    """Upload `.poses` files to S3 with `Content-Encoding: br`.

    `.poses` files live raw (uncompressed) in VIDEO_DIR after the WEB_DIR
    collapse — the previous staging step (brotli-on-write to WEB_DIR via
    the now-deleted `copy_to_web_dir`) is gone.

    Brotli quality=11 is the slowest spec setting (~5+ minutes for 425
    files), so we keep a persistent on-disk cache of the compressed bytes
    at `VIDEO_DIR/.poses_br/`. Each run only recompresses files whose
    raw .poses mtime is newer than the cached .br file; everything else
    is reused. The cache dir is hidden (leading dot) so it won't get
    picked up by the bulk sync's `VID/*` glob. We then `s5cmd cp
    --if-size-differ` from the cache so the network round-trip is
    incremental too. Excluded from the main bulk sync via UPLOAD_COMMAND.
    """
    import brotli
    import glob

    poses_files = sorted(glob.glob(os.path.join(VIDEO_DIR, "*.poses")))
    if not poses_files:
        LOG.logger.info("No .poses files to upload")
        return []
    if dry_run:
        print(f"Would brotli-compress (incremental) and upload {len(poses_files)} "
              f".poses files with Content-Encoding: br to {INSTA360_S3_PREFIX}")
        return []

    os.makedirs(POSES_BROTLI_CACHE_DIR, exist_ok=True)
    # Partition into already-cached (skip) and to-do (compress).
    # Cache validity = file exists AND size > 0 AND mtime >= source mtime.
    # The size>0 check catches partial writes left by SIGINT/SIGKILL during
    # the original (non-atomic) brotli loop; without it a 0-byte cache file
    # with a fresh mtime would be reused forever and shipped to S3.
    todo: list[tuple[str, str]] = []
    n_cached = 0
    for src_path in poses_files:
        name = os.path.basename(src_path)
        dst = os.path.join(POSES_BROTLI_CACHE_DIR, name)
        if (os.path.exists(dst)
                and os.path.getsize(dst) > 0
                and os.path.getmtime(dst) >= os.path.getmtime(src_path)):
            n_cached += 1
        else:
            todo.append((src_path, dst))
    n_compressed = 0
    if todo:
        # brotli q=11 is single-threaded ~5s/file at 200-500 KB; on a
        # cold cache (~425 files) that's 30+ min serially. The C
        # extension releases the GIL during compress(), so threads
        # parallelize cleanly. ThreadPoolExecutor avoids the
        # multiprocessing-spawn cost of re-importing this module's
        # heavy deps in each child.
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import tempfile as _tempfile

        def _compress_one(src_dst: tuple[str, str]) -> str:
            src, dst = src_dst
            with open(src, "rb") as fin:
                raw = fin.read()
            compressed = brotli.compress(raw, quality=11)
            # Atomic write: stage to a temp file in the same dir, then
            # os.replace into the cache slot. A SIGINT between fdopen and
            # replace leaves only the .tmp file (cleaned up below); the
            # cache slot is either absent or a fully-written previous
            # version — never a 0-byte / truncated half-write.
            fd, tmp = _tempfile.mkstemp(
                dir=POSES_BROTLI_CACHE_DIR,
                prefix=".tmp_" + os.path.basename(dst) + ".",
            )
            try:
                with os.fdopen(fd, "wb") as fout:
                    fout.write(compressed)
                os.replace(tmp, dst)
            except BaseException:
                # Includes KeyboardInterrupt / SystemExit so a kill mid-
                # write doesn't leave the .tmp around forever.
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
                raise
            return src

        workers = min(os.cpu_count() or 4, 8)
        LOG.logger.info(
            f".poses brotli: {len(todo)} to compress, {n_cached} cached, "
            f"{workers} workers")
        log_every = max(1, len(todo) // 10)
        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = [ex.submit(_compress_one, item) for item in todo]
            for i, fut in enumerate(as_completed(futures), 1):
                fut.result()  # propagate exceptions
                if i % log_every == 0 or i == len(todo):
                    LOG.logger.info(f".poses brotli: {i}/{len(todo)}")
                n_compressed = i
    LOG.logger.info(
        f".poses brotli cache: {n_compressed} (re)compressed, {n_cached} reused")

    cmd = (
        f"AWS_PROFILE={AWS_PROFILE} s5cmd cp "
        f"--if-size-differ "
        f"--content-encoding br "
        f"--cache-control '{CACHE_CONTROL_BINARY}' "
        f"'{POSES_BROTLI_CACHE_DIR}/*.poses' {INSTA360_S3_PREFIX}"
    )
    return _run_s5cmd_capturing_uploads(
        cmd, dry_run,
        f"Uploading {len(poses_files)} .poses files (Content-Encoding: br, "
        f"--if-size-differ) to {INSTA360_S3_PREFIX}")


def _upload_slogs(dry_run: bool) -> list[str]:
    """Upload `.slog` files to S3 with `Content-Encoding: gzip`.

    `.slog` files are stored on disk uncompressed (human-inspectable per
    SLOG_FORMAT.md). S3 serves them gzipped via Content-Encoding so the
    browser decompresses transparently. We gzip into a temp directory,
    then `s5cmd cp --if-size-differ` the lot — `--if-size-differ` HEADs
    each S3 object first and only uploads when sizes don't match, so
    re-runs cost network only for `.slog` files whose gzipped output has
    actually changed (almost always: none). Excluded from the main
    s5cmd sync via UPLOAD_COMMAND.
    """
    import gzip
    import glob
    import tempfile

    slog_files = sorted(glob.glob(os.path.join(VIDEO_DIR, "*.slog")))
    if not slog_files:
        LOG.logger.info("No .slog files to upload")
        return []
    if dry_run:
        print(f"Would upload {len(slog_files)} .slog files with "
              f"Content-Encoding: gzip to {INSTA360_S3_PREFIX}")
        return []

    with tempfile.TemporaryDirectory(prefix="slog_gz_") as tmpdir:
        for src in slog_files:
            name = os.path.basename(src)
            dst = os.path.join(tmpdir, name)
            with open(src, "rb") as fin, gzip.open(dst, "wb", compresslevel=6) as fout:
                shutil.copyfileobj(fin, fout)
        cmd = (
            f"AWS_PROFILE={AWS_PROFILE} s5cmd cp "
            f"--if-size-differ "
            f"--content-encoding gzip "
            f"--content-type 'application/octet-stream' "
            f"--cache-control '{CACHE_CONTROL_BINARY}' "
            f"'{tmpdir}/*.slog' {INSTA360_S3_PREFIX}"
        )
        return _run_s5cmd_capturing_uploads(
            cmd, dry_run,
            f"Uploading {len(slog_files)} .slog files (pre-gzipped, "
            f"Content-Encoding: gzip, --if-size-differ) to {INSTA360_S3_PREFIX}")


def _upload_analyze(dry_run: bool) -> list[str]:
    """Publish the staged review/tracks artifacts from WEB_ANALYZE_DIR to S3.

    The analyze stage regenerates events_index.json + Events/ + Sessions/ +
    AIS/ + AISDays/ and stages them into WEB_ANALYZE_DIR, but historically
    only aws_sync.sh (the frontend deploy) pushed them to S3 — so a plain
    race_upload.py refreshed the data locally while review.html / tracks.html
    kept serving the previous deploy's copy. This closes that gap.

    Flags mirror aws_sync.sh: a short max-age so a freshly-pushed
    events_index.json / AISDays payload surfaces at the CloudFront edge
    within a minute, and the client-write-only events_edits.json / Comments/
    are excluded so we never clobber live user edits. `aws s3 sync` is
    incremental, so a steady-state re-run transfers nothing and returns no
    keys. Returns the bucket-relative keys actually uploaded, for the shared
    CloudFront invalidation in _do_upload.
    """
    if not os.path.isdir(WEB_ANALYZE_DIR):
        LOG.logger.info(
            f"No staged Analyze/ at {WEB_ANALYZE_DIR}; skipping Analyze upload")
        return []
    cmd = (
        f"AWS_PROFILE={AWS_PROFILE} aws s3 sync "
        f"'{WEB_ANALYZE_DIR}' '{ANALYZE_S3_PREFIX}' "
        f"--no-progress "
        f"--cache-control 'public, max-age=60' "
        f"--exclude 'events_edits.json' --exclude 'Comments/*'"
    )
    if dry_run:
        print(f"Would run: {cmd}")
        return []
    LOG.logger.info("Syncing Analyze/ -> S3")
    proc = subprocess.Popen(
        cmd, shell=True,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, bufsize=1,
    )
    captured: list[str] = []
    assert proc.stdout is not None
    for line in proc.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        captured.append(line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"aws s3 sync Analyze exited with {proc.returncode}")
    keys: list[str] = []
    for line in captured:
        m = _AWS_S3_UPLOAD_RE.match(line)
        if not m:
            continue
        rest = m.group(1)[len("s3://"):]
        slash = rest.find("/")
        if slash > 0:
            keys.append(rest[slash + 1:])
    return keys


def _invalidate_cf(paths: list[str], dry_run: bool) -> None:
    """Punch through CloudFront edge caches for keys we just rewrote.
    `paths` is a list of bucket-relative keys (e.g. ``www/Insta360/foo.mp4``)
    — the function prefixes ``/`` to match the cache behavior. AWS bills
    invalidations per *path argument*, with 1000/month free; we fold the
    keys into a single API call, which counts as one invalidation request
    spanning N paths.

    Empty list → no-op (no API call, nothing to invalidate). The bulk
    s5cmd sync only emits cp lines for files that were actually uploaded,
    so on a steady-state re-run this stays at zero.
    """
    if not paths:
        LOG.logger.info("No CloudFront invalidation needed (no uploads).")
        return
    cf_paths = sorted({"/" + p.lstrip("/") for p in paths})
    # --no-cli-pager: AWS CLI v2 otherwise pipes the JSON response into
    # $AWS_PAGER (default `less`) and blocks on a keypress, wedging the
    # pipeline.
    cmd = ["aws", "cloudfront", "create-invalidation",
           "--no-cli-pager",
           "--distribution-id", CF_DIST_ID,
           "--paths", *cf_paths]
    if dry_run:
        print("Would run:", " ".join(cmd))
        return
    LOG.logger.info(
        f"CloudFront invalidation: {len(cf_paths)} path(s) on {CF_DIST_ID}")
    # Inherit AWS_PROFILE from the parent env (set globally in this script
    # via os.environ, mirroring the s5cmd commands).
    env = {**os.environ, "AWS_PROFILE": AWS_PROFILE}
    rc = subprocess.call(cmd, env=env)
    if rc != 0:
        # Don't fail the whole stage on an invalidation hiccup — the bytes
        # are already in S3, and the worst case is up to 1 week of edge-
        # cache lag (CACHE_CONTROL_BINARY). Caller can re-invalidate
        # manually if needed. Surface as a WARNING so it shows up in the
        # run summary at the end.
        LOG.logger.warning(
            f"create-invalidation exited with {rc}; "
            f"viewers may see stale edge-cached objects until "
            f"{CACHE_CONTROL_BINARY!r} expires.")


def main():
    args = parse_arguments()

    LOG.set_logging_level('INFO')
    G.init_seattle()
    # G.METADATA_PATH and other Data/* paths in global_variables.py are
    # relative; the entire pipeline assumes CWD is /Users/viola/Python/sailing/.
    # Set it once here so any stage can run independently (previously only
    # _do_logs chdir'd, which broke metadata when --no-logs was passed).
    os.chdir("/Users/viola/Python/sailing")
    summary_handler = _install_summary_handler()
    card_report.reset()
    stage_results: list[tuple[str, str]] = []

    if args.upload_only:
        def _upload_only():
            # Refresh videos.json before pushing so the self-repairing
            # splice (legacy VIRB entries pulled from S3) runs on this
            # path too. Without this, --upload-only just re-uploads
            # whatever stale videos.json sits in VIDEO_DIR.
            _ = insta360.create_videos_json()
            changed: list[str] = []
            changed += _run_upload_command(args.dry_run)
            changed += _upload_slogs(args.dry_run)
            changed += _upload_poses(args.dry_run)
            _invalidate_cf(changed, args.dry_run)
            if not args.dry_run:
                insta360.assert_uploadable_set()
        _stage(stage_results, "upload", _upload_only)
        n_err = _print_summary(stage_results, summary_handler)
        sys.exit(1 if n_err else 0)

    def _do_video():
        if args.no_pipeline:
            insta360.copy_video_from_card()
            insta360.extract_video_metadata()
            _ = insta360.create_videos_json()
            insta360.compress_all_videos()
            insta360.compute_all_pose_files(recompute=args.recompute_pose)
        else:
            insta360.process_videos_pipelined(
                delete_after_compress=args.delete_raw,
                skip_upload=args.no_upload,
            )
            insta360.extract_video_metadata()
            _ = insta360.create_videos_json()
            insta360.compute_all_pose_files(recompute=args.recompute_pose)

    def _do_logs():
        bb_log.copy_bblogs_from_card()
        log_sequences = bb_log.find_log_file_sequences(bb_log.BBLOG_DIR)
        bb_log.clean_bad_logs(log_sequences, dry_run=args.dry_run)
        log_sequences = bb_log.find_log_file_sequences(bb_log.BBLOG_DIR)
        for log_files in log_sequences[:]:
            bb_log.compute_pandas_log(log_files, force=args.force_logs)

    def _do_sync():
        # Process each date separately to avoid cross-date clock detection errors.
        big_df = insta360.read_parquet_files()
        all_videos = insta360.list_videos_for_sync()
        dates_seen: dict[str, list[str]] = {}
        for v in all_videos:
            date_str = v.split("_")[1]  # e.g. "20260411"
            dates_seen.setdefault(date_str, []).append(v)
        for date_str in sorted(dates_seen):
            day_videos = dates_seen[date_str]
            needs_sync = [v for v in day_videos
                          if not insta360.existing_log_is_valid(v)]
            if not needs_sync:
                continue
            LOG.logger.info(
                f"Syncing {len(needs_sync)}/{len(day_videos)} videos for "
                f"{date_str} (only missing/stale clips)"
            )
            # Pass only the stale clips. sync_videos_to_bb's Stage 1
            # (tack-based clock detection) reads the full day's BB log
            # internally and only needs gyro from the candidate clips —
            # so feeding it the whole day repeats expensive work for
            # nothing.
            insta360.sync_videos_to_bb(needs_sync, big_df, dry_run=args.dry_run)

    def _do_metadata():
        metadata.add_missing_metadata()

    def _do_analyze():
        run_analyze_pipeline(days=args.analyze_days, dry_run=args.dry_run)

    def _do_upload():
        # After the WEB_DIR collapse there is no staging step. Refresh
        # videos.json against VIDEO_DIR, push the three encoding-distinct
        # sets, surgically invalidate CloudFront for only the keys we
        # actually uploaded this run, then assert the local uploadable set
        # is fully reflected on S3 (raises if any local file is missing).
        _ = insta360.create_videos_json()
        changed: list[str] = []
        changed += _run_upload_command(args.dry_run)
        changed += _upload_slogs(args.dry_run)
        changed += _upload_poses(args.dry_run)
        # Publish the regenerated review/tracks artifacts (events_index.json,
        # Events/, Sessions/, AIS/, AISDays/). Previously only aws_sync.sh did
        # this, so review.html / tracks.html went stale until a frontend
        # deploy. Folded into the same invalidation below.
        changed += _upload_analyze(args.dry_run)
        # Surgical CloudFront invalidation: only the keys s5cmd / aws s3 sync
        # actually uploaded this run, not a wildcard wipe. Steady-state re-runs
        # (everything skipped via --if-size-differ / sync's mtime check)
        # produce no paths and skip the API call entirely.
        _invalidate_cf(changed, args.dry_run)
        if not args.dry_run:
            insta360.assert_uploadable_set()

    def _do_voice_audio():
        # The bulk `upload` stage runs after us and will push the modified
        # mp4(s) via s5cmd sync — pass no_upload=True so we don't double-
        # upload each affected file. When --no-upload is set, neither
        # voice_audio nor the bulk stage uploads, which is the right
        # behaviour for "local-only" runs.
        from Libraries import voice_audio
        voice_audio.auto_apply_audio_drops(
            dry_run=args.dry_run,
            no_upload=True,
        )

    _stage(stage_results, "video",       _do_video,       enabled=not args.no_video)
    _stage(stage_results, "logs",        _do_logs,        enabled=not args.no_logs)
    _stage(stage_results, "sync",        _do_sync,
           enabled=not args.no_sync and not (args.no_video or args.no_logs))
    _stage(stage_results, "metadata",    _do_metadata)
    _stage(stage_results, "analyze",     _do_analyze,     enabled=not args.no_analyze)
    # voice_audio MUST run before upload so each modified mp4 is pushed once
    # with its final audio, not first with camera audio and again after swap.
    _stage(stage_results, "voice_audio", _do_voice_audio, enabled=not args.no_voice_audio)
    _stage(stage_results, "upload",      _do_upload,      enabled=not args.no_upload)

    n_err = _print_summary(stage_results, summary_handler)
    sys.exit(1 if n_err else 0)

if __name__ == "__main__":
    main()
