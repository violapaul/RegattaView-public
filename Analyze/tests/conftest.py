"""Synthetic-track fixtures for start-detection tests.

Scenarios are built in the projected meter frame (``x_m`` eastward, ``y_m``
northward) and reverse-projected through ``G.MAP`` to produce realistic
lat/lon. Wind direction (TWD) is compass degrees (0 = N, 90 = E).
"""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Libraries.global_variables import G  # noqa: E402
from Libraries import process  # noqa: E402,F401 — triggers G.init_seattle

SAMPLES_PER_SEC = 10


def _xy_to_latlon(x: np.ndarray, y: np.ndarray):
    lon, lat = G.MAP(x, y, inverse=True)
    return lat, lon


def _make_df(t_s: np.ndarray, x: np.ndarray, y: np.ndarray,
             hdg: np.ndarray, twd: np.ndarray, sog_ms: np.ndarray,
             awa: np.ndarray | None = None) -> pd.DataFrame:
    lat, lon = _xy_to_latlon(x, y)
    # cog derived from velocity for fidelity, filled where speed is tiny.
    dx = np.gradient(x)
    dy = np.gradient(y)
    cog = np.degrees(np.arctan2(dx, dy)) % 360.0
    cog[(sog_ms < 0.2)] = hdg[(sog_ms < 0.2)]
    if awa is None:
        # Approximate AWA as (hdg - twd) wrapped to [-180, 180] — fine for unit tests.
        awa = ((hdg - twd + 180) % 360) - 180

    return pd.DataFrame({
        'timestamp': pd.to_datetime(t_s, unit='s', utc=True),
        'latitude': lat,
        'longitude': lon,
        'hdg': hdg,
        'cog': cog,
        'twd': twd,
        'sog': sog_ms,
        'spd': sog_ms,
        'awa': awa,
        'twa': ((twd - hdg + 180) % 360) - 180,
    })


@pytest.fixture
def clean_start_track():
    """A boat that loiters south of a known line, crosses it northbound onto
    the course side, and sails a steady upwind beat for 90 s. Wind from the
    north (TWD = 0), so the upwind direction is north.
    """
    fs = SAMPLES_PER_SEC
    pre_s, post_s = 240, 120
    total = pre_s + post_s
    t = np.arange(total * fs) / fs

    # Line midpoint at origin; line bearing east-west (pin west, committee east).
    # Course side = north (positive y).
    twd = np.zeros_like(t)          # wind from north

    # Position: prestart loiter south of line, small circles; then cross at t=pre_s
    # sailing north at ~5 kt (2.57 m/s).
    x = np.zeros_like(t)
    y = np.zeros_like(t)

    # Loiter: figure-eight around (0, -80), radius 20 m, period 60 s.
    loiter_mask = t < pre_s
    t_lo = t[loiter_mask]
    omega = 2 * math.pi / 60.0
    x[loiter_mask] = 20.0 * np.sin(omega * t_lo)
    y[loiter_mask] = -80.0 + 10.0 * np.sin(2 * omega * t_lo)

    # Post-start upwind at ~5 kt. Start at (0, -80+last) and go north.
    post_mask = ~loiter_mask
    t_po = t[post_mask] - pre_s
    x_start = x[loiter_mask][-1]
    y_start = y[loiter_mask][-1]
    x[post_mask] = x_start
    y[post_mask] = y_start + 2.57 * t_po

    # Heading tracks velocity. During loiter this rotates continuously (so the
    # prestart is NOT "settled"); during the beat it is steady at 0 (north).
    dx = np.gradient(x)
    dy = np.gradient(y)
    hdg = np.degrees(np.arctan2(dx, dy)) % 360.0
    # Speed: slow during loiter, ~5 kt upwind after.
    sog = np.where(loiter_mask, 1.0, 2.57)

    df = _make_df(t, x, y, hdg, twd, sog)
    # Candidate index = first post-start sample (line crossing).
    cand_idx = int(pre_s * fs)
    truth = {
        'candidate_idx': cand_idx,
        'line_midpoint_xy': (0.0, 0.0),
        'twd_deg': 0.0,
    }
    return df, truth


@pytest.fixture
def transit_track():
    """A boat sailing straight north at a constant ~5 kt through the line, with
    no prestart loiter. Represents a false-positive transit.
    """
    fs = SAMPLES_PER_SEC
    total = 360
    t = np.arange(total * fs) / fs

    x = np.zeros_like(t)
    y = -400.0 + 2.57 * t           # straight-line north
    hdg = np.zeros_like(t)
    twd = np.zeros_like(t)
    sog = np.full_like(t, 2.57)

    df = _make_df(t, x, y, hdg, twd, sog)
    # Pick a candidate near where y = 0 (crosses the test line).
    cand_idx = int(np.argmin(np.abs(y)))
    truth = {
        'candidate_idx': cand_idx,
        'line_midpoint_xy': (0.0, 0.0),
        'twd_deg': 0.0,
    }
    return df, truth
