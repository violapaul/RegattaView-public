"""CYC Seattle main-site results — 2024+ ClubSpot-hosted regattas.

The CYC archive (cyc-racingarchive.netlify.app) covers 1997-2023. Starting
in 2024, CYC moved to ClubSpot (cycseattle.org runs on it). The per-year
landing pages we discover here live at:

    https://cycseattle.org/<YEAR>-race-results

Discovery shape (confirmed against the live site, 2026-05):

  - The landing page is server-rendered Vue. The actual event link list is
    embedded as URL-encoded JSON inside ``:column_prop="'<urlencoded>'"``
    Vue prop strings on the section-renderer components. After
    URL-decoding and JSON-parsing, each item has a ``content`` field
    holding HTML with ``<a href="...">Event Name</a>`` blocks.

  - Event links to CYC's own regattas are
    ``https://cycseattle.org/regatta/<regatta_id>/results``. Off-host
    ``https://theclubspot.com/regatta/<id>/results`` links exist (e.g.
    "J/24 World Championships 2024" was hosted on the central ClubSpot
    domain by another club). We skip those — they're not CYC events.

  - Each ClubSpot regatta has multiple boatClasses. We query Parse REST
    (``GET https://theclubspot.com/parse/classes/boatClasses?where=...``,
    with ``X-Parse-Application-Id: myclubspot2017``) to enumerate them.
    Classes whose ``scoring.method == "not_racing"`` are skipped to match
    the in-page UI's filter.

  - Per-class series-overall results come from
    ``https://results.theclubspot.com/clubspot-results-v4/<regatta_id>?boatClassIDs=<class_id>``.
    No auth required. Each (regatta, class) becomes one ResultPage with
    ``label="primary"``; the ClubSpot v4 parser pulls the class name out
    of the response and hands it to parse.py via ResultRow.class_name —
    so each class gets its own race row in races.csv, mirroring how
    Sailwave class-tables map to per-class race rows.

  - We do not currently emit per-race breakouts (only series overall).
    The v4 endpoint returns per-race ``scoring_data`` inside each
    registration record but we don't expand them — matches CYC archive
    behaviour where most pages are series overall too. A future phase
    can add ``label="Race N"`` ResultPages if needed.

YEARS: hardcoded list grown over time. Expand as new years are published.
The crawl --since flag still gates which we actually process.
"""

from __future__ import annotations

import html as _html
import json
import re
import urllib.parse
from typing import Iterator, Optional
from urllib.parse import urljoin, urlsplit

from .base import ResultPage, ResultRow, Source
from ..parsers.clubspot import parse_clubspot_v4

CYC_HOST = "cycseattle.org"
CYC_ROOT = f"https://{CYC_HOST}"

# Hardcoded per-year landing pages. Add a new entry for each future year.
YEAR_LANDING_URLS: dict[int, str] = {
    2024: "https://cycseattle.org/2024-race-results",
    2025: "https://cycseattle.org/2025-race-results",
}

# CYC-hosted regatta links inside each year's column_prop blocks
REGATTA_HREF_RE = re.compile(
    r"^https?://" + re.escape(CYC_HOST) + r"/regatta/([A-Za-z0-9]+)/results/?$"
)

# Vue prop attribute carrying URL-encoded JSON. The wrapping is
#   :column_prop="'<urlencoded JSON list>'"
COLUMN_PROP_RE = re.compile(r":column_prop=\"'([^']+)'\"")

PARSE_APP_ID = "myclubspot2017"
PARSE_SERVER = "https://theclubspot.com/parse"
RESULTS_API = "https://results.theclubspot.com/clubspot-results-v4"


def _column_prop_blocks(html: str) -> list[list[dict]]:
    """Pull every ``:column_prop`` payload out of the year landing HTML.

    Returns one decoded JSON list per matched attribute (each list is a
    column; each list element is a section dict with type/content/etc).
    Non-JSON or empty payloads are skipped.
    """
    out: list[list[dict]] = []
    for m in COLUMN_PROP_RE.finditer(html):
        raw = m.group(1)
        try:
            decoded = urllib.parse.unquote(raw)
            data = json.loads(decoded)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, list):
            out.append(data)
    return out


def _hrefs_from_content(content: str) -> Iterator[tuple[str, str]]:
    """Yield (link_text, href) from one section's HTML content.

    Section content uses backslash-escaped quotes around href values,
    e.g. ``href=\\"https://...\\"``. We unescape both ways and pull
    ``<a href="..." ...>text</a>`` matches.

    Anchor text frequently contains empty inline tags injected by the
    page editor (e.g. ``Lake Spring Reg<span></span>atta``). Empty
    inline tags are stripped without inserting whitespace; non-empty
    inner tags are replaced with a space.
    """
    if not content:
        return
    text = content.replace('\\"', '"').replace('\\/', '/')
    text = _html.unescape(text)
    # Anchor with explicit closing tag
    for am in re.finditer(
        r'<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        text,
        re.S | re.I,
    ):
        href = am.group(1).strip()
        inner = am.group(2)
        # Drop *empty* tags first so they don't introduce whitespace splits
        # like 'Reg<span></span>atta' -> 'Reg atta'.
        inner = re.sub(r"<([a-zA-Z][a-zA-Z0-9]*)\b[^>]*>\s*</\1>", "", inner)
        # Replace remaining tags with a space, then collapse whitespace
        link_text = re.sub(r"<[^>]+>", " ", inner)
        link_text = re.sub(r"\s+", " ", link_text).strip()
        yield link_text, href


def regatta_links_from_year_html(html: str) -> Iterator[tuple[str, str]]:
    """Yield (event_name, regatta_id) for each CYC-hosted regatta on a year landing page.

    Skips off-host links (theclubspot.com/regatta/...), non-results links
    (e.g. ``/event/<id>``), and the year-landing self link. Dedupes by
    regatta_id; emits in the order regatta_ids first appear. When a regatta_id
    appears in multiple anchors (icon link + text link both pointing to the
    same regatta), we choose the LONGEST non-empty anchor text — the icon
    anchors render as empty <span>s and would otherwise win.
    """
    # Pass 1: collect candidate (regatta_id, anchor_text) and first-seen order
    order: list[str] = []
    candidates: dict[str, list[str]] = {}
    for column in _column_prop_blocks(html):
        for section in column:
            content = section.get("content") or ""
            for text, href in _hrefs_from_content(content):
                m = REGATTA_HREF_RE.match(href)
                if not m:
                    continue
                regatta_id = m.group(1)
                if regatta_id not in candidates:
                    candidates[regatta_id] = []
                    order.append(regatta_id)
                candidates[regatta_id].append(text or "")
    # Pass 2: pick best label per regatta — longest non-empty text wins
    for regatta_id in order:
        names = [t for t in candidates[regatta_id] if t]
        if names:
            label = max(names, key=len)
        else:
            label = regatta_id
        yield label, regatta_id


def _parse_where_url(regatta_id: str) -> str:
    """Build the Parse REST GET URL for boatClasses of one regatta."""
    where = json.dumps({
        "regattaObject": {
            "__type": "Pointer",
            "className": "regattas",
            "objectId": regatta_id,
        },
        "archived": False,
    }, separators=(",", ":"))
    qs = urllib.parse.urlencode({
        "where": where,
        "order": "name",
        "limit": 100,
    })
    return f"{PARSE_SERVER}/classes/boatClasses?{qs}"


def boat_classes_from_parse_json(body) -> list[tuple[str, str]]:
    """Return [(class_id, class_name), ...] from a boatClasses Parse REST response.

    Filters out classes with ``scoring.method == "not_racing"`` (matches the
    UI filter in the CYC results page) and any class with no name.
    """
    if isinstance(body, (bytes, bytearray)):
        body = body.decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    out: list[tuple[str, str]] = []
    for c in data.get("results", []) or []:
        name = (c.get("name") or "").strip()
        cid = (c.get("objectId") or "").strip()
        if not name or not cid:
            continue
        scoring = c.get("scoring") or {}
        if isinstance(scoring, dict) and scoring.get("method") == "not_racing":
            continue
        out.append((cid, name))
    return out


def results_v4_url(regatta_id: str, class_id: str) -> str:
    return f"{RESULTS_API}/{regatta_id}?boatClassIDs={class_id}"


class CYCMainSource(Source):
    name = "cyc-main"

    def discover(self) -> Iterator[ResultPage]:
        if self.http_client is None:
            raise RuntimeError("CYCMainSource.discover requires http_client")
        seen_urls: set[str] = set()
        for year, year_url in sorted(YEAR_LANDING_URLS.items()):
            try:
                year_html = self.http_client.get(
                    year_url, source=self.name, referer=CYC_ROOT
                ).decode("utf-8", errors="replace")
            except Exception as e:
                print(f"[{self.name}] WARN year landing {year}: {e}")
                continue
            for event_name, regatta_id in regatta_links_from_year_html(year_html):
                # Discover classes for this regatta via Parse REST
                parse_url = _parse_where_url(regatta_id)
                try:
                    classes_body = self.http_client.get(
                        parse_url,
                        source=self.name,
                        referer=year_url,
                        extra_headers={
                            "X-Parse-Application-Id": PARSE_APP_ID,
                        },
                    )
                except Exception as e:
                    print(f"[{self.name}] WARN Parse classes {regatta_id} ({event_name}): {e}")
                    continue
                classes = boat_classes_from_parse_json(classes_body)
                if not classes:
                    # No racing classes — could be a registration-only page
                    continue
                for class_id, _class_name in classes:
                    rurl = results_v4_url(regatta_id, class_id)
                    if rurl in seen_urls:
                        continue
                    seen_urls.add(rurl)
                    yield ResultPage(
                        source=self.name,
                        event=event_name,
                        year=year,
                        label="primary",
                        url=rurl,
                    )

    def parse(self, html: bytes, page: ResultPage) -> Iterator[ResultRow]:
        # 'html' is JSON here. The framework just hands us bytes — name is historical.
        yield from parse_clubspot_v4(html, page)
