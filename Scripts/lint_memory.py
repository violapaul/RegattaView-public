#!/usr/bin/env python3
"""Lint Claude Code memory entries for stale references.

Walks the per-project memory dir (~/.claude/projects/<slug>/memory/) and
flags references that no longer resolve:

  - Markdown links between memory files: [text](other.md)
  - Wiki-style cross-refs: [[memory-name]]
  - Repo-relative paths in backticks: `Scripts/foo.py`, `BACKUP.md`
  - Absolute paths in backticks: `/Users/viola/Data/BBLog`

Exits 0 if clean, 1 if findings, 2 on setup error. Read-only.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    # Resolve to the *main* checkout even when invoked from a worktree:
    # memory is keyed off the canonical repo path. --git-common-dir
    # returns the shared .git dir; its parent is the main checkout.
    out = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        capture_output=True, text=True, check=True,
    )
    return Path(out.stdout.strip()).parent


def memory_dir_for(repo: Path) -> Path:
    # Claude Code derives the slug by replacing / and spaces in the repo
    # path with - and prefixing with -.
    slug = "-" + str(repo).lstrip("/").replace("/", "-").replace(" ", "-")
    return Path.home() / ".claude" / "projects" / slug / "memory"


MD_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
# Anything inside backticks that contains a slash, OR an ALL-CAPS-prefixed
# top-level filename with a known extension. Further filtered below to
# require a file-extension or trailing slash, and to skip globs.
REPO_PATH_RE = re.compile(
    r"`("
    r"[A-Za-z][\w._-]+(?:/[\w.+/_-]+)+/?"
    r"|[A-Z][\w]*\.(?:md|py|sh|json|js|html|css|yml|yaml|hpp|cpp|txt)"
    r")`"
)
ABS_PATH_RE = re.compile(r"`(/Users/[^`]+?)`")
# Strip ::symbol / #anchor / trailing punctuation that isn't part of the path.
PATH_TAIL_STRIP = re.compile(r"(::|#).*$")
GLOB_CHARS = set("*?[]")


def looks_pathlike(s: str) -> bool:
    """Heuristic: must contain a dot in some segment or end with /."""
    if s.endswith("/"):
        return True
    return "." in s.rsplit("/", 1)[-1]


def has_glob(s: str) -> bool:
    return any(c in GLOB_CHARS for c in s)


def get_memory_targets(memdir: Path) -> set[str]:
    """Collect all valid wikilink targets.

    A wikilink `[[X]]` resolves if X matches either the frontmatter
    `name:` field of some memory file OR the filename stem (without
    the .md extension). Both are accepted because the existing memory
    convention is inconsistent: some entries have descriptive `name:`
    fields, and the natural slug to link by is the filename.
    """
    targets: set[str] = set()
    for md in memdir.glob("*.md"):
        if md.name == "MEMORY.md":
            continue
        targets.add(md.stem)  # e.g. project_log_twd_is_true
        m = re.search(r"^name:\s*(.+?)\s*$", md.read_text(), re.MULTILINE)
        if m:
            targets.add(m.group(1))
    return targets


def normalize_repo_path(raw: str) -> str:
    """Strip ::symbol or #anchor suffix; keep trailing slash."""
    return PATH_TAIL_STRIP.sub("", raw)


def lint_file(
    path: Path, repo: Path, memdir: Path, wikilink_targets: set[str]
) -> list[str]:
    findings: list[str] = []
    text = path.read_text()

    # Memory cross-links — [text](other.md) with no slash
    for m in MD_LINK_RE.finditer(text):
        target = m.group(1).strip()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        if target.endswith(".md") and "/" not in target:
            if not (memdir / target).exists():
                findings.append(f"memory link missing: {target}")

    # Wikilinks — match against filename stems and `name:` fields
    for m in WIKILINK_RE.finditer(text):
        ref = m.group(1).strip()
        if ref not in wikilink_targets:
            findings.append(f"unresolved wikilink: [[{ref}]]")

    # Repo-relative paths in backticks (must look pathlike, must not be a glob)
    for m in REPO_PATH_RE.finditer(text):
        rel = normalize_repo_path(m.group(1))
        if not looks_pathlike(rel) or has_glob(rel):
            continue
        if not (repo / rel).exists():
            findings.append(f"repo path missing: {rel}")

    # Absolute paths in backticks
    for m in ABS_PATH_RE.finditer(text):
        abs_clean = normalize_repo_path(m.group(1).rstrip(".,;:!?)'\""))
        if has_glob(abs_clean):
            continue
        if not Path(abs_clean).exists():
            findings.append(f"abs path missing: {abs_clean}")

    return findings


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--memory-dir", type=Path, help="Override memory dir")
    p.add_argument("--repo", type=Path, help="Override repo root")
    args = p.parse_args()

    try:
        repo = args.repo or repo_root()
    except subprocess.CalledProcessError:
        print("Not in a git repo and --repo not given", file=sys.stderr)
        return 2

    memdir = args.memory_dir or memory_dir_for(repo)
    if not memdir.exists():
        print(f"Memory dir not found: {memdir}", file=sys.stderr)
        return 2

    wikilink_targets = get_memory_targets(memdir)
    md_files = sorted(memdir.glob("*.md"))
    any_findings = False
    for md in md_files:
        findings = lint_file(md, repo, memdir, wikilink_targets)
        if findings:
            any_findings = True
            print(f"{md.name}:")
            for f in findings:
                print(f"  - {f}")

    if not any_findings:
        print(f"OK — no stale references across {len(md_files)} memory file(s)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
