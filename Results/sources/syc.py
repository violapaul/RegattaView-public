"""Seattle Yacht Club race results.

SYC publishes results across at least three channels:

  1. **S3 Sailwave HTML** at `seattleyachtclubsailing.s3-us-west-2.amazonaws.com`
     (and the path-style alias `s3-us-west-2.amazonaws.com/seattleyachtclubsailing/`).
     Used for ~2018+ regattas.
  2. **CEClients legacy host** at `www.ceclients.com/sites/SeattleYachtClub/files/...`
     Used for ~2009-2017 regattas. Same Sailwave HTML format.
  3. **Yacht Scoring** at `https://api.yachtscoring.com/v1/public/event/<eID>/cumulative-result`
     Used for a small handful of SYC-hosted events that opted into the YS
     scoring backend (notably J FEST Seattle 2024, eID 16398, and the
     2013-2014 Sperry NOOD).

The canonical index of (1) and (2) is the SYC website's
`/past-race-results` page, which is Cloudflare-protected and unreachable
via plain HTTP for scripted clients (returns 403). It is, however, fully
indexed by the Internet Archive — we seed discovery from the most recent
Wayback snapshot:

    https://web.archive.org/web/<timestamp>id_/https://www.seattleyachtclub.org/past-race-results

The `id_` flag returns the original HTML body without Wayback's toolbar
injection, so we can run the same anchor-extraction pass we'd run if we
could fetch the live page. The snapshot is captured periodically
(2024-08-08 is the latest as of this writing). If you need newer events
that didn't exist at snapshot time, either trigger a fresh Wayback save
of the live page or augment the discovery seed manually.

For (3), Yacht Scoring exposes a public event-search endpoint at
`/v1/public/event` keyed by `searchKeyword`. We probe with the literal
phrase `"Seattle"` to surface any SYC-hosted YS events; the SPA UI does
the same thing.

If a future SYC-hosted event lives on a fourth host (RegattaNetwork,
ClubSpot, etc.) it will not be picked up by this source — those should
land in their own dedicated sources to keep discovery patterns small.
Filed under "follow-up work."
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Iterator, Optional
from urllib.parse import urljoin, urlsplit, unquote

from bs4 import BeautifulSoup

from .base import ResultPage, ResultRow, Source
from ..parsers.sailwave import parse_sailwave_table
from ..parsers.yacht_scoring import parse_cumulative_json
from ..parsers.syc_pdf import parse_syc_pdf

# ----------------------------------------------------------------------
# Discovery URL constants

# Wayback "id_" returns the original payload (no toolbar). The SYC page
# drops older years off the bottom over time, so we union event URLs
# across every historical snapshot we know about. To refresh:
#
#   curl -s 'https://web.archive.org/cdx/search/cdx?url=seattleyachtclub.org/past-race-results&output=json&fl=timestamp&filter=statuscode:200' \
#     | jq -r '.[1:] | .[] | .[0]'
#
# Add any new timestamps to the list. Order doesn't matter (we dedupe).
PAST_RACE_RESULTS_LIVE = "https://www.seattleyachtclub.org/past-race-results"
PAST_RACE_RESULTS_WAYBACK_SNAPSHOTS = (
    "20200529175132",
    "20200612204012",
    "20200823181127",
    "20200922234523",
    "20211020065527",
    "20220627144138",
    "20221205143716",
    "20230523063214",
    "20230819045451",
    "20240520153033",
    "20240808233450",
)
PAST_RACE_RESULTS_WAYBACK_URLS = tuple(
    f"https://web.archive.org/web/{ts}id_/{PAST_RACE_RESULTS_LIVE}"
    for ts in PAST_RACE_RESULTS_WAYBACK_SNAPSHOTS
)

# Live-rendered seeds for years past the latest Wayback snapshot. Drop a
# Playwright-rendered dump of `PAST_RACE_RESULTS_LIVE` into
# `storage/seeds/syc_past_race_results_<YYYY-MM-DD>.html` and the
# discoverer will union events from it. Cloudflare blocks plain curl,
# so we can't fetch this directly from the source class.
LIVE_SEED_DIR = Path(__file__).resolve().parent.parent / "storage" / "seeds"
LIVE_SEED_GLOB = "syc_past_race_results_*.html"

# Yacht Scoring REST API for SYC-hosted events.
YACHT_SCORING_API = "https://api.yachtscoring.com/v1"
# Search keywords that surface SYC-hosted regattas. Each one becomes a
# `searchKeyword` query against /public/event.
YS_SEARCH_KEYWORDS = ["Seattle"]

# Hosts we recognise as Sailwave HTML pages.
SAILWAVE_HOSTS = (
    "seattleyachtclubsailing.s3-us-west-2.amazonaws.com",
    "s3-us-west-2.amazonaws.com",          # path-style alias
    "www.ceclients.com",
    "ceclients.com",
)
# SYC posts post-2018 results as Sailwave-exported PDFs in their CMS at
# `/documents/<id>/<id>/<filename>.pdf[/<uuid>]`. Parsed by syc_pdf.py.
SYC_DOC_HOST = "www.seattleyachtclub.org"
# Anchor text patterns that mark a PDF as a result file (vs. NOR / SI /
# class assignment / race book noise).
_PDF_RESULT_TEXT_RE = re.compile(
    r"(result|standing|finish|score|by\s+(class|course|start|fleet|club|team))",
    re.I,
)
# Anchor texts that look result-y but are noise.
_PDF_RESULT_DENY_RE = re.compile(
    r"(notice\s+of\s+race|sailing\s+instructions|class\s+assignment|"
    r"race\s+book|class\s+flag|registration|protest|sailing\s+program|"
    r"course\s+chart|course\s+diagram|amendment)",
    re.I,
)
# Yacht Scoring host markers (legacy CFM URLs are SPA-only and useless
# without an eID, so we ignore them).
YACHT_SCORING_HOSTS = ("yachtscoring.com", "www.yachtscoring.com")
YS_EID_PATH_RE = re.compile(r"/(?:emenu|event_results_cumulative)/(\d+)")
YS_EID_QUERY_RE = re.compile(r"[?&]eID=(\d+)")


# ----------------------------------------------------------------------
# Wayback discovery — parse the past-race-results page

# Year heading on the SYC page. The h3 tags within the accordion read
# exactly "2024", "2023", … "2004".
_YEAR_H_RE = re.compile(r"^(19|20)\d{2}$")
# Skip these noisy boilerplate header strings that happen to land near
# result anchors but are not regatta names.
_HEADER_BOILERPLATE_RE = re.compile(
    r"(Sailboat\s+Race\s+Book|Race\s+Book|Notice\s+of\s+Race|Printable|"
    r"Register\s+(?:for|HERE)|Regatta\s+Schedule|Past\s+Race\s+Results|"
    r"Race\s+Documents|Notice\s+to\s+Competitors|REGISTER|Click\s+Here)",
    re.I,
)


def _is_target_link(href: str, anchor_text: str = "") -> bool:
    """True iff `href` points at a page we know how to parse.

    `anchor_text` is consulted only for PDF document URLs on the SYC CMS,
    where we keep result PDFs and reject NOR/SI/class-assignment noise.
    """
    if not href:
        return False
    parts = urlsplit(href)
    host = (parts.netloc or "").lower()
    path_l = parts.path.lower()
    is_pdf = ".pdf" in path_l  # may have UUID after .pdf

    # SYC documents/* PDFs — only when the anchor text reads like a result.
    # The /documents/10184/ namespace is the retired CMS — every URL there
    # 404s. Only /documents/20124/ (current namespace) is fetchable. Skip
    # the rest; the live seed has all the 20124 URLs we need.
    if is_pdf and host == SYC_DOC_HOST and "/documents/" in path_l:
        if "/documents/10184/" in path_l:
            return False
        if not anchor_text:
            return False
        if _PDF_RESULT_DENY_RE.search(anchor_text):
            return False
        if _PDF_RESULT_TEXT_RE.search(anchor_text):
            return True
        return False

    # All other PDF/binary types from any host → reject
    if href.lower().endswith((".txt", ".doc", ".docx", ".xlsx")):
        return False
    if is_pdf:
        return False

    if any(host.endswith(h) for h in SAILWAVE_HOSTS):
        if host.endswith("ceclients.com"):
            return "/sites/SeattleYachtClub/" in parts.path
        if host.endswith("amazonaws.com"):
            return bool(parts.path) and parts.path != "/"
        return True
    if any(host.endswith(h) for h in YACHT_SCORING_HOSTS):
        if YS_EID_QUERY_RE.search(href) or YS_EID_PATH_RE.search(parts.path):
            return True
        return False
    return False


def _normalise_host(href: str) -> str:
    """`http://` + path-style S3 -> `https://` + virtual-host style.

    We canonicalise so the on-disk cache doesn't double-store the same
    bucket key under two URLs.
    """
    parts = urlsplit(href)
    if (parts.netloc.lower() == "s3-us-west-2.amazonaws.com"
            and parts.path.startswith("/seattleyachtclubsailing/")):
        new_path = parts.path[len("/seattleyachtclubsailing"):]
        return ("https://seattleyachtclubsailing.s3-us-west-2.amazonaws.com"
                + new_path)
    if parts.scheme == "http" and parts.netloc.lower() in (
        "seattleyachtclubsailing.s3-us-west-2.amazonaws.com",
        "www.ceclients.com", "ceclients.com",
    ):
        # S3 + CEClients both serve https
        return "https://" + href.split("://", 1)[1]
    return href


def _nearest_event_header(anchor) -> Optional[str]:
    """Walk back from `anchor` looking for an event-name <strong>/<b>/header.

    SYC's past-race-results page renders one accordion section per year
    (`<h3>2022</h3>`), with each event headlined by `<strong>` /
    `<b>` / `<h4>` text immediately above the anchor's containing
    `<div>`. We bound the climb so we never escape the year section.
    """
    for tag in anchor.find_all_previous(
        ["strong", "b", "h4", "h5", "h3", "h2", "h1"], limit=24
    ):
        txt = tag.get_text(" ", strip=True)
        if not txt:
            continue
        if _YEAR_H_RE.match(txt):
            # Reached the year heading without finding an event name.
            return None
        if _HEADER_BOILERPLATE_RE.search(txt):
            continue
        if len(txt) > 140:
            # Probably a paragraph, not a heading
            continue
        return txt
    return None


def _enclosing_year(anchor) -> Optional[int]:
    """Year of the accordion section containing this anchor."""
    for h in anchor.find_all_previous(["h3", "h4", "h2"], limit=200):
        txt = h.get_text(" ", strip=True)
        m = _YEAR_H_RE.match(txt)
        if m:
            try:
                return int(txt)
            except ValueError:
                continue
    return None


def _strip_event_year_suffix(name: str) -> str:
    """Drop a leading or trailing year token to keep events portable."""
    name = re.sub(r"^(19|20)\d{2}\s+", "", name)
    name = re.sub(r"\s+(19|20)\d{2}$", "", name)
    return name.strip()


# `Mark Mayer Opening Day Race and Party   May 8` -> `Mark Mayer Opening Day Race and Party`.
# Names on the SYC page are headlined as `<event> <month> <day>[-<day>]`. We strip
# from the first month-name onward (case-insensitive) so the canonical event
# stays date-free across years.
_HEADER_DATE_TAIL_RE = re.compile(
    r"\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\b.*$",
    re.I,
)


def _clean_event_header(text: str) -> str:
    """Normalise whitespace, drop `<month> <day>` tail, strip year-suffix."""
    s = re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()
    s = _HEADER_DATE_TAIL_RE.sub("", s).strip()
    s = _strip_event_year_suffix(s)
    return s


def _slug_from_url(url: str) -> str:
    """Filename-derived fallback when the page lacks structural headers."""
    path = urlsplit(url).path
    parts = path.split("/")
    # SYC documents URLs end in `<filename>.pdf/<uuid>` — pop the trailing
    # UUID so we slug the human-readable filename instead.
    while parts and re.fullmatch(r"[0-9a-f-]{8,}", parts[-1] or ""):
        parts.pop()
    base = unquote(parts[-1] if parts else "")
    base = base.rsplit(".", 1)[0]
    base = base.replace("+", " ").replace("_", " ").strip()
    return base or url


def discover_from_past_race_results(html: str
                                    ) -> Iterator[tuple[Optional[int], str, str]]:
    """Yield (year, event_name, canonical_url) per Sailwave / Yacht Scoring /
    SYC-PDF anchor on the SYC past-race-results page.

    Year falls back to None for anchors outside any year section; the caller
    decides whether to drop them (parse.py's --since filter requires a year).
    """
    soup = BeautifulSoup(html, "lxml")
    seen: set[str] = set()
    for a in soup.find_all("a"):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        # Resolve relative -> absolute against the SYC site root. Anchor
        # text is consulted by _is_target_link for PDF filtering.
        anchor_text = a.get_text(" ", strip=True)
        if href.startswith("/"):
            href = "https://" + SYC_DOC_HOST + href
        if not _is_target_link(href, anchor_text):
            continue
        url = _normalise_host(href)
        if url in seen:
            continue
        seen.add(url)
        year = _enclosing_year(a)
        raw_ev = _nearest_event_header(a) or _slug_from_url(url)
        ev = _clean_event_header(raw_ev) or _strip_event_year_suffix(raw_ev)
        yield year, ev, url


# ----------------------------------------------------------------------
# Yacht Scoring discovery — search /v1/public/event

def discover_from_yacht_scoring(http_client, source_name: str
                                ) -> Iterator[tuple[int, str, str]]:
    """Yield (year, event_name, cumulative-result URL) per SYC-hosted event
    found via the Yacht Scoring public search endpoint."""
    seen_eids: set[int] = set()
    for kw in YS_SEARCH_KEYWORDS:
        # Walk pages until we exhaust matches.
        for page_n in range(1, 50):
            search_url = (
                f"{YACHT_SCORING_API}/public/event"
                f"?searchKeyword={kw}&page={page_n}&size=100"
            )
            try:
                body = http_client.get(search_url, source=source_name).decode(
                    "utf-8", errors="replace"
                )
            except Exception as e:
                print(f"[{source_name}] WARN YS search {kw!r} page={page_n}: {e}")
                break
            try:
                d = json.loads(body)
            except json.JSONDecodeError as e:
                print(f"[{source_name}] WARN YS search {kw!r} JSON: {e}")
                break
            rows = d.get("rows") or []
            if not rows:
                break
            for r in rows:
                eid = r.get("id")
                if not isinstance(eid, int) or eid in seen_eids:
                    continue
                if not _is_syc_yacht_scoring_event(r):
                    continue
                seen_eids.add(eid)
                year = _yacht_scoring_year(r)
                if year is None:
                    continue
                name = (r.get("name") or "").strip() or f"YS event {eid}"
                name = _strip_event_year_suffix(name)
                cum_url = (
                    f"{YACHT_SCORING_API}/public/event/{eid}/cumulative-result"
                )
                yield year, name, cum_url
            count = d.get("count") or 0
            if page_n * 100 >= count:
                break


def _is_syc_yacht_scoring_event(row: dict) -> bool:
    """Whitelist heuristic: keep events whose name or hostClub mentions
    Seattle / SYC-affiliated organisations.

    The /public/event search returns global matches for "Seattle" — most
    are unrelated (e.g. "Sperry Sailing World Regatta - Seattle" lives
    under that filter even though the host is "Sailing World"). We keep
    the search broad and filter here so the heuristic is reviewable.
    """
    name = (row.get("name") or "").lower()
    host_club = (((row.get("hostClub") or {}).get("name")) or "").lower()
    keywords = ("seattle yacht club", "syc", "shilshole",
                "j fest seattle", "sperry top-sider seattle nood")
    if any(k in name for k in keywords) or any(k in host_club for k in keywords):
        return True
    return False


def _yacht_scoring_year(row: dict) -> Optional[int]:
    for key in ("startDate", "endDate", "createdAt"):
        v = row.get(key)
        if isinstance(v, str) and len(v) >= 4 and v[:4].isdigit():
            return int(v[:4])
    # Fallback: leading 4 digits of name
    m = re.match(r"^(\d{4})", row.get("name") or "")
    if m:
        return int(m.group(1))
    return None


# ----------------------------------------------------------------------
# Per-page event-name canonicalisation (Sailwave <h1> > discover-time text)

_TRAILING_YEAR_RE = re.compile(r"\s+(19|20)\d{2}\s*$")


def event_name_from_sailwave(html: str, fallback: str) -> str:
    """Pull canonical name from a Sailwave page's <h1>; fall back to title.

    This mirrors `cyc_archive._event_name_from_html` — kept local so the
    two sources don't have to share a private import path.
    """
    soup = BeautifulSoup(html, "lxml")
    h1 = soup.find("h1")
    if h1:
        txt = re.sub(r"\s+", " ", h1.get_text(" ", strip=True)).strip()
        txt = _TRAILING_YEAR_RE.sub("", txt).strip()
        if txt:
            return _strip_event_year_suffix(txt)
    title = soup.find("title")
    if title:
        txt = title.get_text(" ", strip=True)
        m = re.match(r"sailwave results for (.*?) at ", txt, re.I)
        if m:
            return _strip_event_year_suffix(
                _TRAILING_YEAR_RE.sub("", m.group(1).strip()).strip()
            )
    return fallback


# ----------------------------------------------------------------------
# Source class

class SYCSource(Source):
    name = "syc"

    def discover(self) -> Iterator[ResultPage]:
        if self.http_client is None:
            raise RuntimeError(f"{self.name}.discover requires http_client")

        # 1a. Local Playwright-rendered seeds (post-Wayback dates).
        # Cloudflare blocks plain curl on the live SYC page; periodically
        # refresh via Playwright and drop the rendered HTML into
        # storage/seeds/syc_past_race_results_<DATE>.html.
        seen: set[str] = set()
        for seed_path in sorted(LIVE_SEED_DIR.glob(LIVE_SEED_GLOB)):
            try:
                body = seed_path.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                print(f"[{self.name}] WARN live seed {seed_path}: {e}")
                continue
            for year, event, url in discover_from_past_race_results(body):
                if year is None:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                yield ResultPage(
                    source=self.name,
                    event=event,
                    year=year,
                    label="primary",
                    url=url,
                )

        # 1b. Wayback-seeded SYC past-race-results page (union across all
        # snapshots — the live page drops older years off the bottom over
        # time, so older snapshots backfill events newer snapshots forgot).
        for snapshot_url in PAST_RACE_RESULTS_WAYBACK_URLS:
            try:
                body = self.http_client.get(
                    snapshot_url, source=self.name
                ).decode("utf-8", errors="replace")
            except Exception as e:
                print(f"[{self.name}] WARN past-race-results seed {snapshot_url}: {e}")
                continue
            for year, event, url in discover_from_past_race_results(body):
                if year is None:
                    continue  # parse.py --since filter needs a year
                if url in seen:
                    continue
                seen.add(url)
                yield ResultPage(
                    source=self.name,
                    event=event,
                    year=year,
                    label="primary",
                    url=url,
                )

        # 2. Yacht Scoring search
        try:
            for year, event, url in discover_from_yacht_scoring(
                self.http_client, self.name
            ):
                if url in seen:
                    continue
                seen.add(url)
                yield ResultPage(
                    source=self.name,
                    event=event,
                    year=year,
                    label="primary",
                    url=url,
                )
        except Exception as e:
            print(f"[{self.name}] WARN YS search: {e}")

    def parse(self, html: bytes, page: ResultPage) -> Iterator[ResultRow]:
        """Dispatch by URL: PDF -> pdfplumber parser; YS -> JSON; else Sailwave."""
        parts = urlsplit(page.url)
        host = parts.netloc.lower()
        path_l = parts.path.lower()
        # SYC documents PDFs (path may have a trailing /<uuid>)
        if host == SYC_DOC_HOST and ".pdf" in path_l:
            yield from parse_syc_pdf(html, page)
            return
        if any(host.endswith(h) for h in YACHT_SCORING_HOSTS) or host.endswith(
            "api.yachtscoring.com"
        ):
            yield from parse_cumulative_json(html, page)
            return
        text = html.decode("utf-8", errors="replace")
        canonical = event_name_from_sailwave(text, fallback=page.event)
        if canonical and canonical != page.event:
            page.event = canonical
        yield from parse_sailwave_table(text, page)
