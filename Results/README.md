# RegattaView Results Crawler

Self-contained subtree for crawling and querying PNW sailboat-race
results across multiple clubs. The full design lives in
`.claude/plans/want-to-build-a-replicated-key.md` — this README is just
the operating manual.

## Source status

- **STYC** (`styc`): implemented end-to-end. CGI discovery
  (`racedata.js?mode=results_breaks_all`) + Sailwave HTML parser.
- **CYC archive** (`cyc-archive`): implemented. Walks the static netlify
  archive at `cyc-racingarchive.netlify.app` (1997-2023). Per-year
  landing pages link to per-event Sailwave pages; canonical event names
  come from each event page's `<h1>`. Same Sailwave parser as STYC.
- **CYC main** (`cyc-main`): implemented. Drives off
  `cycseattle.org/<YEAR>-race-results` — these per-year landing pages
  are Vue-rendered HTML where event links are URL-encoded JSON inside
  `:column_prop="..."` Vue attributes. Each event link points at a
  ClubSpot regatta page; per-class series-overall results come from
  `https://results.theclubspot.com/clubspot-results-v4/<regatta_id>?boatClassIDs=<class_id>`
  (no auth, parsed by `parsers/clubspot.py`). Year list is hardcoded;
  bump `YEAR_LANDING_URLS` in `sources/cyc_main.py` for new years.
- **SYC** (`syc`): implemented across **four** discovery channels:
  1. **Live Playwright-rendered seed** at
     `storage/seeds/syc_past_race_results_<DATE>.html` — the canonical
     SYC results page is Cloudflare-blocked, so we render it via
     Playwright and commit the dump. See `storage/seeds/README.md` for
     refresh instructions. This is the primary surface and yields the
     current-namespace `/documents/20124/...` PDF URLs.
  2. **Wayback CDX union** of 11 historical snapshots of the same SYC
     page (constants in `sources/syc.py::PAST_RACE_RESULTS_WAYBACK_SNAPSHOTS`).
     Backfills events the live page has dropped. PDFs in the retired
     `/documents/10184/` namespace are filtered out (every URL there
     404s today).
  3. **S3 Sailwave HTML** at `seattleyachtclubsailing.s3-us-west-2.amazonaws.com`
     and the legacy `www.ceclients.com/sites/SeattleYachtClub/` host —
     parsed by `parsers/sailwave.py`.
  4. **Yacht Scoring** `/v1/public/event` keyword search for the small
     handful of SYC events on Yacht Scoring (J FEST 2024, eID 16398) —
     parsed by `parsers/yacht_scoring.py`.

  Per-event pages dispatch by host in `SYCSource.parse`: SYC `/documents/`
  PDFs → `parsers/syc_pdf.py`; Yacht Scoring → `parsers/yacht_scoring.py`;
  everything else → `parsers/sailwave.py`.
- **CYC ClubSpot** (`cyc-clubspot`): stub that raises
  `NotImplementedError`. Reserved for the day we want to crawl regattas
  on the central ClubSpot platform that aren't linked from
  cycseattle.org's per-year pages.

## Quickstart

```bash
cd Results
pip install -r requirements.txt
# system dep for SYC PDF OCR fallback (only needed if Tesseract isn't installed):
brew install tesseract                 # macOS

# one-shot full pipeline against every active source
python crawl.py --source all           # network: discovery + per-event pages
python parse.py  --source all          # offline: HTML/PDF -> CSV
python identify.py                     # cluster boats
python query.py races-for-boat "Peer Gynt"
```

In this repo the right Python is the `sailing` conda env:
`conda run --no-capture-output -n sailing python <cmd>`. The
`--no-capture-output` flag is required for any conda-run whose stdout
gets redirected/backgrounded, otherwise the log stays empty until exit.

The `--since YEAR` flag (default 2017) skips pages older than that.
Cached HTML, OCR, and parsed-row outputs all survive across runs under
`storage/raw/`.

## CLIs

- `crawl.py --source <name|all> [--since YEAR] [--force]` — discover +
  fetch with disk cache. Re-runs are nearly free; conditional GET
  (etag / Last-Modified) avoids re-downloads.
- `parse.py --source <name|all>` — offline. Reads cached HTML, writes
  `storage/data/{races,results}.csv`. Idempotent.
- `identify.py` — clusters `results.csv` rows into boat identities.
  Writes `boats.csv`, `boat_aliases.csv`, an `exports/boats_review.csv`
  spot-check list, and stamps `boat_id` back onto `results.csv`.
- `query.py boats` — top boats by result count.
- `query.py boat <name>` — every row for boats whose primary or alias
  name contains `<name>`.
- `query.py races-for-boat <name>` — every race the boat entered, with
  the full leaderboard for each. CSV via `--output`, otherwise markdown
  on stdout.
- `query.py race <race_id>` — full leaderboard for one race.

## Where to look

```
storage/
  raw/<source>/<url-sha1>.html.gz       # cached page bodies (gitignored)
  raw/<source>/<body-sha1>.ocr.json.gz  # SYC: cached OCR words by PDF body hash
  raw/<source>/<body-sha1>.rows.json.gz # SYC: cached parsed ResultRows
  raw/<source>/index.jsonl              # url -> sha1, fetched_at, etag
  data/races.csv                        # one row per race
  data/results.csv                      # one row per finisher per race
  data/boats.csv                        # one row per identity
  data/boat_aliases.csv                 # observed (name, sail#) variants
  identity_overrides.yaml               # manual force-merge / force-split (committed)
  seeds/syc_past_race_results_*.html    # Playwright-rendered SYC seeds (committed)
  seeds/README.md                       # how to refresh the SYC seed
exports/
  boats_review.csv                      # 60–87 token_set_ratio name pairs
                                        # inside a sail-# group — eyeball-worthy
```

## SYC PDF caching (the slow path)

SYC posts post-2018 results as PDFs. Two flavours, both at
`seattleyachtclub.org/documents/<id>/<id>/<file>.pdf[/<uuid>]`:

- **Modern Sailwave-PDF** (most events): real Type1 fonts + ToUnicode
  mappings. `parsers/syc_pdf.py::_parse_via_pdfplumber` extracts tables
  directly via pdfplumber.
- **Ghostscript/PScript5-printed PDFs** (2025 Tri-Island, Mark Mayer):
  Type3 fonts with no Unicode mapping. Both pdfplumber and pymupdf
  return `(cid:N)` glyph codes. We render via `pymupdf` at 200 DPI and
  OCR via `pytesseract.image_to_data` (TSV with bounding boxes), then
  spatially cluster words around the "Place" header anchor to recover
  the table.

To avoid burning Tesseract on every parse re-run, two persistent caches
live next to the cached HTML:

| layer | filename | what's stored | when populated |
|---|---|---|---|
| Raw HTML body | `<url-sha1>.html.gz` | gzipped PDF/HTML bytes | `crawl.py` |
| OCR words | `<body-sha1>.ocr.json.gz` | per-page `image_to_data` records | `parse.py` cache miss for CID PDFs |
| Parsed rows | `<body-sha1>.rows.json.gz` | final `ResultRow` list | every `parse.py` cache miss |

`parse_syc_pdf` checks the rows cache first → on hit, skips both
pdfplumber and OCR. Cold parse: ~5 min. With caches: ~16 s for SYC.

## How identity matching works

1. Group rows by digits-only sail number. Inside each group,
   sub-cluster by `boat_name_canon` using
   `rapidfuzz.fuzz.token_set_ratio >= 88` so "Peer Gynt", "PEER GYNT",
   "Peer-Gynt" merge but unrelated boats with the same sail number stay
   apart.
2. Rows with no sail number cluster by exact
   `(boat_name_canon, boat_type)` — refuse to merge across types.
3. Apply manual overrides from `storage/identity_overrides.yaml`
   (`merges` and `splits`) on top.

`exports/boats_review.csv` flags name pairs in the 60–87 score band so
you can promote them to `merges` if they're false splits.

## Adding a new source

1. Create `sources/<name>.py` with a `Source` subclass (set `name`,
   implement `discover()` and `parse()`).
2. Register it in `sources/__init__.py`'s `ALL_SOURCES` dict.
3. If the page format is Sailwave, you can usually delegate `parse()`
   straight to `parsers.sailwave.parse_sailwave_table`.
4. Add a fixture under `tests/fixtures/<name>/` and a parser test.

## Tests

```bash
cd Results && conda run --no-capture-output -n sailing python -m pytest -v
```

All tests are offline (fixture-driven). No network needed. Current
suite: 63 tests covering STYC discovery + parser, CYC archive
discovery + parser, CYC main discovery + parser, SYC discovery +
parser (Sailwave HTML, Yacht Scoring JSON, Wayback past-race-results),
plus identity clustering. The SYC PDF parser doesn't have unit tests
yet — its inputs are large binary fixtures we'd rather not commit.

## Current numbers (rough)

After a full crawl of all four implemented sources and identify run:

- ~50,000 result rows / ~6,000 races / ~3,000 boat identities
- Coverage: STYC 2017–present, CYC archive 1997–2023 (filtered to
  ≥2017), CYC main 2024–2025, SYC 2017–2025 with gaps in 2020 (COVID)
  and partial 2025/2026 (a few PDFs use bad fonts that even OCR
  struggles with).
