"""Start-line representation and inference.

Coordinate conventions:
  - Positions are (mx, my) in meters, produced by ``G.MAP(lon, lat)`` (Transverse
    Mercator centered on the venue). ``mx`` is eastward, ``my`` is northward.
  - Bearings are compass degrees (0 = north, 90 = east).
  - ``course_bearing_deg`` = true wind direction (TWD): a boat heading on
    ``course_bearing_deg`` is sailing directly into the wind, i.e. up the course.
  - ``signed_distance`` is positive on the course side (the side the boat must
    reach to score a start) and negative on the prestart side.

Frame convention for TWD:
  ``df['twd']`` is in the **true** frame. Evidence:
    - Raw PGN 130306 ``Reference=True (ground referenced to North)`` is
      true-north-referenced on Peer Gynt's instruments, as pinned by the
      upwind-tack bisector experiment documented in
      ``Analyze/twd_reference.md`` (heading bisector 349.4° magnetic, raw
      TWD 2.5°, offset 13.1°, matching session variation 14.1° within 1°).
    - ``Libraries/race_logs.estimate_true_wind`` adds
      ``G.MAGNETIC_VARIATION`` to magnetic ``rhdg`` before comparing to
      true ``rcog``, so its output is likewise true.
    - Empirical median ``twd − boat_twd`` is ~0.9° across the archive
      (see ``Analyze/stage1_frame_audit.json``) — consistent with both
      being true, with a small offset from the autopilot's own stale WMM.

  This module is geographic (lat/lon, Leaflet map), also true frame, so no
  magnetic-to-true conversion is required. ``infer_start_line`` reads
  ``df['twd_true']`` (now a simple alias for ``df['twd']`` produced by
  ``Libraries.race_logs``) and the ``StartLine.course_bearing_deg`` field is
  stored in the true frame — matching the line's ``pin_xy`` / ``committee_xy``
  which are in ``G.MAP``-projected meters (also true).

  Historical note: before Stage 1.5 of
  ``/Users/viola/.claude/plans/i-have-a-lot-zany-moon.md``,
  start-analysis derived-channel code added ``variation`` to ``twd`` to
  "convert" it to true. Since ``twd`` was already true, this was a
  double-variation bug that silently rotated ``course_bearing_deg``,
  ``rel_cog_to_twd_deg``, and ``upwind_vmg_kts`` by ~+14° in Seattle. Any
  pre-Stage-1.5 ``starts.json`` inherits that offset; regenerate via
  ``find_starts.py`` after upgrading.
"""
from dataclasses import dataclass
from typing import Optional
import math

import numpy as np

from Libraries.global_variables import G


DEFAULT_LINE_HALF_LENGTH_M = 150.0


def _unit_from_bearing(bearing_deg: float) -> np.ndarray:
    """Convert a compass bearing (0=N, 90=E) to a unit (east, north) vector."""
    b = math.radians(bearing_deg)
    return np.array([math.sin(b), math.cos(b)])


def _circular_mean_deg(values: np.ndarray) -> float:
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return float('nan')
    s = float(np.mean(np.sin(np.radians(v))))
    c = float(np.mean(np.cos(np.radians(v))))
    return math.degrees(math.atan2(s, c)) % 360.0


@dataclass
class StartLine:
    """Start-line representation in projected meters."""
    pin_xy: tuple           # (mx, my)
    committee_xy: tuple     # (mx, my)
    bearing_deg: float      # pin -> committee, compass degrees
    course_bearing_deg: float
    source: str             # 'inferred' | 'user'

    @property
    def midpoint_xy(self) -> tuple:
        return (0.5 * (self.pin_xy[0] + self.committee_xy[0]),
                0.5 * (self.pin_xy[1] + self.committee_xy[1]))

    def _unit_along(self) -> np.ndarray:
        return _unit_from_bearing(self.bearing_deg)

    def _unit_normal(self) -> np.ndarray:
        return _unit_from_bearing(self.course_bearing_deg)

    def signed_distance(self, xy: np.ndarray) -> np.ndarray:
        """Signed distance from the line in meters; + on course side.

        Accepts an ``(N, 2)`` array or a single ``(2,)`` point.
        """
        xy = np.atleast_2d(np.asarray(xy, dtype=float))
        mid = np.asarray(self.midpoint_xy)
        n = self._unit_normal()
        out = (xy - mid) @ n
        return out[0] if out.shape == (1,) else out

    def along_line(self, xy: np.ndarray) -> np.ndarray:
        """Along-line coordinate in meters from pin toward committee."""
        xy = np.atleast_2d(np.asarray(xy, dtype=float))
        pin = np.asarray(self.pin_xy)
        u = self._unit_along()
        out = (xy - pin) @ u
        return out[0] if out.shape == (1,) else out

    def to_dict(self) -> dict:
        """JSON-serializable dict with lat/lon endpoints."""
        pin_lon, pin_lat = G.MAP(self.pin_xy[0], self.pin_xy[1], inverse=True)
        cc_lon, cc_lat = G.MAP(self.committee_xy[0], self.committee_xy[1], inverse=True)
        return {
            'pin_ll': [round(float(pin_lat), 6), round(float(pin_lon), 6)],
            'committee_ll': [round(float(cc_lat), 6), round(float(cc_lon), 6)],
            'bearing_deg': round(float(self.bearing_deg), 1),
            'course_bearing_deg': round(float(self.course_bearing_deg), 1),
            'source': self.source,
        }


def line_from_latlon(
    pin_ll: tuple,
    committee_ll: tuple,
    course_bearing_deg: Optional[float] = None,
    source: str = 'user',
) -> StartLine:
    """Build a StartLine from two lat/lon endpoints.

    ``course_bearing_deg`` defaults to the line bearing rotated +90 (i.e. the
    course side is to the right of pin->committee). Callers that know the wind
    direction should pass it explicitly.
    """
    pin_lon, pin_lat = float(pin_ll[1]), float(pin_ll[0])
    cc_lon, cc_lat = float(committee_ll[1]), float(committee_ll[0])
    pin_x, pin_y = G.MAP(pin_lon, pin_lat)
    cc_x, cc_y = G.MAP(cc_lon, cc_lat)
    dx = cc_x - pin_x
    dy = cc_y - pin_y
    bearing_deg = (math.degrees(math.atan2(dx, dy))) % 360.0
    if course_bearing_deg is None:
        course_bearing_deg = (bearing_deg + 90.0) % 360.0
    return StartLine(
        pin_xy=(pin_x, pin_y),
        committee_xy=(cc_x, cc_y),
        bearing_deg=bearing_deg,
        course_bearing_deg=float(course_bearing_deg) % 360.0,
        source=source,
    )


def _find_upwind_onset(
    upwind_vmg: np.ndarray,
    start_idx: int,
    end_idx: int,
    fs: int,
    stable_s: int,
) -> Optional[int]:
    """First index in ``[start_idx, end_idx)`` where rolling-30s upwind_vmg
    becomes positive and stays positive for at least ``stable_s`` seconds.

    Returns None if no such window exists.
    """
    if end_idx - start_idx < stable_s * fs:
        return None
    win = min(30 * fs, end_idx - start_idx)
    v = np.asarray(upwind_vmg[start_idx:end_idx], dtype=float)
    # Rolling mean via cumulative sum.
    v_fill = np.where(np.isfinite(v), v, 0.0)
    cs = np.concatenate([[0.0], np.cumsum(v_fill)])
    rolling = (cs[win:] - cs[:-win]) / win
    # rolling[i] corresponds to the window ending at v[i+win-1].
    positive = rolling > 0
    need = stable_s * fs
    if need > len(positive):
        return None
    # Find first i where positive[i:i+need] are all True.
    # Efficient scan: reset run length on False.
    run = 0
    for i, p in enumerate(positive):
        run = run + 1 if p else 0
        if run >= need:
            onset_local = i - need + 1 + (win - 1)
            return start_idx + onset_local
    return None


def infer_start_line(
    df,
    candidate_idx: int,
    pre_s: int = 300,
    post_s: int = 120,
    stable_s: int = 20,
    fs: int = 10,
) -> Optional[StartLine]:
    """Infer a start line given a candidate sample.

    Requires the enriched DataFrame produced by
    ``race_logs.add_navigation_derived_channels`` (provides ``upwind_vmg_kts``
    and ``twd_true``). Returns ``None`` if TWD is unavailable or upwind onset
    cannot be located and the candidate's own lat/lon is also invalid.
    """
    required = ('twd_true', 'upwind_vmg_kts', 'latitude', 'longitude')
    for col in required:
        if col not in df.columns:
            return None

    n = len(df)
    lo = max(0, candidate_idx - pre_s * fs)
    hi = min(n, candidate_idx + 60 * fs)
    # Use TRUE-frame TWD for line inference — the geographic map is true.
    # See module docstring for the full frame convention.
    twd_true = df['twd_true'].values[lo:hi]
    twd_med = _circular_mean_deg(twd_true)
    if not np.isfinite(twd_med):
        return None

    post_end = min(n, candidate_idx + post_s * fs)
    onset = _find_upwind_onset(
        df['upwind_vmg_kts'].values, candidate_idx, post_end, fs=fs, stable_s=stable_s,
    )
    anchor_idx = onset if onset is not None else candidate_idx

    lat = df['latitude'].values
    lon = df['longitude'].values
    if not (np.isfinite(lat[anchor_idx]) and np.isfinite(lon[anchor_idx])):
        # Fall back to candidate position.
        if not (np.isfinite(lat[candidate_idx]) and np.isfinite(lon[candidate_idx])):
            return None
        anchor_idx = candidate_idx

    mx, my = G.MAP(float(lon[anchor_idx]), float(lat[anchor_idx]))

    # Line lies perpendicular to upwind direction.
    line_bearing = (twd_med - 90.0) % 360.0
    u_along = _unit_from_bearing(line_bearing)
    half = DEFAULT_LINE_HALF_LENGTH_M
    pin = (mx - u_along[0] * half, my - u_along[1] * half)
    committee = (mx + u_along[0] * half, my + u_along[1] * half)
    return StartLine(
        pin_xy=pin,
        committee_xy=committee,
        bearing_deg=line_bearing,
        course_bearing_deg=twd_med,
        source='inferred',
    )
