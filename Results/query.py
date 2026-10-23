"""Query CLI: boats, boat <name>, races-for-boat <name>, race <id>."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_THIS = Path(__file__).resolve().parent
if str(_THIS.parent) not in sys.path:
    sys.path.insert(0, str(_THIS.parent))

import pandas as pd

DATA_DIR = _THIS / "storage" / "data"
RESULTS_CSV = DATA_DIR / "results.csv"
RACES_CSV = DATA_DIR / "races.csv"
BOATS_CSV = DATA_DIR / "boats.csv"
ALIASES_CSV = DATA_DIR / "boat_aliases.csv"


def load() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    for p in (RESULTS_CSV, RACES_CSV, BOATS_CSV, ALIASES_CSV):
        if not p.exists():
            raise SystemExit(f"missing {p} — run parse.py + identify.py first")
    results = pd.read_csv(RESULTS_CSV, dtype=str).fillna("")
    races = pd.read_csv(RACES_CSV, dtype=str).fillna("")
    boats = pd.read_csv(BOATS_CSV).fillna("")
    aliases = pd.read_csv(ALIASES_CSV, dtype=str).fillna("")
    return results, races, boats, aliases


def cmd_boats(args, results, races, boats, aliases) -> int:
    df = boats.sort_values("result_count", ascending=False)
    out = df.head(args.limit)
    if args.output:
        out.to_csv(args.output, index=False)
        print(f"wrote {args.output} ({len(out)} rows)")
    else:
        with pd.option_context("display.max_rows", None,
                               "display.max_columns", None,
                               "display.width", 200):
            print(out.to_string(index=False))
    return 0


def cmd_boat(args, results, races, boats, aliases) -> int:
    needle = args.name.lower()
    matched_bids = set()
    # match on primary name
    for _, b in boats.iterrows():
        if needle in str(b["primary_name"]).lower():
            matched_bids.add(b["boat_id"])
    # match on alias names too
    for _, a in aliases.iterrows():
        if needle in str(a["boat_name"]).lower():
            matched_bids.add(a["boat_id"])
    if not matched_bids:
        print(f"no boats match {args.name!r}")
        return 1
    rows = results[results["boat_id"].isin(matched_bids)].copy()
    rows = rows.merge(races[["race_id", "event", "year", "race_label", "class_name"]],
                      on="race_id", how="left")
    rows = rows.sort_values(["year", "event", "race_label", "place_int"],
                            kind="stable")
    if args.output:
        rows.to_csv(args.output, index=False)
        print(f"wrote {args.output} ({len(rows)} rows)")
    else:
        cols = ["year", "event", "race_label", "class_name", "place",
                "boat_name", "sail_number", "boat_type", "skipper", "boat_id"]
        with pd.option_context("display.max_rows", None,
                               "display.max_columns", None,
                               "display.width", 240):
            print(rows[cols].to_string(index=False))
    return 0


def _resolve_boat_ids(needle: str, boats, aliases) -> set[str]:
    n = needle.lower()
    bids = set()
    for _, b in boats.iterrows():
        if n in str(b["primary_name"]).lower():
            bids.add(b["boat_id"])
    for _, a in aliases.iterrows():
        if n in str(a["boat_name"]).lower():
            bids.add(a["boat_id"])
    return bids


def cmd_races_for_boat(args, results, races, boats, aliases) -> int:
    bids = _resolve_boat_ids(args.name, boats, aliases)
    if not bids:
        print(f"no boats match {args.name!r}")
        return 1
    if len(bids) > 1:
        print(f"# NOTE: {args.name!r} matches {len(bids)} boat_ids:",
              file=sys.stderr)
        for bid in bids:
            primary = boats.loc[boats["boat_id"] == bid, "primary_name"]
            primary_name = primary.iloc[0] if len(primary) else "<unknown>"
            print(f"#   {bid}  {primary_name!r}", file=sys.stderr)

    seed_rows = results[results["boat_id"].isin(bids)]
    seed_race_ids = list(dict.fromkeys(seed_rows["race_id"].tolist()))
    if not seed_race_ids:
        print("no races found")
        return 1
    seed_races = races[races["race_id"].isin(seed_race_ids)].copy()
    seed_races["year_int"] = pd.to_numeric(seed_races["year"], errors="coerce")
    seed_races = seed_races.sort_values(["year_int", "event", "race_label",
                                         "class_name"], kind="stable")

    leaderboards = results[results["race_id"].isin(seed_race_ids)].copy()
    leaderboards["place_int_num"] = pd.to_numeric(leaderboards["place_int"],
                                                  errors="coerce")
    out_rows = []
    md_chunks: list[str] = []
    for _, race in seed_races.iterrows():
        rid = race["race_id"]
        lb = leaderboards[leaderboards["race_id"] == rid].copy()
        lb = lb.sort_values(
            ["place_int_num", "boat_name"],
            kind="stable",
            na_position="last",
        )
        title = (f"{race['year']} {race['event']} — {race['race_label']} "
                 f"[{race['class_name']}]")
        md_chunks.append(f"\n## {title}\n")
        md_chunks.append(f"<{race['source_url']}>\n")
        md_chunks.append("| Place | Boat | Sail | Type | Skipper | Score |")
        md_chunks.append("|-------|------|------|------|---------|-------|")
        for _, r in lb.iterrows():
            mark = " **★**" if r["boat_id"] in bids else ""
            md_chunks.append(
                f"| {r['place']} | {r['boat_name']}{mark} "
                f"| {r['sail_number']} | {r['boat_type']} "
                f"| {r['skipper']} | {r['score'] or r['corrected'] or r['elapsed']} |"
            )
            out_rows.append({
                "year": race["year"],
                "event": race["event"],
                "race_label": race["race_label"],
                "class_name": race["class_name"],
                "place": r["place"],
                "boat_name": r["boat_name"],
                "sail_number": r["sail_number"],
                "boat_type": r["boat_type"],
                "skipper": r["skipper"],
                "score": r["score"],
                "elapsed": r["elapsed"],
                "corrected": r["corrected"],
                "race_id": rid,
                "source_url": race["source_url"],
                "is_seed_boat": (r["boat_id"] in bids),
            })

    if args.output:
        pd.DataFrame(out_rows).to_csv(args.output, index=False)
        print(f"wrote {args.output} ({len(out_rows)} rows across "
              f"{len(seed_race_ids)} races)")
    else:
        print("\n".join(md_chunks))
    return 0


def cmd_race(args, results, races, boats, aliases) -> int:
    rid = args.race_id
    race_rows = races[races["race_id"] == rid]
    if race_rows.empty:
        print(f"no such race_id {rid!r}")
        return 1
    race = race_rows.iloc[0]
    lb = results[results["race_id"] == rid].copy()
    lb["place_int_num"] = pd.to_numeric(lb["place_int"], errors="coerce")
    lb = lb.sort_values(["place_int_num", "boat_name"], kind="stable",
                        na_position="last")
    title = (f"{race['year']} {race['event']} — {race['race_label']} "
             f"[{race['class_name']}]")
    print(f"# {title}")
    print(f"<{race['source_url']}>\n")
    cols = ["place", "boat_name", "sail_number", "boat_type", "skipper",
            "score", "elapsed", "corrected"]
    with pd.option_context("display.max_rows", None,
                           "display.max_columns", None,
                           "display.width", 240):
        print(lb[cols].to_string(index=False))
    if args.output:
        lb.to_csv(args.output, index=False)
        print(f"\nwrote {args.output}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_boats = sub.add_parser("boats", help="dump boats.csv (top N)")
    p_boats.add_argument("--limit", type=int, default=50)
    p_boats.add_argument("--output", default=None)

    p_boat = sub.add_parser("boat", help="all rows for boats matching <name>")
    p_boat.add_argument("name")
    p_boat.add_argument("--output", default=None)

    p_rfb = sub.add_parser("races-for-boat",
                           help="every race a boat entered, with full leaderboard")
    p_rfb.add_argument("name")
    p_rfb.add_argument("--output", default=None,
                       help="CSV path; default = stdout markdown")

    p_race = sub.add_parser("race", help="full leaderboard for one race_id")
    p_race.add_argument("race_id")
    p_race.add_argument("--output", default=None)

    args = p.parse_args(argv)
    results, races, boats, aliases = load()
    handlers = {
        "boats": cmd_boats,
        "boat": cmd_boat,
        "races-for-boat": cmd_races_for_boat,
        "race": cmd_race,
    }
    return handlers[args.cmd](args, results, races, boats, aliases)


if __name__ == "__main__":
    raise SystemExit(main())
