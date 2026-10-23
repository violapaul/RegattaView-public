"""Cluster results.csv rows into boat identities.

Algorithm (per the plan):
  1. Group rows by sail_number_canon (non-empty, not in {0,00}).
     Within each group, sub-cluster by boat_name_canon using
     rapidfuzz token_set_ratio >= 88.
  2. Rows with no sail_number_canon: cluster by exact
     (boat_name_canon, boat_type) — refuse to merge across types.

Then apply identity_overrides.yaml (merges + splits) on top.

Outputs:
  - storage/data/boats.csv
  - storage/data/boat_aliases.csv
  - storage/data/results.csv (rewritten with boat_id populated)
  - exports/boats_review.csv (suspicious clusters for human eyes)
"""

from __future__ import annotations

import csv
import hashlib
import sys
from collections import Counter, defaultdict
from pathlib import Path

_THIS = Path(__file__).resolve().parent
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

import yaml
from rapidfuzz import fuzz

DATA_DIR = _THIS / "storage" / "data"
EXPORTS = _THIS / "exports"
EXPORTS.mkdir(parents=True, exist_ok=True)
RESULTS_CSV = DATA_DIR / "results.csv"
RACES_CSV = DATA_DIR / "races.csv"
BOATS_CSV = DATA_DIR / "boats.csv"
ALIASES_CSV = DATA_DIR / "boat_aliases.csv"
OVERRIDES_YAML = _THIS / "storage" / "identity_overrides.yaml"
REVIEW_CSV = EXPORTS / "boats_review.csv"

NAME_MERGE_THRESHOLD = 88   # >= -> merge inside a sail-# group
NAME_REVIEW_LO = 60         # 60–87 -> flag for review
SAIL_SENTINELS = {"", "0", "00", "000", "0000"}


def _boat_id_from(canonical_name: str, sail: str, boat_type: str) -> str:
    key = f"{canonical_name}|{sail}|{boat_type}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def load_results() -> list[dict]:
    if not RESULTS_CSV.exists():
        raise SystemExit(f"missing {RESULTS_CSV} — run parse.py first")
    with RESULTS_CSV.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_races_year() -> dict[str, int]:
    """race_id -> year."""
    if not RACES_CSV.exists():
        return {}
    out: dict[str, int] = {}
    with RACES_CSV.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            try:
                out[r["race_id"]] = int(r["year"])
            except (KeyError, ValueError):
                pass
    return out


def cluster_by_sail(rows: list[dict]) -> tuple[
    dict[int, list[int]], list[tuple[str, str, str, int]]
]:
    """Returns (cluster_id -> [row_indices], review_pairs).

    review_pairs are (sail#, name_a, name_b, score) where score is in
    [NAME_REVIEW_LO, NAME_MERGE_THRESHOLD).
    """
    clusters: dict[int, list[int]] = {}
    review: list[tuple[str, str, str, int]] = []
    next_id = 0

    by_sail: dict[str, list[int]] = defaultdict(list)
    no_sail: list[int] = []
    for i, r in enumerate(rows):
        sail = r["sail_number_canon"].strip()
        if sail in SAIL_SENTINELS:
            no_sail.append(i)
        else:
            by_sail[sail].append(i)

    # Sail-# pass: greedy single-link sub-cluster on token_set_ratio
    for sail, indices in by_sail.items():
        sub_clusters: list[list[int]] = []
        sub_names: list[list[str]] = []  # representative names per sub-cluster
        for idx in indices:
            name = rows[idx]["boat_name_canon"]
            placed = False
            for ci, names in enumerate(sub_names):
                # compare against the most-common name in this sub-cluster
                rep = names[0]
                score = fuzz.token_set_ratio(name, rep) if name and rep else 0
                if score >= NAME_MERGE_THRESHOLD:
                    sub_clusters[ci].append(idx)
                    names.append(name)
                    placed = True
                    break
                elif NAME_REVIEW_LO <= score < NAME_MERGE_THRESHOLD:
                    review.append((sail, rep, name, int(score)))
            if not placed:
                sub_clusters.append([idx])
                sub_names.append([name])
        for sc in sub_clusters:
            clusters[next_id] = sc
            next_id += 1

    # Name-only pass for rows with no sail #: exact (name, type) cluster.
    name_type_clusters: dict[tuple[str, str], list[int]] = defaultdict(list)
    for idx in no_sail:
        key = (rows[idx]["boat_name_canon"],
               rows[idx]["boat_type"].strip().lower())
        name_type_clusters[key].append(idx)
    for key, idxs in name_type_clusters.items():
        if not key[0]:
            # Empty name — already filtered upstream, but be safe
            continue
        clusters[next_id] = idxs
        next_id += 1

    return clusters, review


def assign_boat_ids(rows: list[dict],
                    clusters: dict[int, list[int]]) -> dict[int, str]:
    """Pick canonical (name, sail, type) for each cluster and produce a stable
    boat_id. Returns row_index -> boat_id."""
    row2bid: dict[int, str] = {}
    for cid, idxs in clusters.items():
        names = Counter(rows[i]["boat_name_canon"] for i in idxs if rows[i]["boat_name_canon"])
        sails = Counter(rows[i]["sail_number_canon"] for i in idxs
                        if rows[i]["sail_number_canon"] not in SAIL_SENTINELS)
        types = Counter(rows[i]["boat_type"].strip().lower() for i in idxs
                        if rows[i]["boat_type"].strip())
        canon_name = names.most_common(1)[0][0] if names else ""
        canon_sail = sails.most_common(1)[0][0] if sails else ""
        canon_type = types.most_common(1)[0][0] if types else ""
        bid = _boat_id_from(canon_name, canon_sail, canon_type)
        for i in idxs:
            row2bid[i] = bid
    return row2bid


def apply_overrides(rows: list[dict],
                    row2bid: dict[int, str]) -> dict[int, str]:
    if not OVERRIDES_YAML.exists():
        return row2bid
    cfg = yaml.safe_load(OVERRIDES_YAML.read_text()) or {}
    merges = cfg.get("merges") or []
    splits = cfg.get("splits") or []

    # MERGES: for each merge entry, force all listed (name_canon, sail_canon)
    # pairs to share the new bid (re-derived from canonical name).
    for entry in merges:
        canonical = entry.get("canonical", "")
        pairs = entry.get("rows", [])
        target_indices: list[int] = []
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            target_name, target_sail = pair
            target_name_c = (target_name or "").lower().strip()
            target_sail_c = "".join(ch for ch in (target_sail or "") if ch.isdigit())
            for i, r in enumerate(rows):
                if (r["boat_name_canon"] == target_name_c
                        and r["sail_number_canon"] == target_sail_c):
                    target_indices.append(i)
        if not target_indices:
            continue
        # Pick a representative type from the matched rows for the new id.
        types = Counter(rows[i]["boat_type"].strip().lower()
                        for i in target_indices if rows[i]["boat_type"].strip())
        rep_type = types.most_common(1)[0][0] if types else ""
        sails = Counter(rows[i]["sail_number_canon"] for i in target_indices
                        if rows[i]["sail_number_canon"])
        rep_sail = sails.most_common(1)[0][0] if sails else ""
        new_bid = _boat_id_from((canonical or "").lower().strip(), rep_sail, rep_type)
        for i in target_indices:
            row2bid[i] = new_bid

    # SPLITS: for each pair listed, force them onto distinct boat_ids derived
    # from their own (name, sail, type).
    for entry in splits:
        pairs = entry.get("rows", [])
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                continue
            target_name, target_sail = pair
            target_name_c = (target_name or "").lower().strip()
            target_sail_c = "".join(ch for ch in (target_sail or "") if ch.isdigit())
            for i, r in enumerate(rows):
                if (r["boat_name_canon"] == target_name_c
                        and r["sail_number_canon"] == target_sail_c):
                    bid = _boat_id_from(target_name_c, target_sail_c,
                                        r["boat_type"].strip().lower())
                    row2bid[i] = bid
    return row2bid


def write_boats_and_aliases(rows: list[dict], row2bid: dict[int, str],
                            races_year: dict[str, int]) -> None:
    by_bid: dict[str, list[int]] = defaultdict(list)
    for i, bid in row2bid.items():
        by_bid[bid].append(i)

    boats: list[dict] = []
    aliases: list[dict] = []

    for bid, idxs in by_bid.items():
        names = Counter(rows[i]["boat_name"] for i in idxs if rows[i]["boat_name"])
        sails = Counter(rows[i]["sail_number"] for i in idxs if rows[i]["sail_number"])
        types = Counter(rows[i]["boat_type"] for i in idxs if rows[i]["boat_type"])
        years = [races_year.get(rows[i]["race_id"]) for i in idxs]
        years = [y for y in years if y is not None]

        primary_name = names.most_common(1)[0][0] if names else ""
        primary_sail = sails.most_common(1)[0][0] if sails else ""
        primary_type = types.most_common(1)[0][0] if types else ""

        boats.append({
            "boat_id": bid,
            "primary_name": primary_name,
            "primary_sail": primary_sail,
            "primary_type": primary_type,
            "result_count": len(idxs),
            "first_seen_year": min(years) if years else "",
            "last_seen_year": max(years) if years else "",
        })

        # aliases: count distinct (boat_name, sail_number) pairs
        pair_counts: Counter = Counter()
        for i in idxs:
            pair_counts[(rows[i]["boat_name"], rows[i]["sail_number"])] += 1
        for (name, sail), cnt in pair_counts.items():
            aliases.append({
                "boat_id": bid,
                "boat_name": name,
                "sail_number": sail,
                "count": cnt,
            })

    boats.sort(key=lambda b: -b["result_count"])
    with BOATS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "boat_id", "primary_name", "primary_sail", "primary_type",
            "result_count", "first_seen_year", "last_seen_year",
        ])
        w.writeheader()
        for b in boats:
            w.writerow(b)
    with ALIASES_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=[
            "boat_id", "boat_name", "sail_number", "count"])
        w.writeheader()
        for a in aliases:
            w.writerow(a)


def rewrite_results(rows: list[dict], row2bid: dict[int, str]) -> None:
    fieldnames = list(rows[0].keys()) if rows else []
    if "boat_id" not in fieldnames:
        fieldnames.append("boat_id")
    with RESULTS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for i, r in enumerate(rows):
            r["boat_id"] = row2bid.get(i, "")
            w.writerow(r)


def write_review(review: list[tuple[str, str, str, int]]) -> None:
    with REVIEW_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["sail_number_canon", "name_a", "name_b", "token_set_ratio"])
        for sail, a, b, score in sorted(set(review), key=lambda r: -r[3]):
            w.writerow([sail, a, b, score])


def main() -> int:
    rows = load_results()
    if not rows:
        print("no results — nothing to do")
        return 0
    races_year = load_races_year()
    clusters, review = cluster_by_sail(rows)
    row2bid = assign_boat_ids(rows, clusters)
    row2bid = apply_overrides(rows, row2bid)
    write_boats_and_aliases(rows, row2bid, races_year)
    rewrite_results(rows, row2bid)
    write_review(review)
    n_boats = len(set(row2bid.values()))
    print(f"clustered {len(rows)} rows into {n_boats} boat identities")
    print(f"wrote {BOATS_CSV}, {ALIASES_CSV}, {REVIEW_CSV}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
