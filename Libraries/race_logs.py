"""
Reads race logs and prepares for visualization and analysis.

Also manages metadata for each race.
"""
import os
import itertools as it
import re
from types import SimpleNamespace

from numba import jit

import pandas as pd
import numpy as np
import scipy

from .log_helper import LOG
from .global_variables import G
from .dictclass import DictClass
from . import process as p
from . import metadata
from . import true_wind


# COLUMN CLEANUP ################################################################
# White list of columns that have practical meaning.
COL_WHITE_LIST = [
    'altitude',           'awa',             'aws',
    'cog',                'depth',           'filename',
    'hdg',             'latitude',
    'longitude',          'rawa',            'raws',
    'rcog',               'rhdg',
    'row_seconds',
    'rsog',
    'rspd',               'rtwa',            'rtwd',
    'rtws',               'rudder',           'sog',
    'spd',                'timestamp',
    'turn_rate',          'twa',             'variation',
    'zeus_altitude',      'zeus_cog',        'zeus_gnss_type',
    'zeus_sog',           'zg100_pitch',     'zg100_roll',
    'timestamp'          
]

COL_RENAME = dict(wind_angle='rawa',
                  wind_speed='raws',
                  sog='rsog',
                  cog='rcog',
                  true_wind_speed='rtws',
                  true_wind_angle='rtwa',
                  true_north_wind_angle='rtwd',
                  heading='rhdg',
                  rudder_position='rudder',
                  speed_water_referenced='rspd')

def df_retain_columns(df, white_list):
    "Drop columns that bring no value.  Ultimately important because the data gets big."
    white_list = set(white_list)
    to_drop = [col for col in df.columns if col not in white_list]
    return df.drop(columns=to_drop)

# Pre-processing of Log Data ##################################################################

@jit(nopython=True)
def estimate_true_wind_helper(epsilon, aws, awa, hdg, spd, cog, sog,
                              tws_init, twd_init, variation, tws_mult, time_delay):
    "Documented in True_Wind.ipynb"
    # TWD/TWS are ouptputs.  This sets the initial conditions.
    ptwd = np.radians(twd_init) + np.zeros(awa.shape)
    ptws = tws_init + np.zeros(awa.shape)
    # Residuals are stored here.
    res_n = np.zeros(awa.shape)
    res_e = np.zeros(awa.shape)
    deps = np.zeros(awa.shape)
    # Process apparent wind to decompose into boat relative components.
    aw_n = aws * np.cos(np.radians(awa))
    aw_e = aws * np.sin(np.radians(awa))

    # preconvert some values
    rhdg = np.radians(hdg)
    rcog = np.radians(cog)
    variation = np.radians(variation)
    
    eps = epsilon
    for i in range(1, len(aws)-time_delay):
        # Transform to boat relative angles.
        twa = ptwd[i-1] - (rhdg[i] + variation)
        course_angle = rcog[i+time_delay]  - (rhdg[i] + variation)
        
        # Useful below
        c = np.cos(twa)
        s = np.sin(twa)
        
        # Boat relative vector of true wind
        twn = c * ptws[i-1]
        twe = s * ptws[i-1]
        
        # Boat relative vector of travel
        btn = np.cos(course_angle) * sog[i+time_delay]
        bte = np.sin(course_angle) * sog[i+time_delay]
        
        # Forward prediction of apparent wind in the boat frame.
        # AW_from = TW_from + boat-induced wind-from (a boat moving toward
        # `course_angle` at `sog` feels a headwind FROM that bearing), so the
        # boat-motion term ADDS in BOTH components. The east term was
        # historically `twe - bte`, which is a sign bug: a synthetic
        # ground-truth check (Analyze/diag_prod_gybe.py) shows `+bte` gives an
        # exactly-zero residual at the true wind for all points of sail, while
        # `-bte` leaves a 2.5-3.3 deg residual at the truth and biases the
        # solver. The bug mostly cancels upwind but blows up downwind, the
        # source of the ~9 deg gybe shear; `+bte` cuts gybe shear to ~4 deg.
        # Gradients below only differentiate twn/twe, so the sign of the
        # boat-motion term does not change the update rule.
        f_aw_n = twn + btn
        f_aw_e = twe + bte

        # Residuals
        res_n[i] = (aw_n[i] - f_aw_n)
        res_e[i] = (aw_e[i] - f_aw_e)

        # derivatives
        delta_tws = res_n[i] * c + res_e[i] * s
        delta_twd = res_n[i] * ptws[i-1] * -s + res_e[i] * ptws[i-1] * c
        
        # The mathematics allows for solutions were tws is negative, particularly when the
        # wind directions switches rapidly.  Need to ensure its positive.
        ptws[i] = np.absolute(eps * tws_mult * delta_tws + ptws[i-1])
        ptwd[i] = eps * delta_twd + ptwd[i-1]

        # Check the current residuals.  If they are too large then gradually decrease the
        # smoothness (increase epsilon).
        res_error = (np.abs(res_n[i]) + np.abs(res_e[i]))
        if res_error > 1.0 and res_error < 5.0:
            eps = min(1.05 * eps, 10*epsilon)
        elif res_error > 5.0:
            eps = min(1.1 * eps, 1000*epsilon)
        else:
            eps = (eps + epsilon) / 2
        deps[i] = eps

    return np.degrees(ptwd), ptws, res_n, res_e, deps


def estimate_true_wind(epsilon, df, awa_mult=1.0, aws_mult=1.0, spd_mult=1.0, awa_offset=0, tws_mult=30, time_delay=12):
    # Variation is sourced from the venue-scoped global, NOT the on-board
    # PGN 127258 column. The instruments carry a frozen WMM epoch and are ~1°
    # stale (see Libraries/global_variables.py::init_seattle for details).
    variation = G.MAGNETIC_VARIATION
    tws_init = df.aws.iloc[0]
    twd_init = df.rawa.iloc[0] + df.rhdg.iloc[0] + variation
    (twd, tws, res_n, res_e, deps) = estimate_true_wind_helper(epsilon,
                                                         aws = aws_mult * np.asarray(df.caws),
                                                         awa = awa_mult * np.asarray(df.cawa) + awa_offset,
                                                         hdg = np.asarray(df.rhdg),
                                                         spd = spd_mult * np.asarray(df.rspd),
                                                         cog = np.asarray(df.rcog),
                                                         sog = np.asarray(df.rsog),
                                                         tws_init = tws_init,
                                                         twd_init = twd_init,
                                                         variation = variation,
                                                         tws_mult = tws_mult,
                                                         time_delay = time_delay)
    df['twd'] = p.compass_angle(twd)
    df['tws'] = tws
    df['twa'] = p.sign_angle(twd - (df.rhdg + variation))
    return deps


def add_offline_true_wind_products(df):
    """Populate explicit water-ref fallback and ground-ref true wind columns."""
    try:
        water = true_wind.estimate_offline_wind(df, mode="water")
        df['stwd'] = p.compass_angle(water['twd'].values)
        df['stws'] = water['tws'].values
        df['stwa'] = p.sign_angle(water['twa'].values)
    except Exception as exc:
        LOG.logger.warning(
            "Vector water-ref true-wind fallback failed; using legacy helper: "
            f"{type(exc).__name__}: {exc}")
        estimate_true_wind(0.00001, df, tws_mult=16)
        df['stwd'] = p.compass_angle(df.twd)
        df['stws'] = df.tws
        df['stwa'] = df.twa

    try:
        ground = true_wind.estimate_offline_wind(df, mode="ground")
    except Exception as exc:
        LOG.logger.warning(
            "Vector ground-ref true-wind estimate failed: "
            f"{type(exc).__name__}: {exc}")
        n = len(df)
        df['gtwd'] = np.full(n, np.nan)
        df['gtws'] = np.full(n, np.nan)
        df['gtwa'] = np.full(n, np.nan)
        return

    df['gtwd'] = p.compass_angle(ground['twd'].values)
    df['gtws'] = ground['tws'].values
    df['gtwa'] = p.sign_angle(ground['twa'].values)

    
def heel_corrected_apparent_wind(df):
    """
    Heel (aka roll) introduces motion at the masthead, which can in turn impact AWA.
    Correct this.  See True_Wind.ipynb
    """

    # Compute a smoothed version of heel.  Note the cutoff is set to 0.3 Hz.  Since the
    # signal is sampled at 1 Hz.  Nyquist says we cannot hope measure any signal higher
    # than 0.5 Hz.
    coeff = p.butterworth_filter(cutoff=0.3, order=5)

    # Smooth the heel signal.
    # Note causal is false, which will filter the signal but not introduce any delay.
    sheel = p.smooth_angle(coeff, df.zg100_roll, causal=False)

    # Take the derivative, which is rotation rate
    heel_rate = G.SAMPLES_PER_SECOND * np.diff(sheel, prepend=sheel[0])

    # Compute the velocity of the masthead, perpendicular to the boat.
    mast_vel = G.MAST_HEIGHT * np.radians(heel_rate)

    # divide into boat coords, n is positive forward, e is positive to starboard
    aw_n = df.raws * p.cos_d(df.rawa)
    aw_e = df.raws * p.sin_d(df.rawa)

    # Corrected
    caw_e = aw_e - mast_vel

    # Unsmoothed
    df['cawa'] = np.degrees(np.arctan2(caw_e, aw_n))
    df['caws'] = np.sqrt(np.square(aw_n) + np.square(caw_e))

    # Compute smoothed versions
    theta = 0.8
    alpha = 0.97
    saw_n, _ = p.exponential_filter(np.array(aw_n), alpha, theta)
    scaw_e, _ = p.exponential_filter(np.array(caw_e), alpha, theta)

    df['scawa'] = np.degrees(np.arctan2(scaw_e, saw_n))
    df['scaws'] = np.sqrt(np.square(saw_n) + np.square(scaw_e))


def process_sensors(df, causal=False, cutoff=0.3):
    """
    Process sensor data to normalize and smooth.  Note, this done on the boat, before
    display, in real-time, but not logged.  We do our best to simulate that here.
    """
    # Staleness check: if the on-board variation has drifted far from the
    # authoritative global, flag it so we know to revisit G.MAGNETIC_VARIATION
    # or recognize that an instrument was re-flashed to a newer WMM epoch.
    if 'variation' in df.columns:
        v_onboard = df['variation'].dropna()
        if len(v_onboard):
            delta = float(v_onboard.median()) - G.MAGNETIC_VARIATION
            if abs(delta) > 1.5:
                LOG.logger.warning(
                    f"On-board variation median {v_onboard.median():.2f}° differs from "
                    f"G.MAGNETIC_VARIATION={G.MAGNETIC_VARIATION:.2f}° by {delta:+.2f}°. "
                    "Revisit the global or confirm the instruments haven't been re-flashed."
                )

    # first awa and aws.
    df.rawa = p.sign_angle(df.rawa)

    aw_n = df.raws * p.cos_d(df.rawa)
    aw_e = df.raws * p.sin_d(df.rawa)

    theta = 0.8
    alpha = 0.97
    saw_n, _ = p.exponential_filter(np.array(aw_n), alpha, theta)
    saw_e, _ = p.exponential_filter(np.array(aw_e), alpha, theta)

    df['awa'] = np.degrees(np.arctan2(saw_e, saw_n))
    df['aws'] = np.sqrt(np.square(saw_e) + np.square(saw_n))

    heel_corrected_apparent_wind(df)

    # If TWA/TWD/TWS is available from the boat, grab it.
    if 'rtwa' in df.columns:
        df['boat_twa'] = p.sign_angle(df.rtwa)
        df['twa'] = p.sign_angle(df.rtwa)
        df['rtwa'] = p.sign_angle(df.rtwa)        
    if 'rtws' in df.columns:
        df['boat_tws'] = df.rtws
        df['tws'] = df.rtws
    if 'rtwd' in df.columns:
        df['boat_twd'] = df.rtwd
        df['twd'] = df.rtwd

    # Explicit offline products:
    #   st* = water-reference fallback calibrated to match on-boat rtwd/rtws.
    #   gt* = physically calibrated ground-reference wind over ground.
    add_offline_true_wind_products(df)
    
    # Replace with computed quantity
    estimate_true_wind(0.0001, df, tws_mult=16)
    df['twd'] = p.compass_angle(df.twd)

    # Less noise and smaller theta values.
    df['spd'], _ = p.exponential_filter(np.array(df.rspd), 0.8, 0.5)
    df['sog'], _ = p.exponential_filter(np.array(df.rsog), 0.8, 0.5)

    df['hdg'], _ = p.exponential_filter_angle(np.array(df.rhdg), 0.8, 6)
    df['cog'], _ = p.exponential_filter_angle(np.array(df.rcog), 0.9, 6)

    # Frame-aliased columns. Makes every bearing's reference frame
    # self-documenting for downstream consumers. Kept additive: the legacy
    # hdg/cog/twd columns are unchanged; readers can migrate to the
    # explicit aliases at their own pace.
    #
    #   hdg  is MAGNETIC (source: PGN 127250 Reference=Magnetic on Peer Gynt
    #                     — confirmed in Analyze/twd_reference.md);
    #   cog  is TRUE     (source: GPS, always true);
    #   twd  is TRUE     (output of estimate_true_wind, which converts
    #                     rhdg via G.MAGNETIC_VARIATION before comparing
    #                     to rcog).
    var = G.MAGNETIC_VARIATION
    df['hdg_mag']  = p.compass_angle(df['hdg'].values)
    df['hdg_true'] = p.compass_angle(df['hdg'].values + var)
    df['cog_true'] = p.compass_angle(df['cog'].values)
    df['cog_mag']  = p.compass_angle(df['cog'].values - var)
    df['twd_true'] = p.compass_angle(df['twd'].values)
    df['twd_mag']  = p.compass_angle(df['twd'].values - var)
    if 'gtwd' in df.columns:
        df['gtwd_true'] = p.compass_angle(df['gtwd'].values)
        df['gtwd_mag'] = p.compass_angle(df['gtwd'].values - var)
    add_navigation_derived_channels(df, inplace=True)


def add_navigation_derived_channels(df, inplace=False):
    """Add canonical navigation-derived analysis columns.

    These are not raw instruments; they are shared derived quantities that
    downstream analysis should not recompute differently:

    - ``x_m`` / ``y_m``: projected east/north meters via ``G.MAP``.
    - ``rel_cog_to_twd_deg``: signed true-frame ``cog_true - twd_true``.
    - ``upwind_vmg_kts``: ground-referenced upwind VMG in knots.

    Back-compat aliases ``rel_cog`` and ``upwind_vmg`` are populated with the
    same values for existing analysis code.
    """
    out = df if inplace else df.copy()

    if 'x_m' not in out.columns or 'y_m' not in out.columns:
        lon = out['longitude'].values
        lat = out['latitude'].values
        mask = np.isfinite(lon) & np.isfinite(lat)
        x_m = np.full(len(out), np.nan)
        y_m = np.full(len(out), np.nan)
        if mask.any():
            mx, my = G.MAP(lon[mask], lat[mask])
            x_m[mask] = mx
            y_m[mask] = my
        out['x_m'] = x_m
        out['y_m'] = y_m

    if 'twd_true' not in out.columns and 'twd' in out.columns:
        out['twd_true'] = p.compass_angle(out['twd'].values)

    if 'cog_true' not in out.columns and 'cog' in out.columns:
        out['cog_true'] = p.compass_angle(out['cog'].values)

    if ('rel_cog_to_twd_deg' not in out.columns
            and 'cog_true' in out.columns
            and 'twd_true' in out.columns):
        out['rel_cog_to_twd_deg'] = p.angle_diff_d(
            out['cog_true'].values, out['twd_true'].values,
        )

    if 'rel_cog' not in out.columns and 'rel_cog_to_twd_deg' in out.columns:
        out['rel_cog'] = out['rel_cog_to_twd_deg'].values

    if ('upwind_vmg_kts' not in out.columns
            and 'sog' in out.columns
            and 'rel_cog_to_twd_deg' in out.columns):
        sog_kts = out['sog'].values * G.MS_2_KNOTS
        out['upwind_vmg_kts'] = sog_kts * np.cos(np.radians(out['rel_cog_to_twd_deg'].values))

    if 'upwind_vmg' not in out.columns and 'upwind_vmg_kts' in out.columns:
        out['upwind_vmg'] = out['upwind_vmg_kts'].values

    return out


def find_sail_logs(directory):
    "Relies on the extension .pd"
    return sorted([f for f in os.listdir(directory) if re.match(r'.*\.pd', f)])

def clamp_monotonic_deltas(gps_times_sec, row_seconds):
    """Given raw GPS unix-seconds and RPi row_seconds (both arrays), return a
    strictly-monotonic `deltas` array (seconds since first sample) that
    tracks the GPS clock while staying robust to:
      - ms-scale jitter from linear interpolation between time messages,
      - large backward "glitches" (>60s) from BB clock resets or bad NMEA,
      - forward jumps that would over-correct via proportional gain.

    This is the shared core used at parquet-WRITE time (by compute_pandas_log)
    to scrub `unixtime` into a monotonic stream, and at parquet-READ time
    (by compute_record_times) to recover `row_times` on any legacy parquet
    whose `unixtime` still has pre-fix anomalies.
    """
    gps_delta_sec = np.asarray(gps_times_sec, dtype=np.float64)
    gps_delta_sec = gps_delta_sec - gps_delta_sec.min()
    gps_delta_diff = np.diff(gps_delta_sec, prepend=0)
    row_arr = np.asarray(row_seconds, dtype=np.float64)
    row_arr = row_arr - row_arr.min()
    deltas = np.zeros(row_arr.shape)
    feed_forward = float(gps_delta_diff.mean())
    offset = 0.0
    proportional = 0.01
    integral = 0.0
    sum_error = 0.0
    # Monotonicity clamp: row_times/unixtime must never step backward.
    MIN_STEP = 1e-6  # seconds
    # Reject GPS updates that would require an absurd correction. Legitimate
    # RPi clock drift is ~60ms/hour, so real errors stay well under a second;
    # |error| >> that signals a clock glitch, day/timezone wraparound, or
    # concatenated-session confusion.
    MAX_REASONABLE_ERROR = 60.0  # seconds
    for i in range(1, len(gps_delta_sec)):
        step = feed_forward + offset
        deltas[i] = deltas[i-1] + max(step, MIN_STEP)
        if gps_delta_diff[i] > 0.1:
            error = gps_delta_sec[i] - deltas[i]
            if abs(error) > MAX_REASONABLE_ERROR:
                continue  # treat as bad data, keep current offset
            sum_error += error
            offset = proportional * error + integral * sum_error
    return deltas


def compute_record_times(df):
    """Add a monotonic `row_times` column derived from `unixtime` + `row_seconds`.

    Kept for backward compat: any legacy parquet whose `unixtime` column has
    residual backward steps (from pre-fix pipeline runs) is repaired at read
    time. New parquets written by the fixed pipeline already have monotonic
    `unixtime`, so this is effectively a no-op on those.
    """
    base_time = df.timestamp.min()
    gps_delta_sec = (df.timestamp - base_time) / pd.Timedelta('1s')
    deltas = clamp_monotonic_deltas(gps_delta_sec, df.row_seconds)

    res_sec = deltas * pd.Timedelta('1s')
    # Convert back to times and set the correct timezone.
    df['row_times'] = (np.datetime64(base_time) + res_sec).reshape(-1)
    df.row_times = df.row_times.dt.tz_localize('UTC').dt.tz_convert('US/Pacific')


def read_log_file(pickle_file, skip_dock_only=True, discard_columns = True,
                  rename_columns=True, trim=True,
                  process=True, cutoff=0.3,
                  path=None):
    """
    SKIP_DOCK_ONLY filters examples where the boat never gets of the dock, otherwise returns None
    DISCARD_COLUMNS drops columns that are not particular helpful
    RENAME_COLUMNS provides more friendly standard names
    TRIM trims off beginning of the log where some values are problematic or missing
    PROCESS proceses the data values to be more useable (originals are preserved)
    CUTOFF is the Butterworth cutoff frequency for smoothing, in Hz.
    PATH is the path to the pickle file.
    """
    if path is None:
        path = G.PANDAS_LOGS_DIRECTORY
    full_path = os.path.join(path, pickle_file) if path else pickle_file
    # check file exists
    if not os.path.exists(full_path):
        LOG.logger.warning(f"File {full_path} does not exist.")
        return None
    # if it is parquet, read it as parquet
    if pickle_file.endswith('.parquet'):
        df = pd.read_parquet(full_path)
    else:  # assume it is pickle
        df = pd.read_pickle(full_path)
    LOG.logger.info(f"Read {pickle_file} with {len(df)} records.")
    df.filename = pickle_file
    return process_pickled_data(df, skip_dock_only, discard_columns, rename_columns, trim, process, cutoff)

def process_pickled_data(df, skip_dock_only=True, discard_columns = True,
                  rename_columns=True, trim=True,
                  process=True, cutoff=0.3):
    filename = df.filename        
    df = df[df[['date', 'time']].isna().sum(1) == 0].copy()
    df.filename = filename
    if 'timestamp' not in df.columns:
        if 'unixtime' not in df.columns:
            try:
                df['timestamp'] = pd.to_datetime(df.date + " " + df.time)
            except:
                df['timestamp'] = pd.to_datetime(df.date + " " + df.time, format='ISO8601')
        else:
            df['timestamp'] = pd.to_datetime(df['unixtime'], unit='s').dt.tz_localize('UTC')
            df['row_times'] = df.timestamp.dt.tz_convert(G.TIMEZONE)
    session_length = df.timestamp.iloc[-1] - df.timestamp.iloc[0]
    hours = session_length.seconds / (60 * 60)
    LOG.logger.info(f"Session from {df.timestamp.iloc[0]}, {len(df)} rows, {hours} hours.")
    if skip_dock_only and df.speed_water_referenced.max() < 0.1:
        LOG.logger.warn(f" {filename} ... skipping, the boat does not move!")
        return None
    # Not sure why cog is often missing
    if 'zeus_cog' in df.columns:
        if 'cog' in df.columns:
            df.cog = df.cog.fillna(value=df.zeus_cog)
        else:
            df['cog'] = df.zeus_cog
    if rename_columns:
        df = df.rename(columns=COL_RENAME)
    if discard_columns:
        df = df_retain_columns(df, COL_WHITE_LIST)
    # Raw NMEA true wind is often missing early in a session (Zeus hasn't computed it yet).
    # Fill with 0 before trim so we don't lose morning data — estimate_true_wind recomputes these.
    for col in ['rtws', 'rtwa', 'rtwd']:
        if col in df.columns:
            df[col] = df[col].fillna(0)
    # trim off the initial startup where data is missing
    if trim:
        df = df[df.isna().sum(1) == 0]
    clean_gps_outliers(df)
    if process:
        process_sensors(df, causal=False, cutoff=cutoff)
        if 'row_times' not in df.columns:
            compute_record_times(df)
    return df

def process_bb_logs(df, skip_dock_only=True, discard_columns = True,
                  rename_columns=True, trim=True,
                  process=True, cutoff=0.3):
    filename = df.filename
    session_length = df.timestamp.iloc[-1] - df.timestamp.iloc[0]
    hours = session_length.seconds / (60 * 60)
    LOG.logger.info(f"Session from {df.timestamp.iloc[0]}, {len(df)} rows, {hours} hours.")
    if skip_dock_only and df.speed_water_referenced.max() < 0.1:
        LOG.logger.warn(f" {filename} ... skipping, the boat does not move!")
        return None
    # Not sure why cog is often missing
    if 'zeus_cog' in df.columns:
        if 'cog' in df.columns:
            df.cog = df.cog.fillna(value=df.zeus_cog)
        else:
            df['cog'] = df.zeus_cog
    if rename_columns:
        df = df.rename(columns=COL_RENAME)
    if discard_columns:
        df = df_retain_columns(df, COL_WHITE_LIST)
    for col in ['rtws', 'rtwa', 'rtwd']:
        if col in df.columns:
            df[col] = df[col].fillna(0)
    # trim off the initial startup where data is missing
    if trim:
        df = df[df.isna().sum(1) == 0]
    clean_gps_outliers(df)
    if process:
        process_sensors(df, causal=False, cutoff=cutoff)
        if 'row_times' not in df.columns:
            df['row_times'] = df.timestamp.dt.tz_localize('UTC').dt.tz_convert('US/Pacific')
    return df


def clean_gps_outliers(df):
    # Every now and then you see crazy dropouts in GPS
    smooth_longitude = scipy.signal.medfilt(df.longitude, 5)
    smooth_latitude = scipy.signal.medfilt(df.latitude, 5)
    if np.max(np.abs(df.longitude - smooth_longitude)) > 0.1:
        LOG.logger.info(f"Noise in longitude, using median filter.")
        df.longitude = smooth_longitude
    if np.max(np.abs(df.latitude - smooth_latitude)) > 0.1:
        LOG.logger.info(f"Noise in latitude, using median filter.")
        df.latitude = smooth_latitude


def read_logs(log_entries, race_trim=True, **args):
    """
    SKIP_DOCK_ONLY filters examples where the boat never gets of the dock
    CUTOFF is the Butterworth cutoff frequency for smoothing, in Hz.
    """
    dfs = []
    for i, log in zip(it.count(), log_entries):
        if 'file' not in log:
            LOG.logger.warning(f"Log entry {i} does not have a file name.")
            continue
        LOG.logger.info(f"Reading file {log.file}")
        pickle_file = log.file
        df = read_log_file(pickle_file, **args)
        if df is None:
            continue
        LOG.logger.info(f"Found {len(df)} records before trim.")
        if race_trim:
            df = df.iloc[log.begin: log.end].copy()
            LOG.logger.info(f"Trimming to {log.begin} {log.end}")
        # Weird requirement that you can't add a dict to a Dataframe
        # https://stackoverflow.com/a/54137536
        ns = SimpleNamespace(**log)
        df.log = ns
        dfs.append(df)
    valid_dfs = [df for df in dfs if df is not None]
    return valid_dfs, pd.concat(valid_dfs, sort=True, ignore_index=True)

def read_date(date, **args):
    dfs, races, bigdf = read_dates([date], **args)
    if len(dfs) < 1:
        raise Exception(f"Date not found {date}.")
    return dfs[0], races[0]

def read_entries(race_entries, **args):
    races = [DictClass(**e) for e in race_entries]
    dfs, big_df = read_logs(races, path=G.PANDAS_LOGS_DIRECTORY, **args)
    return dfs, races, big_df

def read_dates(dates, **args):
    "Convience function.  Given a list of text dates YYYY-MM-DD, will return races on those dates."
    md = metadata.read_metadata()
    races = []
    for date in dates:
        log = md.dates.get(date, None)
        if log is None:
            LOG.logger.warn(f"Warning, {date} could not be found in the race logs.")
        else:
            races.append(log)
    return read_entries(races, **args)

def trim_race(race, begin, end):
    "Trim the race data by specifying the begin/end.  Used to trim away time at the dock, etc."
    race.begin = begin
    race.end = end
    # casting back to a dict to support YAML serialization
    race_dict = dict(**race)
    metadata.update_race(race_dict)
