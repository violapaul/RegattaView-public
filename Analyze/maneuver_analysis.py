"""Shared tack/gybe analysis for review event details."""
import json
import os

import numpy as np
import pandas as pd

from Libraries.global_variables import G
from Libraries.event_detectors import HeadingManeuverDetector
from Libraries import process


# Paddlewheel calibration. Single source of truth at
# ``Src/src/assets/calibration/sensor_calibration.json`` (also consumed by
# ``Src/src/js/viewer360.js`` for display). Applied here so card metrics
# (entry_spd, min_spd, R80/R90 thresholds) match the helmsman's instrument
# readout. Only applies to the ``spd`` (paddlewheel) source — ``sog``/``rsog``
# are GPS-derived and need no calibration.
def _load_spd_cal():
    path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        '..', 'Src', 'src', 'assets', 'calibration', 'sensor_calibration.json',
    )
    try:
        with open(path) as f:
            spd = (json.load(f) or {}).get('spd') or {}
        return float(spd.get('scale', 1.0)), float(spd.get('offset', 0.0))
    except (OSError, ValueError, TypeError):
        return 1.0, 0.0


_SPD_CAL_SCALE, _SPD_CAL_OFFSET = _load_spd_cal()


def _lpf(arr, cutoff_hz, order=3):
    """Zero-phase Butterworth LPF via filtfilt; NaN-safe.

    Repo convention (see ``Libraries/slog_channel_config.py``). Sample rate
    is hardcoded to 10 Hz inside ``process.butterworth_filter`` to match the
    parquet rate. NaN gaps are linearly interpolated before filtering so
    ``filtfilt`` does not propagate NaN across the whole signal.
    """
    if arr is None:
        return None
    a = np.asarray(arr, dtype=float)
    n = len(a)
    if n < 3 * (order + 1) + 1:
        return a.copy()
    finite = np.isfinite(a)
    if not finite.any():
        return a.copy()
    if not finite.all():
        idx = np.arange(n)
        a = np.interp(idx, idx[finite], a[finite])
    coeff = process.butterworth_filter(cutoff_hz, order)
    return process.smooth(coeff, a, causal=False)


def _lpf_angle_deg(arr, cutoff_hz, order=3):
    """Zero-phase LPF for an angle signal in degrees, returning unwrapped radians.

    Wrap-aware: NaN gaps are filled via interpolation in unit-circle space
    (cos/sin separately) — interpolating raw degrees through a wrap point
    would synthesize an impossible value (e.g. 359, NaN, 1 → 359, 180, 1).
    Filter step is the repo's zero-phase Butterworth via filtfilt, applied
    to the unwrapped radians so the filter never sees a 360-degree jump.
    Returns smoothed *unwrapped radians* — ready for direct differentiation
    in yaw-rate calc, no re-wrap step needed.
    """
    if arr is None:
        return None
    a = np.asarray(arr, dtype=float)
    n = len(a)
    finite = np.isfinite(a)
    if not finite.any():
        return np.zeros(n)
    rad = np.deg2rad(np.where(finite, a, 0.0))
    if not finite.all():
        idx = np.arange(n)
        cosp = np.interp(idx, idx[finite], np.cos(rad[finite]))
        sinp = np.interp(idx, idx[finite], np.sin(rad[finite]))
        rad = np.arctan2(sinp, cosp)
    unwrapped_rad = np.unwrap(rad)
    if n < 3 * (order + 1) + 1:
        return unwrapped_rad
    coeff = process.butterworth_filter(cutoff_hz, order)
    return process.smooth(coeff, unwrapped_rad, causal=False)


def analyze_maneuvers(
    df: pd.DataFrame,
    window_start_idx: int,
    window_end_idx: int,
    event_idx: int,
    fs: int = G.SAMPLES_PER_SECOND,
    reviewable_only: bool = True,
) -> tuple[list, list]:
    """Detect tacks/gybes in a window and return event-relative metrics.

    The detector is intentionally the shared heading-based maneuver detector
    used elsewhere in the repo. The output is shaped for review detail JSON:
    ``time_s`` is relative to the selected event.
    """
    ws = max(0, int(window_start_idx))
    we = min(len(df), int(window_end_idx))
    if we - ws < fs * 60:
        return [], []

    window_df = df.iloc[ws:we]
    hdg_col = 'hdg' if 'hdg' in window_df.columns else 'rhdg'
    awa_col = 'awa' if 'awa' in window_df.columns else 'rawa'
    twa_col = 'twa' if 'twa' in window_df.columns else 'rtwa'
    spd_col = 'spd' if 'spd' in window_df.columns else ('sog' if 'sog' in window_df.columns else 'rsog')
    if hdg_col not in window_df.columns or spd_col not in window_df.columns:
        return [], []

    hdg_arr = window_df[hdg_col].to_numpy(dtype=float)
    awa_arr = window_df[awa_col].to_numpy(dtype=float) if awa_col in window_df.columns else None
    twa_arr = window_df[twa_col].to_numpy(dtype=float) if twa_col in window_df.columns else None
    spd_arr = window_df[spd_col].to_numpy(dtype=float)
    if spd_col == 'spd' and (_SPD_CAL_SCALE != 1.0 or _SPD_CAL_OFFSET != 0.0):
        spd_arr = spd_arr * _SPD_CAL_SCALE + _SPD_CAL_OFFSET
    vmg_col = 'upwind_vmg_kts' if 'upwind_vmg_kts' in window_df.columns else None
    vmg_arr = window_df[vmg_col].to_numpy(dtype=float) if vmg_col else None
    ts = pd.to_datetime(window_df['timestamp'], utc=True).to_numpy() if 'timestamp' in window_df.columns else None

    detector = HeadingManeuverDetector(
        min_turn=55,
        steady_std_max=10,
        steady_seconds=20,
        min_gap_seconds=25,
        sample_rate=fs,
    )
    raw_events = detector._find_maneuvers(hdg_arr)
    tacks = []
    gybes = []

    for local_idx, hdg_change in raw_events:
        abs_idx = ws + int(local_idx)
        time_s = round((abs_idx - event_idx) / fs, 1)
        if awa_arr is not None and local_idx < len(awa_arr):
            event_type = 'tack' if abs(float(awa_arr[local_idx])) < 90.0 else 'gybe'
        else:
            event_type = 'tack' if abs(float(hdg_change)) < 120.0 else 'gybe'

        direction = 'P-to-S' if hdg_change > 0 else 'S-to-P'
        pre_n = int(10 * fs)
        settle_n = int(15 * fs)
        pre_start = max(0, int(local_idx) - pre_n)
        pre_spd = float(np.nanmean(spd_arr[pre_start:local_idx])) if local_idx > 0 else float('nan')

        hdg_series = pd.Series(hdg_arr)
        post_start = int(local_idx)
        post_end = min(len(hdg_arr), post_start + int(60 * fs))
        rolling_std = hdg_series.iloc[post_start:post_end].rolling(
            window=max(2, int(5 * fs)), min_periods=2,
        ).std()
        settle_idx = None
        for j, std_val in enumerate(rolling_std):
            if std_val is not None and np.isfinite(std_val) and std_val < 3.0:
                settle_idx = post_start + j
                break

        duration_s = round((settle_idx - local_idx) / fs, 1) if settle_idx is not None else None
        post_spd = float('nan')
        speed_loss = None
        recovery_s = None
        if settle_idx is not None:
            post_spd_end = min(len(spd_arr), settle_idx + settle_n)
            post_spd = float(np.nanmean(spd_arr[settle_idx:post_spd_end]))
            if np.isfinite(pre_spd) and np.isfinite(post_spd):
                speed_loss = pre_spd - post_spd
                if post_spd < pre_spd * 0.95:
                    for k in range(settle_idx, min(len(spd_arr), settle_idx + int(120 * fs))):
                        if spd_arr[k] >= pre_spd * 0.95:
                            recovery_s = round((k - settle_idx) / fs, 1)
                            break

        pre_spd_kts = _kts(pre_spd)
        post_spd_kts = _kts(post_spd)
        speed_loss_kts = _kts(speed_loss) if speed_loss is not None else None
        pre_twa, post_twa = _before_after_twa(twa_arr, int(local_idx), fs)
        reviewable, quality_score, reasons = _maneuver_quality(
            event_type, float(hdg_change), pre_spd_kts, speed_loss_kts,
            pre_twa_deg=pre_twa, post_twa_deg=post_twa,
        )

        duration_n = int(settle_idx - local_idx) if settle_idx is not None else 0
        vmg_metrics = _vmg_metrics(spd_arr, vmg_arr, int(local_idx), settle_idx, fs)
        min_spd_kts = _min_spd_kts(spd_arr, int(local_idx), duration_n, fs)
        max_yaw_rate_deg_s = _max_yaw_rate(hdg_arr, int(local_idx), fs)
        twa_symmetry_deg = _twa_symmetry(pre_twa, post_twa)

        entry = {
            'time_s': time_s,
            'duration_s': duration_s,
            'direction': direction,
            'hdg_change': round(float(hdg_change), 1),
            'pre_spd_kts': pre_spd_kts,
            'post_spd_kts': post_spd_kts,
            'speed_loss_kts': speed_loss_kts,
            'recovery_s': recovery_s,
            'entry_spd_kts': vmg_metrics['entry_spd_kts'],
            'entry_vmg_kts': vmg_metrics['entry_vmg_kts'],
            'spd_recovery_s_80': vmg_metrics['spd_recovery_s_80'],
            'spd_recovery_s_90': vmg_metrics['spd_recovery_s_90'],
            'time_below_80_spd_s': vmg_metrics['time_below_80_spd_s'],
            'distance_lost_m': vmg_metrics['distance_lost_m'],
            'time_lost_s': vmg_metrics['time_lost_s'],
            'min_spd_kts': min_spd_kts,
            'max_yaw_rate_deg_s': max_yaw_rate_deg_s,
            'twa_symmetry_deg': twa_symmetry_deg,
            'abs_idx': int(abs_idx),
            'pre_twa_deg': _round_or_none(pre_twa),
            'post_twa_deg': _round_or_none(post_twa),
            'quality_score': quality_score,
            'quality_reasons': reasons,
            'reviewable': reviewable,
        }
        if reviewable_only and not reviewable:
            continue
        if ts is not None and 0 <= local_idx < len(ts):
            entry['t_unix'] = round(float(pd.Timestamp(ts[local_idx]).timestamp()), 1)
        if event_type == 'tack':
            tacks.append(entry)
        else:
            gybes.append(entry)

    return tacks, gybes


def _kts(v):
    return round(float(v) * G.MS_2_KNOTS, 2) if v is not None and np.isfinite(v) else None


def _round_or_none(v):
    return round(float(v), 1) if v is not None and np.isfinite(v) else None


def _before_after_twa(twa_arr, local_idx, fs):
    if twa_arr is None:
        return float('nan'), float('nan')
    pre = _signed_median_angle(twa_arr[max(0, local_idx - 30 * fs):max(0, local_idx - 10 * fs)])
    post = _signed_median_angle(twa_arr[min(len(twa_arr), local_idx + 10 * fs):min(len(twa_arr), local_idx + 30 * fs)])
    return pre, post


def _signed_median_angle(values):
    v = np.asarray(values, dtype=float)
    v = v[np.isfinite(v)]
    if len(v) == 0:
        return float('nan')
    signed = ((v + 180.0) % 360.0) - 180.0
    return float(np.nanmedian(signed))


def _maneuver_quality(event_type, hdg_change, pre_spd_kts, speed_loss_kts,
                      pre_twa_deg=float('nan'), post_twa_deg=float('nan')):
    """Return ``(reviewable, score, reasons)`` for a detected maneuver.

    The raw heading detector is intentionally high-recall. These conservative
    gates suppress obvious review noise: low-speed wiggles, huge roundings
    misclassified as tacks, and heading changes with no tack-like speed cost.
    """
    reasons = []
    score = 0.0
    abs_turn = abs(float(hdg_change))
    pre_spd = float(pre_spd_kts) if pre_spd_kts is not None else float('nan')
    loss = float(speed_loss_kts) if speed_loss_kts is not None else float('nan')

    if event_type == 'tack':
        if 60.0 <= abs_turn <= 95.0:
            score += 0.4
        else:
            reasons.append('turn outside tack range')
        if np.isfinite(pre_spd) and pre_spd >= 3.5:
            score += 0.3
        else:
            reasons.append('low pre-speed')
        if np.isfinite(loss) and loss >= 0.15:
            score += 0.3
        else:
            reasons.append('no tack-like speed loss')
        return score >= 0.95, round(score, 3), reasons

    # Gybes must be downwind on both sides and must switch TWA sign. This
    # suppresses mark roundings that are simply downwind -> upwind turns.
    pre_twa = float(pre_twa_deg) if pre_twa_deg is not None else float('nan')
    post_twa = float(post_twa_deg) if post_twa_deg is not None else float('nan')
    if 40.0 <= abs_turn <= 180.0:
        score += 0.25
    else:
        reasons.append('turn outside gybe range')
    if np.isfinite(pre_spd) and pre_spd >= 2.0:
        score += 0.15
    else:
        reasons.append('low pre-speed')
    if np.isfinite(pre_twa) and np.isfinite(post_twa) and abs(pre_twa) >= 90.0 and abs(post_twa) >= 90.0:
        score += 0.30
    else:
        reasons.append('not downwind before/after')
    if np.isfinite(pre_twa) and np.isfinite(post_twa) and np.sign(pre_twa) != np.sign(post_twa):
        score += 0.30
    else:
        reasons.append('no TWA sign switch')
    return score >= 0.85, round(score, 3), reasons


def _vmg_metrics(spd_arr, vmg_arr, local_idx, settle_idx, fs):
    """Per-maneuver recovery + loss metrics.

    Recovery (R80, R90, time-below-80) is measured against **boat speed** —
    that's wind-direction-independent, so a post-tack header doesn't pollute
    the "did I execute the tack well" signal. VMG-based recovery says
    "you never recovered" when the truth is "you got back to 5.5 kt boat
    speed but the wind shifted, so VMG is lower."

    Distance/time lost stays in **VMG-space** — those are race-tactical
    metrics where wind angle is part of the cost.

    All values default to ``None`` when their source signal is missing.
    """
    out = {
        'entry_spd_kts': None,
        'entry_vmg_kts': None,
        'spd_recovery_s_80': None,
        'spd_recovery_s_90': None,
        'time_below_80_spd_s': None,
        'distance_lost_m': None,
        'time_lost_s': None,
    }

    # ----- SPD-based recovery & time-below -----
    # spd_arr is in m/s (parquet native). LPF at 0.2 Hz like the slog's
    # spd/sog cutoff. Threshold and entry baseline live in m/s for the
    # recovery search; only convert to knots for the JSON output.
    # Pre-window is [local-50s, local-20s]: 30 s wide, ending 20 s before
    # the heading peak. ``local_idx`` is the heading-peak (max-yaw) sample,
    # but the speed dip begins ~5-15 s earlier from helm anticipation +
    # boat inertia. A 20 s buffer is conservative enough to clear the
    # initiation of any tack/gybe and gives a clean pre-tack baseline.
    # 10 s buffer (matching the TWA convention in _before_after_twa) is
    # *not* enough for SPD because TWA flips fast around head-to-wind
    # while speed has slow inertia-driven dynamics.
    pre_buffer_n = int(20 * fs)
    pre_n = int(50 * fs)
    if spd_arr is not None:
        spd_filt = _lpf(spd_arr, cutoff_hz=0.2)
        n_spd = len(spd_filt)
        pre_start = max(0, local_idx - pre_n)
        pre_end = max(0, local_idx - pre_buffer_n)
        pre_slice = spd_filt[pre_start:pre_end]
        pre_finite = pre_slice[np.isfinite(pre_slice)]
        if len(pre_finite) > 0:
            entry_spd_mps = float(np.nanmean(pre_finite))
            if np.isfinite(entry_spd_mps) and entry_spd_mps > 0:
                out['entry_spd_kts'] = round(entry_spd_mps * G.MS_2_KNOTS, 2)
                post_window_n = int(120 * fs)
                post_end_spd = min(n_spd, local_idx + post_window_n)
                # Last-transition rule: R_X = time of the last sub-threshold
                # sample + 1 (i.e. when the boat *permanently* recovered
                # above threshold within the post-window). Captures the
                # user's mental model — recovery happens AFTER the dip,
                # which can occur tens of seconds past the heading peak
                # because of boat inertia. Preserves R80 <= R90 because the
                # set of samples below 0.8*entry is a subset of those below
                # 0.9*entry (lower threshold ⇒ stricter "below" condition).
                # If no sample is sub-threshold, R_X = 0 (clean tack, never
                # dipped). If the very last sample is still below, return
                # None (not recovered within the 120 s window).
                def _find_recovery_spd(threshold_mps):
                    if post_end_spd - local_idx <= 0:
                        return None
                    window = spd_filt[local_idx:post_end_spd]
                    below = np.isfinite(window) & (window < threshold_mps)
                    if not below.any():
                        return local_idx
                    last_below = int(np.where(below)[0][-1])
                    if last_below >= len(window) - 1:
                        return None
                    return local_idx + last_below + 1

                thr_80_mps = 0.80 * entry_spd_mps
                thr_90_mps = 0.90 * entry_spd_mps
                r80 = _find_recovery_spd(thr_80_mps)
                r90 = _find_recovery_spd(thr_90_mps)
                if r80 is not None:
                    out['spd_recovery_s_80'] = round((r80 - local_idx) / fs, 1)
                if r90 is not None:
                    out['spd_recovery_s_90'] = round((r90 - local_idx) / fs, 1)
                post_slice = spd_filt[local_idx:post_end_spd]
                post_finite_mask = np.isfinite(post_slice)
                if post_finite_mask.any():
                    below_count = int(np.sum((post_slice < thr_80_mps) & post_finite_mask))
                    out['time_below_80_spd_s'] = round(below_count / fs, 1)

    # ----- VMG-based distance & time lost -----
    if vmg_arr is None:
        return out

    vmg_filt = _lpf(vmg_arr, cutoff_hz=0.2)
    n = len(vmg_filt)
    pre_start = max(0, local_idx - pre_n)
    pre_end_vmg = max(0, local_idx - pre_buffer_n)
    pre_slice = vmg_filt[pre_start:pre_end_vmg]
    pre_finite = pre_slice[np.isfinite(pre_slice)]
    if len(pre_finite) == 0:
        return out
    entry_vmg_kts = float(np.nanmean(pre_finite))
    if not np.isfinite(entry_vmg_kts):
        return out
    out['entry_vmg_kts'] = round(entry_vmg_kts, 2)

    post_window_n = int(120 * fs)
    post_end = min(n, local_idx + post_window_n)

    # Distance lost: integrate VMG deficit (in m/s) over a fixed 30 s window
    # post-maneuver. R80/R90 thresholds aren't monotonic across tacks (a tack
    # can bounce above 90% then dip below 80% later from a wind shift or
    # tactical issue), so using R80 as the endpoint folds post-tack sailing
    # into the tack-cost number. The 30 s cap mirrors the 30 s entry-VMG
    # averaging window; the integrand is zero above entry_vmg, so a true
    # recovery within 30 s contributes the right answer naturally.
    entry_vmg_mps = entry_vmg_kts / G.MS_2_KNOTS
    end_idx = min(post_end, local_idx + int(30 * fs))
    if end_idx > local_idx:
        deficit_slice = vmg_filt[local_idx:end_idx]
        deficit_mps = (entry_vmg_kts - deficit_slice) / G.MS_2_KNOTS
        deficit_mps = np.where(np.isfinite(deficit_mps) & (deficit_mps > 0), deficit_mps, 0.0)
        distance_lost_m = float(np.sum(deficit_mps) / fs)
        if np.isfinite(distance_lost_m):
            out['distance_lost_m'] = round(distance_lost_m, 1)
            # entry_vmg_mps > 0.5 gates a divide-by-near-zero on hove-to drifts
            if entry_vmg_mps > 0.5:
                out['time_lost_s'] = round(distance_lost_m / entry_vmg_mps, 1)

    return out


def _min_spd_kts(spd_arr, local_idx, duration_n, fs):
    """Min boat speed during the turn, robust to single-sample GPS glitches.

    LPF speed at 0.5 Hz (2 s period) before taking min — preserves the
    real speed dip through a tack while killing isolated bad samples.
    """
    if spd_arr is None or len(spd_arr) == 0:
        return None
    span = max(int(duration_n), int(10 * fs))
    end = min(len(spd_arr), local_idx + span)
    if end <= local_idx:
        return None
    spd_filt = _lpf(spd_arr[max(0, local_idx - int(2 * fs)):end], cutoff_hz=0.5)
    seg_start = min(int(2 * fs), local_idx)
    seg = spd_filt[seg_start:]
    seg = seg[np.isfinite(seg)]
    if len(seg) == 0:
        return None
    return _kts(float(np.min(seg)))


def _max_yaw_rate(hdg_arr, local_idx, fs):
    """Peak yaw rate (deg/s) during the maneuver.

    Smooth heading at 0.5 Hz (2 s period) before differentiating. Raw 10 Hz
    diffs are dominated by sample-to-sample heading noise; a 1 deg blip
    between consecutive samples reads as 10 deg/s. 0.5 Hz is high enough to
    preserve a 3-5 s tack peak (tack fundamental is ~0.2 Hz) while killing
    higher-frequency instrumentation noise.
    """
    if hdg_arr is None or len(hdg_arr) < 2:
        return None
    start = max(0, local_idx - int(5 * fs))
    end = min(len(hdg_arr), local_idx + int(15 * fs))
    if end - start < 2:
        return None
    seg = hdg_arr[start:end]
    smoothed_rad = _lpf_angle_deg(seg, cutoff_hz=0.5)
    if smoothed_rad is None or len(smoothed_rad) < 2:
        return None
    yaw_rate_rad_s = np.abs(np.diff(smoothed_rad)) * fs
    if len(yaw_rate_rad_s) == 0:
        return None
    peak_deg_s = float(np.rad2deg(np.max(yaw_rate_rad_s)))
    return _round_or_none(peak_deg_s)


def _twa_symmetry(pre_twa, post_twa):
    if pre_twa is None or post_twa is None:
        return None
    if not (np.isfinite(pre_twa) and np.isfinite(post_twa)):
        return None
    return _round_or_none(abs(float(pre_twa)) - abs(float(post_twa)))

