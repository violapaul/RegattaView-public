"""Production wrappers for the offline true-wind estimator.

This module gives pipeline code a stable entry point while the underlying
vector-state estimator continues to live in ``Analyze.true_wind_estimator``.
It deliberately keeps two products separate:

* water-reference wind: calibrated to reproduce the boat's B&G ``rtwd/rtws``;
* ground-reference wind: physically calibrated, current-aware wind over ground.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Literal

import numpy as np
import pandas as pd

from Analyze.true_wind_estimator import (
    AdaptiveFilterConfig,
    Calibration,
    offline_ground_wind_adaptive,
    offline_water_wind_adaptive,
)
from Libraries.global_variables import G


REPO_ROOT = Path(__file__).resolve().parents[1]
WATER_REF_CALIBRATION_PATH = REPO_ROOT / "Analyze" / "calibration_water_ref_2026.json"
SENSOR_CALIBRATION_PATH = (
    REPO_ROOT / "Src" / "src" / "assets" / "calibration" / "sensor_calibration.json"
)
SPEED_GLITCH_KT = 40.0

# Benchmark winner in Analyze/Plots/adaptive_eval/summary.md.
HUBER_ONLY_CFG = AdaptiveFilterConfig(
    alpha_R_turn=0.0,
    alpha_R_accel=0.0,
    q_growth=1.0,
    q_decay=1.0,
    huber_delta=1.5,
)


@lru_cache(maxsize=1)
def load_water_reference_calibration() -> Calibration:
    """Calibration whose purpose is reproducing on-boat ``rtwd/rtws``."""
    with WATER_REF_CALIBRATION_PATH.open() as f:
        payload = json.load(f)
    return Calibration(**payload["calibration"])


@lru_cache(maxsize=1)
def load_physical_ground_calibration() -> Calibration:
    """Calibration for physically meaningful ground-referenced TWD.

    Ground-reference wind uses GPS SOG/COG for boat motion, so the corrected
    STW scale is not part of this triangle. The physical heading and AWA offsets
    are still applied before rotating the apparent-wind vector to earth frame.
    """
    with SENSOR_CALIBRATION_PATH.open() as f:
        payload = json.load(f)
    heading = payload.get("heading") or {}
    awa = payload.get("awa") or {}
    return Calibration(
        heading_offset_deg=float(heading.get("offset", 0.0)),
        awa_offset_deg=float(awa.get("offset", 0.0)),
    )


def _clean_speed_glitches_ms(values_ms: np.ndarray) -> np.ndarray:
    """Replace absurd speed spikes by linear interpolation, preserving units."""
    values = np.asarray(values_ms, dtype=float).copy()
    bad = np.isfinite(values) & (np.abs(values) * G.MS_2_KNOTS > SPEED_GLITCH_KT)
    if not bad.any():
        return values
    good = np.isfinite(values) & ~bad
    if good.sum() < 2:
        return values
    idx = np.arange(len(values), dtype=float)
    values[bad] = np.interp(idx[bad], idx[good], values[good])
    return values


def raw_estimator_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Build the knots/true-frame input expected by the vector estimator."""
    required = ("rawa", "raws", "rhdg", "rcog", "rsog", "rspd")
    missing = [name for name in required if name not in df.columns]
    if missing:
        raise ValueError(f"missing true-wind estimator columns: {missing}")

    rsog = _clean_speed_glitches_ms(df["rsog"].to_numpy(dtype=float))
    rspd = _clean_speed_glitches_ms(df["rspd"].to_numpy(dtype=float))
    return pd.DataFrame({
        "awa": df["rawa"].to_numpy(dtype=float),
        "aws": df["raws"].to_numpy(dtype=float) * G.MS_2_KNOTS,
        # The estimator expects true heading; calibration.heading_offset_deg
        # is applied inside the estimator.
        "hdg": (df["rhdg"].to_numpy(dtype=float) + G.MAGNETIC_VARIATION) % 360.0,
        "cog": df["rcog"].to_numpy(dtype=float),
        "sog": rsog * G.MS_2_KNOTS,
        "spd": rspd * G.MS_2_KNOTS,
    }, index=df.index)


def estimate_offline_wind(
    df: pd.DataFrame,
    mode: Literal["water", "ground"],
    fs_hz: float | None = None,
) -> pd.DataFrame:
    """Return smoothed ``twd/tws/twa`` estimates for ``df``.

    Returned TWS is converted back to meters/second so callers can store it
    next to existing parquet-derived ``tws``/``stws`` columns.
    """
    fs = float(fs_hz or G.SAMPLES_PER_SECOND)
    est_df = raw_estimator_frame(df)
    if mode == "water":
        cal = load_water_reference_calibration()
        out = offline_water_wind_adaptive(est_df, fs_hz=fs, cal=cal, adaptive_cfg=HUBER_ONLY_CFG)
    elif mode == "ground":
        cal = load_physical_ground_calibration()
        out = offline_ground_wind_adaptive(est_df, fs_hz=fs, cal=cal, adaptive_cfg=HUBER_ONLY_CFG)
    else:
        raise ValueError(f"unknown true-wind mode: {mode!r}")

    result = pd.DataFrame(index=df.index)
    result["twd"] = out["twd"].to_numpy(dtype=float)
    result["tws"] = out["tws"].to_numpy(dtype=float) / G.MS_2_KNOTS
    result["twa"] = out["twa"].to_numpy(dtype=float)
    return result

