#!/usr/bin/env /Users/viola/miniforge3/envs/sailing/bin/python
"""CLI driver for virb_migration.migrate_all.

Default mode is dry-run: enumerates work and validates legacy bytes locally
without mutating S3. Pass --execute for the actual run. Resumable via the
per-clip skip-if-present check inside migrate_one.

Examples:
    python Scripts/migrate_virb.py --dry-run
    python Scripts/migrate_virb.py --basenames V1080461 --dry-run
    python Scripts/migrate_virb.py --limit 3 --execute
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

# Allow running as a script from the repo root.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from Libraries.log_helper import LOG
from Libraries import virb_migration as vm


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true",
                   help="Perform S3 writes. Without this flag the run is a dry-run.")
    p.add_argument("--dry-run", action="store_true",
                   help="Explicit dry-run flag (default).")
    p.add_argument("--limit", type=int, default=None,
                   help="Process at most N clips.")
    p.add_argument("--basenames", nargs="+", default=None,
                   help="Restrict to these clip basenames (e.g. V1080461 V1090469).")
    p.add_argument("--log-dir", default="migrations",
                   help="Per-run log directory; one timestamped JSON per invocation.")
    p.add_argument("--no-mp4-copy", action="store_true",
                   help="Skip MP4 server-side copy; only generate .poses/.log sidecars.")
    return p.parse_args()


def main() -> int:
    args = _parse_args()
    dry_run = not args.execute  # default: dry-run unless --execute

    os.makedirs(args.log_dir, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    log_json = os.path.join(args.log_dir, f"virb_{stamp}.json")

    LOG.logger.info(f"virb migration: dry_run={dry_run} limit={args.limit} "
                    f"basenames={args.basenames}")

    bn_set = set(args.basenames) if args.basenames else None
    results = vm.migrate_all(dry_run=dry_run, limit=args.limit, basenames=bn_set,
                             no_mp4_copy=args.no_mp4_copy)

    with open(log_json, "w") as f:
        json.dump({"dry_run": dry_run, "results": results}, f, indent=2, default=str)
    LOG.logger.info(f"wrote {log_json} ({len(results)} clips)")

    errors = sum(1 for r in results
                 for a in r.get("_actions", []) if str(a.get("status", "")).startswith("error"))
    if errors:
        LOG.logger.warning(f"{errors} action(s) errored — see {log_json}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
