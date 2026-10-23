import math

import numpy as np
import pytest

from Analyze.start_line import (
    StartLine, infer_start_line, line_from_latlon, DEFAULT_LINE_HALF_LENGTH_M,
)
from Analyze.start_features import compute_derived_channels
from Libraries.global_variables import G


def _line_at_origin(course_bearing_deg: float = 0.0) -> StartLine:
    """East-west line through the origin; course side = north by default."""
    pin = (-100.0, 0.0)           # west
    committee = (100.0, 0.0)      # east
    return StartLine(
        pin_xy=pin,
        committee_xy=committee,
        bearing_deg=90.0,         # pin -> committee points east
        course_bearing_deg=course_bearing_deg,
        source='user',
    )


def test_signed_distance_sign_convention():
    line = _line_at_origin()
    # North of line = course side = positive.
    assert line.signed_distance(np.array([0.0, 50.0])) > 0
    assert line.signed_distance(np.array([0.0, -50.0])) < 0
    # On the line.
    assert abs(line.signed_distance(np.array([0.0, 0.0]))) < 1e-9


def test_signed_distance_vectorized():
    line = _line_at_origin()
    pts = np.array([[0.0, 10.0], [0.0, -10.0], [50.0, 0.0]])
    d = line.signed_distance(pts)
    assert d.shape == (3,)
    assert d[0] > 0 and d[1] < 0
    assert abs(d[2]) < 1e-9


def test_along_line_sign_convention():
    line = _line_at_origin()
    # Pin is at x=-100; a point at (0, 0) is 100 m from pin along +east.
    assert abs(line.along_line(np.array([0.0, 0.0])) - 100.0) < 1e-6
    assert abs(line.along_line(np.array([-100.0, 0.0]))) < 1e-6


def test_to_dict_roundtrip():
    line = _line_at_origin(course_bearing_deg=45.0)
    d = line.to_dict()
    assert 'pin_ll' in d and 'committee_ll' in d
    # Round-trip back through line_from_latlon should reproduce bearing.
    rebuilt = line_from_latlon(d['pin_ll'], d['committee_ll'],
                                course_bearing_deg=45.0)
    assert abs((rebuilt.bearing_deg - line.bearing_deg) % 360) < 1e-2
    assert abs(rebuilt.course_bearing_deg - 45.0) < 1e-6


def test_line_from_latlon_bearing():
    # Build a line by projecting two xy points to lat/lon, then reversing.
    pin_xy = (-50.0, 0.0)
    cc_xy = (50.0, 0.0)
    pin_lon, pin_lat = G.MAP(pin_xy[0], pin_xy[1], inverse=True)
    cc_lon, cc_lat = G.MAP(cc_xy[0], cc_xy[1], inverse=True)
    line = line_from_latlon([pin_lat, pin_lon], [cc_lat, cc_lon])
    assert abs((line.bearing_deg - 90.0) % 360) < 0.5


def test_infer_start_line_clean(clean_start_track):
    df, truth = clean_start_track
    df_e = compute_derived_channels(df)
    line = infer_start_line(df_e, truth['candidate_idx'])
    assert line is not None
    # Course bearing should point upwind (north, twd=0).
    # Allow a few degrees because of circular-mean noise on the synthetic signal.
    assert min(abs(line.course_bearing_deg),
               abs(line.course_bearing_deg - 360)) < 5.0
    # Midpoint should land near the line crossing (origin), within 30 m.
    mx, my = line.midpoint_xy
    assert math.hypot(mx, my) < 30.0
    # Endpoints should be ~perpendicular to wind (bearing ~90 or ~270).
    assert abs((line.bearing_deg % 180) - 90) < 5.0
    # Length should be 2 * DEFAULT_LINE_HALF_LENGTH_M.
    dx = line.pin_xy[0] - line.committee_xy[0]
    dy = line.pin_xy[1] - line.committee_xy[1]
    length = math.hypot(dx, dy)
    assert abs(length - 2 * DEFAULT_LINE_HALF_LENGTH_M) < 1.0


def test_infer_start_line_returns_none_on_missing_twd(clean_start_track):
    df, truth = clean_start_track
    # Drop twd BEFORE derived-channel computation; otherwise twd_true is
    # already materialized and persists past the drop. Real failure mode is
    # a parquet that never had TWD in the first place.
    df_no_twd = df.drop(columns=['twd'])
    df_e = compute_derived_channels(df_no_twd)
    assert 'twd_true' not in df_e.columns
    assert infer_start_line(df_e, truth['candidate_idx']) is None


@pytest.mark.parametrize("twd_true_deg", [0.0, 45.0, 90.0, 135.0, 210.0, 315.0])
def test_twd_frame_alignment(twd_true_deg):
    """Pin the TWD frame convention: ``df['twd']`` is TRUE bearing.

    The boat's instrument displays TWD magnetic; ``estimate_true_wind`` in
    ``Libraries/race_logs`` applies variation internally (``rhdg + variation``)
    so the value stored in ``df['twd']`` is in the true frame. All geometry
    here (line bearing, normal, map placement) assumes that. This test builds
    a synthetic track with a chosen TRUE TWD and asserts:

    1. inferred ``course_bearing_deg`` ≈ TWD (no magnetic offset sneaking in)
    2. line bearing ⟂ wind (bearing mod 180 ≈ 90 relative to TWD)
    3. signed distance sign: a point offset from the midpoint by +50 m in the
       TRUE direction of TWD is on the course side (> 0).
    """
    import numpy as np
    import pandas as pd
    from Libraries.global_variables import G

    fs = 10
    pre_s, post_s = 240, 120
    total = pre_s + post_s
    t = np.arange(total * fs) / fs

    # Position: small jitter prestart, then travel 90 s along the upwind
    # (TWD-true) direction at 5 kt after the gun.
    b = np.radians(twd_true_deg)
    u_east, u_north = np.sin(b), np.cos(b)
    x = np.zeros_like(t); y = np.zeros_like(t)
    loiter = t < pre_s
    x[loiter] = 10.0 * np.sin(2 * np.pi * t[loiter] / 60.0)
    y[loiter] = 10.0 * np.cos(2 * np.pi * t[loiter] / 60.0)
    t_po = t[~loiter] - pre_s
    x[~loiter] = x[loiter][-1] + 2.57 * u_east  * t_po
    y[~loiter] = y[loiter][-1] + 2.57 * u_north * t_po

    lon, lat = G.MAP(x, y, inverse=True)
    hdg = np.full_like(t, twd_true_deg)   # heading up the beat
    cog = np.degrees(np.arctan2(np.gradient(x), np.gradient(y))) % 360
    sog = np.where(loiter, 1.0, 2.57)
    twd = np.full_like(t, twd_true_deg)   # TRUE TWD

    df = pd.DataFrame({
        'timestamp': pd.to_datetime(t, unit='s', utc=True),
        'latitude': lat, 'longitude': lon,
        'hdg': hdg, 'cog': cog, 'sog': sog, 'spd': sog,
        'twd': twd,
        'twa': ((twd - hdg + 180) % 360) - 180,
        'awa': np.zeros_like(t),
    })
    df_e = compute_derived_channels(df)
    cand_idx = int(pre_s * fs)
    line = infer_start_line(df_e, cand_idx)
    assert line is not None

    # (1) course_bearing equals the true TWD within a few degrees
    def ang_diff(a, b):
        return abs(((a - b + 180) % 360) - 180)
    assert ang_diff(line.course_bearing_deg, twd_true_deg) < 5.0, \
        f"course_bearing {line.course_bearing_deg} vs twd_true {twd_true_deg}"

    # (2) line is perpendicular to the wind
    assert ang_diff(line.bearing_deg % 180, (twd_true_deg + 90) % 180) < 5.0 \
        or ang_diff(line.bearing_deg % 180, twd_true_deg % 180) > 85.0

    # (3) a point 50 m toward TWD from the midpoint is on the course side (+)
    mid = np.array(line.midpoint_xy)
    course_pt = mid + 50.0 * np.array([u_east, u_north])
    d_course = float(line.signed_distance(course_pt))
    assert d_course > 0, f"expected +, got {d_course}"

    # ...and 50 m the opposite way is on the prestart side (−)
    pre_pt = mid - 50.0 * np.array([u_east, u_north])
    d_pre = float(line.signed_distance(pre_pt))
    assert d_pre < 0, f"expected -, got {d_pre}"


@pytest.mark.parametrize("twd_true_deg", [0.0, 14.1, 90.0, 357.3])
def test_twd_true_pipeline_shape(twd_true_deg):
    """Post-Stage-1.5 pipeline shape: ``df['twd']`` is in the true frame
    (experimentally pinned in ``Analyze/twd_reference.md``), so
    ``compute_derived_channels`` should emit ``twd_true == twd`` as a
    simple alias. No variation correction is applied here — magnetic-to-
    true is handled inside ``Libraries/race_logs.estimate_true_wind``,
    where ``G.MAGNETIC_VARIATION`` enters the arithmetic once. Doing it
    again in ``compute_derived_channels`` would be a double-variation bug
    (historical behavior before Stage 1.5 — see module docstring of
    ``Analyze/start_line.py``).
    """
    import numpy as np
    import pandas as pd
    from Libraries.global_variables import G

    fs = 10
    pre_s, post_s = 240, 120
    total = pre_s + post_s
    t = np.arange(total * fs) / fs

    # Boat sails upwind along the TRUE TWD after the gun.
    b = np.radians(twd_true_deg)
    u_east, u_north = np.sin(b), np.cos(b)
    x = np.zeros_like(t); y = np.zeros_like(t)
    loiter = t < pre_s
    x[loiter] = 10.0 * np.sin(2 * np.pi * t[loiter] / 60.0)
    y[loiter] = 10.0 * np.cos(2 * np.pi * t[loiter] / 60.0)
    t_po = t[~loiter] - pre_s
    x[~loiter] = x[loiter][-1] + 2.57 * u_east  * t_po
    y[~loiter] = y[loiter][-1] + 2.57 * u_north * t_po

    lon, lat = G.MAP(x, y, inverse=True)
    hdg = np.full_like(t, twd_true_deg)
    cog = np.degrees(np.arctan2(np.gradient(x), np.gradient(y))) % 360
    sog = np.where(loiter, 1.0, 2.57)
    df = pd.DataFrame({
        'timestamp': pd.to_datetime(t, unit='s', utc=True),
        'latitude': lat, 'longitude': lon,
        'hdg': hdg, 'cog': cog, 'sog': sog, 'spd': sog,
        'twd': np.full_like(t, twd_true_deg),     # TRUE (post Stage 1.5)
        'twa': ((twd_true_deg - hdg + 180) % 360) - 180,
        'awa': np.zeros_like(t),
    })
    df_e = compute_derived_channels(df)

    # twd_true must now equal twd exactly (no variation offset).
    got_twd_true = float(df_e['twd_true'].iloc[0])
    assert abs(((got_twd_true - twd_true_deg + 180) % 360) - 180) < 1e-6

    line = infer_start_line(df_e, int(pre_s * fs))
    assert line is not None

    def ang_diff(a, b):
        return abs(((a - b + 180) % 360) - 180)
    # Inferred course_bearing matches the true TWD we synthesized.
    assert ang_diff(line.course_bearing_deg, twd_true_deg) < 5.0, \
        (f"course_bearing {line.course_bearing_deg} should match TRUE twd "
         f"{twd_true_deg}")

    # A point offset from the midpoint in the TRUE TWD direction is on the
    # course side.
    mid = np.array(line.midpoint_xy)
    course_pt = mid + 50.0 * np.array([u_east, u_north])
    assert line.signed_distance(course_pt) > 0


def test_variation_column_is_ignored_by_compute_derived_channels():
    """Regression guard: even if a stale ``variation`` column is present,
    ``compute_derived_channels`` must not add it to ``twd``. Before
    Stage 1.5 the code did ``twd_true = (twd + variation) % 360`` which
    was a double-variation bug."""
    import numpy as np
    import pandas as pd
    from Libraries.global_variables import G

    twd_true_deg = 33.0
    variation_column_garbage_deg = 14.1  # the frame-contaminating value

    fs = 10
    total = 120
    t = np.arange(total * fs) / fs
    x = 2.57 * np.sin(np.radians(twd_true_deg)) * t
    y = 2.57 * np.cos(np.radians(twd_true_deg)) * t
    lon, lat = G.MAP(x, y, inverse=True)
    df = pd.DataFrame({
        'timestamp': pd.to_datetime(t, unit='s', utc=True),
        'latitude': lat, 'longitude': lon,
        'hdg': np.full_like(t, twd_true_deg),
        'cog': np.full_like(t, twd_true_deg),
        'sog': np.full_like(t, 2.57),
        'spd': np.full_like(t, 2.57),
        'twd': np.full_like(t, twd_true_deg),
        'variation': np.full_like(t, variation_column_garbage_deg),
        'twa': np.zeros_like(t),
        'awa': np.zeros_like(t),
    })
    df_e = compute_derived_channels(df)
    # If the old bug returned: twd_true would be twd + variation ≈ 47.1°.
    # Post-fix: twd_true must equal twd exactly.
    assert abs(float(df_e['twd_true'].iloc[0]) - twd_true_deg) < 1e-6, (
        "compute_derived_channels added the variation column to twd — "
        "that's the double-variation bug Stage 1.5 removed."
    )
