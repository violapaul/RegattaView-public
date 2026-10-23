"""Shared geometry helpers for race-note analyses.

One authoritative home for the lat/lon→meters and course-frame projection that
per-note driver scripts (under `Analyze/Notes/<slug>/scripts/`) all need. Drivers
import these — they must never copy them. When a new note needs another reusable
helper, add it here rather than re-deriving it in the note's scripts/ dir.

Conventions:
  - lat/lon in degrees; offsets in meters via a local equirectangular approx
    (accurate over the ~100 m scales of a start line / lane).
  - course frame: `+ahead` is along `course_deg` (°true), `+right` is 90° clockwise
    from it (to starboard on a starboard tack). Pass a boat length to get
    boatlengths instead of meters.
"""
from __future__ import annotations

import numpy as np

EARTH_LAT_M = 111320.0  # meters per degree of latitude (≈ lon at the equator)


def lon_m_per_deg(lat_deg):
    """Meters per degree of longitude at latitude `lat_deg`."""
    return EARTH_LAT_M * np.cos(np.radians(lat_deg))


def offset_en(lat, lon, ref_lat, ref_lon):
    """East/north meters of (lat, lon) relative to (ref_lat, ref_lon)."""
    e = (np.asarray(lon, float) - ref_lon) * lon_m_per_deg(ref_lat)
    n = (np.asarray(lat, float) - ref_lat) * EARTH_LAT_M
    return e, n


def course_frame(e, n, course_deg):
    """Project an east/north offset into a course frame.

    Returns (ahead, right): `+ahead` along `course_deg`, `+right` 90° clockwise.
    """
    ahead = e * np.sin(np.radians(course_deg)) + n * np.cos(np.radians(course_deg))
    right = e * np.sin(np.radians(course_deg + 90)) + n * np.cos(np.radians(course_deg + 90))
    return ahead, right


def relative_course_frame(lat, lon, ref_lat, ref_lon, course_deg, loa=None):
    """(lat, lon) in the course frame centered on (ref_lat, ref_lon).

    Returns (ahead, right, rng) in meters, or in boatlengths if `loa` is given.
    """
    e, n = offset_en(lat, lon, ref_lat, ref_lon)
    ahead, right = course_frame(e, n, course_deg)
    rng = np.hypot(e, n)
    if loa:
        return ahead / loa, right / loa, rng / loa
    return ahead, right, rng


def nearest_row(df, ut, t):
    """Row of `df` nearest unixtime `t` (`ut` = df['unixtime'] as an ndarray)."""
    return df.iloc[int(np.argmin(np.abs(ut - t)))]


def angdiff(a, b):
    """Signed (a − b) wrapped to [−180, 180] degrees."""
    return ((np.asarray(a) - np.asarray(b) + 180.0) % 360.0) - 180.0
