"""CYC archive discovery — fixture-driven, no network."""

from pathlib import Path

from Results.sources.cyc_archive import (
    _event_links_from_year_landing,
    _event_name_from_html,
    _year_landing_urls_from_root,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cyc_archive"


def _read(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("utf-8", errors="replace")


def test_root_yields_27_years_1997_to_2023():
    years = _year_landing_urls_from_root(_read("index.html"))
    assert len(years) == 27, len(years)
    assert years[0] == (1997, "https://cyc-racingarchive.netlify.app/results97")
    assert years[-1] == (
        2023,
        "https://cyc-racingarchive.netlify.app/results/2023_specific/results",
    )


def test_root_split_at_2012_url_pattern():
    years = dict(_year_landing_urls_from_root(_read("index.html")))
    # pre-2012 use /resultsYY
    assert years[2011].endswith("/results11")
    # 2012+ use /results/YEAR_specific/results
    assert years[2012].endswith("/results/2012_specific/results")


def test_2017_landing_finds_at_least_25_events():
    links = list(_event_links_from_year_landing(
        _read("year_2017.html"),
        "https://cyc-racingarchive.netlify.app/results/2017_specific/results",
        2017,
    ))
    assert len(links) >= 25, len(links)
    urls = {u for _, u in links}
    # Spot checks for known event slugs
    assert "https://cyc-racingarchive.netlify.app/results/2017_specific/turkey" in urls
    assert "https://cyc-racingarchive.netlify.app/results/2017_specific/spwed" in urls
    assert "https://cyc-racingarchive.netlify.app/results/2017_specific/comgib" in urls


def test_2023_landing_includes_center_sound_links():
    links = list(_event_links_from_year_landing(
        _read("year_2023.html"),
        "https://cyc-racingarchive.netlify.app/results/2023_specific/results",
        2023,
    ))
    urls = {u for _, u in links}
    # Both the by-class and the overall pages, both deduped past their #anchor
    assert "https://cyc-racingarchive.netlify.app/results/2023_specific/center" in urls
    assert "https://cyc-racingarchive.netlify.app/results/2023_specific/centeroa" in urls


def test_year_landing_drops_offsite_and_self_link():
    links = list(_event_links_from_year_landing(
        _read("year_2023.html"),
        "https://cyc-racingarchive.netlify.app/results/2023_specific/results",
        2023,
    ))
    for _, u in links:
        assert "cyc-racingarchive.netlify.app" in u
        assert not u.endswith("_specific/results")
        assert not u.lower().endswith(".pdf")
        assert "#" not in u


def test_year_landing_anchors_dedup_to_same_url():
    """centeroa#r1, centeroa#r2, centeroa#r3 collapse to one URL."""
    links = list(_event_links_from_year_landing(
        _read("year_2023.html"),
        "https://cyc-racingarchive.netlify.app/results/2023_specific/results",
        2023,
    ))
    urls = [u for _, u in links]
    assert urls.count(
        "https://cyc-racingarchive.netlify.app/results/2023_specific/centeroa"
    ) == 1


def test_event_name_from_h1_strips_trailing_year():
    assert _event_name_from_html(_read("turkey_2017.html"),
                                 "fallback") == "Turkey Bowl"
    assert _event_name_from_html(_read("centeroa_2023.html"),
                                 "fallback") == "Center Sound Series"
    assert _event_name_from_html(
        _read("lfatue_2023.html"), "fallback"
    ) == "Lake Tuesday Evening Fall Series"


def test_event_name_falls_back_for_legacy_pages():
    """comgib_2017 is hand-edited HTML with <h2> instead of <h1> Sailwave."""
    name = _event_name_from_html(_read("comgib_2017.html"), "fallback")
    assert "Commodore Gibson" in name, name
