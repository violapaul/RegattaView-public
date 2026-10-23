"""SYC parser dispatch — Sailwave HTML and Yacht Scoring JSON."""

from pathlib import Path

from Results.sources.base import ResultPage
from Results.sources.syc import SYCSource

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "syc"


def _parse(path: str, page: ResultPage):
    body = (FIXTURES / path).read_bytes()
    src = SYCSource()
    return list(src.parse(body, page))


def test_yacht_scoring_jfest_2024_yields_all_classes():
    page = ResultPage(
        source="syc",
        event="J FEST Seattle",
        year=2024,
        label="primary",
        url="https://api.yachtscoring.com/v1/public/event/16398/cumulative-result",
    )
    rows = _parse("ys_jfest_2024_cumulative.json", page)
    # 4 classes (J/Other, J105, J24, J70). 2+8+3+10 = 23 boats.
    assert len(rows) == 23, len(rows)
    classes = {r.class_name for r in rows}
    assert classes == {
        "J Fest > PHRF > J (Other)",
        "J Fest > One Design > J105",
        "J Fest > One Design > J24",
        "J Fest > One Design > J70",
    }, classes


def test_yacht_scoring_winner_per_class_is_place_one():
    page = ResultPage(
        source="syc", event="J FEST Seattle", year=2024, label="primary",
        url="https://api.yachtscoring.com/v1/public/event/16398/cumulative-result",
    )
    rows = _parse("ys_jfest_2024_cumulative.json", page)
    winners = {r.class_name: r for r in rows if r.place_int == 1}
    assert winners["J Fest > PHRF > J (Other)"].boat_name == "Jeopardy"
    assert winners["J Fest > One Design > J105"].boat_name == "Moose Unknown"
    assert winners["J Fest > One Design > J24"].boat_name == "Atom Ant"


def test_yacht_scoring_owner_lastname_in_skipper_field():
    page = ResultPage(
        source="syc", event="J FEST Seattle", year=2024, label="primary",
        url="https://api.yachtscoring.com/v1/public/event/16398/cumulative-result",
    )
    rows = _parse("ys_jfest_2024_cumulative.json", page)
    jeopardy = next(r for r in rows if r.boat_name == "Jeopardy")
    assert jeopardy.skipper == "Ed Pinkham"
    assert jeopardy.boat_type == "J/109"
    assert jeopardy.sail_number == "USA 161"
    assert jeopardy.score == "4"


def test_sailwave_2018_grand_prix_phrf_includes_peer_gynt():
    page = ResultPage(
        source="syc",
        event="42nd Annual Grand Prix Regatta",  # discover-time fallback
        year=2018,
        label="primary",
        url=("https://seattleyachtclubsailing.s3-us-west-2.amazonaws.com/"
             "Final+2018+Grand+Prix+Regatta+PHRF.html"),
    )
    rows = _parse("2018_gp_phrf.html", page)
    pg = [r for r in rows if r.boat_name.lower() == "peer gynt"]
    assert pg, "Peer Gynt missing from 2018 SYC GP PHRF"
    # Multiple per-race tables — Peer Gynt should appear in each as well as
    # the cumulative summary.
    assert len(pg) >= 2, len(pg)
    rep = pg[0]
    assert rep.sail_number == "235"
    assert rep.boat_type and "J-105" in rep.boat_type, rep.boat_type


def test_sailwave_2018_grand_prix_event_name_replaced_with_h1_canonical():
    """The H1 in the page reads `2018 Grand Prix Regatta`. After parse,
    page.event should be cleaned to drop the leading year — match the
    cyc_archive convention so identity downstream sees the same string."""
    page = ResultPage(
        source="syc",
        event="42nd Annual Grand Prix Regatta",
        year=2018,
        label="primary",
        url=("https://seattleyachtclubsailing.s3-us-west-2.amazonaws.com/"
             "Final+2018+Grand+Prix+Regatta+PHRF.html"),
    )
    _parse("2018_gp_phrf.html", page)
    assert "Grand Prix" in page.event, page.event
    assert "2018" not in page.event, page.event


def test_sailwave_2022_tri_island_includes_peer_gynt():
    page = ResultPage(
        source="syc",
        event="Tri-Island",
        year=2022,
        label="primary",
        url=("https://seattleyachtclubsailing.s3-us-west-2.amazonaws.com/"
             "2022+Tri-Island+by+Class.html"),
    )
    rows = _parse("2022_tri_island_by_class.html", page)
    pg = [r for r in rows if r.boat_name.lower() == "peer gynt"]
    assert pg, "Peer Gynt missing from 2022 SYC Tri-Island by Class"
    assert any(r.sail_number == "235" for r in pg)


def test_sailwave_ceclients_2016_mark_mayer_parses_as_sailwave():
    """CEClients-hosted Sailwave HTML must parse via the same pipeline."""
    page = ResultPage(
        source="syc", event="Mark Mayer Opening Day Race", year=2016,
        label="primary",
        url=("https://www.ceclients.com/sites/SeattleYachtClub/files/"
             "Sailwave%20results%20for%20Mark%20Mayer%20Opening%20Day"
             "%20Race%20at%20Seattle%20Yacht%20Club%202016.html"),
    )
    rows = _parse("2016_mark_mayer_ceclients.html", page)
    assert len(rows) > 10, len(rows)
    # Page <h1> exists and gets cleaned of trailing year
    assert "Mark Mayer" in page.event, page.event
    assert "2016" not in page.event, page.event
