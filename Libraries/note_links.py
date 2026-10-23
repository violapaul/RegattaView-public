"""Deep-link builders for race notes → the RegattaView viewer pages.

Notes (Analyze/Notes/) hand-link into the live site so a reader can jump from a
claim to the moment on the water. These helpers emit the exact query strings the
viewer pages parse, so authors don't hand-encode them.

Page params (source of truth = Src/src/js/{sailview,tracks,review}.js):
  - sailview.html ?utc=<unix_sec>&fields=<csv>   — utc auto-resolves clip + in-clip
        offset (resolveVideoForUtc), so no per-video math is needed. Or ?video=<basename>&time=<sec>.
  - tracks.html   ?date=<YYYY-MM-DD>&t=<sec from that day's time_start>  (Pacific sailing day)
  - review.html   ?event=<event_id>&t=<sec relative to the event start>

`tracks_url` needs the day's `time_start` (unix sec), published in
Analyze/AISDays/<date>.json. Pass it explicitly, or let the helper read it.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlencode

PROD_BASE = "https://regattaview.net"
REPO_ROOT = Path(__file__).resolve().parents[1]


def _join(page: str, params: dict, base: str) -> str:
    qs = urlencode({k: v for k, v in params.items() if v is not None}, safe=",:")
    return f"{base}/{page}?{qs}"


def sailview_url(unix: float, fields=None, base: str = PROD_BASE) -> str:
    """Deep-link to sailview at wall-clock `unix` (seconds). `fields` is a list or
    comma string of instrument channels (e.g. ['spd','awa','heel'])."""
    params = {"utc": int(round(unix))}
    if fields:
        params["fields"] = fields if isinstance(fields, str) else ",".join(fields)
    return _join("sailview.html", params, base)


def tracks_url(date: str, unix: float, day_time_start: float | None = None,
               base: str = PROD_BASE) -> str:
    """Deep-link to the AIS day map at wall-clock `unix`. `date` is the Pacific
    sailing day (the Analyze/AISDays/<date>.json key). `day_time_start` is that
    day's time_start (unix sec); if omitted, read it from the day file."""
    if day_time_start is None:
        day_time_start = _day_time_start(date)
    return _join("tracks.html", {"date": date, "t": int(round(unix - day_time_start))}, base)


def review_url(event_id: str, t: float | None = None, base: str = PROD_BASE) -> str:
    """Deep-link to the review page for a detected event; optional `t` seconds
    relative to the event start."""
    params = {"event": event_id}
    if t is not None and abs(t) >= 0.5:
        params["t"] = int(round(t))
    return _join("review.html", params, base)


def _day_time_start(date: str) -> float:
    path = REPO_ROOT / "Analyze" / "AISDays" / f"{date}.json"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found — pass day_time_start explicitly (only git-tracked "
            f"files exist in a worktree; AISDays lives in the main tree)."
        )
    with open(path) as f:
        return float(json.load(f)["time_start"])
