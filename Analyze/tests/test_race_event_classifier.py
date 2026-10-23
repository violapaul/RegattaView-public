import numpy as np
import pandas as pd

from Analyze.race_event_classifier import (
    SUBTYPE_PROBABLE_LEEWARD_ROUNDING,
    SUBTYPE_PROBABLE_START,
    SUBTYPE_PROBABLE_WINDWARD_ROUNDING,
    RaceEventContext,
)
from Libraries.global_variables import G


FS = 10


def _xy_to_latlon(x, y):
    lon, lat = G.MAP(x, y, inverse=True)
    return lat, lon


def _df_from_xy(t, x, y, twd_deg=0.0):
    lat, lon = _xy_to_latlon(x, y)
    vx = np.gradient(x, t)
    vy = np.gradient(y, t)
    sog = np.hypot(vx, vy)
    cog = np.degrees(np.arctan2(vx, vy)) % 360.0
    hdg = cog.copy()
    twd = np.full_like(t, twd_deg, dtype=float)
    awa = ((hdg - twd + 180.0) % 360.0) - 180.0
    return pd.DataFrame({
        'timestamp': pd.to_datetime(t, unit='s', utc=True),
        'latitude': lat,
        'longitude': lon,
        'hdg': hdg,
        'cog': cog,
        'twd': twd,
        'tws': np.full_like(t, 8.0),  # m/s, matching race_logs canonical units
        'sog': sog,
        'spd': sog,
        'awa': awa,
        'twa': ((twd - hdg + 180.0) % 360.0) - 180.0,
    })


def _prestart_box_to_beat():
    pre_s = 700
    post_s = 650
    t = np.arange((pre_s + post_s) * FS) / FS
    pre = t < pre_s
    x = np.zeros_like(t)
    y = np.full_like(t, -80.0)
    # Mostly reaching, bounded, and tortuous before the gun.
    x[pre] = 80.0 * np.sin(2.0 * np.pi * t[pre] / 90.0)
    post_t = t[~pre] - pre_s
    x[~pre] = x[pre][-1]
    y[~pre] = y[pre][-1] + 2.8 * post_t
    return _df_from_xy(t, x, y), pre_s * FS


def _downwind_run_to_beat():
    pre_s = 700
    post_s = 650
    t = np.arange((pre_s + post_s) * FS) / FS
    pre = t < pre_s
    x = np.zeros_like(t)
    y = np.zeros_like(t)
    y[pre] = 1200.0 - 2.8 * t[pre]
    post_t = t[~pre] - pre_s
    y[~pre] = y[pre][-1] + 2.8 * post_t
    return _df_from_xy(t, x, y), pre_s * FS


def _upwind_beat_to_downwind_run():
    pre_s = 700
    post_s = 650
    t = np.arange((pre_s + post_s) * FS) / FS
    pre = t < pre_s
    x = np.zeros_like(t)
    y = np.zeros_like(t)
    y[pre] = 2.8 * t[pre]
    post_t = t[~pre] - pre_s
    y[~pre] = y[pre][-1] - 2.8 * post_t
    return _df_from_xy(t, x, y), pre_s * FS


def _upwind_beat_to_brief_bear_away():
    """Long upwind, brief bear-away (90s downwind), then back upwind.

    Shaped to defeat the legacy state-fraction-only check: the 10-min post
    window has plenty of downwind state, but no sustained downwind leg.
    """
    pre_s = 700
    bear_s = 90
    after_s = 560
    t = np.arange((pre_s + bear_s + after_s) * FS) / FS
    x = np.zeros_like(t)
    y = np.zeros_like(t)
    pre = t < pre_s
    bear = (t >= pre_s) & (t < pre_s + bear_s)
    after = t >= pre_s + bear_s
    y[pre] = 2.8 * t[pre]
    y[bear] = y[pre][-1] - 2.8 * (t[bear] - pre_s)
    y[after] = y[bear][-1] + 2.8 * (t[after] - (pre_s + bear_s))
    return _df_from_xy(t, x, y), pre_s * FS


def test_prestart_box_to_upwind_classifies_as_start():
    df, idx = _prestart_box_to_beat()
    result = RaceEventContext(df).classify_candidate(idx)
    assert result['subtype'] == SUBTYPE_PROBABLE_START
    assert result['features']['pre10_tortuosity'] >= 3.0
    assert result['features']['post10_upwind_frac'] >= 0.75
    assert result['features']['post10_observed_to_polar_vmg_ratio'] > 0.4


def test_downwind_run_to_upwind_classifies_as_leeward_rounding():
    df, idx = _downwind_run_to_beat()
    result = RaceEventContext(df).classify_candidate(idx)
    assert result['subtype'] == SUBTYPE_PROBABLE_LEEWARD_ROUNDING
    assert result['features']['pre10_downwind_frac'] >= 0.75
    assert result['features']['leeward_rounding_score'] >= 0.75


def test_upwind_beat_to_downwind_classifies_as_windward_rounding():
    df, idx = _upwind_beat_to_downwind_run()
    result = RaceEventContext(df).classify_windward_candidate(idx)
    assert result['subtype'] == SUBTYPE_PROBABLE_WINDWARD_ROUNDING
    assert result['features']['pre10_upwind_frac'] >= 0.70
    assert result['features']['previous_upwind_leg_duration_s'] >= 240
    assert result['scores']['windward_rounding_score'] >= 0.67


def test_brief_bear_away_does_not_classify_as_windward_rounding():
    df, idx = _upwind_beat_to_brief_bear_away()
    result = RaceEventContext(df).classify_windward_candidate(idx)
    # Real windward roundings are followed by a sustained downwind leg.
    # A brief bear-away into another beat must not pass the gate.
    assert result['subtype'] is None


def test_polar_ratio_contributes_to_post_beat_quality():
    df, idx = _prestart_box_to_beat()
    # Slow the first beat enough that absolute VMG is marginal, but still a
    # meaningful fraction of the low-wind polar expectation.
    post = np.arange(len(df)) >= idx
    t_post = (np.arange(post.sum()) / FS)
    df.loc[post, 'longitude'] = df.loc[post, 'longitude'].iloc[0]
    y = -80.0 + 1.2 * t_post
    lon, lat = G.MAP(np.zeros_like(y), y, inverse=True)
    df.loc[post, 'latitude'] = lat
    df.loc[post, 'longitude'] = lon
    df.loc[post, 'sog'] = 1.2
    df.loc[post, 'spd'] = 1.2
    df.loc[post, 'cog'] = 0.0
    df.loc[post, 'hdg'] = 0.0
    result = RaceEventContext(df).classify_candidate(idx)
    assert result['features']['post10_mean_upwind_vmg_kts'] < 2.5
    assert result['features']['post10_observed_to_polar_vmg_ratio'] >= 0.4
    assert result['features']['post_beat_score'] >= 0.5
