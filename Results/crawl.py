"""Crawl one or more sources: discover pages and fetch each through the cache."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Ensure 'Results' is importable when invoked as `python crawl.py` from inside it.
_THIS = Path(__file__).resolve().parent
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

from Results.httpcache import HttpClient
from Results.sources import ALL_SOURCES, get_source

STORAGE_ROOT = _THIS / "storage"


def crawl_source(source_name: str, since: int | None, force: bool) -> int:
    http = HttpClient(STORAGE_ROOT)
    src = get_source(source_name)
    src.http_client = http

    fetched = 0
    discovered = 0
    for page in src.discover():
        discovered += 1
        if since is not None and page.year < since:
            continue
        try:
            http.get(page.url, source=src.name, referer=None, force=force)
            fetched += 1
            print(f"[{src.name}] OK  {page.year} {page.event!r:40s} "
                  f"{page.label!r} -> {page.url}")
        except Exception as e:
            print(f"[{src.name}] ERR {page.year} {page.event!r:40s} "
                  f"{page.label!r}: {e}", file=sys.stderr)
    print(f"[{src.name}] discovered={discovered} fetched_or_cached={fetched}")
    return fetched


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True,
                   help="source name or 'all'. Known: " + ", ".join(ALL_SOURCES))
    p.add_argument("--since", type=int, default=2017,
                   help="skip pages with year < SINCE (default 2017 — we "
                        "started racing 8 years ago)")
    p.add_argument("--force", action="store_true",
                   help="re-fetch even if cached")
    args = p.parse_args(argv)

    targets = list(ALL_SOURCES) if args.source == "all" else [args.source]
    total = 0
    for name in targets:
        try:
            total += crawl_source(name, args.since, args.force)
        except NotImplementedError as e:
            print(f"[{name}] skipped (stub): {e}", file=sys.stderr)
    print(f"total fetched_or_cached: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
