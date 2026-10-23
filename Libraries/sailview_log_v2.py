"""v2 `.slog` binary writer/reader for sailview.

Consumes the frozen channel spec at `Libraries/slog_channel_config.V2_COLUMNS`
and the existing smoothers at `Libraries/process.py`. Companion JS reader
will live at `Src/src/js/log_reader_v2.js` (Stage E).

Byte layout (little-endian):

    HEADER (30 bytes fixed + proj4):
      offset  0   uint32   magic = 0x474F4C53 ('SLOG' LE)
      offset  4   uint16   version = 1
      offset  6   uint16   num_cols
      offset  8   uint32   num_rows
      offset 12   uint32   msec_per_sample
      offset 16   float64  start_unix_ms   -- absolute wall-clock anchor
      offset 24   float32  variation_deg   -- session constant, retired from payload
      offset 28   uint16   proj4_len
      offset 30   bytes    proj4[proj4_len]

    PER-COLUMN BLOCK (12 bytes × num_cols, immediately after header):
      0   uint8     col_id         -- see slog_channel_config.COL_ID
      1   uint8     col_bits       -- 10/12/16
      2   uint8     col_smoother   -- 0=raw, 1=filtfilt, 2=filtfilt_angle,
                                      3=medfilt_then_filtfilt
      3   uint8     col_source     -- 0=primary, 1=fallback
      4   float32   col_min
      8   float32   col_max

    PAYLOAD (per column, in column order):
      2 bytes  uint16   first quantized sample
      2*(R-1)  int16    delta-encoded remaining samples (in quant units)

    The payload is delta-encoded because Stage A measured ~3.5× additional
    gzip/brotli compression on deltas vs raw uint16 on the same data. S3
    Content-Encoding: gzip (wired up in a later stage) does the actual
    compression — keeping the file uncompressed makes it inspectable.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

import numpy as np
import scipy.signal
import pandas as pd

from Libraries import process
from Libraries.slog_channel_config import (
    V2_COLUMNS, COL_ID, ChannelSpec,
    Raw, Filtfilt, FiltfiltAngle, MedfiltThenFiltfilt,
)
from Libraries.global_variables import G


# ---- format constants -------------------------------------------------------

MAGIC = 0x474F4C53   # 'SLOG' little-endian
VERSION = 1
HEADER_FIXED_STRUCT = struct.Struct('<IHHIIdfH')  # 30 bytes
COL_BLOCK_STRUCT = struct.Struct('<BBBBff')        # 12 bytes

SMOOTHER_CODE = {
    Raw: 0,
    Filtfilt: 1,
    FiltfiltAngle: 2,
    MedfiltThenFiltfilt: 3,
}
SOURCE_PRIMARY = 0
SOURCE_FALLBACK = 1


# ---- writer -----------------------------------------------------------------

@dataclass
class _EncodedColumn:
    spec: ChannelSpec
    source_flag: int
    col_min: float
    col_max: float
    first: int            # quantized first sample (uint16)
    deltas: np.ndarray    # int16 array of length num_rows - 1


def _select_source(df: pd.DataFrame, spec: ChannelSpec) -> tuple[str, int]:
    """Return (source_column_name, source_flag) per the fallback rule."""
    primary = spec.source_primary
    if primary in df.columns:
        nan_frac = float(df[primary].isna().mean())
        if nan_frac <= spec.primary_nan_frac_max:
            return primary, SOURCE_PRIMARY
    if spec.source_fallback and spec.source_fallback in df.columns:
        return spec.source_fallback, SOURCE_FALLBACK
    # Primary is present but has too many NaNs, no fallback — use primary anyway
    # (caller may NaN-fill before writing if they care).
    return primary, SOURCE_PRIMARY


def _apply_smoother(values: np.ndarray, smoother) -> np.ndarray:
    """Dispatch on smoother spec type."""
    if isinstance(smoother, Raw):
        return values.astype(np.float64, copy=True)
    if isinstance(smoother, Filtfilt):
        coeff = process.butterworth_filter(smoother.cutoff_hz, smoother.order)
        return process.smooth(coeff, values, causal=False)
    if isinstance(smoother, FiltfiltAngle):
        coeff = process.butterworth_filter(smoother.cutoff_hz, smoother.order)
        return process.smooth_angle(coeff, values, causal=False)
    if isinstance(smoother, MedfiltThenFiltfilt):
        pre = scipy.signal.medfilt(values, smoother.medfilt_width)
        coeff = process.butterworth_filter(smoother.cutoff_hz, smoother.order)
        return process.smooth(coeff, pre, causal=False)
    raise TypeError(f'unknown smoother: {smoother!r}')


def _fill_nans(values: np.ndarray) -> np.ndarray:
    """Forward-fill + back-fill NaNs so downstream smoothing/quantization has
    no holes. The v2 format has no provision for per-sample NaN flags.
    """
    out = np.asarray(values, dtype=np.float64).copy()
    if not np.any(np.isnan(out)):
        return out
    # Forward fill
    last = None
    for i in range(len(out)):
        if np.isnan(out[i]):
            if last is not None:
                out[i] = last
        else:
            last = out[i]
    # Back fill for any leading NaNs
    first = None
    for i in range(len(out) - 1, -1, -1):
        if np.isnan(out[i]):
            if first is not None:
                out[i] = first
        else:
            first = out[i]
    # If still all NaN, replace with 0
    out[np.isnan(out)] = 0.0
    return out


def _quantize_and_delta(smoothed: np.ndarray, bits: int,
                        ) -> tuple[int, np.ndarray, float, float]:
    """Quantize a float64 signal to `bits` bits; return first sample,
    int16 deltas, and the (col_min, col_max) used.

    Uses rounding (not truncation) so re-decoding via
    `value = q * (max - min) / (2^bits - 1) + min` is tight against the
    original smoothed signal.
    """
    col_min = float(np.min(smoothed))
    col_max = float(np.max(smoothed))
    span = col_max - col_min if col_max > col_min else 1.0
    quant_max = (1 << bits) - 1
    normalized = (smoothed - col_min) / span
    quantized = np.round(normalized * quant_max).astype(np.int32)
    quantized = np.clip(quantized, 0, quant_max)

    first = int(quantized[0])
    diffs = np.diff(quantized)
    # Catch pathological deltas that wouldn't fit in int16 — shouldn't
    # happen for sailing data at 10 Hz but be explicit.
    max_abs = int(np.max(np.abs(diffs))) if len(diffs) else 0
    if max_abs > 32767:
        raise OverflowError(
            f'delta out of int16 range (max |Δ| = {max_abs}); consider '
            f'promoting col_bits to 16 or adding pre-smoothing')
    return first, diffs.astype(np.int16), col_min, col_max


def _session_variation_offset(df: pd.DataFrame) -> float:
    """G.MAGNETIC_VARIATION minus the median boat-reported variation, used
    to correct on-boat `rtwd` (whose computation used the boat's stale
    variation). 0.0 when no boat variation is available.
    """
    if 'variation' in df.columns:
        v = df['variation'].dropna()
        if len(v):
            return float(G.MAGNETIC_VARIATION) - float(np.median(v))
    return 0.0


def _resolve_variation(df: pd.DataFrame,
                       session_variation_deg: Optional[float]) -> float:
    if session_variation_deg is not None:
        return float(session_variation_deg)
    return float(G.MAGNETIC_VARIATION)


def _resolve_start_unix_ms(df: pd.DataFrame) -> float:
    if 'row_times' in df.columns and len(df):
        t0 = df['row_times'].iloc[0]
        if hasattr(t0, 'timestamp'):
            return float(t0.timestamp() * 1000.0)
    return 0.0


def _resolve_msec_per_sample(df: pd.DataFrame) -> int:
    if 'row_seconds' in df.columns and len(df) >= 2:
        step = float(df['row_seconds'].diff().median())
        return max(1, int(round(step * 1000)))
    return 100


def write_slog(df: pd.DataFrame, path: str,
               session_variation_deg: Optional[float] = None,
               proj4: str = '',
               channels: tuple[ChannelSpec, ...] = V2_COLUMNS) -> dict:
    """Write `df` to a v2 `.slog` file. Returns a dict of write stats."""
    num_rows = len(df)
    if num_rows < 2:
        raise ValueError(f'write_slog requires at least 2 rows, got {num_rows}')

    variation_deg = _resolve_variation(df, session_variation_deg)
    start_unix_ms = _resolve_start_unix_ms(df)
    msec_per_sample = _resolve_msec_per_sample(df)
    variation_offset = _session_variation_offset(df)

    encoded: list[_EncodedColumn] = []
    for spec in channels:
        source_name, source_flag = _select_source(df, spec)
        if source_name not in df.columns:
            # Neither primary nor fallback present — skip the column.
            continue
        values = _fill_nans(df[source_name].values)
        if spec.variation_correction:
            values = values + variation_offset
        if spec.convert_to_knots:
            # Parquet speed channels are m/s; sailview/review expect knots.
            # Matches `sailview_log_v1.knots_convert` applied to v1 `.log`.
            values = values * G.MS_2_KNOTS
        smoothed = _apply_smoother(values, spec.smoother)
        first, deltas, col_min, col_max = _quantize_and_delta(smoothed, spec.bits)
        encoded.append(_EncodedColumn(
            spec=spec, source_flag=source_flag,
            col_min=col_min, col_max=col_max,
            first=first, deltas=deltas,
        ))

    proj4_bytes = proj4.encode('utf-8')
    if len(proj4_bytes) > 65535:
        raise ValueError('proj4 string too long for uint16 length field')

    with open(path, 'wb') as f:
        f.write(HEADER_FIXED_STRUCT.pack(
            MAGIC, VERSION, len(encoded), num_rows,
            msec_per_sample, start_unix_ms, variation_deg,
            len(proj4_bytes)))
        f.write(proj4_bytes)
        for col in encoded:
            f.write(COL_BLOCK_STRUCT.pack(
                COL_ID[col.spec.name],
                col.spec.bits,
                SMOOTHER_CODE[type(col.spec.smoother)],
                col.source_flag,
                col.col_min, col.col_max))
        for col in encoded:
            f.write(struct.pack('<H', col.first))
            f.write(col.deltas.tobytes())

    return dict(
        path=path,
        num_rows=num_rows,
        num_cols=len(encoded),
        msec_per_sample=msec_per_sample,
        start_unix_ms=start_unix_ms,
        variation_deg=variation_deg,
        proj4_len=len(proj4_bytes),
        file_size=30 + len(proj4_bytes)
                  + 12 * len(encoded)
                  + sum(2 + 2 * len(c.deltas) for c in encoded),
        columns=[c.spec.name for c in encoded],
        fallback_columns=[c.spec.name for c in encoded
                          if c.source_flag == SOURCE_FALLBACK],
    )


# ---- reader -----------------------------------------------------------------

ID_TO_NAME = {v: k for k, v in COL_ID.items()}
CODE_TO_SMOOTHER = {v: k for k, v in SMOOTHER_CODE.items()}


def read_slog(path: str) -> dict:
    """Parse a v2 `.slog` and decode to float64 columns.

    Returns:
        dict(
            magic, version, num_rows, num_cols, msec_per_sample,
            start_unix_ms, variation_deg, proj4,
            columns: list of per-column metadata dicts,
            data: {col_name: float64 array of length num_rows},
            source_flags: {col_name: 0|1},
        )
    """
    with open(path, 'rb') as f:
        raw = f.read()

    offset = 0
    (magic, version, num_cols, num_rows, msec_per_sample,
     start_unix_ms, variation_deg, proj4_len
     ) = HEADER_FIXED_STRUCT.unpack_from(raw, offset)
    offset += HEADER_FIXED_STRUCT.size
    if magic != MAGIC:
        raise ValueError(f'{path}: bad magic {magic:#x}, expected {MAGIC:#x}')
    if version != VERSION:
        raise ValueError(f'{path}: unsupported version {version}')

    proj4 = raw[offset:offset + proj4_len].decode('utf-8')
    offset += proj4_len

    columns_meta = []
    for _ in range(num_cols):
        (col_id, col_bits, col_smoother, col_source,
         col_min, col_max) = COL_BLOCK_STRUCT.unpack_from(raw, offset)
        offset += COL_BLOCK_STRUCT.size
        columns_meta.append(dict(
            col_id=col_id,
            name=ID_TO_NAME.get(col_id, f'unknown_{col_id}'),
            bits=col_bits,
            smoother_code=col_smoother,
            source_flag=col_source,
            min=col_min,
            max=col_max,
        ))

    data = {}
    source_flags = {}
    for meta in columns_meta:
        (first,) = struct.unpack_from('<H', raw, offset)
        offset += 2
        delta_bytes = raw[offset: offset + 2 * (num_rows - 1)]
        offset += 2 * (num_rows - 1)
        deltas = np.frombuffer(delta_bytes, dtype=np.int16)
        quantized = np.empty(num_rows, dtype=np.int32)
        quantized[0] = first
        if num_rows > 1:
            quantized[1:] = first + np.cumsum(deltas.astype(np.int32))
        bits = meta['bits']
        quant_max = (1 << bits) - 1
        span = meta['max'] - meta['min']
        if span == 0:
            decoded = np.full(num_rows, meta['min'], dtype=np.float64)
        else:
            decoded = quantized.astype(np.float64) / quant_max * span + meta['min']
        data[meta['name']] = decoded
        source_flags[meta['name']] = meta['source_flag']

    return dict(
        magic=magic,
        version=version,
        num_rows=num_rows,
        num_cols=num_cols,
        msec_per_sample=msec_per_sample,
        start_unix_ms=start_unix_ms,
        variation_deg=variation_deg,
        proj4=proj4,
        columns=columns_meta,
        data=data,
        source_flags=source_flags,
    )
