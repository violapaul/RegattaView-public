"""J/105 polar lookup table with bilinear interpolation.

Boat-speed table copied verbatim from ``j105_new_polars.csv`` (the same
source the upstream WaysWaterMoves polynomial fitter consumed). Bilinear
interpolation in the (TWA, TWS) plane preserves the published values at
every grid point, which a low-degree polynomial fit could not — most
visibly the deg-2 fit was 14% high downwind at TWS=6 and bent the upwind
VMGmax envelope down at TWS=20.

Domain of validity: TWA in [0, 180]°, TWS in [0, 20] kt. The published
table starts at 6 kt TWS; below that, speed is linearly interpolated from
0 kt at TWS=0 to the 6 kt table value. Outside the rectangle, lookup
returns NaN. TWA inputs are taken absolute, so callers can pass signed TWA
without thinking about the side.

Sign convention: ``vmg_kt`` returns the signed projection of velocity
along the wind axis with **upwind positive**, matching the
``upwind_vmg_kts`` channel produced by ``race_logs``.
"""
import numpy as np


TWA_GRID = np.array(
    [0, 32, 36, 40, 45, 52, 60, 75, 90, 110, 120, 135, 150, 160, 170, 180],
    dtype=float,
)
TWS_GRID = np.array([6, 8, 10, 12, 14, 16, 20], dtype=float)

# SPEED_TABLE_KT[i, j] = boat speed (kt) at (TWA_GRID[i], TWS_GRID[j]).
SPEED_TABLE_KT = np.array([
    [0.00, 0.00, 0.00, 0.00, 0.00, 0.00, 0.00],   # TWA  0
    [4.05, 4.85, 5.25, 5.45, 5.55, 5.60, 5.65],   # TWA 32
    [4.50, 5.35, 5.75, 5.95, 6.05, 6.10, 6.15],   # TWA 36
    [4.75, 5.65, 6.05, 6.25, 6.38, 6.48, 6.58],   # TWA 40
    [5.00, 5.90, 6.35, 6.60, 6.75, 6.85, 6.95],   # TWA 45
    [5.30, 6.20, 6.75, 7.05, 7.20, 7.30, 7.40],   # TWA 52
    [5.55, 6.50, 7.10, 7.45, 7.65, 7.75, 7.85],   # TWA 60
    [5.80, 6.80, 7.50, 7.95, 8.20, 8.45, 8.70],   # TWA 75
    [5.95, 7.00, 7.80, 8.35, 8.75, 9.15, 9.65],   # TWA 90
    [5.85, 6.95, 7.85, 8.60, 9.25, 9.85, 10.85],  # TWA 110
    [5.50, 6.65, 7.65, 8.55, 9.45, 10.40, 11.85], # TWA 120
    [4.70, 5.85, 6.85, 7.75, 8.70, 9.75, 12.05],  # TWA 135
    [3.95, 5.15, 6.10, 6.85, 7.65, 8.50, 10.35],  # TWA 150
    [3.60, 4.70, 5.65, 6.35, 7.05, 7.80, 9.20],   # TWA 160
    [3.35, 4.40, 5.35, 6.05, 6.70, 7.40, 8.65],   # TWA 170
    [3.15, 4.20, 5.15, 5.85, 6.50, 7.15, 8.35],   # TWA 180
], dtype=float)

TWA_DOMAIN = (float(TWA_GRID[0]), float(TWA_GRID[-1]))
TWS_TABLE_MIN = float(TWS_GRID[0])
TWS_DOMAIN = (0.0, float(TWS_GRID[-1]))

# Cubic fits for VMGmax(TWS) with the invariant VMGmax(0) = 0:
#     VMGmax(TWS) = TWS * polyval(_*_VMGMAX_PER_TWS_COEF, TWS)
# Coefficients are fit against the table-derived VMGmax values on TWS_GRID.
# This keeps the hot race-classifier reference path fast while preserving the
# table lookup for arbitrary TWA/TWS speed queries.
_UPWIND_VMGMAX_PER_TWS_COEF = np.array([
    0.00111388,
    -0.05470272,
    0.89974881,
])
_DOWNWIND_VMGMAX_PER_TWS_COEF = np.array([
    4.65392531e-04,
    -2.17151067e-02,
    6.92596066e-01,
])


def _twa_abs(twa_deg):
    return np.abs(np.asarray(twa_deg, dtype=float))


def _tws_array(tws_kt):
    return np.asarray(tws_kt, dtype=float)


def in_upwind_domain(twa_deg, tws_kt):
    twa = _twa_abs(twa_deg)
    tws = _tws_array(tws_kt)
    return (
        np.isfinite(twa) & np.isfinite(tws)
        & (twa >= 32.0) & (twa <= 90.0)
        & (tws >= TWS_DOMAIN[0]) & (tws <= TWS_DOMAIN[1])
    )


def in_downwind_domain(twa_deg, tws_kt):
    twa = _twa_abs(twa_deg)
    tws = _tws_array(tws_kt)
    return (
        np.isfinite(twa) & np.isfinite(tws)
        & (twa > 90.0) & (twa <= 180.0)
        & (tws >= TWS_DOMAIN[0]) & (tws <= TWS_DOMAIN[1])
    )


def in_domain(twa_deg, tws_kt):
    twa = _twa_abs(twa_deg)
    tws = _tws_array(tws_kt)
    return (
        np.isfinite(twa) & np.isfinite(tws)
        & (twa >= TWA_DOMAIN[0]) & (twa <= TWA_DOMAIN[1])
        & (tws >= TWS_DOMAIN[0]) & (tws <= TWS_DOMAIN[1])
    )


def _bilinear(twa_deg, tws_kt):
    twa = _twa_abs(twa_deg)
    tws = _tws_array(tws_kt)
    twa_b, tws_b = np.broadcast_arrays(twa, tws)
    valid = (
        np.isfinite(twa_b) & np.isfinite(tws_b)
        & (twa_b >= TWA_DOMAIN[0]) & (twa_b <= TWA_DOMAIN[1])
        & (tws_b >= TWS_DOMAIN[0]) & (tws_b <= TWS_DOMAIN[1])
    )
    twa_c = np.clip(np.where(valid, twa_b, TWA_GRID[0]), TWA_GRID[0], TWA_GRID[-1])
    low_tws = valid & (tws_b < TWS_TABLE_MIN)
    tws_lookup = np.where(low_tws, TWS_TABLE_MIN, tws_b)
    tws_c = np.clip(np.where(valid, tws_lookup, TWS_GRID[0]), TWS_GRID[0], TWS_GRID[-1])
    i = np.clip(np.searchsorted(TWA_GRID, twa_c, side='right') - 1, 0, len(TWA_GRID) - 2)
    j = np.clip(np.searchsorted(TWS_GRID, tws_c, side='right') - 1, 0, len(TWS_GRID) - 2)
    twa0 = TWA_GRID[i]
    twa1 = TWA_GRID[i + 1]
    tws0 = TWS_GRID[j]
    tws1 = TWS_GRID[j + 1]
    a = (twa_c - twa0) / (twa1 - twa0)
    b = (tws_c - tws0) / (tws1 - tws0)
    s00 = SPEED_TABLE_KT[i, j]
    s10 = SPEED_TABLE_KT[i + 1, j]
    s01 = SPEED_TABLE_KT[i, j + 1]
    s11 = SPEED_TABLE_KT[i + 1, j + 1]
    s = (1 - a) * (1 - b) * s00 + a * (1 - b) * s10 + (1 - a) * b * s01 + a * b * s11
    s = np.where(low_tws, s * (tws_b / TWS_TABLE_MIN), s)
    return np.where(valid, s, np.nan)


def speed_kt(twa_deg, tws_kt):
    """Boat speed (kt) by bilinear interpolation. NaN outside domain."""
    return _bilinear(twa_deg, tws_kt)


def upwind_speed_kt(twa_deg, tws_kt):
    return _bilinear(twa_deg, tws_kt)


def downwind_speed_kt(twa_deg, tws_kt):
    return _bilinear(twa_deg, tws_kt)


def vmg_kt(twa_deg, tws_kt):
    """Signed wind-axis VMG (upwind positive)."""
    twa = _twa_abs(twa_deg)
    return _bilinear(twa, tws_kt) * np.cos(np.deg2rad(twa))


def upwind_vmg_kt(twa_deg, tws_kt):
    """Upwind VMG (positive when sailing toward the wind source)."""
    twa = _twa_abs(twa_deg)
    return _bilinear(twa, tws_kt) * np.cos(np.deg2rad(twa))


def downwind_vmg_kt(twa_deg, tws_kt):
    """Downwind VMG magnitude (positive on TWA in (90, 180])."""
    twa = _twa_abs(twa_deg)
    return -_bilinear(twa, tws_kt) * np.cos(np.deg2rad(twa))


def _vmgmax_from_fit(tws_kt, coef):
    tws = _tws_array(tws_kt)
    scalar = tws.shape == ()
    tws_1d = np.atleast_1d(tws).astype(float)
    valid = np.isfinite(tws_1d) & (tws_1d >= TWS_DOMAIN[0]) & (tws_1d <= TWS_DOMAIN[1])
    out = tws_1d * np.polyval(coef, tws_1d)
    out = np.where(valid, out, np.nan)
    return float(out[0]) if scalar else out


def upwind_vmgmax_kt(tws_kt):
    return _vmgmax_from_fit(tws_kt, _UPWIND_VMGMAX_PER_TWS_COEF)


def downwind_vmgmax_kt(tws_kt):
    return _vmgmax_from_fit(tws_kt, _DOWNWIND_VMGMAX_PER_TWS_COEF)
