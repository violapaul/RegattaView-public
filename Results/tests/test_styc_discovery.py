"""STYC CGI discovery — fixture-driven, no network."""

from pathlib import Path

from Results.sources.styc import discover_from_cgi

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "styc" / "racedata_breaks_all.js"

# Subset known to be present — the CGI also exposes other events
# (Commodores Cup, Iceberg, Spring Race, etc.) which we don't enumerate
# here but which discover() should still pick up.
EXPECTED_SUBSET = {
    "Ballard Cup Series I",
    "Ballard Cup Series II",
    "Ballard Cup Series III",
    "Blakely Rock Benefit Race",
    "Boxing Day Race",
    "Down the Sound",
    "Fall Regatta",
    "Fresh Water Race",
    "J & J Race",
    "Race to the Straits",
    "Single-Handed Race",
    "Snowbird Series",
    "Three Buoy Fiasco",
    "Women at the Helm 1",
    "Women at the Helm 2",
}


def test_discover_yields_known_events():
    body = FIXTURE.read_text(encoding="utf-8")
    pages = list(discover_from_cgi(body))
    assert pages, "expected at least one page"
    found_events = {p.event for p in pages}
    missing = EXPECTED_SUBSET - found_events
    assert not missing, f"missing events: {missing}"


def test_discover_strips_t_param():
    body = FIXTURE.read_text(encoding="utf-8")
    pages = list(discover_from_cgi(body))
    for p in pages:
        assert "?t=" not in p.url, f"unstripped t param in {p.url}"


def test_discover_includes_blakely_rock_2023():
    body = FIXTURE.read_text(encoding="utf-8")
    pages = list(discover_from_cgi(body))
    blakely_2023 = [
        p for p in pages
        if p.event == "Blakely Rock Benefit Race" and p.year == 2023
    ]
    assert blakely_2023, "Blakely Rock Benefit Race 2023 missing"
    assert blakely_2023[0].url.startswith("https://race.styc.org/race_info/")
    assert "2023" in blakely_2023[0].url


def test_discover_years_are_ints():
    body = FIXTURE.read_text(encoding="utf-8")
    pages = list(discover_from_cgi(body))
    for p in pages:
        assert isinstance(p.year, int)
        assert 1990 <= p.year <= 2100


def test_discover_yields_many_pages():
    """Sanity: hundreds of (event, year) pairs across the full fixture."""
    body = FIXTURE.read_text(encoding="utf-8")
    pages = list(discover_from_cgi(body))
    assert len(pages) >= 200, len(pages)
