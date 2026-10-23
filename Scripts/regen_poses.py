#!/usr/bin/env python
"""Offline driver: regenerate v2 `.poses` files for a date, year, or pattern.

Rebuilds per-video camera-orientation files in the new v2 format (30 Hz,
delta-encoded int16 quaternions, 'PS02' magic — see POSES_FORMAT.md). Raw
pose files are written to VIDEO_DIR. Brotli compression now happens at
upload time in `race_upload.py::_upload_poses` (no on-disk staging copy
after the WEB_DIR collapse).

Selection flags (mutually exclusive — pick one):
    --date YYYY-MM-DD       one day
    --year YYYY             all videos that year
    --pattern GLOB          arbitrary basename glob, e.g. 'VID_2025*'

Examples:
    conda run -n sailing --no-capture-output \\
        python Scripts/regen_poses.py --date 2025-10-11
    conda run -n sailing --no-capture-output \\
        python Scripts/regen_poses.py --year 2025
    conda run -n sailing --no-capture-output \\
        python Scripts/regen_poses.py --pattern 'VID_202510*'

No upload: this only writes local files. Push to S3 via
`python Scripts/race_upload.py --upload-only` (which invokes
_upload_poses for you) or a direct s5cmd cp for a subset.
"""
from __future__ import annotations

import argparse
import fnmatch
import os
import re
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from Libraries import insta360  # noqa: E402


def _select_basenames(args) -> list[str]:
    """Return sorted list of VID_* basenames (no extension) to regenerate.

    By default only `_00_` primary-fisheye clips are returned — `_10_`
    companion clips never have IMU/ffprobe/telemetry sidecars (those are
    extracted from the primary) so they'd fail pose estimation. Pass
    --include-companions to override.
    """
    # .insv (raw) and .ffprobe/.imu/.telemetry sidecars all live in RAW_DIR
    # after the raw-subdir refactor; the .poses output stays in VIDEO_DIR.
    entries = os.listdir(insta360.RAW_DIR)
    insvs = [f for f in entries if f.endswith('.insv') and f.startswith('VID_')]
    if args.date:
        ymd = args.date.replace('-', '')
        pat = re.compile(rf'^VID_{ymd}_.*\.insv$')
        matches = [f for f in insvs if pat.match(f)]
    elif args.year:
        pat = re.compile(rf'^VID_{args.year}\d{{4}}_.*\.insv$')
        matches = [f for f in insvs if pat.match(f)]
    else:
        # args.pattern: match against basename (with or without .insv)
        pat = args.pattern
        if not pat.endswith('.insv'):
            pat = pat.rstrip('*') + '*.insv' if '*' in pat else pat + '.insv'
        matches = fnmatch.filter(insvs, pat)
    if not args.include_companions:
        matches = [f for f in matches if '_00_' in f]
    return sorted(os.path.splitext(f)[0] for f in matches)


def _ensure_sidecars(basename: str) -> bool:
    """Check for and (try to) regenerate the three sidecar files that
    `compute_video_poses` needs. Returns True if all three exist after
    the repair attempt.
    """
    rd = insta360.RAW_DIR
    needed = [basename + ext for ext in ('.ffprobe', '.imu', '.telemetry')]
    missing = [f for f in needed if not os.path.exists(os.path.join(rd, f))]
    if not missing:
        return True
    # extract_video_metadata is idempotent — it skips files that already have
    # their sidecars, and fills in any missing ones by re-running exiftool /
    # ffprobe / telemetry_parser against the .insv. Only helps if the .insv
    # is still present locally.
    if not os.path.exists(os.path.join(rd, basename + '.insv')):
        return False
    insta360.extract_video_metadata()
    return all(os.path.exists(os.path.join(rd, f)) for f in needed)


def main() -> int:
    ap = argparse.ArgumentParser(
        description='Regenerate v2 .poses files (30 Hz + delta + brotli-stage).')
    sel = ap.add_mutually_exclusive_group(required=True)
    sel.add_argument('--date', help='YYYY-MM-DD')
    sel.add_argument('--year', help='YYYY')
    sel.add_argument('--pattern', help='Basename glob, e.g. VID_2025*')
    ap.add_argument('--dry-run', action='store_true',
                    help='List selected clips; do not regenerate')
    ap.add_argument('--include-companions', action='store_true',
                    help='Also include _10_ companion clips (normally skipped: '
                    'they have no IMU sidecars so pose estimation would fail)')
    ap.add_argument('--skip-existing', action='store_true',
                    help='Skip clips whose raw .poses in VIDEO_DIR is already '
                    "v2 format (starts with 'PS02'). Useful for resuming a "
                    'partial run. Legacy v1 files are re-regenerated.')
    args = ap.parse_args()

    basenames = _select_basenames(args)
    if not basenames:
        print('No matching videos found', file=sys.stderr)
        return 2
    print(f'Selected {len(basenames)} clip(s):')
    for bn in basenames[:5]:
        print(f'  {bn}')
    if len(basenames) > 5:
        print(f'  ... ({len(basenames) - 5} more)')
    if args.dry_run:
        return 0

    total_raw = 0
    ok = 0
    skipped: list[tuple[str, str]] = []  # (basename, reason)
    t0 = time.time()
    for i, bn in enumerate(basenames, 1):
        ts = time.time()
        src = os.path.join(insta360.VIDEO_DIR, bn + '.poses')
        if args.skip_existing and os.path.exists(src):
            with open(src, 'rb') as f:
                head = f.read(4)
            if head == b'PS02':
                print(f'  [{i:>3}/{len(basenames)}] {bn}: SKIP (already v2)',
                      flush=True)
                continue
        if not _ensure_sidecars(bn):
            reason = 'missing sidecars (and .insv unavailable for re-extraction)'
            print(f'  [{i:>3}/{len(basenames)}] {bn}: SKIP ({reason})', flush=True)
            skipped.append((bn, reason))
            continue
        try:
            insta360.compute_video_poses(bn, recompute=True)
            with open(src, 'rb') as fin:
                raw = fin.read()
        except Exception as e:
            print(f'  [{i:>3}/{len(basenames)}] {bn}: FAIL {type(e).__name__}: {e}',
                  flush=True)
            skipped.append((bn, f'{type(e).__name__}: {e}'))
            continue
        total_raw += len(raw)
        ok += 1
        print(f'  [{i:>3}/{len(basenames)}] {bn}: '
              f'raw={len(raw):>7,}B  '
              f'[{time.time()-ts:.1f}s]',
              flush=True)

    elapsed = time.time() - t0
    print(f'\nOK: {ok}  SKIPPED/FAILED: {len(skipped)}  of {len(basenames)}')
    if ok:
        print(f'Totals (successes only): raw={total_raw/1024:.1f} KB '
              f'(brotli compression now happens at upload time)')
    print(f'Elapsed: {elapsed:.1f}s ({elapsed/max(1,ok):.1f}s/success)')
    if skipped:
        print('\nSkipped / failed clips:')
        for bn, reason in skipped:
            print(f'  {bn}: {reason}')
    print('\nNext step to publish: run `python Scripts/race_upload.py '
          '--upload-only` (only changed-size .poses will re-upload due to '
          '--if-size-differ).')
    return 1 if skipped else 0


if __name__ == '__main__':
    sys.exit(main())
