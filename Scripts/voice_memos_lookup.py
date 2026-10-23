#!/usr/bin/env python
"""Read iOS Voice Memos recording metadata and (optionally) write start-time
sidecars for the voice_audio pipeline.

Voice Memos stores recording metadata in
`~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/CloudRecordings.db`
(Core Data / SQLite). It's sandbox-protected — Terminal needs **Full Disk
Access** (System Settings → Privacy & Security → Full Disk Access).

Subcommands:
  list                       — print every recording (name, start UTC, dur)
  audio PATH                 — print the start time of the recording matching
                               the file at PATH (by name, then duration)
  write-sidecars DIR         — for each *.m4a/.wav/.mp3 in DIR without an
                               existing `<name>.start.txt`, look up the
                               recording in Voice Memos and write the sidecar.
                               This is the muscle behind the auto race_upload
                               integration.

Examples:
  python Scripts/voice_memos_lookup.py list
  python Scripts/voice_memos_lookup.py audio "/Users/viola/Data/Insta360/Audio/Seaview Ave NW.m4a"
  python Scripts/voice_memos_lookup.py write-sidecars /Users/viola/Data/Insta360/Audio
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from Libraries import voice_memos  # noqa: E402

LOG = logging.getLogger(__name__)


def cmd_list(args):
    recs = voice_memos.fetch_all(args.dir, args.db)
    rec_dir = voice_memos.find_dir(args.dir)
    print(f"# Source dir: {rec_dir}")
    print(f"# {len(recs)} recording(s)")
    print(f"{'name':<40} {'start (UTC)':<32} {'dur_s':>10}  src")
    print("-" * 96)
    for r in recs:
        n = r["name"]
        n = n if len(n) <= 39 else n[:36] + "..."
        dur = f"{r['duration_s']:.2f}" if r["duration_s"] is not None else "?"
        print(f"{n:<40} {r['start_utc'].isoformat():<32} {dur:>10}  {r['source']}")


def cmd_audio(args):
    recs = voice_memos.fetch_all(args.dir, args.db)
    match, why = voice_memos.find_match(recs, args.audio)
    if not match:
        raise SystemExit(f"No match for {args.audio}")
    print(f"name      : {match['name']}")
    print(f"start_utc : {match['start_utc'].isoformat()}")
    print(f"duration  : {match['duration_s']:.2f}s")
    print(f"source    : {match['source']}")
    print(f"matched by: {why}")


def cmd_write_sidecars(args):
    counts = voice_memos.populate_sidecars(args.audio_dir, dry_run=args.dry_run)
    print(f"wrote={counts['written']} "
          f"skipped(existing sidecar)={counts['skipped']} "
          f"unmatched={counts['unmatched']}")


def cmd_schema(args):
    """Diagnostic: list cache dir contents, then dump DB tables + row counts
    + a sample row, opening the DB *without* immutable mode so the WAL is
    visible."""
    import os, sqlite3
    rec_dir = voice_memos.find_dir(args.dir)
    print(f"# Recordings dir: {rec_dir}")
    try:
        for name in sorted(os.listdir(rec_dir)):
            p = os.path.join(rec_dir, name)
            try:
                size = os.path.getsize(p)
            except OSError:
                size = -1
            print(f"  {size:>12}  {name}")
    except PermissionError as e:
        print(f"  (cannot list: {e})")
    print()
    db = Path(args.db).expanduser() if args.db else rec_dir / "CloudRecordings.db"
    if not db.is_file():
        print(f"# No DB at {db}")
        return
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    cur = con.cursor()
    tables = [r[0] for r in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    print(f"# Tables ({len(tables)}) — DB opened with WAL visible:")
    for t in tables:
        try:
            n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except sqlite3.Error as e:
            n = f"err: {e}"
        print(f"  {t}: {n}")
        if isinstance(n, int) and n > 0:
            cols = [c[1] for c in cur.execute(f'PRAGMA table_info("{t}")')]
            print(f"    cols: {cols}")
            row = cur.execute(f'SELECT * FROM "{t}" LIMIT 1').fetchone()
            shown = tuple(repr(v)[:60] for v in row)
            print(f"    sample: {shown}")
    con.close()


def main():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--dir", default=None,
                   help="Override path to the Voice Memos Recordings dir.")
    p.add_argument("--db", default=None,
                   help="Override path to CloudRecordings.db.")
    p.add_argument("--verbose", "-v", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="Dump all recordings.")
    sub.add_parser("schema", help="Diagnostic: dump tables + row counts + dir listing.")

    sa = sub.add_parser("audio", help="Look up the recording matching one audio file.")
    sa.add_argument("audio", help="Path to an audio file (.m4a / .wav / .mp3).")

    sw = sub.add_parser("write-sidecars",
                        help="Write <name>.start.txt for unsidecared audio files in DIR.")
    sw.add_argument("audio_dir", help="Directory containing audio drops.")
    sw.add_argument("--dry-run", action="store_true",
                    help="Print actions but don't write any sidecar files.")

    args = p.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        if args.cmd == "list":
            cmd_list(args)
        elif args.cmd == "audio":
            cmd_audio(args)
        elif args.cmd == "write-sidecars":
            cmd_write_sidecars(args)
        elif args.cmd == "schema":
            cmd_schema(args)
    except voice_memos.VoiceMemosUnavailable as e:
        raise SystemExit(f"voice_memos: {e}")


if __name__ == "__main__":
    sys.exit(main())
