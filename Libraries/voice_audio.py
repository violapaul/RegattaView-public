"""Auto-apply mic-on-person audio drops to compressed Insta360 videos.

Two ingestion paths feed the same drop directory:
  1. **Auto-ingest from Voice Memos** (when accessible): each pipeline run
     scans the iOS Voice Memos cache for recordings that overlap a local
     video, copies them into AUDIO_DROP_DIR under a stable name, and
     stamps a sub-second `<name>.start.txt` sidecar. The drop dir is the
     long-term archive — iCloud may evict the original eventually.
  2. **Manual drop**: drop *.m4a (or .wav / .mp3 / .aac) into AUDIO_DROP_DIR
     directly (e.g. AirDrop, Voice Memos Share Sheet → Save to Files).

Then, for each audio file in AUDIO_DROP_DIR, this module finds videos
whose BB-sync-corrected timeline (`.slog` header `start_unix_ms`) overlaps
the audio span, swaps in the audio via the swap_video_audio.py core,
re-uploads the modified mp4(s), and records the action in `applied.json`.
Re-runs are no-ops unless an audio file is added or its mtime changes.

Audio start-time policy (drop-dir scan):
  1. sidecar `<name>.start.txt` — one ISO 8601 line; naive (no `Z` or
     `+HH:MM`) is interpreted as system-local wall clock.
  2. iOS Voice Memos DB lookup (sub-second ZDATE; falls back to the cache
     filename's second-precision timestamp if the DB row hasn't synced).
On success the lookup is cached as a sidecar so subsequent runs are
FDA-independent. No mtime fallback — silent wrong answers are worse than
a clear error.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Optional


class VoiceAudioUnresolvable(Exception):
    """Raised when an audio file's recording start can't be determined.
    Workflow rule: Voice Memos is the source of truth; no mtime fallbacks."""

# Use the repo-wide regatta_analysis logger so messages reach race_upload's
# handler. A bare `logging.getLogger(__name__)` would have no handler and the
# stage would log silently into the void.
from Libraries.log_helper import LOG as _RegattaLogger  # noqa: E402
LOG = _RegattaLogger.logger

REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "Scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import swap_video_audio as svy  # noqa: E402

AUDIO_DROP_DIR = "/Users/viola/Data/Insta360/Audio"
AUDIO_EXTS = (".m4a", ".wav", ".mp3", ".aac")
MANIFEST_NAME = "applied.json"
SIDECAR_SUFFIX = ".start.txt"

# Pessimistic upper bound for any single Insta360 video so the prefilter
# can keep the dir-walk cheap without spawning ffprobe per candidate.
PESSIMISTIC_VIDEO_S = 3600.0


def _read_sidecar_start(audio_path: str):
    """Return (start_utc, sidecar_path) or (None, None)."""
    base, _ = os.path.splitext(audio_path)
    candidates = [audio_path + SIDECAR_SUFFIX, base + ".start.txt"]
    for path in candidates:
        if os.path.isfile(path):
            with open(path) as f:
                text = f.read().strip()
            if not text:
                continue
            try:
                return svy.parse_iso_utc(text), path
            except ValueError:
                LOG.warning("Sidecar %s is not parseable ISO 8601: %r", path, text)
    return None, None


_BB_SPANS_CACHE: Optional[list[tuple[datetime, datetime]]] = None


def _bb_log_spans():
    """Return [(start_utc, end_utc), ...] for every BB log parquet under
    `Libraries.insta360.PANDAS_DIRECTORY`. Process-cached.

    Reads only the `unixtime` column (float seconds since epoch) so the
    cost is ~megabytes per parquet, not the full ~hundred-MB session.
    `row_times` is a derived datetime column added downstream by
    `race_logs.read_log_file` and is not stored in the raw parquet.
    """
    global _BB_SPANS_CACHE
    if _BB_SPANS_CACHE is not None:
        return _BB_SPANS_CACHE
    # Local import: pyarrow is a heavyish dep and only this helper needs it.
    import pyarrow.parquet as pq
    from Libraries import insta360 as _insta360
    spans: list[tuple[datetime, datetime]] = []
    paths = sorted(glob.glob(os.path.join(_insta360.PANDAS_DIRECTORY, "*.parquet")))
    for path in paths:
        try:
            t = pq.read_table(path, columns=["unixtime"])
            col = t.column("unixtime")
            if col.length() == 0:
                continue
            start = datetime.fromtimestamp(col[0].as_py(), tz=timezone.utc)
            end = datetime.fromtimestamp(
                col[col.length() - 1].as_py(), tz=timezone.utc)
            spans.append((start, end))
        except Exception as e:
            LOG.debug("bb_log_spans: failed to read %s: %s", path, e)
    LOG.info("voice_audio: indexed %d BB log session(s) for ingest gating",
             len(spans))
    _BB_SPANS_CACHE = spans
    return spans


def _bb_log_overlaps(start, end) -> bool:
    """True if any BB log session's row_times range overlaps [start, end]."""
    for span_start, span_end in _bb_log_spans():
        if span_start <= end and span_end >= start:
            return True
    return False


_VOICE_MEMOS_CACHE = None


def _voice_memos_recordings():
    """One lookup per pipeline run: fetch_all is cheap when DB is populated
    (one query) but ffprobes filesystem-fallback files; cache it."""
    global _VOICE_MEMOS_CACHE
    if _VOICE_MEMOS_CACHE is not None:
        return _VOICE_MEMOS_CACHE
    try:
        from Libraries import voice_memos
        _VOICE_MEMOS_CACHE = voice_memos.fetch_all()
    except Exception as e:
        LOG.debug("Voice Memos DB lookup unavailable: %s", e)
        _VOICE_MEMOS_CACHE = []
    return _VOICE_MEMOS_CACHE


def _nudge_voice_memos():
    """Background-launch Voice Memos.app so CloudKit deltas land in the
    local DB before we read it. No-op if the app isn't installed or the
    `open` command isn't available. Doesn't take focus (`-g`)."""
    try:
        subprocess.run(["open", "-ga", "Voice Memos"],
                       check=False, timeout=5, capture_output=True)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        LOG.debug("Could not nudge Voice Memos.app: %s", e)


def _cache_sidecar(audio_path: str, start_utc: datetime) -> None:
    """Write a `<name>.start.txt` sidecar so future runs don't depend on
    FDA or Voice Memos sync state. Failure is non-fatal."""
    cache_path = audio_path + SIDECAR_SUFFIX
    try:
        with open(cache_path, "w") as f:
            f.write(start_utc.isoformat() + "\n")
        LOG.info("Cached recording start to %s", os.path.basename(cache_path))
    except OSError as e:
        LOG.debug("Could not write %s: %s", cache_path, e)


def _resolve_audio_start(audio_path: str, duration_s: float):
    """Pick the audio's absolute UTC start. Voice Memos is the source of
    truth (the user's policy: "always capture with Voice Memos"). No
    mtime fallback — silent wrong answers are worse than a clear error.

    Priority:
      1. `<name>.start.txt` sidecar (cache from a prior successful lookup,
         or a manual override)
      2. iOS Voice Memos DB lookup (sub-second when DB has the row,
         second-precision via filename otherwise) — and on success the
         result is cached as a sidecar.

    Raises VoiceAudioUnresolvable if neither yields a value. The caller
    is expected to log + skip the file, NOT process it.
    """
    sidecar, sidecar_path = _read_sidecar_start(audio_path)
    if sidecar is not None:
        return sidecar, f"sidecar:{os.path.basename(sidecar_path)}"

    recordings = _voice_memos_recordings()
    if recordings:
        try:
            from Libraries import voice_memos
            match, why = voice_memos.find_match(recordings, audio_path)
            if match:
                _cache_sidecar(audio_path, match["start_utc"])
                return match["start_utc"], f"voice_memos[{match['source']}]:{why}"
        except Exception as e:
            LOG.debug("Voice Memos match failed for %s: %s", audio_path, e)

    raise VoiceAudioUnresolvable(
        f"Cannot resolve recording start for {os.path.basename(audio_path)}. "
        f"Voice Memos returned {len(recordings)} recording(s); none matched by "
        f"duration. Fixes (in order of likelihood):\n"
        f"  - open Voice Memos.app and let it sync (CloudKit may not have "
        f"pushed the latest recording yet)\n"
        f"  - grant Terminal Full Disk Access if 0 recordings were returned\n"
        f"  - drop a <name>.start.txt sidecar with the UTC start time as a "
        f"manual override"
    )


def _scan_videos_for_audio_span(audio_start, audio_end, video_dir):
    """Cheap pre-filter: read 30-byte .slog headers, keep mp4s whose
    `start_unix_ms` could overlap [audio_start, audio_end] given the
    pessimistic video-length upper bound. The svy.match_video duration
    check culls precisely afterward.

    Glob is `V*_*.mp4` so previously-synthesized `VIA_*` audio-only clips
    are reused on subsequent runs instead of triggering a fresh synthesis.
    """
    a_start_ms = audio_start.timestamp() * 1000.0
    a_end_ms = audio_end.timestamp() * 1000.0
    out = []
    for mp4 in sorted(Path(video_dir).glob("V*_*.mp4")):
        slog = mp4.with_suffix(".slog")
        start_ms = svy.read_slog_start_unix_ms(str(slog))
        if start_ms is None:
            continue
        if start_ms < a_start_ms - PESSIMISTIC_VIDEO_S * 1000.0:
            continue
        if start_ms > a_end_ms:
            continue
        out.append(str(mp4))
    return out


_FILENAME_BAD = re.compile(r'[/\\:*?"<>|\x00-\x1f]')


def _safe_filename_component(s: str) -> str:
    s = _FILENAME_BAD.sub("_", s).strip().strip(".")
    return s or "Voice Memo"


def _ingest_basename(rec: dict) -> str:
    """`<label> <YYYYMMDD-HHMMSS>.m4a` — date suffix disambiguates re-recordings
    with the same custom label."""
    label = _safe_filename_component(rec.get("name") or "Voice Memo")
    ts = rec["start_utc"].astimezone().strftime("%Y%m%d-%H%M%S")
    return f"{label} {ts}.m4a"


def _ingest_from_voice_memos(audio_dir: str, video_dir: str) -> None:
    """Copy Voice Memos recordings that overlap local videos into the drop dir,
    stamping each with a sub-second sidecar at copy time. Idempotent; safe to
    call on every run. Silent no-op if Voice Memos is unreachable (no FDA,
    sandboxed, or no recordings yet) — the manual drop-dir flow keeps working.
    """
    recordings = _voice_memos_recordings()
    if not recordings:
        return
    os.makedirs(audio_dir, exist_ok=True)

    # Index existing drop-dir audio by duration so we don't re-ingest a
    # recording the user already exported manually under their own name.
    existing_durs: list[float] = []
    for f in os.listdir(audio_dir):
        if not f.lower().endswith(AUDIO_EXTS):
            continue
        full = os.path.join(audio_dir, f)
        try:
            existing_durs.append(svy.ffprobe_duration_s(full))
        except Exception as e:
            LOG.debug("ffprobe %s: %s", full, e)

    for rec in recordings:
        src = rec.get("path")
        dur = rec.get("duration_s")
        start = rec.get("start_utc")
        if not src or not dur or not start:
            continue
        if not os.path.isfile(src):
            continue
        if any(abs(d - dur) <= 2.0 for d in existing_durs):
            continue
        # Auto-ingest gate: keep recordings whose UTC span overlaps a BB
        # log session ("the boat was on, so this was likely recorded on
        # the boat"). This is broader than the prior "overlaps a local
        # video" gate, so audio-only race days (camera dead/forgotten)
        # auto-ingest too — and the new VIA_ synthesis path in
        # auto_apply_audio_drops handles the missing-video side.
        end = start + timedelta(seconds=dur)
        if not _bb_log_overlaps(start, end):
            continue
        dst_basename = _ingest_basename(rec)
        dst = os.path.join(audio_dir, dst_basename)
        if os.path.exists(dst):
            continue
        try:
            shutil.copy2(src, dst)
        except OSError as e:
            LOG.warning("Could not copy %s → %s: %s", src, dst, e)
            continue
        try:
            with open(dst + SIDECAR_SUFFIX, "w") as f:
                f.write(start.isoformat() + "\n")
        except OSError as e:
            LOG.debug("Could not write sidecar for %s: %s", dst, e)
        LOG.info("Ingested %r from Voice Memos → %s",
                 rec.get("name"), dst_basename)
        existing_durs.append(dur)


def _uncovered_intervals(start, end, matches):
    """Given an audio span [start, end] and a list of VideoMatch records,
    return a list of (gap_start, gap_end) datetime tuples describing the
    portion of [start, end] NOT covered by any non-skipped match.

    Treats overlapping/adjacent video spans as a union. Coalesces touching
    gaps. Excludes matches whose `skip_reason` is set or whose `overlap_s`
    is 0 (no useful overlap means no coverage).
    """
    spans = []
    for m in matches:
        if getattr(m, "skip_reason", None):
            continue
        if m.overlap_s <= 0.0:
            continue
        v_end = m.start_utc + timedelta(seconds=m.duration_s)
        spans.append((max(m.start_utc, start), min(v_end, end)))
    spans.sort()

    gaps = []
    cursor = start
    for s_start, s_end in spans:
        if s_start > cursor:
            gaps.append((cursor, s_start))
        if s_end > cursor:
            cursor = s_end
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def _significant_video_end(matches, min_dur_s):
    """Latest end-time among real (non-synthesized) camera clips that
    overlap the audio and are at least `min_dur_s` long.

    Returns None when the day has no significant video (an audio-only
    session, or only sub-threshold slivers). In that case the caller keeps
    the full audio span — the audio *is* the content, so there is nothing
    to trim it back to.
    """
    ends = []
    for m in matches:
        if getattr(m, "skip_reason", None) or m.overlap_s <= 0.0:
            continue
        # Skip our own synthesized audio-only clips; only real footage counts.
        if os.path.basename(m.path).startswith("VIA_"):
            continue
        if m.duration_s < min_dur_s:
            continue
        ends.append(m.start_utc + timedelta(seconds=m.duration_s))
    return max(ends) if ends else None


def _synthesize_for_uncovered_gaps(audio_path, audio_start, audio_duration_s,
                                   matches, video_dir, dry_run):
    """For each uncovered sub-interval of the audio span, synthesize a
    VIA_* clip and return a list of fresh VideoMatch records to append to
    the caller's match list. The caller's process_one_video loop then
    muxes the audio into each (with the correct per-clip offset_s, which
    match_video computed from the new clip's slog header).

    Two gates keep us from creating clips that immediately get thrown away:

      * **Mic-left-on trim** — the recorder is often left running after the
        camera stops. When the day has significant video, the trailing
        audio-only span past the end of the last real clip is dropped
        rather than rendered as a long black "Audio Only" tail. Audio-only
        days (no significant video) keep the full span.
      * **Minimum duration** — gaps shorter than
        `insta360.MIN_VIDEO_DURATION_S` are skipped, because
        `clean_too_short_videos` (same threshold, called from
        `create_videos_json`) would immediately quarantine them to
        TooSmall/. Synthesizing + muxing such a clip is wasted work and the
        audio is discarded along with it, so we share the one threshold.
    """
    # Lazy import: insta360 pulls in pandas + the parquet corpus, and is
    # only needed on this rare synthesis path.
    from Libraries import insta360
    min_video_s = insta360.MIN_VIDEO_DURATION_S

    audio_end = audio_start + timedelta(seconds=audio_duration_s)
    sig_end = _significant_video_end(matches, min_video_s)
    if sig_end is not None and audio_end > sig_end:
        LOG.info(
            "voice_audio: trimming %.0fs of trailing audio (mic left on) to "
            "end of last significant video at %s",
            (audio_end - sig_end).total_seconds(), sig_end.isoformat())
        audio_end = sig_end

    gaps = [
        (g_start, g_end)
        for (g_start, g_end) in _uncovered_intervals(audio_start, audio_end, matches)
        if (g_end - g_start).total_seconds() >= min_video_s
    ]
    new_matches = []
    if not gaps:
        return new_matches
    if dry_run:
        for g_start, g_end in gaps:
            LOG.info(
                "[dry-run] would synthesize VIA_ clip for %s gap %.1fs starting %s",
                os.path.basename(audio_path),
                (g_end - g_start).total_seconds(), g_start.isoformat())
        return new_matches

    # Local import: blank_video pulls in pandas + the full parquet corpus,
    # which is expensive and only needed in this rare path.
    from Libraries import blank_video

    for g_start, g_end in gaps:
        dur = (g_end - g_start).total_seconds()
        try:
            basename = blank_video.create_for_audio(
                audio_path, g_start, dur, video_dir=video_dir)
        except Exception as e:
            LOG.error(
                "blank_video: synthesis failed for gap starting %s: %s",
                g_start.isoformat(), e)
            continue
        new_path = os.path.join(video_dir, basename + ".mp4")
        new_matches.append(svy.match_video(new_path, audio_start, audio_duration_s))
    return new_matches


def _read_manifest(audio_dir: str) -> dict:
    path = os.path.join(audio_dir, MANIFEST_NAME)
    if not os.path.isfile(path):
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except json.JSONDecodeError:
        LOG.warning("Manifest %s not valid JSON; ignoring", path)
        return {}


def _write_manifest(audio_dir: str, manifest: dict) -> None:
    path = os.path.join(audio_dir, MANIFEST_NAME)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def auto_apply_audio_drops(
    audio_dir: str = AUDIO_DROP_DIR,
    video_dir: str = svy.INSTA360_LOCAL_DIR,
    *,
    dry_run: bool = False,
    no_upload: bool = False,
) -> None:
    """Ingest fresh Voice Memos recordings into `audio_dir`, then scan it for
    new/modified audio files and swap each into the matching videos in
    `video_dir`. Idempotent across re-runs."""
    os.makedirs(audio_dir, exist_ok=True)

    # Background-open Voice Memos.app so any CloudKit deltas land in the
    # local DB before we read it. Cheap; doesn't take focus.
    _nudge_voice_memos()
    # Pull new recordings into the drop dir (long-term archive). No-op if
    # Voice Memos is unreachable.
    _ingest_from_voice_memos(audio_dir, video_dir)

    audio_files = sorted(
        os.path.join(audio_dir, f)
        for f in os.listdir(audio_dir)
        if f.lower().endswith(AUDIO_EXTS)
    )
    if not audio_files:
        LOG.info("No audio drops in %s", audio_dir)
        return

    manifest = _read_manifest(audio_dir)
    changed = False

    for audio in audio_files:
        name = os.path.basename(audio)
        mtime = os.path.getmtime(audio)
        prev = manifest.get(name)
        if prev and abs(prev.get("audio_mtime", 0.0) - mtime) < 1.0:
            LOG.info("Skip %s (already applied %s)", name, prev.get("applied_at"))
            continue

        try:
            duration_s = svy.ffprobe_duration_s(audio)
        except Exception as e:
            LOG.error("Could not probe %s: %s", name, e)
            continue
        try:
            start, source = _resolve_audio_start(audio, duration_s)
        except VoiceAudioUnresolvable as e:
            LOG.error("%s — skipping", e)
            continue
        end = start + timedelta(seconds=duration_s)
        LOG.info("%s: start=%s [%s], duration=%.1fs",
                 name, start.isoformat(), source, duration_s)

        candidates = _scan_videos_for_audio_span(start, end, video_dir)
        matches = [svy.match_video(v, start, duration_s) for v in candidates]
        # Synthesize VIA_ clips for any portion of [start, end] not covered
        # by a real video. Handles both the "no video at all" case and the
        # "camera died mid-race" case in one pass.
        matches += _synthesize_for_uncovered_gaps(
            audio_path=audio, audio_start=start, audio_duration_s=duration_s,
            matches=matches, video_dir=video_dir, dry_run=dry_run)
        if not matches:
            # _synthesize_for_uncovered_gaps already logged the reason
            # (BB log empty, ffmpeg failure, etc.); just move on.
            continue
        svy.print_match_table(matches)

        args_ns = SimpleNamespace(
            audio_offset_s=None,
            bitrate="128k",
            no_camera_backup=False,
            no_upload=no_upload,
            dry_run=dry_run,
            yes=True,  # auto mode: never prompt
        )
        applied_videos = []
        any_failed = False
        for m in matches:
            if m.skip_reason or m.overlap_s <= 0.0:
                continue
            ok = svy.process_one_video(m, audio, args_ns)
            if ok:
                applied_videos.append(os.path.basename(m.path))
            else:
                any_failed = True

        if dry_run:
            LOG.info("[dry-run] %s would have been applied to %d video(s)",
                     name, sum(1 for m in matches
                               if not m.skip_reason and m.overlap_s > 0.0))
            continue

        if applied_videos and not any_failed:
            manifest[name] = {
                "audio_mtime": mtime,
                "audio_start_utc": start.isoformat(),
                "audio_start_source": source,
                "audio_duration_s": duration_s,
                "applied_at": datetime.now(timezone.utc).isoformat(),
                "videos": sorted(applied_videos),
            }
            changed = True
        elif any_failed:
            LOG.warning("Some swaps failed for %s; not recording in manifest", name)

    if changed:
        _write_manifest(audio_dir, manifest)
        LOG.info("Updated %s", os.path.join(audio_dir, MANIFEST_NAME))
