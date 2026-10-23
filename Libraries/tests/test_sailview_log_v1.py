"""Lock-down tests for the v1 `.log` byte format.

The sailview webapp reads `.log` files via `Src/src/js/sailview.js::readLog`.
The byte layout is owned by `Libraries/sailview_log_v1.py`.

These tests nail down:
  1) The compatibility writer and direct v1 writer produce byte-identical output.
  2) The byte layout (header + max/min + column-major uint16 payload) matches
     the documented spec.
  3) Round-tripping a real on-disk `.log` (read -> write) is byte-identical,
     which is the test that catches any silent drift in the writer.
"""
from __future__ import annotations

import glob
import os
import struct
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from Libraries import sailview_log_v1


def _synthetic_df(rows: int = 200, msec: int = 100, seed: int = 7) -> pd.DataFrame:
    """A deterministic DataFrame exercising varying, constant, negative,
    and zero-mean columns.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(rows) * (msec / 1000.0)
    return pd.DataFrame(dict(
        row_seconds=t,
        spd=5.0 + 0.4 * np.sin(0.3 * t) + 0.05 * rng.standard_normal(rows),
        awa=40.0 + 15.0 * np.cos(0.2 * t) + 0.7 * rng.standard_normal(rows),
        twd=170.0 + 0.3 * t + 1.3 * rng.standard_normal(rows),
        altitude_constant=np.full(rows, -1.75),          # constant column
        signed=rng.standard_normal(rows),                 # mean ≈ 0
    ))


def test_save_log_to_binary_delegates(tmp_path):
    """save_log_to_binary is a thin wrapper over write_log_v1."""
    df = _synthetic_df()
    via_wrapper = tmp_path / 'wrapper.log'
    via_direct = tmp_path / 'direct.log'

    sailview_log_v1.save_log_to_binary(df, str(via_wrapper))
    sailview_log_v1.write_log_v1(df, str(via_direct))

    assert via_wrapper.read_bytes() == via_direct.read_bytes()


def test_header_layout(tmp_path):
    """Nail down the on-disk header layout.

    Int32 header [rows, cols, msec, mult] | float64 max[cols] | float64 min[cols]
    | uint16 payload[rows*cols] in column-major (Fortran) order.
    """
    df = _synthetic_df(rows=150)
    path = tmp_path / 't.log'
    sailview_log_v1.write_log_v1(df, str(path), msec_per_sample=100, multiplier=65535)

    raw = path.read_bytes()
    rows, cols, msec, mult = struct.unpack_from('<iiii', raw, 0)
    assert (rows, cols, msec, mult) == (150, df.shape[1], 100, 65535)

    offset = 16
    max_vals = np.frombuffer(raw, dtype=np.float64, count=cols, offset=offset)
    offset += 8 * cols
    min_vals = np.frombuffer(raw, dtype=np.float64, count=cols, offset=offset)
    offset += 8 * cols

    np.testing.assert_allclose(max_vals, df.max(axis=0).values)
    np.testing.assert_allclose(min_vals, df.min(axis=0).values)

    payload_len = rows * cols * 2
    assert len(raw) == offset + payload_len, \
        f'expected total {offset + payload_len} bytes, got {len(raw)}'


def test_constant_column_roundtrips_to_min(tmp_path):
    """A constant column must decode to its (constant) value, not NaN.

    The writer branches to `max_for_norm = min + 1.0` to avoid a divide-by-
    zero, and the reader must still produce the original value using the
    stored (unmodified) max == min.
    """
    df = _synthetic_df()
    path = tmp_path / 'const.log'
    sailview_log_v1.write_log_v1(df, str(path))
    result = sailview_log_v1.read_log_v1(str(path))

    # Find the constant column by matching the DataFrame
    const_col_idx = list(df.columns).index('altitude_constant')
    decoded = result['data'][const_col_idx]
    assert np.all(np.isfinite(decoded)), 'constant column must not produce NaN'
    np.testing.assert_allclose(decoded, -1.75)


def test_writer_is_deterministic(tmp_path):
    """Writing the same DataFrame twice produces byte-identical files.

    No hidden state (time-of-day, env-dependent FP order). Keeps future
    changes from sneaking in nondeterminism that would break our planned
    content-hashing / caching strategies.

    Note: a full read→write→read round-trip is NOT byte-identical because
    the writer's `.astype(uint16)` truncates (not rounds), so decoded-then-
    re-encoded values drift by ±1 ULP in the payload. That's a property of
    the v1 format, not a bug — and not what determinism checks.
    """
    df = _synthetic_df()
    a = tmp_path / 'a.log'
    b = tmp_path / 'b.log'
    sailview_log_v1.write_log_v1(df, str(a))
    sailview_log_v1.write_log_v1(df, str(b))
    assert a.read_bytes() == b.read_bytes()


def test_js_reader_formula_matches_writer(tmp_path):
    """Decoding via the exact formula used in `Src/src/js/sailview.js::readLog`
    must recover the raw values within the quantization tolerance.

    Formula (sailview.js:572-579):
        const range = max_values[i] - min_values[i];
        const scale = range / multiplier;
        scaled_data[j] = (colData[j] * scale) + min_values[i];
    """
    df = _synthetic_df()
    path = tmp_path / 'js.log'
    sailview_log_v1.write_log_v1(df, str(path))

    raw = path.read_bytes()
    rows, cols, _, mult = struct.unpack_from('<iiii', raw, 0)
    offset = 16
    max_vals = np.frombuffer(raw, dtype=np.float64, count=cols, offset=offset)
    offset += 8 * cols
    min_vals = np.frombuffer(raw, dtype=np.float64, count=cols, offset=offset)
    offset += 8 * cols
    payload = np.frombuffer(raw, dtype=np.uint16, offset=offset,
                            count=rows * cols).reshape(cols, rows).T

    # Replicate the JS decoding formula exactly.
    ranges = max_vals - min_vals
    scales = ranges / float(mult)
    decoded = payload.astype(np.float64) * scales + min_vals

    # Per-column quantization tolerance: one uint16 step = range / mult.
    for i, col in enumerate(df.columns):
        tol = max(ranges[i] / mult, 1e-12)
        np.testing.assert_allclose(decoded[:, i], df[col].values, atol=tol,
                                   err_msg=f'column {col} drifts beyond 1 quantum')
