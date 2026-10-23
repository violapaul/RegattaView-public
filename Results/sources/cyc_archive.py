"""CYC Seattle netlify archive — Sailwave HTML at cyc-racingarchive.netlify.app.

Discovery shape (confirmed against the live site, 2026-05):
  - Root index https://cyc-racingarchive.netlify.app/ is a static HTML table
    of (year, racebook, results) rows for 1997..2023. Two URL families:
      * pre-2012:  /results<YY>          (e.g. /results11)
      * 2012+:     /results/<YEAR>_specific/results
  - Each per-year landing page is hand-curated HTML linking to per-event
    result pages on the same host under
      /results/<YEAR>_specific/<eventid>          (no extension)
    Some links go off-host (yachtscoring.com, race.styc.org); we filter
    those out — they belong to other sources / phases.
  - Per-event pages are Sailwave HTML (version 2.19.8+), with the gold
    event name in the page's <h1> (e.g. "Lake Tuesday Evening Fall
    Series 2023", "Center Sound Series 2023", "Turkey Bowl 2017"). A few
    legacy pages (e.g. comgib_2017) are hand-edited HTML tables instead
    of Sailwave; the Sailwave parser still extracts boat/skipper rows
    via the generic <table>+<th> heuristic.

Discovery does not pre-fetch event pages — the year landing alone gives
us URL + year, and parse() pulls the canonical event name from the
cached body's <h1>. crawl.py's --since filter applies before any event
page is fetched, so 2017+ runs do not pay for older years.
"""

from __future__ import annotations

import re
from typing import Iterator, Optional
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .base import ResultPage, ResultRow, Source
from ..parsers.sailwave import parse_sailwave_table

ARCHIVE_HOST = "cyc-racingarchive.netlify.app"
ROOT_URL = f"https://{ARCHIVE_HOST}/"

# /results/<YEAR>_specific/<eventid>  (no .pdf, no fragment)
EVENT_URL_RE = re.compile(
    r"^https?://" + re.escape(ARCHIVE_HOST)
    + r"/results/(\d{4})_specific/([A-Za-z0-9._\-]+)/?$"
)


def _year_landing_urls_from_root(root_html: str) -> list[tuple[int, str]]:
    """Parse the root index HTML and return [(year, year_landing_url), ...].

    The root table has one <tr> per year; the third <td> contains the
    Results <a> link. Pre-2012 link to /results<YY>, 2012+ to
    /results/<YEAR>_specific/results.
    """
    soup = BeautifulSoup(root_html, "lxml")
    out: list[tuple[int, str]] = []
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        # Match the two known patterns
        m_old = re.match(r"^/results(\d{2})/?$", href)
        m_new = re.match(r"^/results/(\d{4})_specific/results/?$", href)
        if m_old:
            yy = int(m_old.group(1))
            year = 1900 + yy if yy >= 50 else 2000 + yy
            out.append((year, urljoin(ROOT_URL, href)))
        elif m_new:
            out.append((int(m_new.group(1)), urljoin(ROOT_URL, href)))
    # Stable sort, dedupe (root has each year exactly once but be defensive)
    seen = set()
    uniq: list[tuple[int, str]] = []
    for y, u in sorted(out):
        if (y, u) in seen:
            continue
        seen.add((y, u))
        uniq.append((y, u))
    return uniq


def _event_links_from_year_landing(html: str, year_url: str,
                                   year: int) -> Iterator[tuple[str, str]]:
    """Yield (link_text, absolute_url) for every in-archive event link on a
    year landing page.

    Filters:
      - same host (cyc-racingarchive.netlify.app)
      - matches the /results/<YEAR>_specific/<eventid> pattern
      - the eventid is NOT 'results' (skip the year-landing self-link)
    Strips URL fragments (#r1, #summary_5, etc).
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        absolute = urljoin(year_url, href)
        # drop fragment
        absolute = absolute.split("#", 1)[0]
        m = EVENT_URL_RE.match(absolute)
        if not m:
            continue
        url_year = int(m.group(1))
        eventid = m.group(2)
        if url_year != year:
            # Cross-year reference (rare but possible) — let it discover under
            # its own year via that year's landing page rather than mis-filing.
            continue
        if eventid.lower() == "results":
            # Self-link to the year landing
            continue
        if eventid.lower().endswith(".pdf"):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)
        text = (a.get_text(" ", strip=True) or "").strip()
        yield text, absolute


# Trailing year suffix on Sailwave <h1>. e.g. "Center Sound Series 2023".
_TRAILING_YEAR_RE = re.compile(r"\s+\d{4}\s*$")


def _event_name_from_html(html: str, fallback: str) -> str:
    """Pull the canonical event name from the page's <h1>, stripping the
    trailing year (we already track year separately). Falls back to the
    discover-time link text on pages with no <h1>."""
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1:
        txt = h1.get_text(" ", strip=True)
        if txt:
            txt = re.sub(r"\s+", " ", txt).strip()
            txt = _TRAILING_YEAR_RE.sub("", txt).strip()
            if txt:
                return txt
    # Hand-edited pages (e.g. comgib_2017): try <h2 class="norm"> or <title>.
    h2 = soup.find("h2")
    if h2:
        txt = h2.get_text(" ", strip=True)
        if txt and "yacht club" not in txt.lower():
            return _TRAILING_YEAR_RE.sub("", re.sub(r"\s+", " ", txt)).strip()
    title = soup.find("title")
    if title:
        txt = title.get_text(" ", strip=True)
        # Sailwave titles look like "Sailwave results for X 2023 at Y 2023"
        m = re.match(r"sailwave results for (.*?) at ", txt, re.I)
        if m:
            return _TRAILING_YEAR_RE.sub("", m.group(1).strip()).strip()
    return fallback


class CYCArchiveSource(Source):
    name = "cyc-archive"

    def discover(self) -> Iterator[ResultPage]:
        if self.http_client is None:
            raise RuntimeError("CYCArchiveSource.discover requires http_client")
        root = self.http_client.get(ROOT_URL, source=self.name).decode(
            "utf-8", errors="replace"
        )
        seen_event_urls: set[str] = set()
        for year, year_url in _year_landing_urls_from_root(root):
            try:
                year_body = self.http_client.get(
                    year_url, source=self.name, referer=ROOT_URL
                ).decode("utf-8", errors="replace")
            except Exception as e:
                print(f"[{self.name}] WARN year landing {year}: {e}")
                continue
            for link_text, ev_url in _event_links_from_year_landing(
                year_body, year_url, year
            ):
                if ev_url in seen_event_urls:
                    continue
                seen_event_urls.add(ev_url)
                yield ResultPage(
                    source=self.name,
                    event=link_text or ev_url.rstrip("/").rsplit("/", 1)[-1],
                    year=year,
                    label="primary",
                    url=ev_url,
                )

    def parse(self, html: bytes, page: ResultPage) -> Iterator[ResultRow]:
        text = html.decode("utf-8", errors="replace")
        canonical = _event_name_from_html(text, fallback=page.event)
        if canonical and canonical != page.event:
            # Mutate page in-place so parse.py picks up the canonical name.
            page.event = canonical
        yield from parse_sailwave_table(text, page)
