"""ClubSpot v4 JSON parser — fixture-driven, no network."""

from dataclasses import dataclass
from pathlib import Path

from Results.parsers.clubspot import parse_clubspot_v4
from Results.sources.base import ResultPage

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cyc_main"


def _read(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _page(event="Frigid Digit Regatta", year=2024, label="primary",
          url="https://results.theclubspot.com/clubspot-results-v4/u4kaCviJKt?boatClassIDs=gENAC526Hw"):
    return ResultPage(source="cyc-main", event=event, year=year,
                      label=label, url=url)


def test_frigid_ilca7_yields_three_rows():
    rows = list(parse_clubspot_v4(_read("results_frigid_ilca7.json"), _page()))
    assert len(rows) == 3, len(rows)
    # All rows share the same class
    assert all(r.class_name == "ILCA 7 (std)" for r in rows)
    # Sorted by net asc with stable rank
    nets = [int(r.score) for r in rows]
    assert nets == sorted(nets), nets
    assert rows[0].place == "1"
    assert rows[0].place_int == 1
    assert rows[0].boat_name == "ZZZZAP"
    assert rows[0].sail_number == "175948"


def test_frigid_ilca7_skipper_from_participant_names():
    rows = list(parse_clubspot_v4(_read("results_frigid_ilca7.json"), _page()))
    by_name = {r.boat_name: r for r in rows}
    assert by_name["ZZZZAP"].skipper == "Alex Zaputil"
    assert by_name["ZZZZAP"].yacht_club == "RVYC"


def test_wns_phrfa_extracts_phrf_rating_and_boat_type():
    page = _page(event="Wednesday Night Series",
                 url="https://results.theclubspot.com/clubspot-results-v4/nSJ96Heb5Y?boatClassIDs=qgAoHrrQ67")
    rows = list(parse_clubspot_v4(_read("results_wns_phrfa.json"), page))
    assert rows, "expected non-empty"
    # PHRF A class
    assert all(r.class_name == "PHRF A (0-100)" for r in rows)
    # PHRF rating populated for at least the leader
    by_name = {r.boat_name: r for r in rows}
    # Spot check known boats
    assert "Shada" in by_name
    assert by_name["Shada"].rating  # non-empty PHRF
    # Place is computed
    assert by_name["Shada"].place_int == 1
    assert by_name["Shada"].place == "1"
    # Boat type populated for at least one entry (some boats omit it)
    assert any(r.boat_type for r in rows), "expected at least one boat_type"


def test_empty_or_error_json_yields_no_rows():
    page = _page()
    assert list(parse_clubspot_v4(b"", page)) == []
    assert list(parse_clubspot_v4(b"Unable to fetch results.", page)) == []
    assert list(parse_clubspot_v4(b'{"scoresByRegistration": []}', page)) == []


def test_tied_nets_share_place():
    """Synthetic: build a 3-boat payload with two boats tied on net=5."""
    import json as _json
    payload = {
        "scoresByRegistration": [
            {"registrationObject": {"boatName": "A", "boatClassObject": {"name": "PHRF A"}},
             "total": 5, "net": 5},
            {"registrationObject": {"boatName": "B", "boatClassObject": {"name": "PHRF A"}},
             "total": 5, "net": 5},
            {"registrationObject": {"boatName": "C", "boatClassObject": {"name": "PHRF A"}},
             "total": 8, "net": 8},
        ]
    }
    rows = list(parse_clubspot_v4(_json.dumps(payload), _page()))
    by_name = {r.boat_name: r for r in rows}
    assert by_name["A"].place_int == 1
    assert by_name["B"].place_int == 1   # tie shares rank
    assert by_name["C"].place_int == 3   # next rank skips


def test_unscored_entry_has_empty_place_and_score():
    import json as _json
    payload = {
        "scoresByRegistration": [
            {"registrationObject": {"boatName": "Alpha", "boatClassObject": {"name": "PHRF A"}},
             "total": None, "net": None},
        ]
    }
    rows = list(parse_clubspot_v4(_json.dumps(payload), _page()))
    assert len(rows) == 1
    assert rows[0].place == ""
    assert rows[0].place_int is None
    assert rows[0].score is None
