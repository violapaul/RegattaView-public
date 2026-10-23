"""Locked-down v1 binary `.log` writer/reader for sailview.

This module owns the v1 `.log` contract served under
`s3://regattaanalysis/www/Insta360/VID_*.log`: the dataframe column order,
unit conversions, and exact byte layout consumed by the web viewer.

Byte layout (little-endian):

    offset 0      : int32 num_rows
    offset 4      : int32 num_cols
    offset 8      : int32 msec_per_sample   (default 100)
    offset 12     : int32 multiplier        (default 65535)
    offset 16     : float64 max_values[num_cols]
    offset 16+8C  : float64 min_values[num_cols]
    offset 16+16C : uint16 payload[num_rows * num_cols], column-major
                    (Fortran order), so column j occupies rows consecutive
                    samples starting at offset 16 + 16C + 2 * num_rows * j.

The payload is quantized as
    quantized = round((raw - min) / (max - min) * multiplier)
then cast to uint16. For constant columns where max == min, the writer
bumps a local max by +1.0 during normalization so the division yields 0
cleanly; the stored `max_values[j]` keeps the original max (== min), and
the reader recovers `value = min` for every sample (range = 0 makes the
scale term vanish). This branch is critical for sessions where a column
is constant (e.g. `variation` per session).
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from Libraries.global_variables import G
from Libraries.log_helper import LOG


HEADER_DTYPE = np.int32
MAXMIN_DTYPE = np.float64
PAYLOAD_DTYPE = np.uint16

DEFAULT_MSEC_PER_SAMPLE = 100
DEFAULT_MULTIPLIER = 65535


# Columns emitted into the v1 web binary log. Must stay in lockstep with
# historical files already on S3 and any frontend index-to-name mappings.
# Append-only: do not reorder or remove existing entries.
COLUMN_LIST = ['row_seconds', 'spd', 'sog',
               'latitude', 'longitude', 'turn_rate',
               'rudder',  'depth', 'altitude',
               'zg100_pitch', 'zg100_roll',
               'awa', 'aws', 'twd', 'tws', 'twa',
               'hdg', 'cog', 'map_lon', 'map_lat',
               'row_times',
               # Authoritative magnetic variation for the session
               # (G.MAGNETIC_VARIATION, not stale on-board PGN 127258 values).
               'variation']

KNOTS_CONVERSION_LIST = ['spd', 'sog', 'aws', 'tws']


def knots_convert(df: pd.DataFrame) -> None:
    """Convert speed columns from meters per second to knots in place."""
    for field in KNOTS_CONVERSION_LIST:
        df[field] = df[field] * G.MS_2_KNOTS


def latlon_convert(df: pd.DataFrame) -> None:
    """Add projected map coordinates from latitude/longitude."""
    lon, lat = np.asarray(df.longitude), np.asarray(df.latitude)
    map_lon, map_lat = G.MAP(lon, lat)
    df['map_lon'] = map_lon
    df['map_lat'] = map_lat


def javascript_row_times(df: pd.DataFrame) -> None:
    """Convert timezone-aware row_times to JavaScript epoch milliseconds."""
    utc_not_localized = df.row_times.dt.tz_convert('UTC').dt.tz_localize(None)
    df['row_times'] = (
        utc_not_localized - pd.Timestamp("1970-01-01")
    ) // pd.Timedelta("1ms")


def df_convert(df: pd.DataFrame) -> pd.DataFrame:
    """Convert and order a processed log dataframe for v1 `.log` serialization."""
    cdf = df.copy()
    latlon_convert(cdf)
    cdf['variation'] = float(G.MAGNETIC_VARIATION)
    for column in COLUMN_LIST:
        if column not in cdf.columns:
            LOG.logger.warning(f"Missing columns {column}.")
            # The binary format has no schema sidecar per file; every historical
            # column index must be present, even if a source log lacks it.
            cdf[column] = 0.0
    knots_convert(cdf)
    javascript_row_times(cdf)
    return cdf[COLUMN_LIST]


def column_dict() -> dict[str, int]:
    """Return the v1 `.log` column-name to column-index mapping."""
    return {column: i for i, column in enumerate(COLUMN_LIST)}


def save_log_to_binary(df, log_file: str, msec_per_sample: int = DEFAULT_MSEC_PER_SAMPLE,
                       multiplier: int = DEFAULT_MULTIPLIER) -> None:
    """Compatibility wrapper for the historic writer name."""
    write_log_v1(df, log_file, msec_per_sample=msec_per_sample, multiplier=multiplier)


def write_log_v1(df, log_file: str, msec_per_sample: int = DEFAULT_MSEC_PER_SAMPLE,
                 multiplier: int = DEFAULT_MULTIPLIER) -> None:
    """Serialize `df` into the v1 `.log` binary at `log_file`.

    Bytes written are byte-identical to the historic v1 writer output for any
    given dataframe and settings.
    """
    rows, cols = df.shape
    header = np.array(
        [rows, cols, msec_per_sample, multiplier], dtype=HEADER_DTYPE)

    values = df.values
    max_values = np.max(values, axis=0).astype(MAXMIN_DTYPE)
    min_values = np.min(values, axis=0).astype(MAXMIN_DTYPE)

    # Guard against constant columns (max == min): the sailview reader
    # computes every scaled value as `(uint16 * range / multiplier) + min`,
    # so a zero range yields `min` for every sample — correct. We bump the
    # local normalization max to `min + 1.0` so the normalization produces
    # a well-defined 0 without dividing by zero / emitting NaNs.
    max_for_norm = np.where(max_values == min_values, min_values + 1.0, max_values)
    normalized = (values - min_values) / (max_for_norm - min_values)
    quantized = (normalized * multiplier).astype(PAYLOAD_DTYPE)

    with open(log_file, 'wb') as fs:
        fs.write(header.tobytes())
        fs.write(max_values.tobytes())
        fs.write(min_values.tobytes())
        # Column-major (Fortran) so column j is contiguous on disk — matches
        # the sailview reader's per-column slice.
        fs.write(quantized.tobytes('F'))


def read_log_v1(log_file: str) -> dict:
    """Parse a v1 `.log` and return its header plus per-column float64 values.

    Returns a dict with keys:
        num_rows, num_cols, msec_per_sample, multiplier  -- ints
        max_values, min_values                           -- float64 arrays (num_cols,)
        data                                             -- dict view keyed by column
                                                            index, each a float64 array
                                                            of length num_rows
        raw                                              -- uint16 (num_rows, num_cols)
                                                            row-major view of the payload
    """
    with open(log_file, 'rb') as fs:
        header = np.frombuffer(fs.read(16), dtype=HEADER_DTYPE)
        num_rows, num_cols, msec_per_sample, multiplier = (int(x) for x in header)

        max_values = np.frombuffer(fs.read(8 * num_cols), dtype=MAXMIN_DTYPE).copy()
        min_values = np.frombuffer(fs.read(8 * num_cols), dtype=MAXMIN_DTYPE).copy()

        payload_bytes = fs.read(num_rows * num_cols * 2)
    if len(payload_bytes) != num_rows * num_cols * 2:
        raise ValueError(
            f'{log_file}: truncated payload — expected {num_rows * num_cols * 2} '
            f'bytes, got {len(payload_bytes)}')

    # The payload is column-major: column j lives at [2*num_rows*j : 2*num_rows*(j+1)].
    # Reshape (cols, rows) then transpose to (rows, cols).
    raw = np.frombuffer(payload_bytes, dtype=PAYLOAD_DTYPE).reshape(num_cols, num_rows).T.copy()

    # Decode back to float64 using the stored (non-normalization) max/min.
    # For constant columns (max == min) the range is 0 and every sample
    # resolves to min, matching the sailview JS reader.
    ranges = (max_values - min_values).astype(MAXMIN_DTYPE)
    data_2d = raw.astype(MAXMIN_DTYPE) * ranges / float(multiplier) + min_values

    return dict(
        num_rows=num_rows,
        num_cols=num_cols,
        msec_per_sample=msec_per_sample,
        multiplier=multiplier,
        max_values=max_values,
        min_values=min_values,
        data={j: data_2d[:, j].copy() for j in range(num_cols)},
        raw=raw,
    )
