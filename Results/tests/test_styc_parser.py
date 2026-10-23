"""Sailwave parser exercised against the saved Blakely Rock 2023 fixture."""

from pathlib import Path

from Results.sources.base import ResultPage
from Results.sources.styc import STYCSource

FIXTURE = (Path(__file__).resolve().parent
           / "fixtures" / "styc" / "blakely_2023_race1.html")


def _parse_fixture():
    page = ResultPage(
        source="styc",
        event="Blakely Rock Benefit Race",
        year=2023,
        label="primary",
        url="https://race.styc.org/race_info/Blakely%20Rock%20Benefit/2023/race1.htm",
    )
    body = FIXTURE.read_bytes()
    src = STYCSource()
    return list(src.parse(body, page))


def test_parser_returns_rows():
    rows = _parse_fixture()
    assert len(rows) > 20, f"expected many rows, got {len(rows)}"


def test_parser_finds_peer_gynt():
    rows = _parse_fixture()
    pg = [r for r in rows if r.boat_name.lower() == "peer gynt"]
    assert pg, "Peer Gynt not found in Blakely 2023 race1.htm"
    r = pg[0]
    assert r.sail_number == "235", r.sail_number
    assert r.place_int == 1, (r.place, r.place_int)
    assert r.boat_type and "J/105" in r.boat_type, r.boat_type


def test_parser_extracts_class_name():
    rows = _parse_fixture()
    classes = {r.class_name for r in rows if r.class_name}
    # The race1.htm uses numbered classes; expect Slooper plus Flying Sails.
    assert any("Slooper" in c for c in classes), classes
    assert any("Flying Sails" in c for c in classes), classes
    # Class names must have the boilerplate trimmed
    for c in classes:
        assert "Start:" not in c, c
        assert "Course:" not in c, c
        assert "Distance:" not in c, c


def test_parser_handles_dnc_rows():
    """DNC entries appear in the Finish column; parser still emits the row."""
    rows = _parse_fixture()
    blanks = [r for r in rows if not r.corrected and not r.elapsed]
    assert blanks, "expected at least one row with no corrected/elapsed"


def test_parser_class_for_peer_gynt_is_class7_flying_sails():
    rows = _parse_fixture()
    pg = [r for r in rows if r.boat_name == "Peer Gynt"][0]
    assert pg.class_name and "Flying Sails" in pg.class_name, pg.class_name
