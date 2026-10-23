"""ClubSpot v4 results JSON parser.

The ClubSpot results API at
``https://results.theclubspot.com/clubspot-results-v4/<regatta_id>?boatClassIDs=<class_id>``
returns one class's series-overall scores. Shape (trimmed):

    {
        "scoresByRegistration": [
            {
                "registrationObject": {
                    "boatName": "Peer Gynt",
                    "sailNumber": "235",
                    "boatType": "J/105",
                    "participantNames": ["Jane Doe", "John Roe"],
                    "clubName": "CYC Seattle",
                    "handicap_phrf": 90,
                    "boatClassObject": {"name": "PHRF A (0-100)", ...},
                    ...
                },
                "scoring_data": [{"race_number": 1, "points": 1.5, ...}, ...],
                "total": 21,
                "net": 19
            },
            ...
        ],
        "races": [...],
        "last_cached": "2024-..."
    }

We emit one ResultRow per registration. Place is derived by sorting on
``net`` ascending (ties get the same integer rank, next rank skips). Each
row's ``class_name`` is taken from ``boatClassObject.name``. The
ResultPage.url already encodes one specific class — every row in this page
shares the same ``class_name``.

Events with a ClubSpot scoring method of ``not_racing`` are filtered upstream
by the discover() function, so this parser does not have to defend against
them.

Rating field: we copy ``handicap_phrf`` if non-empty, else ``orc_cert`` (the
two only fields populated on CYC pages we've seen). Series scores are stored
in ``score`` (Sailwave parlance: 'score' = total/net points). We use ``net``
in preference to ``total`` so throwouts apply.
"""

from __future__ import annotations

import json
from typing import Iterator, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..sources.base import ResultPage, ResultRow


def _safe_str(v) -> str:
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        # ints stay clean ("90"), floats keep their repr
        return str(v)
    return str(v).strip()


def _participant_names(reg: dict) -> str:
    names = reg.get("participantNames")
    if isinstance(names, list) and names:
        # Skip empty-string entries that ClubSpot occasionally includes
        names = [n.strip() for n in names if n and str(n).strip()]
        if names:
            return ", ".join(names)
    fn = (reg.get("firstName") or "").strip()
    ln = (reg.get("lastName") or "").strip()
    full = (fn + " " + ln).strip()
    return full


def _rating(reg: dict) -> str:
    for key in ("handicap_phrf", "orc_cert", "rating"):
        v = reg.get(key)
        if v not in (None, "", []):
            return _safe_str(v)
    return ""


def _ranked_with_places(rows: list[dict]) -> list[tuple[int, dict]]:
    """Return (place_int, row) sorted by net asc, ties share rank.

    Ties: standard competition ranking (1, 2, 2, 4) — next rank reflects the
    number of preceding entries. Entries with no net score sort to the end
    with place None (we render those rows with empty place).
    """
    scored = []
    unscored = []
    for entry in rows:
        net = entry.get("net")
        if isinstance(net, (int, float)):
            scored.append((net, entry))
        else:
            unscored.append(entry)
    scored.sort(key=lambda x: x[0])
    out: list[tuple[Optional[int], dict]] = []
    last_score = None
    last_place = 0
    for i, (net, entry) in enumerate(scored, start=1):
        if last_score is not None and net == last_score:
            place = last_place
        else:
            place = i
            last_place = i
            last_score = net
        out.append((place, entry))
    for entry in unscored:
        out.append((None, entry))
    return out


def parse_clubspot_v4(json_body, page):
    """Yield ResultRow per registration in a v4 results JSON body."""
    from ..sources.base import ResultRow  # local to avoid circular import

    if isinstance(json_body, (bytes, bytearray)):
        text = json_body.decode("utf-8", errors="replace")
    else:
        text = json_body
    text = text.strip()
    if not text or text.startswith("Unable to fetch"):
        return
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return
    rows = data.get("scoresByRegistration") or []
    if not rows:
        return

    # All rows share the same class on a per-class fetch — pull it once
    class_name: Optional[str] = None
    for entry in rows:
        bc = (entry.get("registrationObject") or {}).get("boatClassObject") or {}
        nm = (bc.get("name") or "").strip()
        if nm:
            class_name = nm
            break

    for place_int, entry in _ranked_with_places(rows):
        reg = entry.get("registrationObject") or {}
        boat_name = (reg.get("boatName") or "").strip()
        if not boat_name:
            # Single-handed dinghy entries often have no boat name; fall back
            # to the skipper name so the row is still attributable.
            boat_name = _participant_names(reg)
        if not boat_name:
            continue
        net = entry.get("net")
        place_str = str(place_int) if place_int is not None else ""
        score = ""
        if isinstance(net, (int, float)):
            score = _safe_str(net)
        elif isinstance(entry.get("total"), (int, float)):
            score = _safe_str(entry.get("total"))
        yield ResultRow(
            page=page,
            place=place_str,
            place_int=place_int,
            boat_name=boat_name,
            sail_number=_safe_str(reg.get("sailNumber")) or None,
            boat_type=_safe_str(reg.get("boatType")) or None,
            skipper=_participant_names(reg) or None,
            yacht_club=_safe_str(reg.get("clubName")) or None,
            rating=_rating(reg) or None,
            elapsed=None,
            corrected=None,
            score=score or None,
            class_name=class_name,
        )
