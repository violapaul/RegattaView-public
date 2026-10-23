"""CYC archive Sailwave parsing — fixture-driven, no network."""

from pathlib import Path

from Results.sources.base import ResultPage
from Results.sources.cyc_archive import CYCArchiveSource

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cyc_archive"


def _parse(name: str, event_hint: str, year: int):
    body = (FIXTURES / name).read_bytes()
    page = ResultPage(
        source="cyc-archive", event=event_hint, year=year,
        label="primary",
        url=f"https://cyc-racingarchive.netlify.app/results/{year}_specific/{name.split('_')[0]}",
    )
    rows = list(CYCArchiveSource().parse(body, page))
    return page, rows


def test_lfatue_2023_summary_table():
    page, rows = _parse("lfatue_2023.html", "Fall", 2023)
    # parse() should rewrite page.event from <h1>
    assert page.event == "Lake Tuesday Evening Fall Series", page.event
    assert len(rows) == 21
    # Exactly one summary table -> one class
    assert {r.class_name for r in rows} == {"Overall"}
    # Skipper Name header now mapped
    assert all(r.skipper for r in rows), [r for r in rows if not r.skipper]
    # Sail # / Boat Type populated for J/24 fleet
    assert all(r.boat_type == "J/24" for r in rows), [r.boat_type for r in rows]


def test_turkey_2017_per_class_summary_rows():
    page, rows = _parse("turkey_2017.html", "Turkey Bowl", 2017)
    assert page.event == "Turkey Bowl"
    # Multiple classes show up as distinct class_name banners
    classes = {r.class_name for r in rows}
    assert len(classes) >= 3, classes
    # 505 fleet must be present
    assert any("505" in (c or "") for c in classes), classes
    # Place strings include "1st", "2nd", "3rd" — parser should still set place_int
    firsts = [r for r in rows if r.place == "1st"]
    assert firsts, "no 1st-place rows"
    assert all(r.place_int == 1 for r in firsts), [r.place_int for r in firsts]


def test_spwed_2017_emits_summary_and_race_rows():
    page, rows = _parse("spwed_2017.html", "Spring Wed", 2017)
    assert page.event == "Sound Wednesday Evening Spring Series"
    # The class_name extracted for racetable rows includes the date suffix,
    # so they get distinct race_ids in parse.py.
    class_names = {r.class_name for r in rows}
    summary_classes = {c for c in class_names if c and c.endswith("Class")}
    race_classes = {c for c in class_names
                    if c and c.startswith("R") and "Class" in c}
    assert summary_classes, class_names
    assert race_classes, class_names


def test_centeroa_2023_includes_peer_gynt_with_sail_235():
    page, rows = _parse("centeroa_2023.html", "Overall", 2023)
    assert page.event == "Center Sound Series"
    pg = [r for r in rows if r.boat_name.lower() == "peer gynt"]
    assert pg, "Peer Gynt missing from CSS 2023 race 1"
    r = pg[0]
    assert r.sail_number == "235", r.sail_number
    # corrected time present (Sailwave racetable)
    assert r.corrected, r


def test_comgib_2017_legacy_html_table():
    """Hand-edited HTML, not Sailwave — generic <table>+<th> heuristic."""
    page, rows = _parse("comgib_2017.html", "Commodore Gibson", 2017)
    # event derived from <h2> fallback
    assert "Commodore" in page.event, page.event
    # Saturday + Sunday tables -> two class_names from <h2> walk-back
    assert {"Saturday", "Sunday"}.issubset({r.class_name for r in rows})
    # Boat type lifted from "Type" column
    types = {r.boat_type for r in rows if r.boat_type}
    assert types, "no boat_type captured"
