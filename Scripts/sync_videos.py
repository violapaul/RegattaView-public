#!/usr/bin/env /Users/viola/miniforge3/envs/sailing/bin/python

"""
Unified video-to-BoatBrain synchronization tool.

Handles the full pipeline from detecting camera clock errors through
fine-grained gyro/heading alignment to durable metadata correction.

Stages:
  1. Coarse clock offset detection  (automatic, no manual anchor)
  2. Metadata correction             (.ffprobe / .exif, backed up)
  3. Fine per-video alignment        (uncapped correlation search)
  4. Validation report               (GOOD / WEAK / BOUNDARY flags)
  5. Apply .log files + upload       (with --apply / --upload)

Usage:
    # Report mode (default) -- review before committing
    sync_videos.py --date 20260228

    # Apply corrections and upload
    sync_videos.py --date 20260228 --apply --upload

    # Check a single video
    sync_videos.py --video VID_20260228_085613_00_004
"""

import argparse
import os
import sys

from Libraries.log_helper import LOG
from Libraries.global_variables import G

G.init_seattle()

import Libraries.insta360 as insta360

AWS_PROFILE = os.environ.get("AWS_PROFILE", "regatta")
# Mirror of race_upload.py's per-pattern bulk-sync. Each pattern is a
# non-recursive glob so s5cmd never enumerates VIDEO_DIR/raw/,
# VIDEO_DIR/extras/, or .poses_br/ — sidesteps both the broken-symlink
# stat-error issue and the flaky --exclude-during-recurse behavior.
# .slog and .poses are NOT pushed from this script — invoke
# race_upload.py --upload-only for those.
BULK_UPLOAD_PATTERNS = (
    "VID_*.mp4",
    "VID_*.log",
    "VID_*.m4a",
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
        f"'{insta360.VIDEO_DIR}/{pattern}' "
        f"s3://regattaanalysis/www/Insta360/"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Synchronize Insta360 videos with BoatBrain logs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--date", help="Video date (YYYYMMDD)")
    parser.add_argument("--video", help="Single video basename")
    parser.add_argument(
        "--offset", type=int, default=30,
        help="BB window padding in seconds (default 30)",
    )
    parser.add_argument("--apply", action="store_true", help="Write .log files and correct metadata")
    parser.add_argument("--upload", action="store_true", help="Copy to web dir and upload to S3")
    args = parser.parse_args()

    LOG.set_logging_level("INFO")

    if not args.date and not args.video:
        parser.error("Provide --date or --video")

    videos = insta360.list_videos_for_sync(date=args.date, video=args.video)
    if not videos:
        sys.exit(f"No videos found in {insta360.VIDEO_DIR}")

    print(f"Found {len(videos)} video(s)\n")

    # Load BB data
    big_df = insta360.read_parquet_files()

    # Run sync pipeline
    dry_run = not args.apply
    reports = insta360.sync_videos_to_bb(videos, big_df, dry_run=dry_run, offset=args.offset)

    # Check for boundary issues
    boundary = sum(1 for r in reports if r.status == "BOUNDARY")
    if boundary > 0 and args.apply:
        sys.exit(f"Refusing to upload with {boundary} BOUNDARY videos.")

    # Upload if requested
    if args.apply and args.upload:
        print(f"\n=== Upload ===")
        insta360.create_videos_json()
        print("Refreshed videos.json in VIDEO_DIR")
        # Bulk push only — .slog and .poses need their own Content-Encoding
        # passes; for those run `race_upload.py --upload-only`.
        for pattern in BULK_UPLOAD_PATTERNS:
            os.system(_build_bulk_sync_cmd(pattern))
        print("Upload complete. Run `race_upload.py --upload-only` to push "
              ".slog and .poses with their Content-Encoding headers.")
    elif not args.apply:
        if boundary == 0 and reports:
            print("\nAlignments look acceptable. Re-run with --apply to commit.")


if __name__ == "__main__":
    main()
