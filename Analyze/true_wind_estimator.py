"""Ground-referenced true-wind estimator.

Vector-state (wind_north, wind_east) Kalman filter with an offline RTS
smoother. Supersedes the earlier Gauss-Newton / gradient-descent
helpers; the math review at ``Analyze/estimator_math_review.md``
explains why the vector-state formulation is preferred.

Supplied by the user on 2026-04-22 as a replacement for both the
in-repo gradient-descent solver (``Libraries/race_logs.py::
estimate_true_wind_helper``) and the Gauss-Newton rewrite in Paul's
personal tree (``~/Resilio Sync/Dropbox/Python/sailing/estimator.py``).
Saved verbatim below the horizontal rule; a small ``instantaneous_water_wind``
wrapper is appended above the main section for the narrative-plot
shear comparison (same math, SPD + HDG+variation substituted for SOG/COG).

Expected input DataFrame columns:
    awa, aws, hdg, sog, cog              (ground-referenced path)
    spd  (optional, only for water-ref)  (water-referenced path)

Angles in **compass degrees** (0 = N, positive clockwise). Speeds in
**knots** for tuning to match the `Calibration` dataclass defaults
(``measurement_std_kn``, ``process_std_kn_per_s``) which carry knot
units in their names. Convert parquet's m/s columns via
``G.MS_2_KNOTS`` before calling.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Optional, Sequence, Tuple, List, Dict, Any

import numpy as np
import pandas as pd
from scipy.optimize import minimize

try:
    from numba import njit
except ImportError:  # pragma: no cover - Numba is an env requirement
    # Fallback: pass-through decorator so the module still imports.
    def njit(*args, **kwargs):
        if len(args) == 1 and callable(args[0]) and not kwargs:
            return args[0]
        def _wrap(fn):
            return fn
        return _wrap


# ---------------------------
# Angle / vector helpers
# ---------------------------

def wrap180(angle_deg: np.ndarray | float) -> np.ndarray | float:
    return (np.asarray(angle_deg) + 180.0) % 360.0 - 180.0


def wrap360(angle_deg: np.ndarray | float) -> np.ndarray | float:
    return np.asarray(angle_deg) % 360.0


def sind(angle_deg: np.ndarray | float) -> np.ndarray | float:
    return np.sin(np.deg2rad(angle_deg))


def cosd(angle_deg: np.ndarray | float) -> np.ndarray | float:
    return np.cos(np.deg2rad(angle_deg))


def atan2d(y: np.ndarray | float, x: np.ndarray | float) -> np.ndarray | float:
    return np.rad2deg(np.arctan2(y, x))


def polar_to_ne(speed: np.ndarray, direction_deg: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """
    Nautical convention:
      - direction_deg is degrees clockwise from North
      - returns north/east components
    """
    n = speed * cosd(direction_deg)
    e = speed * sind(direction_deg)
    return n, e


def ne_to_polar(north: np.ndarray, east: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    speed = np.hypot(north, east)
    direction_deg = wrap360(atan2d(east, north))
    return speed, direction_deg


def rotate_boat_to_earth(
    forward: np.ndarray, starboard: np.ndarray, hdg_true_deg: np.ndarray
) -> Tuple[np.ndarray, np.ndarray]:
    """
    Boat frame:
      forward   = bow-positive
      starboard = starboard-positive

    Earth frame:
      north/east, heading in nautical degrees (clockwise from North)
    """
    north = forward * cosd(hdg_true_deg) - starboard * sind(hdg_true_deg)
    east = forward * sind(hdg_true_deg) + starboard * cosd(hdg_true_deg)
    return north, east


def centered_boxcar(x: np.ndarray, window: int) -> np.ndarray:
    if window <= 1:
        return np.asarray(x, dtype=float).copy()
    x = np.asarray(x, dtype=float)
    mask = np.isfinite(x).astype(float)
    xx = np.where(np.isfinite(x), x, 0.0)
    kernel = np.ones(window, dtype=float)
    num = np.convolve(xx, kernel, mode="same")
    den = np.convolve(mask, kernel, mode="same")
    out = np.divide(num, den, out=np.full_like(num, np.nan), where=den > 0)
    return out


def centered_angle_boxcar(angle_deg: np.ndarray, window: int) -> np.ndarray:
    a = np.deg2rad(np.asarray(angle_deg, dtype=float))
    s = centered_boxcar(np.sin(a), window)
    c = centered_boxcar(np.cos(a), window)
    return wrap180(np.rad2deg(np.arctan2(s, c)))


def fractional_lead(x: np.ndarray, lead_samples: float) -> np.ndarray:
    """
    lead_samples > 0 means "use future samples at the current time index".

    If GPS is delayed by +1.2 s relative to AWA/AWS/HDG, and fs=10 Hz,
    use lead_samples=12.0 to align GPS to the fast sensors offline.
    """
    x = np.asarray(x, dtype=float)
    idx = np.arange(len(x), dtype=float)
    return np.interp(idx + lead_samples, idx, x, left=x[0], right=x[-1])


def huber_scalar(x: float, delta: float) -> float:
    ax = abs(float(x))
    if ax <= delta:
        return 0.5 * ax * ax
    return delta * (ax - 0.5 * delta)


# ---------------------------
# Calibration parameters
# ---------------------------

@dataclass(frozen=True)
class Calibration:
    """
    Minimal parameterization for a first-pass true-wind estimator.

    heading_offset_deg:
        Add this to HDG before using it. This can absorb variation and
        sensor alignment. It is NOT identifiable from tack-constancy alone,
        so keep it fixed unless you have an external reference.

    awa_offset_deg:
        Global vane zero offset.

    awa_scale_upwind / awa_scale_downwind:
        Empirical angular distortion correction. The notebook you uploaded
        suggests that upwind and downwind can behave differently. Start
        with only the upwind term free.

    aws_scale_upwind / aws_scale_downwind:
        Empirical speed correction. Same comment as above.

    gps_delay_s:
        Delay of SOG/COG relative to AWA/AWS/HDG. Positive means GPS is late.

    stw_scale / stw_delay_s:
        Only used if you later switch to a water-referenced model.
        The code below focuses on the better-conditioned ground-referenced case.
    """
    heading_offset_deg: float = 0.0
    awa_offset_deg: float = 0.0
    awa_scale_upwind: float = 1.0
    awa_scale_downwind: float = 1.0
    aws_scale_upwind: float = 1.0
    aws_scale_downwind: float = 1.0
    gps_delay_s: float = 0.0
    stw_scale: float = 1.0
    stw_delay_s: float = 0.0


def corrected_awa_aws(
    awa_deg: np.ndarray, aws: np.ndarray, cal: Calibration
) -> Tuple[np.ndarray, np.ndarray]:
    awa_deg = np.asarray(awa_deg, dtype=float)
    aws = np.asarray(aws, dtype=float)

    upwind = np.abs(wrap180(awa_deg)) <= 90.0
    awa_scale = np.where(upwind, cal.awa_scale_upwind, cal.awa_scale_downwind)
    aws_scale = np.where(upwind, cal.aws_scale_upwind, cal.aws_scale_downwind)

    awa_corr = wrap180(awa_deg * awa_scale + cal.awa_offset_deg)
    aws_corr = aws * aws_scale
    return awa_corr, aws_corr


# ---------------------------
# Ground-wind measurement model
# ---------------------------

REQUIRED_COLUMNS = ("awa", "aws", "hdg", "sog", "cog")


def instantaneous_ground_wind(
    df: pd.DataFrame,
    fs_hz: float,
    cal: Calibration,
    align_gps_offline: bool = True,
) -> pd.DataFrame:
    """
    Compute the instantaneous ground-referenced wind vector implied by the sensors.

    This is the robust quantity to estimate first because it avoids the
    current/leeway ambiguity:
        W_ground = R(hdg_true) @ AW_boat - V_ground

    The output is still noisy. Use `offline_ground_wind_rts` or the causal
    filter below on top of this.
    """
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"DataFrame missing columns: {missing}")

    awa_corr, aws_corr = corrected_awa_aws(df["awa"].to_numpy(), df["aws"].to_numpy(), cal)
    hdg_true = wrap360(df["hdg"].to_numpy(dtype=float) + cal.heading_offset_deg)

    aw_forward = aws_corr * cosd(awa_corr)
    aw_starboard = aws_corr * sind(awa_corr)
    aw_north, aw_east = rotate_boat_to_earth(aw_forward, aw_starboard, hdg_true)

    boat_north, boat_east = polar_to_ne(df["sog"].to_numpy(dtype=float), df["cog"].to_numpy(dtype=float))
    if align_gps_offline and abs(cal.gps_delay_s) > 1e-12:
        lead = cal.gps_delay_s * fs_hz
        boat_north = fractional_lead(boat_north, lead)
        boat_east = fractional_lead(boat_east, lead)

    wind_north = aw_north - boat_north
    wind_east = aw_east - boat_east
    tws_inst, twd_inst = ne_to_polar(wind_north, wind_east)
    twa_inst = wrap180(twd_inst - hdg_true)

    out = pd.DataFrame(index=df.index)
    out["aw_north"] = aw_north
    out["aw_east"] = aw_east
    out["boat_north"] = boat_north
    out["boat_east"] = boat_east
    out["wind_north_inst"] = wind_north
    out["wind_east_inst"] = wind_east
    out["tws_inst"] = tws_inst
    out["twd_inst"] = twd_inst
    out["twa_inst"] = twa_inst
    out["awa_corr"] = awa_corr
    out["aws_corr"] = aws_corr
    out["hdg_true"] = hdg_true
    return out


def instantaneous_water_wind(
    df: pd.DataFrame,
    fs_hz: float,
    cal: Calibration,
) -> pd.DataFrame:
    """Companion to :func:`instantaneous_ground_wind` using water-referenced motion.

    Substitutes ``V_water = SPD * (cos(hdg_true), sin(hdg_true))`` for the
    ground-referenced ``V_ground``. This is the B&G-style computation (see
    ``Analyze/estimator_math_review.md`` §6): the resulting "true wind" is
    actually ``W_true - C`` where ``C`` is the water current, so it carries
    a current-dependent bias and (due to boat inertia at tacks) a transient
    shear. Useful as a reference line on the shear plot.

    Expects a ``spd`` column. If the DataFrame only has ``rspd`` (parquet
    naming), rename or alias upstream.
    """
    if 'spd' not in df.columns:
        raise ValueError("instantaneous_water_wind needs a 'spd' column (m/s or knots, caller's convention)")

    awa_corr, aws_corr = corrected_awa_aws(df['awa'].to_numpy(), df['aws'].to_numpy(), cal)
    hdg_true = wrap360(df['hdg'].to_numpy(dtype=float) + cal.heading_offset_deg)

    aw_forward = aws_corr * cosd(awa_corr)
    aw_starboard = aws_corr * sind(awa_corr)
    aw_north, aw_east = rotate_boat_to_earth(aw_forward, aw_starboard, hdg_true)

    # Water-referenced boat velocity: SPD along HDG_true.
    spd = df['spd'].to_numpy(dtype=float) * cal.stw_scale
    boat_north = spd * cosd(hdg_true)
    boat_east = spd * sind(hdg_true)
    if abs(cal.stw_delay_s) > 1e-12:
        lead = cal.stw_delay_s * fs_hz
        boat_north = fractional_lead(boat_north, lead)
        boat_east = fractional_lead(boat_east, lead)

    wind_north = aw_north - boat_north
    wind_east = aw_east - boat_east
    tws_inst, twd_inst = ne_to_polar(wind_north, wind_east)
    twa_inst = wrap180(twd_inst - hdg_true)

    out = pd.DataFrame(index=df.index)
    out['aw_north'] = aw_north
    out['aw_east'] = aw_east
    out['boat_north'] = boat_north
    out['boat_east'] = boat_east
    out['wind_north_inst'] = wind_north
    out['wind_east_inst'] = wind_east
    out['tws_inst'] = tws_inst
    out['twd_inst'] = twd_inst
    out['twa_inst'] = twa_inst
    out['awa_corr'] = awa_corr
    out['aws_corr'] = aws_corr
    out['hdg_true'] = hdg_true
    return out


# ---------------------------
# Causal random-walk filter
# ---------------------------

@dataclass
class RandomWalkFilterConfig:
    measurement_std_kn: float = 0.60
    process_std_kn_per_s: float = 0.10
    initial_state_std_kn: float = 8.0


class CausalTrueWindFilter:
    """
    Streaming, causal estimator.

    Important limitation:
      If GPS is delayed, an exact causal estimator cannot produce a perfectly
      aligned estimate at the current timestamp without a forward prediction model.
      This class handles that cleanly by buffering the fast channels and returning
      an estimate for an earlier timestamp once the delayed GPS sample arrives.

    In other words: causal and delay-compensated, but the output is delayed by
    roughly `gps_delay_s`.
    """
    def __init__(
        self,
        fs_hz: float,
        cal: Calibration,
        config: RandomWalkFilterConfig | None = None,
    ) -> None:
        self.fs_hz = float(fs_hz)
        self.cal = cal
        self.config = config or RandomWalkFilterConfig()
        self.delay_samples = int(round(max(0.0, cal.gps_delay_s) * self.fs_hz))

        self._fast_buffer: List[Dict[str, float]] = []
        self._x: Optional[np.ndarray] = None
        self._P: np.ndarray = np.eye(2) * (self.config.initial_state_std_kn ** 2)

        dt = 1.0 / self.fs_hz
        self._Q = np.eye(2) * (self.config.process_std_kn_per_s * dt) ** 2
        self._R = np.eye(2) * (self.config.measurement_std_kn ** 2)

    def _measurement_from_sample(
        self, awa: float, aws: float, hdg: float, sog: float, cog: float
    ) -> Tuple[float, float, float]:
        awa_corr, aws_corr = corrected_awa_aws(np.array([awa]), np.array([aws]), self.cal)
        awa_corr = float(awa_corr[0])
        aws_corr = float(aws_corr[0])

        hdg_true = float(wrap360(hdg + self.cal.heading_offset_deg))

        aw_forward = aws_corr * float(cosd(awa_corr))
        aw_starboard = aws_corr * float(sind(awa_corr))
        aw_north, aw_east = rotate_boat_to_earth(
            np.array([aw_forward]), np.array([aw_starboard]), np.array([hdg_true])
        )
        aw_north = float(aw_north[0])
        aw_east = float(aw_east[0])

        boat_north, boat_east = polar_to_ne(np.array([sog], dtype=float), np.array([cog], dtype=float))
        wind_north = aw_north - float(boat_north[0])
        wind_east = aw_east - float(boat_east[0])
        return wind_north, wind_east, hdg_true

    def update(
        self,
        timestamp: Any,
        awa: float,
        aws: float,
        hdg: float,
        sog: float,
        cog: float,
    ) -> Optional[Dict[str, float]]:
        self._fast_buffer.append(
            {"timestamp": timestamp, "awa": awa, "aws": aws, "hdg": hdg}
        )

        if len(self._fast_buffer) <= self.delay_samples:
            return None

        fast = self._fast_buffer.pop(0)
        z_n, z_e, hdg_true = self._measurement_from_sample(
            awa=float(fast["awa"]),
            aws=float(fast["aws"]),
            hdg=float(fast["hdg"]),
            sog=float(sog),
            cog=float(cog),
        )

        z = np.array([z_n, z_e], dtype=float)
        if self._x is None:
            self._x = z.copy()

        # Predict
        x_pred = self._x
        P_pred = self._P + self._Q

        # Update
        y = z - x_pred
        S = P_pred + self._R
        K = P_pred @ np.linalg.inv(S)
        self._x = x_pred + K @ y
        self._P = (np.eye(2) - K) @ P_pred

        tws, twd = ne_to_polar(np.array([self._x[0]]), np.array([self._x[1]]))
        twa = wrap180(twd - hdg_true)

        return {
            "timestamp": fast["timestamp"],
            "wind_north": float(self._x[0]),
            "wind_east": float(self._x[1]),
            "tws": float(tws[0]),
            "twd": float(twd[0]),
            "twa": float(twa[0]),
            "hdg_true": hdg_true,
        }


def run_causal_filter_on_dataframe(
    df: pd.DataFrame,
    fs_hz: float,
    cal: Calibration,
    config: RandomWalkFilterConfig | None = None,
) -> pd.DataFrame:
    filt = CausalTrueWindFilter(fs_hz=fs_hz, cal=cal, config=config)
    out = pd.DataFrame(index=df.index, columns=["wind_north", "wind_east", "tws", "twd", "twa", "hdg_true"], dtype=float)

    for idx, row in df.iterrows():
        res = filt.update(
            timestamp=idx,
            awa=float(row["awa"]),
            aws=float(row["aws"]),
            hdg=float(row["hdg"]),
            sog=float(row["sog"]),
            cog=float(row["cog"]),
        )
        if res is None:
            continue
        out.loc[res["timestamp"], ["wind_north", "wind_east", "tws", "twd", "twa", "hdg_true"]] = [
            res["wind_north"],
            res["wind_east"],
            res["tws"],
            res["twd"],
            res["twa"],
            res["hdg_true"],
        ]
    return out


# ---------------------------
# Offline RTS smoother
# ---------------------------

@dataclass
class OfflineSmootherConfig:
    measurement_std_kn: float = 0.60
    process_std_kn_per_s: float = 0.08
    initial_state_std_kn: float = 8.0


def _kalman_filter_random_walk(
    z: np.ndarray,
    fs_hz: float,
    config: OfflineSmootherConfig,
) -> Dict[str, np.ndarray]:
    n = z.shape[0]
    x_filt = np.zeros((n, 2), dtype=float)
    P_filt = np.zeros((n, 2, 2), dtype=float)
    x_pred = np.zeros((n, 2), dtype=float)
    P_pred = np.zeros((n, 2, 2), dtype=float)

    dt = 1.0 / float(fs_hz)
    Q = np.eye(2) * (config.process_std_kn_per_s * dt) ** 2
    R_full = np.eye(2) * (config.measurement_std_kn ** 2)

    x = z[0].copy()
    if not np.all(np.isfinite(x)):
        x = np.zeros(2, dtype=float)
    P = np.eye(2) * (config.initial_state_std_kn ** 2)

    for i in range(n):
        if i == 0:
            xp = x.copy()
            Pp = P.copy()
        else:
            xp = x.copy()
            Pp = P + Q

        x_pred[i] = xp
        P_pred[i] = Pp

        mask = np.isfinite(z[i])
        if not np.any(mask):
            x = xp
            P = Pp
        else:
            H = np.eye(2)[mask]
            zi = z[i, mask]
            Ri = R_full[np.ix_(mask, mask)]
            innov = zi - H @ xp
            S = H @ Pp @ H.T + Ri
            K = Pp @ H.T @ np.linalg.inv(S)
            x = xp + K @ innov
            P = (np.eye(2) - K @ H) @ Pp

        x_filt[i] = x
        P_filt[i] = P

    return {
        "x_filt": x_filt,
        "P_filt": P_filt,
        "x_pred": x_pred,
        "P_pred": P_pred,
    }


@njit(cache=True)
def _kalman_rts_rw_numba(
    z_n: np.ndarray,
    z_e: np.ndarray,
    dt: float,
    q_var: float,
    r_var: float,
    p0_var: float,
    x0_n: float,
    x0_e: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """Numba-jitted scalar random-walk Kalman filter + RTS smoother.

    Because Q and R are scalar-diagonal in
    ``_kalman_filter_random_walk``/``_rts_smoother_random_walk``, the
    2-D random-walk factors into two independent scalar Kalmans sharing
    the same scalar covariance ``p``.  ``q_var`` should be
    ``(process_std_kn_per_s * dt) ** 2`` and ``r_var``
    ``measurement_std_kn ** 2``; ``p0_var`` is ``initial_state_std_kn ** 2``.

    Returns ``(x_smooth_n, x_smooth_e)``.  NaN measurement samples are
    treated as missing (prediction-only update).
    """
    n = z_n.shape[0]
    x_filt_n = np.zeros(n)
    x_filt_e = np.zeros(n)
    p_filt = np.zeros(n)
    x_pred_n = np.zeros(n)
    x_pred_e = np.zeros(n)
    p_pred = np.zeros(n)

    # Initial state (caller supplies; usually first valid measurement).
    xn = x0_n
    xe = x0_e
    p = p0_var

    for i in range(n):
        # Predict (at i==0 there is no process noise added, matching the
        # non-jitted version's i==0 branch).
        if i == 0:
            xpn = xn
            xpe = xe
            pp = p
        else:
            xpn = xn
            xpe = xe
            pp = p + q_var

        x_pred_n[i] = xpn
        x_pred_e[i] = xpe
        p_pred[i] = pp

        zn = z_n[i]
        ze = z_e[i]
        zn_ok = not np.isnan(zn)
        ze_ok = not np.isnan(ze)

        if zn_ok and ze_ok:
            s = pp + r_var
            k = pp / s
            xn = xpn + k * (zn - xpn)
            xe = xpe + k * (ze - xpe)
            p = (1.0 - k) * pp
        elif zn_ok:
            s = pp + r_var
            k = pp / s
            xn = xpn + k * (zn - xpn)
            xe = xpe
            p = (1.0 - k) * pp
        elif ze_ok:
            s = pp + r_var
            k = pp / s
            xn = xpn
            xe = xpe + k * (ze - xpe)
            p = (1.0 - k) * pp
        else:
            xn = xpn
            xe = xpe
            p = pp

        x_filt_n[i] = xn
        x_filt_e[i] = xe
        p_filt[i] = p

    # RTS backward pass.
    x_smooth_n = np.empty(n)
    x_smooth_e = np.empty(n)
    x_smooth_n[n - 1] = x_filt_n[n - 1]
    x_smooth_e[n - 1] = x_filt_e[n - 1]

    for i in range(n - 2, -1, -1):
        c = p_filt[i] / p_pred[i + 1]
        x_smooth_n[i] = x_filt_n[i] + c * (x_smooth_n[i + 1] - x_pred_n[i + 1])
        x_smooth_e[i] = x_filt_e[i] + c * (x_smooth_e[i + 1] - x_pred_e[i + 1])

    return x_smooth_n, x_smooth_e


# ---------------------------
# Adaptive forward filter + RTS (tack-gated R, shift-tracking Q, Huber clip)
# ---------------------------


@dataclass
class AdaptiveFilterConfig:
    """Configuration for :func:`offline_ground_wind_adaptive` and
    :func:`offline_water_wind_adaptive`.

    The three nonlinearities (dynamic R, dynamic Q, Huber clip) compose:
    Huber handles 1-3 sample outliers, R-inflation handles 5-15 s tack
    transients, Q-inflation handles persistent (>~10 s) elevated innovation
    from real wind shifts. Defaults are educated guesses; tune in
    ``Analyze/adaptive_eval.py``.
    """
    # Base noise variances (same as existing OfflineSmootherConfig).
    measurement_std_kn: float = 0.60
    process_std_kn_per_s: float = 0.08
    initial_state_std_kn: float = 8.0

    # R-inflation (tack gate).
    alpha_R_turn: float = 30.0     # turn-rate sensitivity (unit: (deg/s / 10)^2)
    alpha_R_accel: float = 5.0     # speed-change sensitivity (unit: (kn/s / 1.0)^2)
    r_scale_max: float = 20.0      # cap

    # Q-inflation (shift tracking).
    alpha_innov_ewm: float = 0.03   # EWMA bandwidth (1 / 33 samples ~= 3 s at 10 Hz)
    shift_threshold_kn2: float = 2.0  # triggers Q inflation when innov_ewm exceeds this
    q_growth: float = 1.05          # per-sample multiplier when over threshold
    q_decay: float = 0.98           # per-sample multiplier when under threshold
    q_scale_max: float = 50.0       # cap

    # Huber outlier.
    huber_delta: float = 1.5        # in units of sigma


def _linear_interp_nan(a: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """Return (filled, was_nan) where NaN positions have been linearly
    interpolated using np.interp over valid samples. If too few valid
    samples, fills with zeros.
    """
    a = np.asarray(a, dtype=float)
    was_nan = ~np.isfinite(a)
    if was_nan.all():
        return np.zeros_like(a), was_nan
    if not was_nan.any():
        return a.copy(), was_nan
    idx = np.arange(len(a), dtype=float)
    good = ~was_nan
    filled = a.copy()
    filled[was_nan] = np.interp(idx[was_nan], idx[good], a[good])
    return filled, was_nan


def _compute_boat_dynamics(
    hdg_deg_arr: np.ndarray,
    spd_kn_arr: np.ndarray,
    fs_hz: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """Return (turn_rate_dps, accel_kn_s) as same-length 1D arrays.

    - ``turn_rate_dps`` is the derivative of the unwrapped heading (radians)
      scaled back to degrees-per-second, with a 5-sample centered boxcar
      applied to attenuate compass jitter.
    - ``accel_kn_s`` is the derivative of ``spd_kn_arr`` in knots-per-second,
      also 5-sample boxcar smoothed.

    NaNs are linearly interpolated before ``np.gradient`` (to avoid NaN
    propagation across the whole array) and restored as NaN in the output
    at the original NaN positions. Adaptive-filter callers can then treat
    NaN dynamics defensively (we fall back to 0.0 inside the Numba loop).
    """
    hdg_filled, hdg_was_nan = _linear_interp_nan(np.asarray(hdg_deg_arr, dtype=float))
    spd_filled, spd_was_nan = _linear_interp_nan(np.asarray(spd_kn_arr, dtype=float))

    # Unwrap heading in radian space.
    hdg_unwrapped_rad = np.unwrap(np.deg2rad(hdg_filled))
    d_hdg_rad = np.gradient(hdg_unwrapped_rad)  # per-sample
    turn_rate_dps = d_hdg_rad * float(fs_hz) * (180.0 / np.pi)
    turn_rate_dps = centered_boxcar(turn_rate_dps, 5)

    d_spd = np.gradient(spd_filled) * float(fs_hz)
    accel_kn_s = centered_boxcar(d_spd, 5)

    # Restore NaN at original missing positions (safer: caller may guard).
    turn_rate_dps = np.where(hdg_was_nan, np.nan, turn_rate_dps)
    accel_kn_s = np.where(spd_was_nan, np.nan, accel_kn_s)
    return turn_rate_dps, accel_kn_s


@njit(cache=True)
def _kalman_rts_adaptive_numba(
    z_n: np.ndarray,
    z_e: np.ndarray,
    dt: float,
    q_var_base: float,
    r_var_base: float,
    p0_var: float,
    x0_n: float,
    x0_e: float,
    turn_rate_dps: np.ndarray,
    accel_kn_s: np.ndarray,
    alpha_R_turn: float,
    alpha_R_accel: float,
    r_scale_max: float,
    alpha_innov_ewm: float,
    shift_thresh: float,
    q_growth: float,
    q_decay: float,
    q_scale_max: float,
    huber_delta: float,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Numba-jitted adaptive Kalman filter + RTS smoother.

    Adaptive forward pass:
      1) Dynamic R inflation driven by (turn_rate_dps, accel_kn_s).
      2) Dynamic Q inflation driven by an EWMA of ``|innov|^2`` — real
         wind shifts push innov_ewm above ``shift_thresh``, triggering
         geometric growth of q_scale; otherwise it decays back to 1.
      3) Huber weight on innovation: ``w = min(1, delta / |norm_innov|)``
         per component, then the conservative min across components is
         applied to both.

    RTS backward pass: classic
      ``x_s[i] = x_f[i] + c * (x_s[i+1] - x_p[i+1])`` with ``c = p_f[i] / p_p[i+1]``.
    Because Q/R are non-stationary, p_pred is stored per sample.

    NaN measurements (z_n or z_e) are treated as missing (prediction-only
    update); diagnostic traces still receive the adaptive r_scale/q_scale
    but huber_weight is 1.0 on that sample.

    Returns:
        x_smooth_n, x_smooth_e,
        x_filt_n, x_filt_e,
        r_scale_trace, q_scale_trace, huber_w_trace
    """
    n = z_n.shape[0]
    x_filt_n = np.zeros(n)
    x_filt_e = np.zeros(n)
    p_filt = np.zeros(n)
    x_pred_n = np.zeros(n)
    x_pred_e = np.zeros(n)
    p_pred = np.zeros(n)

    r_scale_trace = np.zeros(n)
    q_scale_trace = np.zeros(n)
    huber_w_trace = np.ones(n)

    xn = x0_n
    xe = x0_e
    p = p0_var

    q_scale = 1.0
    innov_ewm_prev = 0.0

    for i in range(n):
        # --- dynamic R --- (always computed, even at i==0)
        tr = turn_rate_dps[i]
        ac = accel_kn_s[i]
        if np.isnan(tr):
            tr = 0.0
        if np.isnan(ac):
            ac = 0.0
        r_scale = 1.0 + alpha_R_turn * (tr / 10.0) * (tr / 10.0) \
                      + alpha_R_accel * (ac / 1.0) * (ac / 1.0)
        if r_scale > r_scale_max:
            r_scale = r_scale_max
        R_eff = r_var_base * r_scale

        # --- dynamic Q ---  (q_scale is the "current" state, carried over)
        Q_eff = q_var_base * q_scale

        # --- predict ---
        if i == 0:
            xpn = xn
            xpe = xe
            pp = p
        else:
            xpn = xn
            xpe = xe
            pp = p + Q_eff

        x_pred_n[i] = xpn
        x_pred_e[i] = xpe
        p_pred[i] = pp

        zn = z_n[i]
        ze = z_e[i]
        zn_ok = not np.isnan(zn)
        ze_ok = not np.isnan(ze)

        if zn_ok and ze_ok:
            innov_n = zn - xpn
            innov_e = ze - xpe
            innov_sq = innov_n * innov_n + innov_e * innov_e

            # EWMA update for NEXT sample's Q decision, using this sample's innov.
            innov_ewm = alpha_innov_ewm * innov_sq + (1.0 - alpha_innov_ewm) * innov_ewm_prev

            S = pp + R_eff
            sigma = np.sqrt(S) if S > 0.0 else 1e-9
            norm_n = abs(innov_n) / sigma
            norm_e = abs(innov_e) / sigma
            w_n = 1.0 if norm_n <= huber_delta else (huber_delta / norm_n)
            w_e = 1.0 if norm_e <= huber_delta else (huber_delta / norm_e)
            w = w_n if w_n < w_e else w_e

            K = pp / S
            xn = xpn + K * w * innov_n
            xe = xpe + K * w * innov_e
            p = (1.0 - K) * pp

            huber_w_trace[i] = w
            innov_ewm_prev = innov_ewm
        elif zn_ok:
            innov_n = zn - xpn
            innov_sq = innov_n * innov_n
            innov_ewm = alpha_innov_ewm * innov_sq + (1.0 - alpha_innov_ewm) * innov_ewm_prev

            S = pp + R_eff
            sigma = np.sqrt(S) if S > 0.0 else 1e-9
            norm_n = abs(innov_n) / sigma
            w = 1.0 if norm_n <= huber_delta else (huber_delta / norm_n)

            K = pp / S
            xn = xpn + K * w * innov_n
            xe = xpe
            p = (1.0 - K) * pp
            huber_w_trace[i] = w
            innov_ewm_prev = innov_ewm
        elif ze_ok:
            innov_e = ze - xpe
            innov_sq = innov_e * innov_e
            innov_ewm = alpha_innov_ewm * innov_sq + (1.0 - alpha_innov_ewm) * innov_ewm_prev

            S = pp + R_eff
            sigma = np.sqrt(S) if S > 0.0 else 1e-9
            norm_e = abs(innov_e) / sigma
            w = 1.0 if norm_e <= huber_delta else (huber_delta / norm_e)

            K = pp / S
            xn = xpn
            xe = xpe + K * w * innov_e
            p = (1.0 - K) * pp
            huber_w_trace[i] = w
            innov_ewm_prev = innov_ewm
        else:
            # Missing measurement: propagate prediction, no innov update.
            xn = xpn
            xe = xpe
            p = pp
            huber_w_trace[i] = 1.0
            # Decay innov_ewm_prev naturally (treat innov as 0 for EWMA).
            innov_ewm_prev = (1.0 - alpha_innov_ewm) * innov_ewm_prev

        # Update q_scale for NEXT iteration based on current innov_ewm_prev.
        if innov_ewm_prev > shift_thresh:
            q_scale *= q_growth
            if q_scale > q_scale_max:
                q_scale = q_scale_max
        else:
            q_scale *= q_decay
            if q_scale < 1.0:
                q_scale = 1.0

        r_scale_trace[i] = r_scale
        q_scale_trace[i] = q_scale

        x_filt_n[i] = xn
        x_filt_e[i] = xe
        p_filt[i] = p

    # --- RTS backward pass ---
    x_smooth_n = np.empty(n)
    x_smooth_e = np.empty(n)
    x_smooth_n[n - 1] = x_filt_n[n - 1]
    x_smooth_e[n - 1] = x_filt_e[n - 1]

    for i in range(n - 2, -1, -1):
        denom = p_pred[i + 1]
        if denom <= 0.0:
            c = 0.0
        else:
            c = p_filt[i] / denom
        x_smooth_n[i] = x_filt_n[i] + c * (x_smooth_n[i + 1] - x_pred_n[i + 1])
        x_smooth_e[i] = x_filt_e[i] + c * (x_smooth_e[i + 1] - x_pred_e[i + 1])

    return (x_smooth_n, x_smooth_e,
            x_filt_n, x_filt_e,
            r_scale_trace, q_scale_trace, huber_w_trace)


def _rts_smoother_random_walk(
    filt: Dict[str, np.ndarray],
) -> Dict[str, np.ndarray]:
    x_filt = filt["x_filt"]
    P_filt = filt["P_filt"]
    x_pred = filt["x_pred"]
    P_pred = filt["P_pred"]

    n = x_filt.shape[0]
    x_smooth = np.zeros_like(x_filt)
    P_smooth = np.zeros_like(P_filt)

    x_smooth[-1] = x_filt[-1]
    P_smooth[-1] = P_filt[-1]

    for i in range(n - 2, -1, -1):
        C = P_filt[i] @ np.linalg.inv(P_pred[i + 1])
        x_smooth[i] = x_filt[i] + C @ (x_smooth[i + 1] - x_pred[i + 1])
        P_smooth[i] = P_filt[i] + C @ (P_smooth[i + 1] - P_pred[i + 1]) @ C.T

    return {"x_smooth": x_smooth, "P_smooth": P_smooth}


def offline_ground_wind_rts(
    df: pd.DataFrame,
    fs_hz: float,
    cal: Calibration,
    config: OfflineSmootherConfig | None = None,
) -> pd.DataFrame:
    config = config or OfflineSmootherConfig()
    meas = instantaneous_ground_wind(df, fs_hz=fs_hz, cal=cal, align_gps_offline=True)

    z = meas[["wind_north_inst", "wind_east_inst"]].to_numpy(dtype=float)
    filt = _kalman_filter_random_walk(z, fs_hz=fs_hz, config=config)
    smooth = _rts_smoother_random_walk(filt)

    out = meas.copy()
    out["wind_north_filt"] = filt["x_filt"][:, 0]
    out["wind_east_filt"] = filt["x_filt"][:, 1]
    out["wind_north"] = smooth["x_smooth"][:, 0]
    out["wind_east"] = smooth["x_smooth"][:, 1]

    tws_filt, twd_filt = ne_to_polar(out["wind_north_filt"].to_numpy(), out["wind_east_filt"].to_numpy())
    twa_filt = wrap180(twd_filt - out["hdg_true"].to_numpy())
    out["tws_filt"] = tws_filt
    out["twd_filt"] = twd_filt
    out["twa_filt"] = twa_filt

    tws, twd = ne_to_polar(out["wind_north"].to_numpy(), out["wind_east"].to_numpy())
    twa = wrap180(twd - out["hdg_true"].to_numpy())
    out["tws"] = tws
    out["twd"] = twd
    out["twa"] = twa
    return out


def offline_water_wind_rts(
    df: pd.DataFrame,
    fs_hz: float,
    cal: Calibration,
    config: OfflineSmootherConfig | None = None,
) -> pd.DataFrame:
    """Water-referenced sibling of :func:`offline_ground_wind_rts`.

    Useful on the shear-comparison plot to show that even the same
    smoother produces an artefact-laden TWD when fed water-referenced
    instantaneous values.
    """
    config = config or OfflineSmootherConfig()
    meas = instantaneous_water_wind(df, fs_hz=fs_hz, cal=cal)

    z = meas[['wind_north_inst', 'wind_east_inst']].to_numpy(dtype=float)
    filt = _kalman_filter_random_walk(z, fs_hz=fs_hz, config=config)
    smooth = _rts_smoother_random_walk(filt)

    out = meas.copy()
    out['wind_north_filt'] = filt['x_filt'][:, 0]
    out['wind_east_filt'] = filt['x_filt'][:, 1]
    out['wind_north'] = smooth['x_smooth'][:, 0]
    out['wind_east'] = smooth['x_smooth'][:, 1]

    tws_filt, twd_filt = ne_to_polar(out['wind_north_filt'].to_numpy(), out['wind_east_filt'].to_numpy())
    twa_filt = wrap180(twd_filt - out['hdg_true'].to_numpy())
    out['tws_filt'] = tws_filt
    out['twd_filt'] = twd_filt
    out['twa_filt'] = twa_filt

    tws, twd = ne_to_polar(out['wind_north'].to_numpy(), out['wind_east'].to_numpy())
    twa = wrap180(twd - out['hdg_true'].to_numpy())
    out['tws'] = tws
    out['twd'] = twd
    out['twa'] = twa
    return out


def _run_adaptive_from_meas(
    meas: pd.DataFrame,
    fs_hz: float,
    adaptive_cfg: AdaptiveFilterConfig,
) -> pd.DataFrame:
    """Shared helper: given an instantaneous-wind measurement DataFrame
    (as produced by ``instantaneous_ground_wind`` or ``instantaneous_water_wind``)
    and the boat's hdg_true already present, run the adaptive Kalman+RTS
    and append the filtered/smoothed/diagnostic columns.
    """
    # Boat dynamics from hdg_true (deg) and whichever boat speed column
    # is implicit in the measurement. For the adaptive filter we treat
    # 'boat_north'/'boat_east' as the boat velocity and compute its
    # magnitude in knots — this covers both ground-ref (SOG/COG) and
    # water-ref (SPD along HDG) without branching.
    hdg_arr = meas['hdg_true'].to_numpy(dtype=float)
    boat_n = meas['boat_north'].to_numpy(dtype=float)
    boat_e = meas['boat_east'].to_numpy(dtype=float)
    spd_kn = np.hypot(boat_n, boat_e)

    turn_rate_dps, accel_kn_s = _compute_boat_dynamics(hdg_arr, spd_kn, fs_hz)

    z_n = meas['wind_north_inst'].to_numpy(dtype=float)
    z_e = meas['wind_east_inst'].to_numpy(dtype=float)

    # Initial state = first finite measurement (else zero).
    x0_n = 0.0
    x0_e = 0.0
    for i in range(len(z_n)):
        if np.isfinite(z_n[i]) and np.isfinite(z_e[i]):
            x0_n = float(z_n[i])
            x0_e = float(z_e[i])
            break

    dt = 1.0 / float(fs_hz)
    q_var_base = (adaptive_cfg.process_std_kn_per_s * dt) ** 2
    r_var_base = adaptive_cfg.measurement_std_kn ** 2
    p0_var = adaptive_cfg.initial_state_std_kn ** 2

    (x_smooth_n, x_smooth_e,
     x_filt_n, x_filt_e,
     r_scale_trace, q_scale_trace, huber_w_trace) = _kalman_rts_adaptive_numba(
        z_n, z_e, dt,
        q_var_base, r_var_base, p0_var,
        x0_n, x0_e,
        turn_rate_dps, accel_kn_s,
        adaptive_cfg.alpha_R_turn, adaptive_cfg.alpha_R_accel, adaptive_cfg.r_scale_max,
        adaptive_cfg.alpha_innov_ewm, adaptive_cfg.shift_threshold_kn2,
        adaptive_cfg.q_growth, adaptive_cfg.q_decay, adaptive_cfg.q_scale_max,
        adaptive_cfg.huber_delta,
    )

    out = meas.copy()
    out['wind_north_filt'] = x_filt_n
    out['wind_east_filt'] = x_filt_e
    out['wind_north'] = x_smooth_n
    out['wind_east'] = x_smooth_e

    tws_filt, twd_filt = ne_to_polar(x_filt_n, x_filt_e)
    twa_filt = wrap180(twd_filt - hdg_arr)
    out['tws_filt'] = tws_filt
    out['twd_filt'] = twd_filt
    out['twa_filt'] = twa_filt

    tws, twd = ne_to_polar(x_smooth_n, x_smooth_e)
    twa = wrap180(twd - hdg_arr)
    out['tws'] = tws
    out['twd'] = twd
    out['twa'] = twa

    # Diagnostics.
    out['turn_rate_dps'] = turn_rate_dps
    out['accel_kn_s'] = accel_kn_s
    out['r_scale'] = r_scale_trace
    out['q_scale'] = q_scale_trace
    out['huber_weight'] = huber_w_trace
    return out


def offline_ground_wind_adaptive(
    df: pd.DataFrame,
    fs_hz: float,
    cal: Calibration,
    adaptive_cfg: AdaptiveFilterConfig | None = None,
) -> pd.DataFrame:
    """Ground-referenced adaptive (tack-gated R, shift-tracking Q, Huber) + RTS.

    Mirrors :func:`offline_ground_wind_rts` but substitutes
    :func:`_kalman_rts_adaptive_numba` for the linear forward pass. The
    backward RTS is fed the adaptive filter's per-sample ``p_pred``.
    """
    adaptive_cfg = adaptive_cfg or AdaptiveFilterConfig()
    meas = instantaneous_ground_wind(df, fs_hz=fs_hz, cal=cal, align_gps_offline=True)
    return _run_adaptive_from_meas(meas, fs_hz=fs_hz, adaptive_cfg=adaptive_cfg)


def offline_water_wind_adaptive(
    df: pd.DataFrame,
    fs_hz: float,
    cal: Calibration,
    adaptive_cfg: AdaptiveFilterConfig | None = None,
) -> pd.DataFrame:
    """Water-referenced sibling of :func:`offline_ground_wind_adaptive`."""
    adaptive_cfg = adaptive_cfg or AdaptiveFilterConfig()
    meas = instantaneous_water_wind(df, fs_hz=fs_hz, cal=cal)
    return _run_adaptive_from_meas(meas, fs_hz=fs_hz, adaptive_cfg=adaptive_cfg)


# ---------------------------
# Tack detection / calibration windows
# ---------------------------

def detect_tacks(
    df: pd.DataFrame,
    fs_hz: float,
    awa_threshold_deg: float = 35.0,
    heading_change_threshold_deg: float = 50.0,
    smooth_s: float = 1.0,
    confirm_s: float = 3.0,
    turn_window_s: float = 8.0,
    min_spacing_s: float = 20.0,
) -> np.ndarray:
    """
    Detect likely tacks from AWA sign changes plus heading change.

    This is intentionally simple and deterministic.
    """
    awa = np.asarray(df["awa"], dtype=float)
    hdg = np.asarray(df["hdg"], dtype=float)

    smooth_n = max(1, int(round(smooth_s * fs_hz)))
    confirm_n = max(1, int(round(confirm_s * fs_hz)))
    turn_n = max(1, int(round(turn_window_s * fs_hz)))
    min_spacing_n = max(1, int(round(min_spacing_s * fs_hz)))

    awa_sm = centered_boxcar(awa, smooth_n)
    hdg_unwrapped = np.rad2deg(np.unwrap(np.deg2rad(hdg)))
    hdg_sm = centered_boxcar(hdg_unwrapped, smooth_n)

    centers: List[int] = []
    i = 1
    n = len(df)

    while i < n:
        if not np.isfinite(awa_sm[i - 1]) or not np.isfinite(awa_sm[i]):
            i += 1
            continue

        sign_change = (awa_sm[i - 1] <= 0.0 < awa_sm[i]) or (awa_sm[i - 1] >= 0.0 > awa_sm[i])
        if not sign_change:
            i += 1
            continue

        pre0 = max(0, i - confirm_n)
        pre1 = i
        post0 = i
        post1 = min(n, i + confirm_n)
        h0 = max(0, i - turn_n)
        h1 = min(n, i + turn_n)

        pre_awa = np.nanmedian(awa_sm[pre0:pre1])
        post_awa = np.nanmedian(awa_sm[post0:post1])
        dh = abs(hdg_sm[h1 - 1] - hdg_sm[h0])

        good = (
            np.isfinite(pre_awa)
            and np.isfinite(post_awa)
            and abs(pre_awa) >= awa_threshold_deg
            and abs(post_awa) >= awa_threshold_deg
            and np.sign(pre_awa) != np.sign(post_awa)
            and dh >= heading_change_threshold_deg
        )
        if good:
            if not centers or i - centers[-1] >= min_spacing_n:
                centers.append(i)
                i += min_spacing_n
                continue

        i += 1

    return np.asarray(centers, dtype=int)


def extract_tack_segments(
    df: pd.DataFrame,
    fs_hz: float,
    tack_centers: Optional[Sequence[int]] = None,
    pre_s: float = 15.0,
    post_s: float = 25.0,
    extra_margin_s: float = 3.0,
    max_segments: Optional[int] = 200,
) -> List[Tuple[int, int, int]]:
    """
    Returns a list of (start, center, stop) tuples in sample indices.

    extra_margin_s is only to give the offline delay compensation a little room
    near the boundaries.
    """
    n = len(df)
    if tack_centers is None:
        tack_centers = detect_tacks(df, fs_hz=fs_hz)

    pre_n = int(round(pre_s * fs_hz))
    post_n = int(round(post_s * fs_hz))
    margin_n = int(round(extra_margin_s * fs_hz))

    segments: List[Tuple[int, int, int]] = []
    for c in tack_centers:
        start = max(0, int(c) - pre_n - margin_n)
        stop = min(n, int(c) + post_n + margin_n)
        center = int(c) - start
        segments.append((start, center, stop))

    if max_segments is not None and len(segments) > max_segments:
        keep = np.linspace(0, len(segments) - 1, max_segments).round().astype(int)
        segments = [segments[i] for i in keep]

    return segments


# ---------------------------
# Offline calibration fit
# ---------------------------

@dataclass
class CalibrationFitConfig:
    smooth_window_s: float = 2.0
    pre_window_s: Tuple[float, float] = (10.0, 3.0)   # mean over [center-10s, center-3s]
    post_window_s: Tuple[float, float] = (3.0, 12.0)  # mean over [center+3s, center+12s]
    jump_weight: float = 1.0
    residual_weight: float = 0.25
    roughness_weight: float = 0.05
    prior_weight: float = 0.02
    huber_delta_kn: float = 0.30


def _segment_cost(
    seg_df: pd.DataFrame,
    fs_hz: float,
    center: int,
    cal: Calibration,
    fit_cfg: CalibrationFitConfig,
) -> float:
    wind = instantaneous_ground_wind(seg_df, fs_hz=fs_hz, cal=cal, align_gps_offline=True)
    wn = centered_boxcar(wind["wind_north_inst"].to_numpy(), max(1, int(round(fit_cfg.smooth_window_s * fs_hz))))
    we = centered_boxcar(wind["wind_east_inst"].to_numpy(), max(1, int(round(fit_cfg.smooth_window_s * fs_hz))))

    pre_outer, pre_inner = fit_cfg.pre_window_s
    post_inner, post_outer = fit_cfg.post_window_s

    p0 = max(0, center - int(round(pre_outer * fs_hz)))
    p1 = max(0, center - int(round(pre_inner * fs_hz)))
    q0 = min(len(seg_df), center + int(round(post_inner * fs_hz)))
    q1 = min(len(seg_df), center + int(round(post_outer * fs_hz)))

    if p1 <= p0 + 2 or q1 <= q0 + 2:
        return 0.0

    pre_vec = np.array([np.nanmean(wn[p0:p1]), np.nanmean(we[p0:p1])], dtype=float)
    post_vec = np.array([np.nanmean(wn[q0:q1]), np.nanmean(we[q0:q1])], dtype=float)
    jump = float(np.linalg.norm(post_vec - pre_vec))

    residual_mag = np.hypot(wind["wind_north_inst"].to_numpy() - wn, wind["wind_east_inst"].to_numpy() - we)
    residual_cost = float(np.nanmean(np.where(
        residual_mag <= fit_cfg.huber_delta_kn,
        0.5 * residual_mag ** 2,
        fit_cfg.huber_delta_kn * (residual_mag - 0.5 * fit_cfg.huber_delta_kn),
    )))

    dwn = np.diff(wn)
    dwe = np.diff(we)
    rough = np.hypot(dwn, dwe)
    rough_cost = float(np.nanmean(np.where(
        rough <= fit_cfg.huber_delta_kn,
        0.5 * rough ** 2,
        fit_cfg.huber_delta_kn * (rough - 0.5 * fit_cfg.huber_delta_kn),
    )))

    return (
        fit_cfg.jump_weight * huber_scalar(jump, fit_cfg.huber_delta_kn)
        + fit_cfg.residual_weight * residual_cost
        + fit_cfg.roughness_weight * rough_cost
    )


def fit_ground_wind_calibration(
    df: pd.DataFrame,
    fs_hz: float,
    segments: Optional[Sequence[Tuple[int, int, int]]] = None,
    initial: Calibration | None = None,
    fit_cfg: CalibrationFitConfig | None = None,
    bounds: Optional[Sequence[Tuple[float, float]]] = None,
    maxiter: int = 150,
) -> Dict[str, Any]:
    """
    Fit a small calibration model using tack invariance on ground-referenced wind.

    Parameters optimized here:
        awa_offset_deg
        awa_scale_upwind
        aws_scale_upwind
        gps_delay_s

    Notes:
      - heading_offset_deg is not identifiable from tack constancy, so it is fixed.
      - For 100+ hours of data, do NOT optimize on the full log. Build tack segments first.
      - This objective is intentionally simple and robust, not mathematically exhaustive.
    """
    initial = initial or Calibration()
    fit_cfg = fit_cfg or CalibrationFitConfig()
    if segments is None:
        segments = extract_tack_segments(df, fs_hz=fs_hz)

    if not segments:
        raise ValueError("No tack segments available. Check AWA/HDG columns or loosen detection thresholds.")

    if bounds is None:
        bounds = [
            (-12.0, 12.0),   # awa_offset_deg
            (0.70, 1.30),    # awa_scale_upwind
            (0.80, 1.20),    # aws_scale_upwind
            (0.00, 3.00),    # gps_delay_s
        ]

    x0 = np.array([
        initial.awa_offset_deg,
        initial.awa_scale_upwind,
        initial.aws_scale_upwind,
        initial.gps_delay_s,
    ], dtype=float)

    def unpack(x: np.ndarray) -> Calibration:
        return replace(
            initial,
            awa_offset_deg=float(x[0]),
            awa_scale_upwind=float(x[1]),
            aws_scale_upwind=float(x[2]),
            gps_delay_s=float(x[3]),
        )

    def objective(x: np.ndarray) -> float:
        cal = unpack(x)
        total = 0.0
        for start, center, stop in segments:
            seg_df = df.iloc[start:stop]
            total += _segment_cost(seg_df, fs_hz=fs_hz, center=center, cal=cal, fit_cfg=fit_cfg)

        # Mild priors: keep the optimizer from wandering into silly values.
        prior = (
            (cal.awa_offset_deg / 6.0) ** 2
            + ((cal.awa_scale_upwind - 1.0) / 0.15) ** 2
            + ((cal.aws_scale_upwind - 1.0) / 0.10) ** 2
            + ((cal.gps_delay_s - initial.gps_delay_s) / 1.0) ** 2
        )
        total += fit_cfg.prior_weight * prior
        return total / max(1, len(segments))

    result = minimize(
        objective,
        x0,
        method="Powell",
        bounds=bounds,
        options={"maxiter": maxiter, "disp": False},
    )

    best = unpack(result.x)
    return {
        "calibration": best,
        "opt_result": result,
        "segments": segments,
    }


# ---------------------------
# Example workflow helper
# ---------------------------

def example_workflow(
    df: pd.DataFrame,
    fs_hz: float = 10.0,
) -> Dict[str, Any]:
    """
    Small end-to-end helper:
      1) detect tacks
      2) build tack segments
      3) fit calibration
      4) run causal estimator
      5) run offline RTS smoother
    """
    tack_centers = detect_tacks(df, fs_hz=fs_hz)
    segments = extract_tack_segments(df, fs_hz=fs_hz, tack_centers=tack_centers, max_segments=120)

    fit = fit_ground_wind_calibration(
        df=df,
        fs_hz=fs_hz,
        segments=segments,
        initial=Calibration(gps_delay_s=1.0),
    )
    cal = fit["calibration"]

    causal = run_causal_filter_on_dataframe(df, fs_hz=fs_hz, cal=cal)
    offline = offline_ground_wind_rts(df, fs_hz=fs_hz, cal=cal)

    return {
        "tack_centers": tack_centers,
        "segments": segments,
        "calibration": cal,
        "causal": causal,
        "offline": offline,
        "fit": fit,
    }


# ---------------------------
# Smoke tests for adaptive filter
# ---------------------------

def _pure_python_adaptive_reference(
    z_n: np.ndarray, z_e: np.ndarray, dt: float,
    q_var_base: float, r_var_base: float, p0_var: float,
    x0_n: float, x0_e: float,
    turn_rate_dps: np.ndarray, accel_kn_s: np.ndarray,
    alpha_R_turn: float, alpha_R_accel: float, r_scale_max: float,
    alpha_innov_ewm: float, shift_thresh: float,
    q_growth: float, q_decay: float, q_scale_max: float,
    huber_delta: float,
):
    """Pure-numpy reference implementation that mirrors
    :func:`_kalman_rts_adaptive_numba` exactly. Used to numerically
    verify the Numba-jitted function.
    """
    n = z_n.shape[0]
    x_filt_n = np.zeros(n); x_filt_e = np.zeros(n); p_filt = np.zeros(n)
    x_pred_n = np.zeros(n); x_pred_e = np.zeros(n); p_pred = np.zeros(n)
    r_scale_tr = np.zeros(n); q_scale_tr = np.zeros(n); huber_tr = np.ones(n)

    xn, xe, p = x0_n, x0_e, p0_var
    q_scale = 1.0
    innov_ewm_prev = 0.0

    for i in range(n):
        tr = turn_rate_dps[i]
        ac = accel_kn_s[i]
        if not np.isfinite(tr):
            tr = 0.0
        if not np.isfinite(ac):
            ac = 0.0
        r_scale = 1.0 + alpha_R_turn * (tr / 10.0) ** 2 + alpha_R_accel * (ac / 1.0) ** 2
        r_scale = min(r_scale, r_scale_max)
        R_eff = r_var_base * r_scale
        Q_eff = q_var_base * q_scale

        if i == 0:
            xpn, xpe, pp = xn, xe, p
        else:
            xpn, xpe, pp = xn, xe, p + Q_eff

        x_pred_n[i] = xpn; x_pred_e[i] = xpe; p_pred[i] = pp

        zn = z_n[i]; ze = z_e[i]
        zn_ok = np.isfinite(zn); ze_ok = np.isfinite(ze)

        if zn_ok and ze_ok:
            innov_n = zn - xpn; innov_e = ze - xpe
            innov_sq = innov_n * innov_n + innov_e * innov_e
            innov_ewm = alpha_innov_ewm * innov_sq + (1.0 - alpha_innov_ewm) * innov_ewm_prev
            S = pp + R_eff
            sigma = np.sqrt(S) if S > 0.0 else 1e-9
            norm_n = abs(innov_n) / sigma; norm_e = abs(innov_e) / sigma
            w_n = 1.0 if norm_n <= huber_delta else (huber_delta / norm_n)
            w_e = 1.0 if norm_e <= huber_delta else (huber_delta / norm_e)
            w = min(w_n, w_e)
            K = pp / S
            xn = xpn + K * w * innov_n
            xe = xpe + K * w * innov_e
            p = (1.0 - K) * pp
            huber_tr[i] = w
            innov_ewm_prev = innov_ewm
        elif zn_ok:
            innov_n = zn - xpn
            innov_sq = innov_n * innov_n
            innov_ewm = alpha_innov_ewm * innov_sq + (1.0 - alpha_innov_ewm) * innov_ewm_prev
            S = pp + R_eff
            sigma = np.sqrt(S) if S > 0.0 else 1e-9
            norm_n = abs(innov_n) / sigma
            w = 1.0 if norm_n <= huber_delta else (huber_delta / norm_n)
            K = pp / S
            xn = xpn + K * w * innov_n
            xe = xpe
            p = (1.0 - K) * pp
            huber_tr[i] = w
            innov_ewm_prev = innov_ewm
        elif ze_ok:
            innov_e = ze - xpe
            innov_sq = innov_e * innov_e
            innov_ewm = alpha_innov_ewm * innov_sq + (1.0 - alpha_innov_ewm) * innov_ewm_prev
            S = pp + R_eff
            sigma = np.sqrt(S) if S > 0.0 else 1e-9
            norm_e = abs(innov_e) / sigma
            w = 1.0 if norm_e <= huber_delta else (huber_delta / norm_e)
            K = pp / S
            xn = xpn
            xe = xpe + K * w * innov_e
            p = (1.0 - K) * pp
            huber_tr[i] = w
            innov_ewm_prev = innov_ewm
        else:
            xn = xpn; xe = xpe; p = pp
            huber_tr[i] = 1.0
            innov_ewm_prev = (1.0 - alpha_innov_ewm) * innov_ewm_prev

        if innov_ewm_prev > shift_thresh:
            q_scale = min(q_scale * q_growth, q_scale_max)
        else:
            q_scale = max(q_scale * q_decay, 1.0)

        r_scale_tr[i] = r_scale
        q_scale_tr[i] = q_scale
        x_filt_n[i] = xn; x_filt_e[i] = xe; p_filt[i] = p

    x_sn = np.empty(n); x_se = np.empty(n)
    x_sn[-1] = x_filt_n[-1]; x_se[-1] = x_filt_e[-1]
    for i in range(n - 2, -1, -1):
        denom = p_pred[i + 1]
        c = 0.0 if denom <= 0.0 else p_filt[i] / denom
        x_sn[i] = x_filt_n[i] + c * (x_sn[i + 1] - x_pred_n[i + 1])
        x_se[i] = x_filt_e[i] + c * (x_se[i + 1] - x_pred_e[i + 1])

    return x_sn, x_se, x_filt_n, x_filt_e, r_scale_tr, q_scale_tr, huber_tr


def _smoke_tests() -> None:
    """Run four adaptive-filter smoke tests and print results."""
    import time

    np.random.seed(0)
    fs = 10.0
    cfg = AdaptiveFilterConfig()
    dt = 1.0 / fs
    q_var_base = (cfg.process_std_kn_per_s * dt) ** 2
    r_var_base = cfg.measurement_std_kn ** 2
    p0_var = cfg.initial_state_std_kn ** 2

    # Pre-warm JIT.
    tn = np.zeros(10); te = np.zeros(10)
    _ = _kalman_rts_adaptive_numba(
        tn, te, dt, q_var_base, r_var_base, p0_var, 0.0, 0.0,
        np.zeros(10), np.zeros(10),
        cfg.alpha_R_turn, cfg.alpha_R_accel, cfg.r_scale_max,
        cfg.alpha_innov_ewm, cfg.shift_threshold_kn2,
        cfg.q_growth, cfg.q_decay, cfg.q_scale_max,
        cfg.huber_delta,
    )

    results = {}

    # --- Test 1: stationarity (constant wind + constant heading).
    N = 600
    z_n = np.full(N, 10.0) + 0.05 * np.random.randn(N)
    z_e = np.full(N, -3.0) + 0.05 * np.random.randn(N)
    turn_rate = np.zeros(N)
    accel = np.zeros(N)

    # Adaptive.
    xs_n_a, xs_e_a, *_ = _kalman_rts_adaptive_numba(
        z_n, z_e, dt, q_var_base, r_var_base, p0_var, z_n[0], z_e[0],
        turn_rate, accel,
        cfg.alpha_R_turn, cfg.alpha_R_accel, cfg.r_scale_max,
        cfg.alpha_innov_ewm, cfg.shift_threshold_kn2,
        cfg.q_growth, cfg.q_decay, cfg.q_scale_max,
        cfg.huber_delta,
    )
    # Linear (numba baseline).
    xs_n_l, xs_e_l = _kalman_rts_rw_numba(
        z_n, z_e, dt, q_var_base, r_var_base, p0_var, z_n[0], z_e[0],
    )
    # Trim boundaries (filter initial transient).
    diff_n = np.abs(xs_n_a[50:-50] - xs_n_l[50:-50]).max()
    diff_e = np.abs(xs_e_a[50:-50] - xs_e_l[50:-50]).max()
    stat_ok = (diff_n < 1e-4) and (diff_e < 1e-4)
    results['stationarity'] = {
        'max_abs_diff_n': diff_n,
        'max_abs_diff_e': diff_e,
        'pass': stat_ok,
    }
    print(f"[stationarity]     max|adaptive - linear|: n={diff_n:.2e}, e={diff_e:.2e}  {'PASS' if stat_ok else 'FAIL'}")

    # --- Test 2: outlier rejection (3-sample 10 kn spike).
    N = 600
    true_n = 10.0
    z_n = np.full(N, true_n) + 0.05 * np.random.randn(N)
    z_e = np.zeros(N) + 0.05 * np.random.randn(N)
    spike_idx = slice(300, 303)
    z_n[spike_idx] += 10.0  # 10 kn outlier

    xs_n_a, xs_e_a, xfn_a, xfe_a, *_ = _kalman_rts_adaptive_numba(
        z_n, z_e, dt, q_var_base, r_var_base, p0_var, true_n, 0.0,
        np.zeros(N), np.zeros(N),
        cfg.alpha_R_turn, cfg.alpha_R_accel, cfg.r_scale_max,
        cfg.alpha_innov_ewm, cfg.shift_threshold_kn2,
        cfg.q_growth, cfg.q_decay, cfg.q_scale_max,
        cfg.huber_delta,
    )
    xs_n_l, xs_e_l = _kalman_rts_rw_numba(
        z_n, z_e, dt, q_var_base, r_var_base, p0_var, true_n, 0.0,
    )
    # Look at causal filter output around the spike (samples 300..320)
    adaptive_dev = np.max(np.abs(xfn_a[300:320] - true_n))
    linear_dev = np.max(np.abs(_linear_forward_only(z_n, z_e, dt, q_var_base, r_var_base, p0_var, true_n, 0.0)[0][300:320] - true_n))
    outlier_ok = (adaptive_dev < 1.0) and (linear_dev > 0.3)
    results['outlier'] = {
        'adaptive_peak_dev_kn': adaptive_dev,
        'linear_peak_dev_kn': linear_dev,
        'pass': outlier_ok,
    }
    print(f"[outlier]          adaptive peak dev = {adaptive_dev:.3f} kn, linear peak dev = {linear_dev:.3f} kn  "
          f"{'PASS' if outlier_ok else 'FAIL'}  (target: adaptive<1, linear>0.3)")

    # --- Test 3: shift tracking (20 deg TWD step).
    N = 1200
    # 20-deg step in TWD at sample 600. Start at (10, 0) -> rotate to 20 deg.
    tws = 10.0
    twd_pre = 0.0   # due-north
    twd_post = 20.0  # rotated 20 deg CW
    true_n_pre = tws * np.cos(np.deg2rad(twd_pre))
    true_e_pre = tws * np.sin(np.deg2rad(twd_pre))
    true_n_post = tws * np.cos(np.deg2rad(twd_post))
    true_e_post = tws * np.sin(np.deg2rad(twd_post))
    z_n = np.concatenate([np.full(600, true_n_pre), np.full(N - 600, true_n_post)]) + 0.05 * np.random.randn(N)
    z_e = np.concatenate([np.full(600, true_e_pre), np.full(N - 600, true_e_post)]) + 0.05 * np.random.randn(N)

    # Adaptive (use causal filtered state for settling time).
    _, _, xfn_a, xfe_a, *_ = _kalman_rts_adaptive_numba(
        z_n, z_e, dt, q_var_base, r_var_base, p0_var, true_n_pre, true_e_pre,
        np.zeros(N), np.zeros(N),
        cfg.alpha_R_turn, cfg.alpha_R_accel, cfg.r_scale_max,
        cfg.alpha_innov_ewm, cfg.shift_threshold_kn2,
        cfg.q_growth, cfg.q_decay, cfg.q_scale_max,
        cfg.huber_delta,
    )
    # Linear causal baseline (re-run forward only).
    xfn_l, xfe_l = _linear_forward_only(z_n, z_e, dt, q_var_base, r_var_base, p0_var, true_n_pre, true_e_pre)

    def samples_to_settle(xn, xe):
        twd = np.rad2deg(np.arctan2(xe, xn))
        err = np.abs((twd - twd_post + 180) % 360 - 180)
        post = err[600:]
        inside = post <= 1.0
        if not inside.any():
            return len(post)
        # earliest sample where error stays <= 1 for 50 consecutive samples
        for j in range(len(post) - 50):
            if np.all(post[j:j + 50] <= 1.0):
                return j
        return len(post)

    adaptive_settle = samples_to_settle(xfn_a, xfe_a)
    linear_settle = samples_to_settle(xfn_l, xfe_l)
    shift_ok = adaptive_settle <= linear_settle
    results['shift'] = {
        'adaptive_samples': adaptive_settle,
        'linear_samples': linear_settle,
        'pass': shift_ok,
    }
    print(f"[shift]            settle: adaptive={adaptive_settle/fs:.1f}s, linear={linear_settle/fs:.1f}s  "
          f"{'PASS' if shift_ok else 'FAIL'}  (target: adaptive<=linear)")

    # --- Test 4: Numba vs pure-Python reference on 1000-sample random walk.
    N = 1000
    rng = np.random.default_rng(42)
    z_n = 5.0 + np.cumsum(0.02 * rng.standard_normal(N)) + 0.1 * rng.standard_normal(N)
    z_e = -2.0 + np.cumsum(0.02 * rng.standard_normal(N)) + 0.1 * rng.standard_normal(N)
    tr = 0.5 * rng.standard_normal(N)
    ac = 0.1 * rng.standard_normal(N)

    out_numba = _kalman_rts_adaptive_numba(
        z_n, z_e, dt, q_var_base, r_var_base, p0_var, z_n[0], z_e[0],
        tr, ac,
        cfg.alpha_R_turn, cfg.alpha_R_accel, cfg.r_scale_max,
        cfg.alpha_innov_ewm, cfg.shift_threshold_kn2,
        cfg.q_growth, cfg.q_decay, cfg.q_scale_max,
        cfg.huber_delta,
    )
    out_pure = _pure_python_adaptive_reference(
        z_n, z_e, dt, q_var_base, r_var_base, p0_var, z_n[0], z_e[0],
        tr, ac,
        cfg.alpha_R_turn, cfg.alpha_R_accel, cfg.r_scale_max,
        cfg.alpha_innov_ewm, cfg.shift_threshold_kn2,
        cfg.q_growth, cfg.q_decay, cfg.q_scale_max,
        cfg.huber_delta,
    )
    max_diff = 0.0
    for a, b in zip(out_numba, out_pure):
        max_diff = max(max_diff, float(np.max(np.abs(a - b))))
    numba_ok = max_diff < 1e-9
    results['numba_vs_pure'] = {
        'max_abs_diff': max_diff,
        'pass': numba_ok,
    }
    print(f"[numba-vs-pure]    max|numba - pure_python| = {max_diff:.2e}  {'PASS' if numba_ok else 'FAIL'}  (target: <1e-9)")

    print()
    all_pass = all(v['pass'] for v in results.values())
    print("ALL SMOKE TESTS PASS" if all_pass else "ONE OR MORE SMOKE TESTS FAILED")


def _linear_forward_only(
    z_n: np.ndarray, z_e: np.ndarray, dt: float,
    q_var: float, r_var: float, p0_var: float,
    x0_n: float, x0_e: float,
) -> Tuple[np.ndarray, np.ndarray]:
    """Forward-only scalar Kalman (no RTS) for the linear reference used in
    the shift/outlier smoke tests — need the causal state for settle/peak
    measurements, not the zero-phase smoothed output."""
    n = z_n.shape[0]
    xn, xe, p = x0_n, x0_e, p0_var
    out_n = np.empty(n); out_e = np.empty(n)
    for i in range(n):
        if i == 0:
            pp = p
        else:
            pp = p + q_var
        zn = z_n[i]; ze = z_e[i]
        if np.isfinite(zn) and np.isfinite(ze):
            s = pp + r_var
            k = pp / s
            xn = xn + k * (zn - xn)
            xe = xe + k * (ze - xe)
            p = (1.0 - k) * pp
        else:
            p = pp
        out_n[i] = xn; out_e[i] = xe
    return out_n, out_e


if __name__ == '__main__':
    _smoke_tests()

