"""Yacht Scoring JSON cumulative-result parser.

Yacht Scoring is a SPA backed by `https://api.yachtscoring.com/v1`. The
public-facing series leaderboard for an event lives at
`/v1/public/event/<eID>/cumulative-result` and returns JSON of the shape:

    {
      "eventData": {...},
      "data": [
        {
          "circleName": str,
          "divisions": [
            {
              "divisionName": str,
              "classes": [
                {
                  "class": int,
                  "className": str,
                  "boats": [
                    {
                      "name": str,
                      "sailPrefix": str?,
                      "sailNumber": str?,
                      "design": str?,
                      "owner": {"firstName": str?, "lastName": str?, ...},
                      "rating": int|str?,
                      "raceTotal": float,
                      "records": [{"raceNumber": int, "raceValue": float, "finishStatus": str, ...}],
                      ...
                    }
                  ]
                }
              ]
            }
          ]
        }
      ]
    }

Boats inside each class come back already sorted by raceTotal (low → high
== best). Place is the 1-based index after that sort. We yield one
ResultRow per boat. Each (circle, division, class) triple becomes a
distinct race in races.csv via the `class_name` field.
"""

from __future__ import annotations

import json
from typing import Iterator, Optional

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..sources.base import ResultPage, ResultRow


def _join_name(owner: dict | None) -> Optional[str]:
    if not owner:
        return None
    fn = (owner.get("firstName") or "").strip()
    ln = (owner.get("lastName") or "").strip()
    full = f"{fn} {ln}".strip()
    return full or None


def _join_sail(prefix: Optional[str], number: Optional[str]) -> Optional[str]:
    pre = (prefix or "").strip()
    num = (number or "").strip()
    if pre and num:
        return f"{pre} {num}"
    return num or pre or None


def _format_class(circle: str, division: str, class_name: str) -> str:
    """`<circle> > <division> > <className>`. Drop redundant repeats."""
    parts = [p for p in (circle, division, class_name) if p]
    # Dedupe consecutive equal segments (J Fest > J Fest > J70 -> J Fest > J70)
    out: list[str] = []
    for p in parts:
        if not out or out[-1] != p:
            out.append(p)
    return " > ".join(out) if out else ""


def parse_cumulative_json(body, page):
    """Yield one ResultRow per boat per class in the cumulative-result JSON.

    `body` is bytes or str. `page.event` and `page.year` come from discovery.
    """
    from ..sources.base import ResultRow  # local to avoid circular import

    if isinstance(body, (bytes, bytearray)):
        text = body.decode("utf-8", errors="replace")
    else:
        text = body
    try:
        d = json.loads(text)
    except json.JSONDecodeError:
        return  # nothing to yield
    data = d.get("data") or []
    for circle in data:
        circle_name = (circle.get("circleName") or "").strip()
        for division in (circle.get("divisions") or []):
            division_name = (division.get("divisionName") or "").strip()
            for cls in (division.get("classes") or []):
                class_label = (cls.get("className") or "").strip()
                full_class = _format_class(circle_name, division_name, class_label)
                # Boats in cumulative-result already come sorted by raceTotal
                for i, boat in enumerate(cls.get("boats") or []):
                    name = (boat.get("name") or "").strip()
                    if not name:
                        continue
                    place_int = i + 1
                    yield ResultRow(
                        page=page,
                        place=str(place_int),
                        place_int=place_int,
                        boat_name=name,
                        sail_number=_join_sail(boat.get("sailPrefix"),
                                               boat.get("sailNumber")),
                        boat_type=(boat.get("design") or "").strip() or None,
                        skipper=_join_name(boat.get("owner")),
                        yacht_club=None,  # not exposed on the public endpoint
                        rating=(str(boat.get("rating")).strip()
                                if boat.get("rating") is not None
                                and boat.get("rating") != ""
                                else None),
                        elapsed=None,
                        corrected=None,
                        score=(str(boat.get("raceTotal"))
                               if boat.get("raceTotal") is not None
                               else None),
                        class_name=full_class or None,
                    )
