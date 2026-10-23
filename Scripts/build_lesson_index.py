#!/usr/bin/env python
"""Build Analyze/Transcripts/index.json from the per-recording artifacts.

Walks Analyze/Transcripts/*.keypoints.json, joins each with its sibling
.summary.md (for the headline) and freshest .transcript.<engine>.json (for
the transcription model), and emits a single manifest consumed by
lessons.html.

Idempotent — safe to re-run any time. summarize_transcript.py invokes
build_index() as a best-effort final step; this CLI is the manual escape
hatch (covers regenerated/renamed files).
"""
from __future__ import annotations

import argparse
import glob
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

LOG = logging.getLogger(__name__)

DEFAULT_DIR = REPO_ROOT / "Analyze" / "Transcripts"
INDEX_VERSION = 1
HEADLINE_MAX_SKILLS = 3
HEADLINE_MAX_CHARS = 140


def _read_json(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def _utc_iso(dt: datetime) -> str:
    s = dt.astimezone(timezone.utc).isoformat(timespec="seconds")
    return s.replace("+00:00", "Z")


def _atomic_write_json(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def _newest_transcript(dir_path: Path, basename: str) -> tuple[str | None, str | None, float | None]:
    """Return (engine_tag, model_string, audio_duration_s) for the freshest
    transcript file matching <basename>.transcript.<engine>.json, or
    (None, None, None) if absent.
    """
    pattern = str(dir_path / f"{basename}.transcript.*.json")
    candidates = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
    for path in candidates:
        m = re.match(rf"^{re.escape(basename)}\.transcript\.(.+)\.json$", os.path.basename(path))
        if not m:
            continue
        engine_tag = m.group(1)
        try:
            doc = _read_json(Path(path))
            model = doc.get("model")
            duration = doc.get("audio_duration_s")
        except Exception:
            model = None
            duration = None
        return engine_tag, model, duration
    return None, None, None


def _headline_from_summary(summary_path: Path, mode: str, kp_count: int, skill_count: int) -> str:
    """Cheap, scannable one-liner. For lessons: the first few `##` section
    titles. For races: the first highlight bullet. Falls back to the count
    summary if either is unavailable.
    """
    try:
        text = summary_path.read_text()
    except OSError:
        text = ""

    if mode == "lesson":
        sections = []
        for line in text.splitlines():
            m = re.match(r"^\s*##\s+(.+?)\s*$", line)
            if not m:
                continue
            title = m.group(1).strip()
            if title.lower() == "action items":
                continue
            sections.append(title)
            if len(sections) >= HEADLINE_MAX_SKILLS:
                break
        if sections:
            joined = ", ".join(sections)
            head = f"{skill_count} skills · {joined}" if skill_count else joined
            return _truncate(head)

    if mode == "race":
        in_highlights = False
        for line in text.splitlines():
            if re.match(r"^\s*##\s+Highlights\s*$", line, re.IGNORECASE):
                in_highlights = True
                continue
            if in_highlights and re.match(r"^\s*##\s+", line):
                break
            if in_highlights:
                m = re.match(r"^\s*[-\d.*]+\s+(.+?)\s*$", line)
                if m:
                    return _truncate(m.group(1))

    parts = []
    if kp_count:
        parts.append(f"{kp_count} keypoints")
    if mode == "lesson" and skill_count:
        parts.append(f"{skill_count} skills")
    return ", ".join(parts) if parts else "(no headline)"


def _truncate(s: str) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) <= HEADLINE_MAX_CHARS:
        return s
    return s[: HEADLINE_MAX_CHARS - 1].rstrip() + "…"


def _skill_count(keypoints: list[dict], mode: str) -> int:
    if mode != "lesson":
        return 0
    seen = set()
    for kp in keypoints:
        cat = (kp.get("category") or "").strip()
        if cat:
            seen.add(cat)
    return len(seen)


def build_index(dir_path: str | Path = DEFAULT_DIR) -> Path:
    """Scan dir_path for *.keypoints.json and write index.json. Returns path."""
    dir_path = Path(dir_path)
    out_path = dir_path / "index.json"

    recordings = []
    for kp_path in sorted(dir_path.glob("*.keypoints.json")):
        try:
            kp_doc = _read_json(kp_path)
        except Exception as e:
            LOG.warning("skip %s (read failed: %s)", kp_path.name, e)
            continue

        basename = kp_path.name[: -len(".keypoints.json")]
        kps = kp_doc.get("keypoints", [])
        mode = kp_doc.get("mode", "race")
        summary_path = dir_path / f"{basename}.summary.md"
        engine_tag, model_transcript, transcript_duration = _newest_transcript(dir_path, basename)
        skill_count = _skill_count(kps, mode)
        headline = _headline_from_summary(summary_path, mode, len(kps), skill_count)

        recordings.append({
            "audio_basename": basename,
            "audio_file": kp_doc.get("audio_file", basename),
            "audio_start_utc": kp_doc.get("audio_start_utc"),
            "audio_duration_s": kp_doc.get("audio_duration_s") or transcript_duration,
            "mode": mode,
            "model_summary": kp_doc.get("model"),
            "model_transcript": model_transcript,
            "transcript_engine": engine_tag,
            "keypoint_count": len(kps),
            "skill_count": skill_count,
            "headline": headline,
        })

    recordings.sort(key=lambda r: r.get("audio_start_utc") or "", reverse=True)

    out_doc = {
        "version": INDEX_VERSION,
        "generated_utc": _utc_iso(datetime.now(timezone.utc)),
        "recordings": recordings,
    }
    _atomic_write_json(out_path, out_doc)
    LOG.info("wrote %s (%d recordings)", out_path, len(recordings))
    return out_path


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dir", default=str(DEFAULT_DIR),
                    help="Analyze/Transcripts/ directory (default: repo-relative)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    out = build_index(args.dir)
    print(out)


if __name__ == "__main__":
    main()
