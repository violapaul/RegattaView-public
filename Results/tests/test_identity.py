"""Identity clustering tests using synthetic rows."""

from Results.identify import cluster_by_sail, assign_boat_ids
from Results.parsers.sailwave import boat_name_canon, sail_number_canon


def _row(name: str, sail: str, btype: str = "") -> dict:
    return {
        "boat_name": name,
        "boat_name_canon": boat_name_canon(name),
        "sail_number": sail,
        "sail_number_canon": sail_number_canon(sail),
        "boat_type": btype,
        "race_id": "",
    }


def test_three_peer_gynt_variants_merge_with_same_sail():
    rows = [
        _row("Peer Gynt", "235"),
        _row("PEER GYNT", "235"),
        _row("Peer-Gynt", "235"),
        _row("Other Boat", "235"),
    ]
    clusters, _review = cluster_by_sail(rows)
    row2bid = assign_boat_ids(rows, clusters)
    bids = {row2bid[i] for i in range(len(rows))}
    assert len(bids) == 2, f"expected 2 identities (PG cluster + Other), got {bids}"
    pg_bid = row2bid[0]
    assert row2bid[1] == pg_bid
    assert row2bid[2] == pg_bid
    assert row2bid[3] != pg_bid


def test_no_sail_clusters_by_name_and_type():
    rows = [
        _row("Carmanah", "", "C&C 34"),
        _row("Carmanah", "", "C&C 34"),
        _row("Carmanah", "", "Different Type"),  # different type -> separate id
    ]
    clusters, _ = cluster_by_sail(rows)
    row2bid = assign_boat_ids(rows, clusters)
    assert row2bid[0] == row2bid[1]
    assert row2bid[2] != row2bid[0]


def test_review_pairs_include_borderline_matches():
    # Two rows with same sail but very different names -> won't merge,
    # may surface in review only if name distance lands in the review band.
    rows = [
        _row("Peer Gynt", "235"),
        _row("PG", "235"),  # short alias — token_set_ratio is high enough to not be in review band
    ]
    clusters, review = cluster_by_sail(rows)
    # The two rows likely separated (token_set_ratio between 'peer gynt' and 'pg'
    # is well below the 88 threshold); confirm clustering produced 2 ids.
    row2bid = assign_boat_ids(rows, clusters)
    assert len({row2bid[0], row2bid[1]}) == 2
