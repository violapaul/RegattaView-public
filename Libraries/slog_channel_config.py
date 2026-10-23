"""Frozen v2 `.slog` channel configuration.

Output of Stage A smoothing experiments on 2026-04-19 parquet data (see
`Analyze/Plots/slog_experiments/20260419/`). The v2 `.slog` writer
(`Libraries/sailview_log_v2.py`, Stage D) consumes this declarative spec.

Key design choices baked in:
- Zero-phase `filtfilt` smoothing everywhere that smoothing is applied. The
  v1 pipeline's causal exponential filter was measured to trail raw by 2-3°
  on angles and ~0.1 kt on speed through a tack ("old signals" complaint);
  filtfilt has zero phase lag by construction because the `.slog` is
  produced offline from parquet.
- `awa` uses cutoff 0.3 Hz so that genuine wind-shift dynamics survive
  while boat-roll-induced ripples are attenuated; 0.2 Hz was too smooth
  and flattened real shifts.
- `twd`/`tws` ship the **on-boat NMEA PGN 130306** values (`rtwd`/`rtws`)
  because that matches the helmsman's instrument readout during the race.
  The Python `estimate_true_wind(ε=1e-4)` path (default `twd`/`tws` in the
  parquet) produced phantom 14° shifts through tacks and is explicitly
  rejected. When `rtwd`/`rtws` are absent (the autopilot is the only node
  that places them on the bus, and may not be active for a session), we
  fall back to the ε=1e-5 snapshot `stwd`/`stws` — slower but stable.
- `rtwd` carries the boat's stale magnetic variation bias (~1° on Peer
  Gynt per the venue note). We correct it by adding
  `(G.MAGNETIC_VARIATION − median(df.variation))` before smoothing.
- `gtwd`/`gtws`/`gtwa` are the explicit physically calibrated
  ground-referenced wind product. They are appended to the schema so existing
  `twd` semantics remain instrument-compatible.
- 12-bit quantization for sensor channels (0.09° on a 360° angle, 0.005 kt
  on a 20 kt range — well past sensor accuracy); 16-bit for lat/lon
  (sub-meter positional fidelity matters for the track).
"""

from dataclasses import dataclass, field
from typing import Optional, Union


# ---- smoother specs ---------------------------------------------------------

@dataclass(frozen=True)
class Raw:
    """Pass through unchanged."""


@dataclass(frozen=True)
class Filtfilt:
    """Zero-phase Butterworth lowpass via `scipy.signal.filtfilt`.

    Consumer: `Libraries/process.smooth(butterworth_filter(cutoff, order), sig, causal=False)`.
    """
    cutoff_hz: float
    order: int = 3


@dataclass(frozen=True)
class FiltfiltAngle:
    """Zero-phase lowpass for angle channels; handles 360° wrap.

    Consumer: `Libraries/process.smooth_angle(butterworth_filter(cutoff, order), sig, causal=False)`.
    """
    cutoff_hz: float
    order: int = 3


@dataclass(frozen=True)
class MedfiltThenFiltfilt:
    """Median prefilter (outlier removal) then zero-phase lowpass. For lat/lon."""
    medfilt_width: int
    cutoff_hz: float
    order: int = 3


Smoother = Union[Raw, Filtfilt, FiltfiltAngle, MedfiltThenFiltfilt]


# ---- channel spec -----------------------------------------------------------

@dataclass(frozen=True)
class ChannelSpec:
    """Declarative recipe for one v2 `.slog` column.

    Writer behaviour (see Stage D `Libraries/sailview_log_v2.py`):
      1. Select source: if `df[source_primary].isna().mean() <= primary_nan_frac_max`,
         use `source_primary`; otherwise (and if `source_fallback` is set) use
         `source_fallback`. Record which one was used in the file header's
         `source` enum.
      2. If `variation_correction` is set, add the per-session offset
         `G.MAGNETIC_VARIATION − median(df['variation'])` to the selected
         source (angle channels only).
      3. If `convert_to_knots` is set, multiply by `G.MS_2_KNOTS`. Parquet
         stores speed channels in m/s; sailview/review expect knots (v1
         `.log` applies `sailview_log_v1.knots_convert` before writing —
         this flag replicates that for v2).
      4. Apply `smoother`.
      5. Quantize to `bits`.
    """
    name: str
    kind: str                               # 'scalar' | 'angle'
    source_primary: str
    smoother: Smoother
    bits: int
    source_fallback: Optional[str] = None
    primary_nan_frac_max: float = 0.2       # use fallback when > this fraction is NaN
    variation_correction: bool = False
    convert_to_knots: bool = False


# ---- frozen v2 channel set --------------------------------------------------

V2_COLUMNS: tuple[ChannelSpec, ...] = (
    ChannelSpec('spd',        'scalar', 'rspd',  Filtfilt(0.2),        bits=12,
                convert_to_knots=True),
    ChannelSpec('sog',        'scalar', 'rsog',  Filtfilt(0.2),        bits=12,
                convert_to_knots=True),
    ChannelSpec('awa',        'angle',  'rawa',  FiltfiltAngle(0.3),   bits=12),
    ChannelSpec('aws',        'scalar', 'raws',  Filtfilt(0.2),        bits=12,
                convert_to_knots=True),
    ChannelSpec('hdg',        'angle',  'rhdg',  FiltfiltAngle(0.2),   bits=12),
    ChannelSpec('cog',        'angle',  'rcog',  FiltfiltAngle(0.2),   bits=12),
    ChannelSpec('twd',        'angle',  'rtwd',  FiltfiltAngle(0.2),   bits=12,
                source_fallback='stwd', variation_correction=True),
    ChannelSpec('tws',        'scalar', 'rtws',  Filtfilt(0.2),        bits=12,
                source_fallback='stws', convert_to_knots=True),
    ChannelSpec('zg100_roll', 'scalar', 'zg100_roll', Filtfilt(0.3),   bits=12),
    ChannelSpec('rudder',     'scalar', 'rudder',     Raw(),           bits=12),
    ChannelSpec('depth',      'scalar', 'depth',      Raw(),           bits=12),
    ChannelSpec('latitude',   'scalar', 'latitude',   MedfiltThenFiltfilt(5, 0.5), bits=16),
    ChannelSpec('longitude',  'scalar', 'longitude',  MedfiltThenFiltfilt(5, 0.5), bits=16),
    ChannelSpec('gtwd',       'angle',  'gtwd',       FiltfiltAngle(0.2), bits=12),
    ChannelSpec('gtws',       'scalar', 'gtws',       Filtfilt(0.2),      bits=12,
                convert_to_knots=True),
    ChannelSpec('gtwa',       'angle',  'gtwa',       FiltfiltAngle(0.2), bits=12),
)


# Stable numeric IDs for the header's `col_id` byte. Append-only — do not
# renumber. Unknown IDs are ignored by readers.
COL_ID: dict[str, int] = {
    'spd': 0,
    'sog': 1,
    'awa': 2,
    'aws': 3,
    'hdg': 4,
    'cog': 5,
    'twd': 6,
    'tws': 7,
    'zg100_roll': 8,
    'rudder': 9,
    'depth': 10,
    'latitude': 11,
    'longitude': 12,
    'gtwd': 13,
    'gtws': 14,
    'gtwa': 15,
}
