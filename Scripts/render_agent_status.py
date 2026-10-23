#!/usr/bin/env python3
"""Render AGENT_STATUS.md → AGENT_STATUS.html with collapsible entries.

Pending entries render expanded; Finished entries collapsed. Pure stdlib so
it can be invoked from a Claude Code hook without conda env activation.

Usage: python3 Scripts/render_agent_status.py [--check]

  --check   exit 1 if AGENT_STATUS.html is out of date (does not write).
"""
from __future__ import annotations

import argparse
import html
import re
import subprocess
import sys
from pathlib import Path


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()
    return Path(out)


# --- minimal markdown → inline HTML ------------------------------------------------

_CODE_RE = re.compile(r"`([^`]+)`")
_BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _inline(text: str) -> str:
    # Escape first, then re-introduce markup with raw HTML. Use sentinels so
    # later passes can't double-process.
    text = html.escape(text)
    placeholders: list[str] = []

    def stash(m: re.Match) -> str:
        placeholders.append(m.group(0))
        return f"\x00{len(placeholders)-1}\x00"

    # Code spans first — protect their contents from bold/link processing.
    def code_sub(m: re.Match) -> str:
        placeholders.append(f"<code>{m.group(1)}</code>")
        return f"\x00{len(placeholders)-1}\x00"

    text = _CODE_RE.sub(code_sub, text)

    def link_sub(m: re.Match) -> str:
        label, url = m.group(1), m.group(2)
        placeholders.append(f'<a href="{url}">{label}</a>')
        return f"\x00{len(placeholders)-1}\x00"

    text = _LINK_RE.sub(link_sub, text)
    text = _BOLD_RE.sub(r"<strong>\1</strong>", text)

    def restore(m: re.Match) -> str:
        return placeholders[int(m.group(1))]

    text = re.sub(r"\x00(\d+)\x00", restore, text)
    return text


# --- block-level parsing -----------------------------------------------------------


def _render_blocks(lines: list[str]) -> str:
    """Render a list of body lines (no headings) into HTML blocks."""
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            i += 1
            continue
        # Horizontal rule (treat as separator, skip).
        if stripped == "---":
            i += 1
            continue
        # Unordered list (handles indented continuation + nested bullets).
        if stripped.startswith("- "):
            list_html, consumed = _render_list(lines, i, base_indent=_indent(line))
            out.append(list_html)
            i += consumed
            continue
        # Plain paragraph: gather contiguous non-blank, non-list lines.
        para: list[str] = [stripped]
        i += 1
        while i < len(lines):
            nxt = lines[i].strip()
            if not nxt or nxt.startswith("- ") or nxt == "---":
                break
            para.append(nxt)
            i += 1
        out.append(f"<p>{_inline(' '.join(para))}</p>")
    return "\n".join(out)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _render_list(lines: list[str], start: int, base_indent: int) -> tuple[str, int]:
    """Render an unordered list starting at lines[start]. Returns (html, lines_consumed)."""
    items_html: list[str] = []
    i = start
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped:
            # Blank line: peek ahead — list continues only if next non-blank is a
            # bullet at base_indent.
            j = i + 1
            while j < len(lines) and not lines[j].strip():
                j += 1
            if j < len(lines) and lines[j].strip().startswith("- ") and _indent(lines[j]) == base_indent:
                i = j
                continue
            break
        if not stripped.startswith("- ") or _indent(line) != base_indent:
            break
        # New item. Capture the item content (everything after "- ") plus
        # any continuation lines (deeper indent or non-bullet wrapped text).
        item_lines = [stripped[2:]]
        i += 1
        nested_block: list[str] = []
        while i < len(lines):
            nxt = lines[i]
            nxt_stripped = nxt.strip()
            if not nxt_stripped:
                # Could be a paragraph break inside this item or end of list.
                # Look ahead — if next non-blank is a deeper bullet, capture as nested.
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and lines[j].strip().startswith("- ") and _indent(lines[j]) > base_indent:
                    nested_block.append("")
                    i = j
                    continue
                break
            if nxt_stripped.startswith("- ") and _indent(nxt) > base_indent:
                nested_block.append(nxt)
                i += 1
                continue
            if nxt_stripped.startswith("- ") and _indent(nxt) == base_indent:
                break  # next sibling
            if _indent(nxt) > base_indent:
                # Continuation text for current item.
                item_lines.append(nxt_stripped)
                i += 1
                continue
            break
        item_html = _inline(" ".join(item_lines))
        if nested_block:
            sub_html, _ = _render_list(nested_block, 0, base_indent=_indent(nested_block[0]))
            item_html = f"{item_html}\n{sub_html}"
        items_html.append(f"<li>{item_html}</li>")
    return "<ul>\n" + "\n".join(items_html) + "\n</ul>", (i - start)


# --- top-level structure -----------------------------------------------------------


def parse(md: str) -> dict:
    """Split AGENT_STATUS.md into preamble + sections + entries."""
    lines = md.splitlines()

    # Title (# Agent Status) — first non-blank H1.
    title = "Agent Status"
    cursor = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            cursor = i + 1
            break

    # Preamble: everything from cursor until the first H2.
    preamble: list[str] = []
    while cursor < len(lines) and not lines[cursor].startswith("## "):
        preamble.append(lines[cursor])
        cursor += 1

    # Sections keyed by H2 name.
    sections: dict[str, list[dict]] = {}
    current_section: str | None = None
    current_entry: dict | None = None
    entry_body: list[str] = []
    # We also let H3-headed prose live inside the preamble (e.g. "Writing entries
    # that actually help") — those H3s before the first H2 are part of preamble.
    while cursor < len(lines):
        line = lines[cursor]
        if line.startswith("## "):
            # Flush in-progress entry.
            if current_entry is not None and current_section is not None:
                current_entry["body"] = "\n".join(entry_body).strip("\n")
                sections[current_section].append(current_entry)
                current_entry = None
                entry_body = []
            current_section = line[3:].strip()
            sections.setdefault(current_section, [])
            cursor += 1
            continue
        if line.startswith("### ") and current_section is not None:
            # Flush previous entry.
            if current_entry is not None:
                current_entry["body"] = "\n".join(entry_body).strip("\n")
                sections[current_section].append(current_entry)
                entry_body = []
            current_entry = {"title": line[4:].strip(), "body": ""}
            cursor += 1
            continue
        if current_section is None:
            preamble.append(line)
        else:
            entry_body.append(line)
        cursor += 1

    if current_entry is not None and current_section is not None:
        current_entry["body"] = "\n".join(entry_body).strip("\n")
        sections[current_section].append(current_entry)

    return {"title": title, "preamble": preamble, "sections": sections}


def render_preamble(preamble_lines: list[str]) -> str:
    """Render preamble — may contain H3 prose sections like 'Writing entries...'."""
    out: list[str] = []
    block: list[str] = []
    i = 0
    while i < len(preamble_lines):
        line = preamble_lines[i]
        if line.startswith("### "):
            if block:
                out.append(_render_blocks(block))
                block = []
            heading = line[4:].strip()
            # Gather body until next ### or end.
            sub_body: list[str] = []
            i += 1
            while i < len(preamble_lines) and not preamble_lines[i].startswith("### "):
                sub_body.append(preamble_lines[i])
                i += 1
            out.append(
                f'<details class="rubric">\n'
                f"<summary>{_inline(heading)}</summary>\n"
                f"{_render_blocks(sub_body)}\n"
                f"</details>"
            )
            continue
        block.append(line)
        i += 1
    if block:
        out.append(_render_blocks(block))
    return "\n".join(out)


def render_entry(entry: dict, *, open_by_default: bool) -> str:
    body_html = _render_blocks(entry["body"].splitlines())
    open_attr = " open" if open_by_default else ""
    return (
        f'<details class="entry"{open_attr}>\n'
        f'<summary><span class="entry-title">{_inline(entry["title"])}</span></summary>\n'
        f"<div class=\"entry-body\">\n{body_html}\n</div>\n"
        f"</details>"
    )


CSS = """
:root {
  --bg: #fdfdfc;
  --fg: #1a1a1a;
  --muted: #666;
  --rule: #e5e5e3;
  --accent: #3a5a7c;
  --pending-bg: #fff8e6;
  --pending-border: #e2c97a;
}
* { box-sizing: border-box; }
body {
  font: 15px/1.55 -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
  color: var(--fg);
  background: var(--bg);
  max-width: 820px;
  margin: 2rem auto;
  padding: 0 1.25rem 4rem;
}
h1 {
  font-size: 1.6rem;
  margin: 0 0 0.5rem;
  border-bottom: 1px solid var(--rule);
  padding-bottom: 0.4rem;
}
h2 {
  font-size: 1.15rem;
  margin: 2rem 0 0.75rem;
  color: var(--accent);
  text-transform: uppercase;
  letter-spacing: 0.04em;
}
p { margin: 0.5rem 0; }
ul { margin: 0.4rem 0 0.4rem 1.2rem; padding: 0; }
li { margin: 0.15rem 0; }
code {
  font: 13px/1.4 "SF Mono", Menlo, Consolas, monospace;
  background: #f1efe9;
  padding: 1px 5px;
  border-radius: 3px;
}
a { color: var(--accent); }
details { margin: 0.5rem 0; }
summary {
  cursor: pointer;
  list-style: none;
  padding: 0.35rem 0;
  position: relative;
  padding-left: 1.25rem;
}
summary::-webkit-details-marker { display: none; }
summary::before {
  content: "▸";
  position: absolute;
  left: 0.1rem;
  color: var(--muted);
  transition: transform 0.12s ease;
  display: inline-block;
}
details[open] > summary::before { transform: rotate(90deg); }
details.rubric > summary { font-weight: 600; color: var(--accent); }
details.entry {
  border: 1px solid var(--rule);
  border-radius: 6px;
  padding: 0.25rem 0.75rem;
  background: #fff;
}
section.pending details.entry {
  background: var(--pending-bg);
  border-color: var(--pending-border);
}
details.entry > summary {
  font-weight: 600;
  padding-left: 1.25rem;
}
.entry-title { font-family: "SF Mono", Menlo, Consolas, monospace; font-size: 0.95em; }
.entry-body { padding: 0 0 0.5rem 0.25rem; }
.entry-body > p:first-child { margin-top: 0.25rem; }
.generated {
  color: var(--muted);
  font-size: 12px;
  margin-top: 3rem;
  border-top: 1px solid var(--rule);
  padding-top: 0.75rem;
}
"""


def render(md_text: str) -> str:
    parsed = parse(md_text)
    title = _inline(parsed["title"])
    preamble_html = render_preamble(parsed["preamble"])

    sections_html: list[str] = []
    for section_name, entries in parsed["sections"].items():
        open_default = section_name.lower() == "pending"
        section_class = section_name.lower()
        items = "\n".join(
            render_entry(e, open_by_default=open_default) for e in entries
        )
        body = items if entries else "<p><em>(none)</em></p>"
        sections_html.append(
            f'<section class="{section_class}">\n'
            f"<h2>{_inline(section_name)}</h2>\n"
            f"{body}\n"
            f"</section>"
        )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>{CSS}</style>
</head>
<body>
<h1>{title}</h1>
{preamble_html}
{chr(10).join(sections_html)}
<p class="generated">Generated from AGENT_STATUS.md by Scripts/render_agent_status.py. Edit the .md, not this file.</p>
</body>
</html>
"""


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if AGENT_STATUS.html is out of date")
    args = ap.parse_args()

    root = repo_root()
    md_path = root / "AGENT_STATUS.md"
    html_path = root / "AGENT_STATUS.html"

    md_text = md_path.read_text()
    rendered = render(md_text)

    if args.check:
        if not html_path.exists() or html_path.read_text() != rendered:
            print(f"AGENT_STATUS.html out of date — run: python3 Scripts/render_agent_status.py", file=sys.stderr)
            return 1
        return 0

    html_path.write_text(rendered)
    print(f"Wrote {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
