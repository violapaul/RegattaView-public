"""STYC racedata.js CGI driver + per-event Sailwave page expansion."""

from __future__ import annotations

import re
from typing import Iterator, Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from .base import ResultPage, ResultRow, Source
from ..parsers.sailwave import parse_sailwave_table

CGI_URL = "https://race.styc.org/cgi-bin/racedata.js?mode=results_breaks_all"
CGI_REFERER = "https://www.styc.org/results"

# CGI emits chunks like:
#   document.write("<details>");
#   document.write("<summary ...><strong>Event Name</strong></summary>");
#   document.write("<A href=\"...year_url...\" ...>YEAR </A><br>...");
#   document.write("</details>");
#
# We walk the unescaped content per <details> block and pull (event, year, url).
DETAILS_RE = re.compile(r"<details>(.*?)</details>", re.S | re.I)
EVENT_RE = re.compile(r"<strong>([^<]+)</strong>", re.I)
LINK_RE = re.compile(
    r'<A\s+href="([^"]+)"[^>]*>\s*(\d{4})\s*</A>',
    re.I,
)
INTRA_RACE_LINK_RE = re.compile(
    r'<a\s+href=([\'"]?)(race\d+\.htm|series_[a-z_]+\.htm)\1',
    re.I,
)


def _strip_t_param(url: str) -> str:
    parts = urlsplit(url)
    if not parts.query:
        return url
    keep = "&".join(p for p in parts.query.split("&")
                    if not p.lower().startswith("t="))
    return urlunsplit(parts._replace(query=keep))


def _unescape_js(s: str) -> str:
    """document.write strings escape backslashes and quotes JS-style."""
    return s.replace('\\"', '"').replace("\\/", "/").replace("\\\\", "\\")


def _extract_blocks(cgi_body: str) -> Iterator[tuple[str, str]]:
    """Yield (event_name, html_block) for every <details> in the CGI output.

    The CGI returns document.write(...) calls — concatenate the string args
    first, then split on <details>.
    """
    # collapse all document.write("...") payloads into one big HTML string
    payloads = re.findall(r'document\.write\("(.*?)"\)', cgi_body, re.S)
    html = "".join(_unescape_js(p) for p in payloads)
    for m in DETAILS_RE.finditer(html):
        block = m.group(1)
        ev = EVENT_RE.search(block)
        if ev:
            yield ev.group(1).strip(), block


def discover_from_cgi(cgi_body: str) -> Iterator[ResultPage]:
    """Pure parse step — fed CGI body, yields one ResultPage per (event, year)."""
    for event, block in _extract_blocks(cgi_body):
        for url, year in LINK_RE.findall(block):
            url_clean = _strip_t_param(url.strip())
            try:
                year_int = int(year)
            except ValueError:
                continue
            yield ResultPage(
                source=STYCSource.name,
                event=event,
                year=year_int,
                label="primary",
                url=url_clean,
            )


def discover_intra_race_links(html: str, primary_url: str) -> Iterator[tuple[str, str]]:
    """Yield (label, url) for each per-race / per-overall page linked from a series page."""
    for m in INTRA_RACE_LINK_RE.finditer(html):
        rel = m.group(2)
        absolute = urljoin(primary_url, rel)
        if rel.startswith("race"):
            num = re.match(r"race(\d+)", rel, re.I)
            label = f"Race {num.group(1)}" if num else rel
        else:
            # series_phrf_overall.htm -> 'Series PHRF Overall'
            stem = rel[: -len(".htm")] if rel.lower().endswith(".htm") else rel
            label = stem.replace("_", " ").title()
        yield label, absolute


class STYCSource(Source):
    name = "styc"

    def discover(self) -> Iterator[ResultPage]:
        if self.http_client is None:
            raise RuntimeError("STYCSource.discover requires http_client")
        cgi_body = self.http_client.get(CGI_URL, source=self.name,
                                        referer=CGI_REFERER).decode("utf-8",
                                                                    errors="replace")
        primaries = list(discover_from_cgi(cgi_body))
        seen_urls = set()
        for page in primaries:
            if page.url in seen_urls:
                continue
            seen_urls.add(page.url)
            yield page
            # Pull series-page intra links (race1.htm, series_phrf_overall.htm, …)
            try:
                body = self.http_client.get(page.url, source=self.name,
                                            referer=CGI_REFERER).decode(
                    "utf-8", errors="replace")
            except Exception:
                continue
            for label, sub_url in discover_intra_race_links(body, page.url):
                if sub_url in seen_urls:
                    continue
                seen_urls.add(sub_url)
                yield ResultPage(
                    source=self.name,
                    event=page.event,
                    year=page.year,
                    label=label,
                    url=sub_url,
                )

    def parse(self, html: bytes, page: ResultPage) -> Iterator[ResultRow]:
        text = html.decode("utf-8", errors="replace")
        yield from parse_sailwave_table(text, page)
