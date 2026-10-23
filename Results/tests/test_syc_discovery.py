"""SYC past-race-results page discovery — fixture-driven, no network."""

from pathlib import Path

from Results.sources.syc import (
    PAST_RACE_RESULTS_LIVE,
    _clean_event_header,
    _is_syc_yacht_scoring_event,
    _is_target_link,
    _normalise_host,
    _strip_event_year_suffix,
    discover_from_past_race_results,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "syc"


def _read_fixture(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("utf-8", errors="replace")


def test_discovery_yields_many_links_across_recent_years():
    items = list(discover_from_past_race_results(
        _read_fixture("past_race_results_2024.html")))
    # Snapshot covers ~2007-2022; we expect dozens of result links.
    assert len(items) > 80, len(items)
    years = {y for y, _, _ in items}
    # Must include at least the post-2017 corpus
    for y in (2017, 2018, 2021, 2022):
        assert y in years, f"year {y} missing from {sorted(years)}"


def test_discovery_2022_includes_known_events():
    items = list(discover_from_past_race_results(
        _read_fixture("past_race_results_2024.html")))
    urls_2022 = [u for y, _, u in items if y == 2022]
    # Spot checks against the live snapshot
    assert any("2022+Tri-Island+by+Class.html" in u for u in urls_2022), urls_2022
    assert any("2022+Mark+Mayer+by+Start.html" in u for u in urls_2022), urls_2022
    assert any("2022+Grand+Prix+Results.html" in u for u in urls_2022), urls_2022


def test_discovery_2018_includes_grand_prix_phrf_on_path_style_s3():
    """The 2018 GP PHRF link uses the older path-style S3 alias and we
    canonicalise it to the virtual-host form."""
    items = list(discover_from_past_race_results(
        _read_fixture("past_race_results_2024.html")))
    urls_2018 = [u for y, _, u in items if y == 2018]
    assert any(
        u == "https://seattleyachtclubsailing.s3-us-west-2.amazonaws.com"
             "/Final+2018+Grand+Prix+Regatta+PHRF.html"
        for u in urls_2018
    ), urls_2018


def test_discovery_uses_https_canonical_host():
    """All Sailwave URLs must be https://, regardless of the page's link form."""
    items = list(discover_from_past_race_results(
        _read_fixture("past_race_results_2024.html")))
    for _, _, url in items:
        assert url.startswith("https://"), url


def test_discovery_drops_pdf_and_registration_links():
    items = list(discover_from_past_race_results(
        _read_fixture("past_race_results_2024.html")))
    for _, _, url in items:
        assert not url.lower().endswith(".pdf"), url
        assert "regattanetwork.com" not in url, url


def test_discovery_event_names_are_date_free():
    """No `(May 8|August 28-29|October 21-23)` tail noise leaks into the event name."""
    items = list(discover_from_past_race_results(
        _read_fixture("past_race_results_2024.html")))
    months = (
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    )
    for _, ev, _ in items:
        for m in months:
            assert f" {m} " not in ev and not ev.endswith(m), ev


def test_discovery_dedupes_repeated_link_targets():
    items = list(discover_from_past_race_results(
        _read_fixture("past_race_results_2024.html")))
    urls = [u for _, _, u in items]
    assert len(urls) == len(set(urls)), "duplicate URLs leaked through"


def test_clean_event_header_strips_dates_and_nbsp():
    assert (_clean_event_header(
        "Smith Island Race: Tri-Island #1\xa0\xa0\xa0\xa0 April 30-May 1")
            == "Smith Island Race: Tri-Island #1")
    assert (_clean_event_header(
        "Mark Mayer Opening Day Race and Party\xa0 \xa0\xa0 May 8")
            == "Mark Mayer Opening Day Race and Party")
    assert (_clean_event_header(
        "The Seattle Yacht\xa0Club Championships\xa0 \xa0 \xa0October 29")
            == "The Seattle Yacht Club Championships")


def test_strip_event_year_suffix_handles_both_ends():
    assert _strip_event_year_suffix("2018 Grand Prix") == "Grand Prix"
    assert _strip_event_year_suffix("Grand Prix 2018") == "Grand Prix"
    assert _strip_event_year_suffix("Grand Prix") == "Grand Prix"


def test_normalise_host_collapses_path_style_s3_to_vhost():
    assert (_normalise_host(
        "http://s3-us-west-2.amazonaws.com/seattleyachtclubsailing/Foo.html")
            == "https://seattleyachtclubsailing.s3-us-west-2.amazonaws.com/Foo.html")


def test_normalise_host_upgrades_http_to_https():
    assert (_normalise_host(
        "http://seattleyachtclubsailing.s3-us-west-2.amazonaws.com/X.html")
            .startswith("https://"))
    assert (_normalise_host(
        "http://www.ceclients.com/sites/SeattleYachtClub/files/X.html")
            .startswith("https://"))


def test_is_target_link_rejects_pdfs_and_unrelated_hosts():
    assert _is_target_link(
        "https://seattleyachtclubsailing.s3-us-west-2.amazonaws.com/X.html")
    assert _is_target_link(
        "https://www.ceclients.com/sites/SeattleYachtClub/files/X.htm")
    # ceclients root not under /SeattleYachtClub/ -> off-tree, ignore
    assert not _is_target_link(
        "https://www.ceclients.com/sites/OtherClub/files/X.htm")
    # Plain Yacht Scoring without an eID -> useless
    assert not _is_target_link(
        "https://www.yachtscoring.com/event_results_cumulative.cfm")
    # PDF -> ignore
    assert not _is_target_link(
        "https://seattleyachtclubsailing.s3-us-west-2.amazonaws.com/X.pdf")
    # Regatta Network registration -> not results
    assert not _is_target_link(
        "https://www.regattanetwork.com/clubmgmt/applet_club_events.php?CLUB_ID=444")


def test_is_target_link_accepts_yacht_scoring_with_eid():
    assert _is_target_link("https://yachtscoring.com/emenu/16398")
    assert _is_target_link(
        "https://www.yachtscoring.com/event_results_cumulative.cfm?eID=16398")


def test_is_syc_yacht_scoring_event_filters_unrelated_hits():
    # SYC event surfaces by hostClub
    assert _is_syc_yacht_scoring_event(
        {"name": "2014 NOOD", "hostClub": {"name": "Seattle Yacht Club"}})
    # J FEST Seattle has no hostClub but matches the keyword in name
    assert _is_syc_yacht_scoring_event(
        {"name": "J FEST Seattle 2024", "hostClub": None})
    # Unrelated event with "Seattle" in some other field (e.g. NOOD-style PR) — skip
    assert not _is_syc_yacht_scoring_event(
        {"name": "Cape Dory Typhoon Nationals",
         "hostClub": {"name": "Rappahannock River Yacht Club"}})
    # Event mentioning Seattle in the name but not Seattle-based — kept (false-positive
    # accepted; better to over-include and let the user filter).
    assert _is_syc_yacht_scoring_event(
        {"name": "Sperry Top-Sider Seattle NOOD", "hostClub": None})


def test_past_race_results_live_url_constant_is_seattleyachtclub_org():
    # Tripwire: if the official URL ever moves we want to notice in CI.
    assert PAST_RACE_RESULTS_LIVE == (
        "https://www.seattleyachtclub.org/past-race-results"
    )
