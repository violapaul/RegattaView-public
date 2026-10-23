#!/usr/bin/env /Users/viola/miniforge3/envs/sailing/bin/python

from Libraries.log_helper import LOG
from Libraries.global_variables import G
import Libraries.insta360 as insta360

# Set logging level
LOG.set_logging_level('INFO')
G.init_seattle()

def main():
    # Get date from command line argument
    import argparse
    parser = argparse.ArgumentParser(
        description='Recompute logs for videos from a specific date using the current sync pipeline'
    )
    parser.add_argument('date', help='Date in YYYYMMDD format (e.g., 20250322)')
    parser.add_argument('--offset', type=int, default=30, help='BB window padding in seconds (default: 30)')
    args = parser.parse_args()

    videos = insta360.list_videos_for_sync(date=args.date)
    if not videos:
        LOG.logger.error(f"No videos found for date {args.date}")
        return

    LOG.logger.info(f"Found {len(videos)} videos for date {args.date}")

    big_df = insta360.read_parquet_files()
    insta360.sync_videos_to_bb(
        videos,
        big_df,
        dry_run=False,
        offset=args.offset,
        force_recompute_existing=True,
    )

if __name__ == "__main__":
    main()