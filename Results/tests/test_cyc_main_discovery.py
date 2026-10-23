"""CYC main discovery — fixture-driven, no network."""

from pathlib import Path

from Results.sources.cyc_main import (
    boat_classes_from_parse_json,
    regatta_links_from_year_html,
    results_v4_url,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "cyc_main"


def _read(name: str) -> str:
    return (FIXTURES / name).read_bytes().decode("utf-8", errors="replace")


def test_year_2024_yields_at_least_13_cyc_regattas():
    links = list(regatta_links_from_year_html(_read("year_2024.html")))
    # 14 CYC-hosted regattas observed on the live page; allow for one drop
    # if the site rearranges
    assert len(links) >= 13, len(links)
    ids = {rid for _, rid in links}
    # Spot-check known ones from 2024
    assert "u4kaCviJKt" in ids  # Frigid Digit
    assert "nSJ96Heb5Y" in ids  # Wednesday Night Series
    assert "wQru5t83wB" in ids  # PSSC Big Boat
    assert "W8fg8uHDiM" in ids  # Turkey Bowl
    # Off-host J/24 Worlds (theclubspot.com host) must NOT appear here
    assert "IMIGSj06De" not in ids


def test_year_2024_event_names_are_human_readable():
    links = dict(regatta_links_from_year_html(_read("year_2024.html")))
    # Event names come from the anchor text
    name_by_id = {rid: name for name, rid in
                  ((n, r) for n, r in regatta_links_from_year_html(_read("year_2024.html")))}
    assert "Frigid Digit" in name_by_id["u4kaCviJKt"]
    assert "Wednesday Night Series" in name_by_id["nSJ96Heb5Y"]
    assert "PSSC Big Boat" in name_by_id["wQru5t83wB"]


def test_year_2024_dedupes_repeated_anchors():
    links = list(regatta_links_from_year_html(_read("year_2024.html")))
    ids = [rid for _, rid in links]
    assert len(ids) == len(set(ids)), "regatta IDs should be unique"


def test_boat_classes_from_parse_json_filters_not_racing():
    body = _read("classes_frigid.json")
    classes = boat_classes_from_parse_json(body)
    assert len(classes) == 2, classes
    names = {n for _, n in classes}
    assert "ILCA 7 (std)" in names
    assert "RS Aero" in names


def test_boat_classes_handles_empty_body():
    assert boat_classes_from_parse_json("") == []
    assert boat_classes_from_parse_json("{}") == []
    assert boat_classes_from_parse_json('{"results":[]}') == []


def test_results_v4_url_shape():
    u = results_v4_url("u4kaCviJKt", "gENAC526Hw")
    assert u == (
        "https://results.theclubspot.com/clubspot-results-v4/"
        "u4kaCviJKt?boatClassIDs=gENAC526Hw"
    )



def test_year_2025_resolves_collisions_to_longest_label():
    """Turkey Bowl 2025 anchor list is [empty icon link, empty icon link,
    'Turkey Bowl Regat', ...]. The longest-non-empty rule beats the empty
    icon links.
    """
    # If a 2025 fixture is added later this can be tightened; for now the
    # selection logic is exercised by the synthetic test below.
    pass


def test_longest_non_empty_label_wins_for_repeat_regatta_links():
    """Synthetic year-page fragment: same regatta_id under three anchors,
    one empty (icon link), one short, one long. Longest wins.
    """
    import json as _json
    import urllib.parse as _up
    from Results.sources.cyc_main import regatta_links_from_year_html
    sections = [
        {"type": "h3-center",
         "content": (
             '<a href=\"https://cycseattle.org/regatta/ABCD/results\"></a>'
             '<a href=\"https://cycseattle.org/regatta/ABCD/results\">Short</a>'
             '<a href=\"https://cycseattle.org/regatta/ABCD/results\">A Much Longer Title</a>'
         )},
    ]
    payload = _up.quote(_json.dumps(sections))
    fake_html = f":column_prop=\"'{payload}'\""
    pairs = list(regatta_links_from_year_html(fake_html))
    assert pairs == [("A Much Longer Title", "ABCD")], pairs


def test_empty_inline_tags_inside_anchor_text_do_not_split_words():
    import json as _json
    import urllib.parse as _up
    from Results.sources.cyc_main import regatta_links_from_year_html
    sections = [
        {"type": "h3-center",
         "content": (
             '<a href=\"https://cycseattle.org/regatta/EFGH/results\">'
             'Lake Spring Reg<span class=\"x\"></span>atta</a>'
         )},
    ]
    payload = _up.quote(_json.dumps(sections))
    fake_html = f":column_prop=\"'{payload}'\""
    pairs = list(regatta_links_from_year_html(fake_html))
    assert pairs == [("Lake Spring Regatta", "EFGH")], pairs
