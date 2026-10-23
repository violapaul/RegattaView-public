"""Parse cached HTML into races.csv and results.csv. No network."""

from __future__ import annotations

import argparse
import csv
import hashlib
import sys
from pathlib import Path

_THIS = Path(__file__).resolve().parent
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

from Results.httpcache import HttpClient
from Results.sources import ALL_SOURCES, get_source
from Results.parsers.sailwave import boat_name_canon, sail_number_canon

STORAGE_ROOT = _THIS / "storage"
DATA_DIR = STORAGE_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

RACES_CSV = DATA_DIR / "races.csv"
RESULTS_CSV = DATA_DIR / "results.csv"

RACES_FIELDS = [
    "race_id", "source", "event", "year", "race_date", "race_label",
    "class_name", "source_url", "fetched_at",
]
RESULTS_FIELDS = [
    "race_id", "place", "place_int", "boat_name", "boat_name_canon",
    "sail_number", "sail_number_canon", "boat_type", "skipper", "yacht_club",
    "rating", "elapsed", "corrected", "score", "boat_id",
]


def race_id(source: str, event: str, year: int, class_name: str | None,
            race_label: str) -> str:
    key = f"{source}|{event}|{year}|{class_name or ''}|{race_label}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def _page_for_url(src, url: str, label: str = "primary"):
    """Best-effort reconstruct a ResultPage from cache. Event/year recovered
    from a discover() pass keyed by URL."""
    raise RuntimeError("use page_index instead")


def build_page_index(src) -> dict[str, "ResultPage"]:
    """Re-run discover() (mostly cache-served) so we know each URL's
    event/year/label."""
    idx: dict[str, "ResultPage"] = {}
    for page in src.discover():
        # First wins — primary 'series.htm' before its derived 'Race N' if any
        # share a URL (shouldn't happen, but defensive).
        idx.setdefault(page.url, page)
    return idx


def parse_source(source_name: str,
                 races_writer: csv.DictWriter,
                 results_writer: csv.DictWriter,
                 since: int | None = None) -> tuple[int, int]:
    http = HttpClient(STORAGE_ROOT)
    src = get_source(source_name)
    src.http_client = http
    page_index = build_page_index(src)

    races_seen: set[str] = set()
    n_races = 0
    n_rows = 0

    # Group cached pages by URL — pick latest fetched_at if multiple records.
    latest_per_url: dict[str, tuple[str, bytes, str]] = {}
    for url, sha1, body, fetched_at in http.iter_cached(src.name):
        prev = latest_per_url.get(url)
        if prev is None or fetched_at > prev[2]:
            latest_per_url[url] = (sha1, body, fetched_at)

    for url, (sha1, body, fetched_at) in latest_per_url.items():
        page = page_index.get(url)
        if page is None:
            # Cached but no longer in discovery — skip silently
            continue
        if since is not None and page.year < since:
            continue
        try:
            rows = list(src.parse(body, page))
        except Exception as e:
            print(f"[{src.name}] parse-error url={url}: {e}", file=sys.stderr)
            continue
        # group rows by class_name so each class is its own race entry
        rows_by_class: dict[str | None, list] = {}
        for r in rows:
            rows_by_class.setdefault(r.class_name, []).append(r)
        for class_name, class_rows in rows_by_class.items():
            rid = race_id(src.name, page.event, page.year, class_name,
                          page.label)
            if rid not in races_seen:
                races_seen.add(rid)
                races_writer.writerow({
                    "race_id": rid,
                    "source": src.name,
                    "event": page.event,
                    "year": page.year,
                    "race_date": "",
                    "race_label": page.label,
                    "class_name": class_name or "",
                    "source_url": url,
                    "fetched_at": fetched_at,
                })
                n_races += 1
            for r in class_rows:
                results_writer.writerow({
                    "race_id": rid,
                    "place": r.place,
                    "place_int": r.place_int if r.place_int is not None else "",
                    "boat_name": r.boat_name,
                    "boat_name_canon": boat_name_canon(r.boat_name),
                    "sail_number": r.sail_number or "",
                    "sail_number_canon": sail_number_canon(r.sail_number),
                    "boat_type": r.boat_type or "",
                    "skipper": r.skipper or "",
                    "yacht_club": r.yacht_club or "",
                    "rating": r.rating or "",
                    "elapsed": r.elapsed or "",
                    "corrected": r.corrected or "",
                    "score": r.score or "",
                    "boat_id": "",
                })
                n_rows += 1
    print(f"[{src.name}] races={n_races} rows={n_rows}")
    return n_races, n_rows


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--source", required=True,
                   help="source name or 'all'. Known: " + ", ".join(ALL_SOURCES))
    p.add_argument("--since", type=int, default=2017,
                   help="drop rows from races with year < SINCE "
                        "(default 2017 — we started racing 8 years ago)")
    args = p.parse_args(argv)

    targets = list(ALL_SOURCES) if args.source == "all" else [args.source]

    with RACES_CSV.open("w", newline="", encoding="utf-8") as rf, \
         RESULTS_CSV.open("w", newline="", encoding="utf-8") as resf:
        races_w = csv.DictWriter(rf, fieldnames=RACES_FIELDS)
        results_w = csv.DictWriter(resf, fieldnames=RESULTS_FIELDS)
        races_w.writeheader()
        results_w.writeheader()
        total_r = 0
        total_rows = 0
        for name in targets:
            try:
                nr, nrow = parse_source(name, races_w, results_w, args.since)
                total_r += nr
                total_rows += nrow
            except NotImplementedError as e:
                print(f"[{name}] skipped (stub): {e}", file=sys.stderr)
    print(f"wrote {RACES_CSV} ({total_r} races) and {RESULTS_CSV} ({total_rows} rows)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
