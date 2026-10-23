"""Round-trip and contract tests for the v2 `.slog` format."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from Libraries import sailview_log_v2 as sv2
from Libraries.slog_channel_config import (
    V2_COLUMNS, COL_ID, Filtfilt, FiltfiltAngle, Raw,
)


def _synthetic_df(rows: int = 2000, seed: int = 13) -> pd.DataFrame:
    """A DataFrame with enough rows to make filtfilt well-conditioned and
    all the columns V2_COLUMNS might source from.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(rows) * 0.1
    start = pd.Timestamp('2026-04-19 09:00:00Z')
    row_times = pd.to_datetime([start + pd.Timedelta(seconds=float(x)) for x in t])
    # Most channels follow a slow signal + noise so filtfilt has something
    # to work with. Variation is constant per session.
    return pd.DataFrame(dict(
        row_seconds=t,
        row_times=row_times,
        rspd=5.0 + 0.6 * np.sin(0.1 * t) + 0.08 * rng.standard_normal(rows),
        rsog=5.0 + 0.6 * np.sin(0.1 * t) + 0.1 * rng.standard_normal(rows),
        rawa=40.0 + 20.0 * np.sin(0.05 * t) + 1.5 * rng.standard_normal(rows),
        raws=11.0 + 1.8 * np.sin(0.04 * t) + 0.25 * rng.standard_normal(rows),
        rhdg=210.0 + 10.0 * np.sin(0.03 * t) + 0.5 * rng.standard_normal(rows),
        rcog=212.0 + 10.0 * np.sin(0.03 * t) + 1.0 * rng.standard_normal(rows),
        rtwd=180.0 + 6.0 * np.sin(0.02 * t) + 0.4 * rng.standard_normal(rows),
        rtws=10.5 + 0.5 * np.sin(0.015 * t) + 0.08 * rng.standard_normal(rows),
        stwd=180.5 + 5.0 * np.sin(0.02 * t),
        stws=10.4 + 0.4 * np.sin(0.015 * t),
        gtwd=178.0 + 4.5 * np.sin(0.02 * t),
        gtws=10.2 + 0.35 * np.sin(0.015 * t),
        gtwa=-35.0 + 8.0 * np.sin(0.03 * t),
        zg100_roll=-5.0 + 8.0 * np.sin(0.1 * t) + 0.8 * rng.standard_normal(rows),
        rudder=np.clip(5.0 * np.sin(0.2 * t), -15, 15),
        depth=12.0 + 0.05 * rng.standard_normal(rows),
        latitude=47.69 + 0.0005 * np.sin(0.01 * t),
        longitude=-122.42 + 0.0005 * np.cos(0.01 * t),
        variation=np.full(rows, 15.4),
    ))


def test_header_layout(tmp_path):
    df = _synthetic_df()
    path = tmp_path / 'synth.slog'
    stats = sv2.write_slog(df, str(path),
                           session_variation_deg=15.4,
                           proj4='+proj=utm +zone=10 +ellps=WGS84')

    buf = path.read_bytes()
    (magic, version, num_cols, num_rows, msec, start, var, proj_len
     ) = struct.unpack_from('<IHHIIdfH', buf, 0)
    assert magic == 0x474F4C53   # 'SLOG'
    assert version == 1
    assert num_cols == stats['num_cols']
    assert num_rows == len(df)
    assert msec == 100
    assert abs(var - 15.4) < 1e-4
    proj_str = buf[sv2.HEADER_FIXED_STRUCT.size:
                   sv2.HEADER_FIXED_STRUCT.size + proj_len].decode('utf-8')
    assert 'utm' in proj_str


def test_ground_wind_columns_are_append_only():
    assert COL_ID['gtwd'] == 13
    assert COL_ID['gtws'] == 14
    assert COL_ID['gtwa'] == 15
    assert [spec.name for spec in V2_COLUMNS[-3:]] == ['gtwd', 'gtws', 'gtwa']


def test_roundtrip_decodes_within_quantization(tmp_path):
    """Decode a written file via read_slog and confirm each column matches
    the SMOOTHED signal within 1 quant step.
    """
    df = _synthetic_df()
    path = tmp_path / 'rt.slog'
    sv2.write_slog(df, str(path), session_variation_deg=15.4, proj4='')

    result = sv2.read_slog(str(path))
    # For each channel in V2_COLUMNS that was written, decoded should match
    # the Python-side re-smoothed signal within one quantum.
    from Libraries.slog_channel_config import V2_COLUMNS
    from Libraries.global_variables import G
    for spec in V2_COLUMNS:
        if spec.name not in result['data']:
            continue  # skipped column (not present in df, not tested here)
        source_name, _ = sv2._select_source(df, spec)
        raw_vals = sv2._fill_nans(df[source_name].values)
        if spec.variation_correction:
            raw_vals = raw_vals + sv2._session_variation_offset(df)
        if spec.convert_to_knots:
            raw_vals = raw_vals * G.MS_2_KNOTS
        expected = sv2._apply_smoother(raw_vals, spec.smoother)
        decoded = result['data'][spec.name]
        meta = next(c for c in result['columns'] if c['name'] == spec.name)
        span = meta['max'] - meta['min']
        tol = span / ((1 << spec.bits) - 1) + 1e-9
        np.testing.assert_allclose(
            decoded, expected, atol=tol,
            err_msg=f'{spec.name}: decoded deviates > 1 quantum from re-smoothed')


def test_rtwd_fallback_to_stwd_when_primary_is_all_nan(tmp_path):
    """When `rtwd` is NaN-majority (no autopilot), the writer falls back to
    `stwd` and records col_source=1.
    """
    df = _synthetic_df()
    df['rtwd'] = np.nan
    df['rtws'] = np.nan
    path = tmp_path / 'fb.slog'
    stats = sv2.write_slog(df, str(path), session_variation_deg=15.4, proj4='')

    result = sv2.read_slog(str(path))
    assert result['source_flags']['twd'] == sv2.SOURCE_FALLBACK
    assert result['source_flags']['tws'] == sv2.SOURCE_FALLBACK
    assert 'twd' in stats['fallback_columns']
    assert 'tws' in stats['fallback_columns']


def test_v2_file_is_smaller_than_v1_equivalent(tmp_path):
    """Sanity: v2's schema reduction + delta gives smaller files than v1.

    The v2 file should be no larger than ~80% of v1 on the same synthetic
    input (schema clean saves 8/21 columns ≈ 38%; delta doesn't shrink the
    uncompressed file). Tolerating a wide margin because test synth data
    differs from real race data.
    """
    from Libraries import sailview_log_v1
    df = _synthetic_df()

    # Compare against the full legacy column count. Names do not matter to the
    # v1 byte writer; this keeps the size assertion representative after v2
    # gained explicit ground-wind columns.
    numeric_cols = [c for c in df.columns if c != 'row_times']
    v1_df = pd.DataFrame({
        f'v1_{i}': df[numeric_cols[i % len(numeric_cols)]].to_numpy()
        for i in range(22)
    })
    v1_path = tmp_path / 'baseline.log'
    sailview_log_v1.write_log_v1(v1_df, str(v1_path))

    v2_path = tmp_path / 'cmp.slog'
    sv2.write_slog(df, str(v2_path), session_variation_deg=15.4, proj4='')

    v1_size = v1_path.stat().st_size
    v2_size = v2_path.stat().st_size
    assert v2_size < v1_size, f'v2 {v2_size} should be < v1 {v1_size}'


def test_constant_column_does_not_break_writer(tmp_path):
    """Rudder is a slow-moving signal here; depth is constant-ish. Neither
    should crash the writer and both should decode cleanly.
    """
    df = _synthetic_df()
    df['depth'] = 15.0   # exactly constant
    path = tmp_path / 'const.slog'
    sv2.write_slog(df, str(path), session_variation_deg=15.4, proj4='')
    result = sv2.read_slog(str(path))
    np.testing.assert_allclose(result['data']['depth'], 15.0, atol=1e-6)
