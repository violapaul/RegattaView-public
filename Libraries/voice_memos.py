"""Read iOS Voice Memos recording metadata via the macOS-synced cache at
`~/Library/Group Containers/group.com.apple.VoiceMemos.shared/Recordings/`.

Two data sources, in priority order:

1. **`CloudRecordings.db` (Core Data SQLite, with WAL).** Holds the
   sub-second ZDATE (REAL, seconds-since-2001-UTC), the user-facing
   ZCUSTOMLABEL ("Seaview Ave NW"), and ZDURATION. We open with
   `mode=ro` *without* `immutable=1` so SQLite reads the WAL — without
   that, recently-recorded rows are invisible.
2. **Filesystem fallback.** If the DB is empty / unreachable, parse the
   cache filenames `YYYYMMDD HHMMSS-CHECKSUM.m4a` (system-local time,
   1-second precision) and probe durations via ffprobe. Sufficient to
   unblock when the DB hasn't synced yet.

The dir is sandbox-protected; reading it requires the calling process
(usually Terminal) to have **Full Disk Access** (System Settings →
Privacy & Security → Full Disk Access).

Public functions:
  fetch_all(dir=None, db=None)    -> list[Recording]
  find_match(recs, audio_path)    -> (Recording | None, why: str | None)
  populate_sidecars(audio_dir, *, dry_run=False) -> dict counts
  has_access()                    -> bool

`Recording`: {name, start_utc, duration_s, path, source}.
  source ∈ {'db', 'db+fs', 'fs'} — flags precision (db = sub-second).
"""
from __future__ import annotations

import logging
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from Libraries.log_helper import LOG as _RegattaLogger  # noqa: E402
LOG = _RegattaLogger.logger

DEFAULT_RECORDINGS_DIRS = [
    Path.home() / "Library" / "Group Containers"
        / "group.com.apple.VoiceMemos.shared" / "Recordings",
    Path.home() / "Library" / "Application Support"
        / "com.apple.voicememos" / "Recordings",
]

# Core Data reference date is 2001-01-01 00:00:00 UTC.
# unix_epoch = ZDATE + COREDATA_EPOCH_OFFSET_S
COREDATA_EPOCH_OFFSET_S = 978307200

# Voice Memos cached filenames look like '20260427 173028-7BDEC22D.m4a'
# (date + space + time + dash + 8-hex). The timestamp is system-local.
RECORDING_NAME_RE = re.compile(
    r"^(?P<date>\d{8})\s+(?P<time>\d{6})-(?P<sfx>[0-9A-F]+)\.m4a$",
    re.IGNORECASE,
)

AUDIO_EXTS = (".m4a", ".wav", ".mp3", ".aac")
SIDECAR_SUFFIX = ".start.txt"
DURATION_TOLERANCE_S = 2.0  # iMessage re-encode drift; 2s is generous


class VoiceMemosUnavailable(Exception):
    """Raised when the cache dir can't be located/read (most often: missing FDA)."""


def find_dir(override: str | None = None) -> Path:
    if override:
        p = Path(override).expanduser()
        if not p.is_dir():
            raise VoiceMemosUnavailable(f"--dir not a directory: {p}")
        return p
    for p in DEFAULT_RECORDINGS_DIRS:
        try:
            if p.is_dir():
                return p
        except PermissionError:
            continue
    raise VoiceMemosUnavailable(
        "Voice Memos Recordings directory not found in any expected location. "
        "Open Voice Memos.app once to populate the local cache, then ensure "
        "Terminal has Full Disk Access (System Settings → Privacy & Security → "
        "Full Disk Access)."
    )


def has_access() -> bool:
    try:
        d = find_dir()
        # Listing the dir is the actual permission check.
        list(d.iterdir())
        return True
    except (VoiceMemosUnavailable, PermissionError, OSError):
        return False


def _ffprobe_duration_s(audio_path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(audio_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def _parse_filename_local(name: str) -> datetime | None:
    """Parse `20260427 173028-7BDEC22D.m4a` into a system-local naive datetime."""
    m = RECORDING_NAME_RE.match(name)
    if not m:
        return None
    return datetime.strptime(
        f"{m['date']}{m['time']}", "%Y%m%d%H%M%S"
    )


def _scan_filesystem(rec_dir: Path) -> dict[str, dict]:
    """Filesystem index by basename. Cheap (no ffprobe yet); duration is
    filled in lazily later only for records that are returned."""
    try:
        entries = sorted(rec_dir.iterdir())
    except PermissionError as e:
        raise VoiceMemosUnavailable(
            f"Cannot list {rec_dir}: {e}. Terminal probably needs Full Disk Access."
        ) from e
    out: dict[str, dict] = {}
    for f in entries:
        if not f.is_file() or f.suffix.lower() != ".m4a":
            continue
        local_dt = _parse_filename_local(f.name)
        if local_dt is None:
            continue
        out[f.name] = {
            "name": f.stem,
            "start_utc": local_dt.astimezone(timezone.utc),
            "duration_s": None,
            "path": str(f),
            "source": "fs",
        }
    return out


def _read_db(rec_dir: Path, override: str | None = None) -> list[dict]:
    """Open CloudRecordings.db (read-only, WAL-aware) and return rows.
    Empty list if the DB is missing/unreadable/empty — caller falls back."""
    candidates = [Path(override).expanduser()] if override else [
        rec_dir / "CloudRecordings.db",
    ]
    for db_path in candidates:
        try:
            if not db_path.is_file():
                continue
        except PermissionError:
            continue
        try:
            # mode=ro lets SQLite read both main file and WAL.
            # immutable=1 would skip the WAL — that's how rows can disappear.
            con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        except sqlite3.OperationalError as e:
            LOG.debug("Cannot open %s: %s", db_path, e)
            continue
        try:
            cur = con.cursor()
            tables = {r[0] for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            # Apple has used both ZCLOUDRECORDING and ZRECORDING; pick whichever
            # is populated. Some macOS versions have both, only one with rows.
            for table in ("ZCLOUDRECORDING", "ZRECORDING"):
                if table not in tables:
                    continue
                cols = {r[1] for r in cur.execute(f"PRAGMA table_info({table})")}
                name_col = next((c for c in (
                    "ZCUSTOMLABEL", "ZENCRYPTEDTITLE", "ZTITLE") if c in cols), None)
                path_col = next((c for c in (
                    "ZPATH", "ZAUDIOFILE", "ZFILEPATH") if c in cols), None)
                if not (name_col and "ZDATE" in cols and "ZDURATION" in cols):
                    continue
                select_cols = [name_col, "ZDATE", "ZDURATION"]
                if path_col:
                    select_cols.append(path_col)
                rows = cur.execute(
                    f"SELECT {', '.join(select_cols)} FROM {table} "
                    f"WHERE ZDATE IS NOT NULL"
                ).fetchall()
                if not rows:
                    continue
                out = []
                for row in rows:
                    name, zdate, dur = row[0], row[1], row[2]
                    path_field = row[3] if path_col and len(row) > 3 else None
                    out.append({
                        "name": str(name) if name else "(untitled)",
                        # Sub-second: ZDATE is REAL (float64).
                        "start_utc": datetime.fromtimestamp(
                            float(zdate) + COREDATA_EPOCH_OFFSET_S,
                            tz=timezone.utc),
                        "duration_s": float(dur or 0.0),
                        "_path_field": path_field,
                        "source": "db",
                    })
                return out
        finally:
            con.close()
    return []


def fetch_all(directory: str | None = None,
              db: str | None = None) -> list[dict]:
    """Return all known recordings. DB rows (sub-second ZDATE) win over the
    filename-derived second-precision fallback. Sorted newest-first.

    Filesystem records that are also in the DB are merged so callers see
    the file path on disk plus the DB's name and sub-second start_utc.
    """
    rec_dir = find_dir(directory)
    fs_index = _scan_filesystem(rec_dir)  # basename → record (no duration)
    db_records = _read_db(rec_dir, db)

    merged: list[dict] = []
    used_basenames: set[str] = set()
    for db_rec in db_records:
        # ZPATH is typically the bare filename; if it's a longer path we still
        # match by basename. Some macOS versions store nothing in ZPATH, in
        # which case we match by start_utc within ±1.5s against filename time.
        basename = None
        path_field = db_rec.get("_path_field")
        if path_field:
            cand = os.path.basename(str(path_field))
            if cand in fs_index:
                basename = cand
        if basename is None:
            for fs_name, fs_rec in fs_index.items():
                if fs_name in used_basenames:
                    continue
                delta = (db_rec["start_utc"] - fs_rec["start_utc"]).total_seconds()
                if -1.5 <= delta <= 1.5:
                    basename = fs_name
                    break
        if basename:
            used_basenames.add(basename)
            fs_rec = fs_index[basename]
            merged.append({
                "name": db_rec["name"],
                "start_utc": db_rec["start_utc"],  # sub-second
                "duration_s": db_rec["duration_s"] or _ffprobe_duration_s(fs_rec["path"]),
                "path": fs_rec["path"],
                "source": "db+fs",
            })
        else:
            merged.append({
                "name": db_rec["name"],
                "start_utc": db_rec["start_utc"],
                "duration_s": db_rec["duration_s"],
                "path": None,
                "source": "db",
            })

    # Filesystem-only fallbacks (DB didn't see them — recent recording, or
    # DB empty entirely). ffprobe duration here.
    for basename, fs_rec in fs_index.items():
        if basename in used_basenames:
            continue
        try:
            fs_rec["duration_s"] = _ffprobe_duration_s(fs_rec["path"])
        except subprocess.CalledProcessError as e:
            LOG.debug("ffprobe failed on %s: %s", basename, e)
            continue
        merged.append(fs_rec)

    merged.sort(key=lambda r: r["start_utc"], reverse=True)
    return merged


def _ffprobe_duration_s(audio_path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(audio_path)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    return float(out)


def find_match(recordings: list[dict], audio_path: str):
    """Best-effort match by duration. The Voice Memos cache filename doesn't
    contain the user-facing label (the iMessage'd file's basename), so we
    rely solely on duration to pair the dropped audio against the cache.

    Returns (recording_or_None, why_or_None). Ambiguous duration matches
    return None to avoid silently picking the wrong recording.
    """
    try:
        audio_dur = _ffprobe_duration_s(audio_path)
    except Exception as e:
        LOG.warning("ffprobe failed on %s: %s", audio_path, e)
        return None, None
    by_dur = [r for r in recordings
              if abs(r["duration_s"] - audio_dur) <= DURATION_TOLERANCE_S]
    if len(by_dur) == 1:
        delta = by_dur[0]["duration_s"] - audio_dur
        return by_dur[0], f"duration ({audio_dur:.1f}s ± {delta:+.2f}s)"
    if len(by_dur) > 1:
        LOG.warning(
            "Ambiguous duration match for %s (%.1fs): %d candidates within "
            "%.1fs (%s). Refusing to guess; rename or sidecar manually.",
            os.path.basename(audio_path), audio_dur, len(by_dur),
            DURATION_TOLERANCE_S,
            ", ".join(r["name"] for r in by_dur[:4]),
        )
    return None, None


def populate_sidecars(audio_dir: str, *, dry_run: bool = False) -> dict:
    """Write `<basename>.start.txt` for each audio file in `audio_dir` whose
    sidecar is missing and which has a match in Voice Memos. Idempotent.

    Returns counts: {written, skipped, unmatched, no_audio}.
    Logs at INFO level. Silently no-ops if Voice Memos is unavailable
    (so it can be wired into a pipeline without breaking when FDA is off)."""
    counts = {"written": 0, "skipped": 0, "unmatched": 0, "no_audio": 0}
    audio_dir_p = Path(audio_dir)
    if not audio_dir_p.is_dir():
        return counts
    audio_files = sorted(p for p in audio_dir_p.iterdir()
                         if p.suffix.lower() in AUDIO_EXTS)
    if not audio_files:
        counts["no_audio"] = 1
        return counts

    try:
        recordings = fetch_all()
    except VoiceMemosUnavailable as e:
        LOG.warning("Voice Memos lookup unavailable: %s", e)
        return counts

    LOG.info("Voice Memos: %d recording(s) in DB", len(recordings))

    for f in audio_files:
        sidecar = f.with_name(f.name + SIDECAR_SUFFIX)
        if sidecar.exists():
            counts["skipped"] += 1
            continue
        match, why = find_match(recordings, str(f))
        if not match:
            LOG.warning("  %s: no match in Voice Memos DB", f.name)
            counts["unmatched"] += 1
            continue
        # Write the recording start in UTC ISO so the value is unambiguous in
        # the file even though the sidecar parser also accepts naive local.
        text = match["start_utc"].isoformat()
        if dry_run:
            LOG.info("  [dry-run] %s ← %s (%s)", f.name, text, why)
            continue
        sidecar.write_text(text + "\n")
        LOG.info("  %s ← %s (%s)", f.name, text, why)
        counts["written"] += 1
    return counts
