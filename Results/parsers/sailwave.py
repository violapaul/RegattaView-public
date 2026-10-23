"""Generic Sailwave HTML table extractor.

Sailwave outputs follow a stable shape:
    <p class=classtitle>Class N - Long Name - Start: ... - Course: ...</p>
    <table>
        <tr><th>Boat Name</th><th>SailNo</th>... <th>Place</th></tr>
        <tr><td>Foo</td><td>123</td>... <td>1</td></tr>
        ...
    </table>

The header order can vary slightly per page (single-day vs series). We map
every header cell against a synonym table and project each data row into a
canonical ResultRow.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterator, Optional

from bs4 import BeautifulSoup

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..sources.base import ResultPage, ResultRow


# Header text -> canonical column key. Compared case-insensitively,
# punctuation and whitespace stripped.
HEADER_SYNONYMS: dict[str, str] = {
    # placement
    "place": "place",
    "pos": "place",
    "rank": "place",
    "position": "place",
    # boat name
    "boatname": "boat_name",
    "boat": "boat_name",
    "yacht": "boat_name",
    "name": "boat_name",
    # sail number
    "sailno": "sail_number",
    "sail": "sail_number",
    "sail#": "sail_number",
    "sailnumber": "sail_number",
    "sailnum": "sail_number",
    # boat type
    "boattype": "boat_type",
    "type": "boat_type",
    "class": "boat_type",
    "design": "boat_type",
    "model": "boat_type",
    # skipper
    "skipper": "skipper",
    "skippername": "skipper",
    "helm": "skipper",
    "helmname": "skipper",
    "owner": "skipper",
    "helmsman": "skipper",
    "captain": "skipper",
    # club
    "club": "yacht_club",
    "yc": "yacht_club",
    "yachtclub": "yacht_club",
    # rating
    "rating": "rating",
    "phrf": "rating",
    "handicap": "rating",
    "tcf": "rating",
    "orc": "rating",
    # times
    "elapsed": "elapsed",
    "et": "elapsed",
    "elapsedtime": "elapsed",
    "corrected": "corrected",
    "ct": "corrected",
    "correctedtime": "corrected",
    # score / total / OA — series totals show up here too
    "score": "score",
    "pts": "score",
    "points": "score",
    "total": "score",
    "nett": "score",
    "net": "score",
}


_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _normalize_header(text: str) -> str:
    return _PUNCT_RE.sub("", text.lower()).strip()


def _normalize_name(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text)
    s = s.encode("ascii", "ignore").decode("ascii")
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_sail(text: str) -> str:
    if not text:
        return ""
    return re.sub(r"\D", "", text)


def _cell_text(td) -> str:
    if td is None:
        return ""
    txt = td.get_text(" ", strip=True)
    txt = txt.replace("\xa0", " ").strip()
    return txt


def _parse_place(raw: str) -> tuple[str, Optional[int]]:
    raw = raw.strip()
    if not raw:
        return "", None
    if raw.isdigit():
        return raw, int(raw)
    # Match a leading run of digits, allowing English ordinal suffixes
    # (1st, 2nd, 3rd, 4th, ...) Sailwave summary tables emit.
    m = re.match(r"^(\d+)(?:st|nd|rd|th)?(?:\b|$)", raw, re.I)
    if m:
        return raw, int(m.group(1))
    return raw, None


def _extract_class_name(table) -> Optional[str]:
    """Walk backward from <table> looking for the nearest <p class=classtitle> /
    <h2> / <h3>.

    Sailwave uses `<p class=classtitle>Class 1 - Slooper Division - ...</p>`
    immediately above the table.
    """
    node = table
    for _ in range(20):  # bounded crawl
        node = node.previous_sibling
        if node is None:
            break
        if getattr(node, "name", None) in ("p", "h1", "h2", "h3", "h4"):
            classes = node.get("class") or []
            if (node.name == "p" and "classtitle" in classes) or node.name in (
                "h1", "h2", "h3", "h4"
            ):
                txt = node.get_text(" ", strip=True)
                if txt:
                    return _clean_class_text(txt)
    # Fallback: first preceding <p class=classtitle> anywhere upstream
    p = table.find_previous("p", class_="classtitle")
    if p is not None:
        return _clean_class_text(p.get_text(" ", strip=True))
    return None


def _clean_class_text(txt: str) -> str:
    # 'Class: 1 - Slooper Division - Start: 11:00:00 - Course: NKMN - Distance: 13.518'
    # -> 'Class 1 - Slooper Division'
    txt = re.sub(r"\s+", " ", txt).strip()
    # drop trailing 'Options' button text Sailwave injects
    txt = re.sub(r"\s*Options\s*$", "", txt, flags=re.I).strip()
    # truncate at first ' - Start:' / ' - Course:' / ' - Distance:'
    for sep in (" - Start:", " - Course:", " - Distance:"):
        idx = txt.find(sep)
        if idx >= 0:
            txt = txt[:idx]
    return txt.strip()


def parse_sailwave_table(html, page):
    """Yield ResultRow objects parsed from one Sailwave HTML page."""
    from ..sources.base import ResultRow  # local to avoid circular import
    soup = BeautifulSoup(html, "lxml")
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header_cells = rows[0].find_all(["th", "td"])
        headers = [_normalize_header(_cell_text(c)) for c in header_cells]
        # Heuristic: at least one canonical header AND a place/total column.
        column_map: list[Optional[str]] = [
            HEADER_SYNONYMS.get(h) for h in headers
        ]
        canonical = [c for c in column_map if c is not None]
        if "boat_name" not in canonical:
            continue
        if "place" not in canonical and "score" not in canonical:
            # Not a results table — could be a navigation table
            continue

        class_name = _extract_class_name(table)

        for tr in rows[1:]:
            tds = tr.find_all(["td", "th"])
            if not tds:
                continue
            data: dict[str, str] = {}
            for col_key, td in zip(column_map, tds):
                if col_key is None:
                    continue
                txt = _cell_text(td)
                # First mapping wins (defensive: duplicate header keys)
                data.setdefault(col_key, txt)
            boat_name = (data.get("boat_name") or "").strip()
            if not boat_name:
                continue
            place_raw = (data.get("place") or "").strip()
            place, place_int = _parse_place(place_raw)
            yield ResultRow(
                page=page,
                place=place,
                place_int=place_int,
                boat_name=boat_name,
                sail_number=(data.get("sail_number") or "").strip() or None,
                boat_type=(data.get("boat_type") or "").strip() or None,
                skipper=(data.get("skipper") or "").strip() or None,
                yacht_club=(data.get("yacht_club") or "").strip() or None,
                rating=(data.get("rating") or "").strip() or None,
                elapsed=(data.get("elapsed") or "").strip() or None,
                corrected=(data.get("corrected") or "").strip() or None,
                score=(data.get("score") or "").strip() or None,
                class_name=class_name,
            )


def boat_name_canon(name: str) -> str:
    return _normalize_name(name)


def sail_number_canon(sail: Optional[str]) -> str:
    return _normalize_sail(sail or "")
