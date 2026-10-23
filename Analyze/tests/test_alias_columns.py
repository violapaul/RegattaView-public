"""Stage 2 guardrail: ``process_sensors`` emits frame-aliased bearing
columns (``hdg_mag``/``hdg_true``, ``cog_mag``/``cog_true``, ``twd_mag``/
``twd_true``) that round-trip consistently via ``G.MAGNETIC_VARIATION``.

The aliases make each bearing's reference frame explicit in the column
name. Legacy ``hdg``/``cog``/``twd`` are unchanged (for backward compat).
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from Libraries.global_variables import G  # noqa: E402
from Libraries import race_logs  # noqa: E402


REF_PARQUET_DIR = "/Users/viola/Data/SailingData/PandasLogs"
REF_PARQUET = "2023-10-27T14_39.parquet"

# Circular round-trip tolerance (degrees). compass_angle uses integer
# modulo on float64, so exactness is ~1e-13.
TOL_DEG = 1e-6


def _wrap_signed_deg(a):
    a = np.asarray(a, dtype=float)
    return ((a + 180.0) % 360.0) - 180.0


@pytest.fixture(scope="module")
def df():
    path = os.path.join(REF_PARQUET_DIR, REF_PARQUET)
    if not os.path.exists(path):
        pytest.skip(f"Reference parquet not available: {path}")
    return race_logs.read_log_file(
        REF_PARQUET,
        path=REF_PARQUET_DIR,
        discard_columns=False,
    )


def test_alias_columns_are_present(df):
    for col in ('hdg_mag', 'hdg_true', 'cog_mag', 'cog_true',
                'twd_mag', 'twd_true'):
        assert col in df.columns, f"expected alias column {col!r} missing"


def test_alias_columns_are_compass_range(df):
    """All aliases must be in [0, 360) so downstream geometry works."""
    for col in ('hdg_mag', 'hdg_true', 'cog_mag', 'cog_true',
                'twd_mag', 'twd_true'):
        vals = df[col].values
        finite = vals[np.isfinite(vals)]
        if len(finite) == 0:
            continue
        assert finite.min() >= 0.0, f"{col} has value < 0: {finite.min()}"
        assert finite.max() < 360.0, f"{col} has value >= 360: {finite.max()}"


@pytest.mark.parametrize("mag_col,true_col", [
    ('hdg_mag', 'hdg_true'),
    ('cog_mag', 'cog_true'),
    ('twd_mag', 'twd_true'),
])
def test_alias_round_trip(df, mag_col, true_col):
    """`(x_mag + variation) mod 360 == x_true` pointwise."""
    var = G.MAGNETIC_VARIATION
    roundtripped = (df[mag_col].values + var) % 360.0
    delta = _wrap_signed_deg(roundtripped - df[true_col].values)
    max_err = float(np.max(np.abs(delta[np.isfinite(delta)])))
    assert max_err < TOL_DEG, (
        f"{mag_col} + variation != {true_col}: max err {max_err:.3e}°"
    )


def test_legacy_columns_match_canonical_alias(df):
    """`hdg == hdg_mag`, `cog == cog_true`, `twd == twd_true` — the legacy
    columns are in the frame the codebase had been assuming, and the alias
    names make that explicit without changing values."""
    for legacy, canonical in [('hdg', 'hdg_mag'),
                              ('cog', 'cog_true'),
                              ('twd', 'twd_true')]:
        # Both were run through compass_angle, so both in [0, 360).
        delta = _wrap_signed_deg(df[legacy].values - df[canonical].values)
        max_err = float(np.max(np.abs(delta[np.isfinite(delta)])))
        assert max_err < TOL_DEG, (
            f"legacy {legacy!r} diverges from canonical alias "
            f"{canonical!r}: max err {max_err:.3e}°"
        )
