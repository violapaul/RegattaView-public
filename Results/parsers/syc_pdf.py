"""Parse SYC's Sailwave-generated result PDFs.

SYC posts post-2018 race results as PDFs at
`https://www.seattleyachtclub.org/documents/<id>/<id>/<filename>.pdf[/<uuid>]`.

Two PDF families:

1. **Modern Sailwave-PDF** (most 2018-2024 events). Real text + Type1
   fonts with ToUnicode mappings. pdfplumber.extract_tables() recovers
   the result rows cleanly.

2. **Ghostscript/PScript5-printed PDFs** (some 2025 events, esp.
   Tri-Island Series). Type3 fonts with no Unicode mapping —
   pdfplumber and pymupdf both return `(cid:N)` glyph codes. Only OCR
   can read these. We fall back automatically when the regular
   extractor finds no usable tables.

Column header conventions (PDF-specific — note "Boat Type" and "Class"
are distinct columns, where "Class" is the racing class assignment, not
the design):

    Place | Boat Name | Boat Type | Course | Class | Sail No. | Rating
        | Skipper / Helm | Yacht Club | Finish | Elapsed | Corrected | BCE

Section headers above each table look like:

    Smith Island - 1 Class - April 27,2024 at 09:05:00
"""

from __future__ import annotations

import io
import re
import unicodedata
from typing import TYPE_CHECKING, Iterator, Optional

import pdfplumber

if TYPE_CHECKING:
    from ..sources.base import ResultPage, ResultRow

# PDF-specific header synonyms (differs from Sailwave HTML in that
# "Class" maps to class_name, not boat_type).
_HEADER_SYNONYMS: dict[str, str] = {
    "place": "place", "pos": "place", "rank": "place",
    "placeoverall": "place",
    "boatname": "boat_name", "boat": "boat_name", "yacht": "boat_name",
    "boattype": "boat_type", "type": "boat_type", "design": "boat_type",
    "course": "course",
    "class": "class_name",
    "sailno": "sail_number", "sail": "sail_number", "sailnumber": "sail_number",
    "rating": "rating", "phrf": "rating", "tcf": "rating", "orc": "rating",
    "skipper": "skipper", "skipperhelm": "skipper", "helm": "skipper",
    "owner": "skipper", "skipperhelmsman": "skipper",
    "yachtclub": "yacht_club", "club": "yacht_club", "yc": "yacht_club",
    "finish": "finish",
    "elapsed": "elapsed", "et": "elapsed",
    "corrected": "corrected", "ct": "corrected",
    "score": "score", "total": "score", "pts": "score", "points": "score",
    "nett": "score", "net": "score",
    # BCE = "Behind Class Elapsed" — drop
    "bce": None,
}

_PUNCT_RE = re.compile(r"[^a-z0-9]+")


def _norm_header(text: str) -> str:
    """Strip newlines, punctuation; lowercase."""
    if not text:
        return ""
    return _PUNCT_RE.sub("", text.lower())


def _norm_cell(text: Optional[str]) -> str:
    """Collapse newlines/whitespace inside a PDF table cell."""
    if not text:
        return ""
    s = text.replace("\n", " ").replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _normalize_name(text: str) -> str:
    if not text:
        return ""
    s = unicodedata.normalize("NFKD", text)
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _normalize_sail(text: str) -> str:
    return re.sub(r"\D", "", text or "")


def _parse_place(text: str) -> tuple[str, Optional[int]]:
    """('1', 1) | ('2nd', 2) | ('DNF', None) | ('', None)"""
    s = (text or "").strip()
    if not s:
        return "", None
    m = re.match(r"^(\d+)", s)
    if m:
        try:
            return s, int(m.group(1))
        except ValueError:
            pass
    return s, None


# Section header regex. Matches lines like:
#   "Smith Island - 1 Class - April 27,2024 at 09:05:00"
#   "ORC Long - April 27, 2024"
#   "PHRF A - 2024-04-27"
# We capture: race-label (the part before the first " - "), class-or-tail
# (middle), date-tail. None of the captures are required to align across
# documents — we just want enough to disambiguate per-class race rows.
_SECTION_HDR_RE = re.compile(
    r"^([^\n]+?)\s+-\s+([^\n]+?)\s+-\s+"
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*\s+\d{1,2}[, ]+\s*\d{4}"
    r"(?:\s+at\s+\d{1,2}:\d{2}(?::\d{2})?)?)\s*$",
    re.M,
)


def _extract_section_headers(text: str) -> list[tuple[str, str, str]]:
    """Return list of (race_label, class_name, date_str) tuples in order."""
    return [
        (_norm_cell(m.group(1)), _norm_cell(m.group(2)), _norm_cell(m.group(3)))
        for m in _SECTION_HDR_RE.finditer(text)
    ]


_DATE_PARSE_RE = re.compile(
    r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[A-Za-z]*)\s+"
    r"(\d{1,2})[, ]+\s*(\d{4})"
)
_MONTH_NUM = {"jan":1,"feb":2,"mar":3,"apr":4,"may":5,"jun":6,
              "jul":7,"aug":8,"sep":9,"oct":10,"nov":11,"dec":12}


def _iso_date(s: str) -> str:
    m = _DATE_PARSE_RE.search(s)
    if not m:
        return ""
    mon = _MONTH_NUM.get(m.group(1)[:3].lower())
    if not mon:
        return ""
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"


def parse_syc_pdf(pdf_bytes: bytes, page: "ResultPage") -> Iterator["ResultRow"]:
    """Yield one ResultRow per finisher in `pdf_bytes`.

    Two layers of cache, both keyed by SHA1 of pdf_bytes:
      - Final parsed rows (`<sha1>.rows.json.gz`) — skips both pdfplumber
        and OCR entirely on hit.
      - OCR word records (`<sha1>.ocr.json.gz`) — skips Tesseract.
    """
    from ..sources.base import ResultRow  # deferred to avoid circular import

    # Top-level row cache hit → return immediately.
    cached_rows = _load_rows_cache(pdf_bytes, page)
    if cached_rows is not None:
        for d in cached_rows:
            yield ResultRow(page=page, **d)
        return

    rows = list(_parse_via_pdfplumber(pdf_bytes, page))
    if not rows:
        # No usable rows from pdfplumber — likely CID-encoded. OCR fallback.
        rows = list(_parse_via_ocr(pdf_bytes, page))

    _save_rows_cache(pdf_bytes, page, rows)
    yield from rows


def _rows_cache_path(pdf_bytes: bytes, page: "ResultPage"):
    import hashlib
    from pathlib import Path
    sha1 = hashlib.sha1(pdf_bytes).hexdigest()
    storage_root = Path(__file__).resolve().parent.parent / "storage" / "raw"
    return storage_root / page.source / f"{sha1}.rows.json.gz"


_ROW_FIELDS = (
    "place", "place_int", "boat_name", "sail_number", "boat_type",
    "skipper", "yacht_club", "rating", "elapsed", "corrected", "score",
    "class_name",
)


def _load_rows_cache(pdf_bytes: bytes, page: "ResultPage"):
    import gzip, json
    p = _rows_cache_path(pdf_bytes, page)
    if not p.exists():
        return None
    try:
        with gzip.open(p, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except Exception as e:
        print(f"[syc_pdf] rows cache read error {p}: {e}")
        return None


def _save_rows_cache(pdf_bytes: bytes, page: "ResultPage", rows: list) -> None:
    import gzip, json
    p = _rows_cache_path(pdf_bytes, page)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = [
        {k: getattr(r, k) for k in _ROW_FIELDS}
        for r in rows
    ]
    try:
        with gzip.open(p, "wb") as f:
            f.write(json.dumps(payload).encode("utf-8"))
    except Exception as e:
        print(f"[syc_pdf] rows cache write error {p}: {e}")


def _parse_via_pdfplumber(pdf_bytes: bytes, page: "ResultPage"
                          ) -> Iterator["ResultRow"]:
    from ..sources.base import ResultRow
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for pdf_page in pdf.pages:
            text = pdf_page.extract_text() or ""
            sections = _extract_section_headers(text)
            tables = pdf_page.extract_tables() or []

            # Best-effort 1:1 zip. If counts mismatch, fall back to no
            # section context (still emit rows; class_name will be empty).
            if len(sections) == len(tables):
                paired = list(zip(tables, sections))
            else:
                paired = [(t, ("", "", "")) for t in tables]

            for table, (race_label, sect_class, date_str) in paired:
                if not table or len(table) < 2:
                    continue
                headers_raw = table[0]
                col_keys: list[Optional[str]] = []
                for h in headers_raw:
                    nh = _norm_header(h or "")
                    col_keys.append(_HEADER_SYNONYMS.get(nh))

                # Need at least a place + boat_name to be useful
                if "place" not in col_keys or "boat_name" not in col_keys:
                    continue

                race_date = _iso_date(date_str)

                for raw in table[1:]:
                    if not raw or all(not _norm_cell(c) for c in raw):
                        continue
                    fields: dict[str, str] = {}
                    for key, cell in zip(col_keys, raw):
                        if key is None:
                            continue
                        fields.setdefault(key, _norm_cell(cell))

                    boat_name = fields.get("boat_name", "")
                    if not boat_name:
                        continue

                    place_str, place_int = _parse_place(fields.get("place", ""))
                    # PDF "Class" cell wins over section header for class_name;
                    # fall back to section if cell is blank.
                    class_name = (fields.get("class_name") or sect_class or "").strip()
                    # Augment race label with date-day so per-day series races
                    # become distinct race_ids in parse.py.
                    label = race_label or page.label
                    if race_date:
                        label = f"{label} [{race_date}]" if label else race_date

                    yield ResultRow(
                        page=page,
                        place=place_str,
                        place_int=place_int,
                        boat_name=boat_name,
                        sail_number=fields.get("sail_number") or None,
                        boat_type=fields.get("boat_type") or None,
                        skipper=fields.get("skipper") or None,
                        yacht_club=fields.get("yacht_club") or None,
                        rating=fields.get("rating") or None,
                        elapsed=fields.get("elapsed") or None,
                        corrected=fields.get("corrected") or None,
                        score=fields.get("score") or None,
                        class_name=class_name or None,
                    )


# ----------------------------------------------------------------------
# OCR fallback for Type3 / CID-encoded PDFs
#
# Strategy: render each page via pymupdf, OCR via tesseract with TSV
# output (per-word bounding boxes), then for each page:
#   1. Find the "Place" header word — that anchors the table top.
#   2. Use the X-coordinates of header words on the same row to define
#      column centres.
#   3. Cluster non-header words by Y (10px tolerance) into row bands.
#   4. Within each band, assign each word to its nearest column centre
#      and concatenate same-column words.
#   5. Emit data rows that start with a place token (^\d+(st|nd|rd|th)?$).

_PLACE_TOKEN_RE = re.compile(r"^\d+(?:st|nd|rd|th)?$", re.I)
_OCR_HEADER_KEYS = {
    "place": "place", "pos": "place", "rank": "place",
    "boat": "boat_name",   # disambiguated by X-anchor below
    "type": "boat_type",
    "course": "course",
    "class": "class_name",
    "sail": "sail_number", "number": "sail_number",
    "rating": "rating", "phrf": "rating",
    "skipper": "skipper", "helm": "skipper",
    "yacht": "yacht_club", "club": "yacht_club",
    "total": "score",
    "finish": "finish",
    "elapsed": "elapsed",
    "corrected": "corrected",
}


def _parse_via_ocr(pdf_bytes: bytes, page: "ResultPage"
                   ) -> Iterator["ResultRow"]:
    from ..sources.base import ResultRow

    # OCR cache: key = SHA1(pdf_bytes), value = list of per-page word
    # records as JSON. ~5 min Tesseract pass becomes ~50 ms cache load.
    cached = _load_ocr_cache(pdf_bytes, page)
    if cached is not None:
        for page_words in cached:
            yield from _ocr_page_rows_from_words(page_words, page)
        return

    try:
        import fitz
        import pytesseract
        from PIL import Image
    except ImportError as e:
        print(f"[syc_pdf] OCR unavailable ({e}); skipping {page.url}")
        return

    pages_words: list[list[dict]] = []
    with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
        for pdf_page in doc:
            mat = fitz.Matrix(200 / 72, 200 / 72)  # 200 DPI
            pix = pdf_page.get_pixmap(matrix=mat, alpha=False)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            try:
                data = pytesseract.image_to_data(
                    img, output_type=pytesseract.Output.DICT
                )
            except pytesseract.TesseractError as e:
                print(f"[syc_pdf] tesseract error: {e}")
                pages_words.append([])
                continue
            words = []
            for i, txt in enumerate(data["text"]):
                t = (txt or "").strip()
                if not t:
                    continue
                words.append({
                    "text": t, "x": data["left"][i], "y": data["top"][i],
                    "w": data["width"][i], "h": data["height"][i],
                })
            pages_words.append(words)
            yield from _ocr_page_rows_from_words(words, page)

    _save_ocr_cache(pdf_bytes, page, pages_words)


def _ocr_cache_path(pdf_bytes: bytes, page: "ResultPage"):
    """storage/raw/<source>/<sha1>.ocr.json.gz keyed by PDF body hash."""
    import hashlib
    from pathlib import Path
    sha1 = hashlib.sha1(pdf_bytes).hexdigest()
    storage_root = Path(__file__).resolve().parent.parent / "storage" / "raw"
    return storage_root / page.source / f"{sha1}.ocr.json.gz"


def _load_ocr_cache(pdf_bytes: bytes, page: "ResultPage"):
    import gzip, json
    p = _ocr_cache_path(pdf_bytes, page)
    if not p.exists():
        return None
    try:
        with gzip.open(p, "rb") as f:
            return json.loads(f.read().decode("utf-8"))
    except Exception as e:
        print(f"[syc_pdf] OCR cache read error {p}: {e}")
        return None


def _save_ocr_cache(pdf_bytes: bytes, page: "ResultPage",
                    pages_words: list) -> None:
    import gzip, json
    p = _ocr_cache_path(pdf_bytes, page)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        with gzip.open(p, "wb") as f:
            f.write(json.dumps(pages_words).encode("utf-8"))
    except Exception as e:
        print(f"[syc_pdf] OCR cache write error {p}: {e}")


def _ocr_page_rows_from_words(words: list[dict], page: "ResultPage"
                              ) -> Iterator["ResultRow"]:
    """Convert OCR word records (text + bounding box) into ResultRows."""
    from ..sources.base import ResultRow
    if not words:
        return

    # Find each "Place" header on this page — there can be multiple
    # (one per class table). Each anchors a column-set + row band.
    place_anchors = [w for w in words
                     if w["text"].lower() in ("place", "pos", "rank")
                     and not _PLACE_TOKEN_RE.match(w["text"])]
    if not place_anchors:
        return

    page_height = max(w["y"] + w["h"] for w in words)

    for ai, anchor in enumerate(place_anchors):
        # Header band: starts at anchor.y, ends at the first place-token
        # word ("1st", "2nd", ...) below it. Caps at +80 px to handle
        # tables with no data rows.
        band_top = anchor["y"]
        first_place_y = min(
            (w["y"] for w in words
             if w["y"] > anchor["y"] + 10
             and _PLACE_TOKEN_RE.match(w["text"])
             and abs(w["x"] - anchor["x"]) < 80),
            default=anchor["y"] + 80,
        )
        band_bot = min(first_place_y - 5, anchor["y"] + 80)
        header_band = [w for w in words if band_top <= w["y"] <= band_bot]
        # Group header band by x to identify column centres
        # Strategy: take all distinct words, dedup by (x_bucket, text) and
        # assign columns by tracking the X position of each canonical key.
        col_anchors: list[tuple[int, str]] = []  # (x, key)
        seen_keys: set[str] = set()
        for w in sorted(header_band, key=lambda x: (x["x"], x["y"])):
            key = _OCR_HEADER_KEYS.get(w["text"].lower())
            if key and key not in seen_keys:
                col_anchors.append((w["x"] + w["w"] // 2, key))
                seen_keys.add(key)
        if "place" not in seen_keys or "boat_name" not in seen_keys:
            continue
        col_anchors.sort()  # left-to-right

        # Determine the row range: from header bottom down to next anchor
        # (or page bottom).
        rows_top = band_bot + 5
        rows_bot = (place_anchors[ai + 1]["y"] - 10
                    if ai + 1 < len(place_anchors) else page_height)
        body_words = [w for w in words if rows_top <= w["y"] <= rows_bot]

        # Cluster body words into rows by Y. Use the first column (Place)
        # as the row anchor: rows start with a place token like "1st".
        place_x = next(x for x, k in col_anchors if k == "place")
        place_rows = [w for w in body_words
                      if abs(w["x"] + w["w"] // 2 - place_x) < 60
                      and _PLACE_TOKEN_RE.match(w["text"])]

        for prow in place_rows:
            row_y = prow["y"] + prow["h"] // 2
            # Pull all body words within +/- 20 px of this row's Y
            row_words = [w for w in body_words
                         if abs((w["y"] + w["h"] // 2) - row_y) <= 20]
            if not row_words:
                continue
            # Sometimes a cell wraps to 2 lines; pull words from a slightly
            # wider band but only if they're not already claimed by the
            # next row's place anchor.
            # Assign each word to the nearest column anchor.
            cells: dict[str, list[str]] = {}
            for w in sorted(row_words, key=lambda x: x["x"]):
                cx = w["x"] + w["w"] // 2
                # find nearest anchor
                nearest = min(col_anchors, key=lambda c: abs(c[0] - cx))
                if abs(nearest[0] - cx) > 200:
                    continue  # too far from any column
                cells.setdefault(nearest[1], []).append(w["text"])

            # Strip stray punctuation OCR loves to insert (`|`, `_`, `.`)
            for k in cells:
                cells[k] = [
                    re.sub(r"^[\|_.\s]+|[\|_.\s]+$", "", w)
                    for w in cells[k]
                ]
                cells[k] = [w for w in cells[k] if w]
            place_str = " ".join(cells.get("place", [])).strip()
            place_str, place_int = _parse_place(place_str)
            boat_name = " ".join(cells.get("boat_name", [])).strip()
            if not boat_name:
                continue
            # Sail number: digits only, drop OCR junk
            sail_raw = " ".join(cells.get("sail_number", []))
            sail_clean = re.sub(r"[^\d]", " ", sail_raw).split()
            sail_number = sail_clean[0] if sail_clean else ""

            # Yacht club often eats the leading score — drop trailing
            # decimal tokens.
            yc_words = cells.get("yacht_club", [])
            yc_clean = []
            for w in yc_words:
                if re.fullmatch(r"\d+(?:\.\d+)?", w):
                    break
                yc_clean.append(w)

            yield ResultRow(
                page=page,
                place=place_str,
                place_int=place_int,
                boat_name=boat_name,
                sail_number=sail_number or None,
                boat_type=" ".join(cells.get("boat_type", [])) or None,
                skipper=" ".join(cells.get("skipper", [])) or None,
                yacht_club=" ".join(yc_clean) or None,
                rating=" ".join(cells.get("rating", [])) or None,
                elapsed=" ".join(cells.get("elapsed", [])) or None,
                corrected=" ".join(cells.get("corrected", [])) or None,
                score=" ".join(cells.get("score", [])) or None,
                class_name=" ".join(cells.get("class_name", [])) or None,
            )
