from __future__ import annotations

import numpy as np
import pandas as pd

from Analyze.true_wind_estimator import Calibration
from Libraries import true_wind
from Libraries.global_variables import G

G.init_seattle()


def _raw_frame(
    n: int = 300,
    *,
    awa_deg: float = 0.0,
    aws_kt: float = 10.0,
    hdg_true_deg: float = 0.0,
    cog_true_deg: float = 0.0,
    sog_kt: float = 0.0,
    spd_kt: float = 0.0,
) -> pd.DataFrame:
    return pd.DataFrame({
        "rawa": np.full(n, awa_deg, dtype=float),
        "raws": np.full(n, aws_kt / G.MS_2_KNOTS, dtype=float),
        "rhdg": np.full(n, hdg_true_deg - G.MAGNETIC_VARIATION, dtype=float),
        "rcog": np.full(n, cog_true_deg, dtype=float),
        "rsog": np.full(n, sog_kt / G.MS_2_KNOTS, dtype=float),
        "rspd": np.full(n, spd_kt / G.MS_2_KNOTS, dtype=float),
    })


def _angle_error(a, b):
    return ((np.asarray(a) - b + 180.0) % 360.0) - 180.0


def test_raw_estimator_frame_converts_speeds_to_knots():
    df = _raw_frame(5, aws_kt=12.3, sog_kt=4.5, spd_kt=4.2)
    est = true_wind.raw_estimator_frame(df)

    np.testing.assert_allclose(est["aws"], 12.3)
    np.testing.assert_allclose(est["sog"], 4.5)
    np.testing.assert_allclose(est["spd"], 4.2)
    np.testing.assert_allclose(est["hdg"], 0.0)


def test_ground_wind_stationary_boat_reports_wind_from_heading(monkeypatch):
    monkeypatch.setattr(
        true_wind,
        "load_physical_ground_calibration",
        lambda: Calibration(),
    )
    df = _raw_frame(awa_deg=0.0, aws_kt=10.0, hdg_true_deg=0.0)

    out = true_wind.estimate_offline_wind(df, mode="ground", fs_hz=10.0)

    assert np.nanmax(np.abs(_angle_error(out["twd"], 0.0))) < 1e-6
    np.testing.assert_allclose(out["tws"] * G.MS_2_KNOTS, 10.0, atol=1e-6)
    np.testing.assert_allclose(out["twa"], 0.0, atol=1e-6)


def test_ground_wind_subtracts_boat_velocity_over_ground(monkeypatch):
    monkeypatch.setattr(
        true_wind,
        "load_physical_ground_calibration",
        lambda: Calibration(),
    )
    # Apparent source vector is 15 kt from dead ahead; boat is moving 5 kt
    # toward that source, so true ground wind is 10 kt from north.
    df = _raw_frame(awa_deg=0.0, aws_kt=15.0, hdg_true_deg=0.0,
                    cog_true_deg=0.0, sog_kt=5.0)

    out = true_wind.estimate_offline_wind(df, mode="ground", fs_hz=10.0)

    assert np.nanmax(np.abs(_angle_error(out["twd"], 0.0))) < 1e-6
    np.testing.assert_allclose(out["tws"] * G.MS_2_KNOTS, 10.0, atol=1e-6)


def test_physical_heading_offset_is_applied_inside_estimator(monkeypatch):
    monkeypatch.setattr(
        true_wind,
        "load_physical_ground_calibration",
        lambda: Calibration(heading_offset_deg=-10.0),
    )
    # Raw heading says 010 true, but physical calibration rotates it back to
    # 000 true. With AWA=0 on a stationary boat, TWD should be 000.
    df = _raw_frame(awa_deg=0.0, aws_kt=8.0, hdg_true_deg=10.0)

    out = true_wind.estimate_offline_wind(df, mode="ground", fs_hz=10.0)

    assert np.nanmax(np.abs(_angle_error(out["twd"], 0.0))) < 1e-6
    np.testing.assert_allclose(out["twa"], 0.0, atol=1e-6)

