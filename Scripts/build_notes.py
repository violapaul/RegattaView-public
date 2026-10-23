#!/usr/bin/env python
"""Build race notes: render Analyze/Notes/<slug>/note.md -> note.html (pandoc,
self-contained) and rebuild Analyze/Notes/notes_index.json.

A note is a directory under Analyze/Notes/ holding:
  note.md        source (hand-edited)
  meta.yaml      title, date, race, type, tags, summary
  figures/*.png  embedded into note.html at build time
  note.html      BUILT here (standalone, figures embedded base64)

The site's gated viewer (notes.html) fetches note.html via a Lambda-signed URL.
Authoring loop: edit note.md -> `python Scripts/build_notes.py --note <slug>` ->
refresh notes.html?note=<slug>.

Mirrors Scripts/build_lesson_index.py (REPO_ROOT bootstrap, atomic JSON write).
Idempotent. pandoc is required (brew install pandoc).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
NOTES_DIR = REPO_ROOT / "Analyze" / "Notes"
CSS = NOTES_DIR / "_assets" / "style.css"
INDEX = NOTES_DIR / "notes_index.json"
INDEX_VERSION = 1

LOG = logging.getLogger("build_notes")


def _utc_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def _note_dirs() -> list[Path]:
    if not NOTES_DIR.is_dir():
        return []
    return sorted(d for d in NOTES_DIR.iterdir()
                  if d.is_dir() and not d.name.startswith("_") and (d / "note.md").exists())


def _read_meta(note_dir: Path) -> dict:
    p = note_dir / "meta.yaml"
    meta = {}
    if p.exists():
        meta = yaml.safe_load(p.read_text()) or {}
    meta.setdefault("title", note_dir.name)
    meta.setdefault("slug", note_dir.name)
    return meta


def _headline(note_dir: Path, meta: dict) -> str:
    if meta.get("summary"):
        return str(meta["summary"]).strip()
    # else first ATX heading text in note.md
    for line in (note_dir / "note.md").read_text().splitlines():
        m = re.match(r"^#{1,3}\s+(.*)", line.strip())
        if m:
            return m.group(1).strip()
    return ""


def render(note_dir: Path) -> None:
    if not shutil.which("pandoc"):
        sys.exit("ERROR: pandoc not found on PATH (brew install pandoc).")
    meta = _read_meta(note_dir)
    out = note_dir / "note.html"
    cmd = [
        "pandoc", "note.md", "-o", "note.html",
        "--standalone", "--embed-resources", "--toc", "--toc-depth=2",
        "-c", str(CSS),
        "--metadata", f"title={meta['title']}",
    ]
    subprocess.run(cmd, cwd=note_dir, check=True)
    # Open in-note deep-links at the top level (the viewer shows note.html in an iframe).
    html = out.read_text()
    if "<base " not in html:
        html = re.sub(r"(<head[^>]*>)", r"\1\n<base target='_blank'>", html, count=1)
        out.write_text(html)
    LOG.info("rendered %s", out.relative_to(REPO_ROOT))


def build_index() -> None:
    notes = []
    for d in _note_dirs():
        meta = _read_meta(d)
        notes.append({
            "slug": d.name,
            "title": meta.get("title"),
            "date": meta.get("date"),
            "race": meta.get("race"),
            "type": meta.get("type"),
            "tags": meta.get("tags") or [],
            "headline": _headline(d, meta),
        })
    notes.sort(key=lambda n: (n.get("date") or "", n["slug"]), reverse=True)
    payload = {"version": INDEX_VERSION, "generated_utc": _utc_iso(datetime.now(timezone.utc)),
               "notes": notes}
    _atomic_write(INDEX, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    LOG.info("wrote %s (%d notes)", INDEX.relative_to(REPO_ROOT), len(notes))


def main(argv=None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--note", help="build a single note by slug")
    g.add_argument("--all", action="store_true", help="build every note (default)")
    ap.add_argument("--index-only", action="store_true", help="rebuild notes_index.json only")
    args = ap.parse_args(argv)

    if not args.index_only:
        if args.note:
            d = NOTES_DIR / args.note
            if not (d / "note.md").exists():
                sys.exit(f"no note.md in {d}")
            render(d)
        else:
            dirs = _note_dirs()
            if not dirs:
                LOG.warning("no notes found under %s", NOTES_DIR)
            for d in dirs:
                render(d)
    build_index()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
