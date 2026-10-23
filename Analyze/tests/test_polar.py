import numpy as np

from Analyze import polar


def test_in_domain_partitions_correctly():
    assert bool(polar.in_upwind_domain(45.0, 12.0))
    assert not bool(polar.in_upwind_domain(150.0, 12.0))
    assert bool(polar.in_downwind_domain(150.0, 12.0))
    assert not bool(polar.in_downwind_domain(45.0, 12.0))
    assert bool(polar.in_domain(45.0, 12.0))
    assert bool(polar.in_domain(150.0, 12.0))
    assert bool(polar.in_domain(150.0, 4.0))
    assert not bool(polar.in_domain(150.0, 25.0))


def test_speed_dispatches_by_twa():
    s_up = float(polar.speed_kt(45.0, 12.0))
    s_dn = float(polar.speed_kt(150.0, 12.0))
    assert 5.0 <= s_up <= 8.0
    assert 5.0 <= s_dn <= 8.0
    # speed_kt at 45 must equal upwind_speed_kt at 45.
    assert abs(s_up - float(polar.upwind_speed_kt(45.0, 12.0))) < 1e-12
    assert abs(s_dn - float(polar.downwind_speed_kt(150.0, 12.0))) < 1e-12


def test_signed_vmg_sign_convention():
    # Upwind angle: positive (toward wind).
    assert float(polar.vmg_kt(45.0, 12.0)) > 0
    # Downwind angle: negative (away from wind).
    assert float(polar.vmg_kt(150.0, 12.0)) < 0
    # Beam reach: zero regardless of speed.
    assert abs(float(polar.vmg_kt(90.0, 12.0))) < 1e-9


def test_vmgmax_envelopes_monotone_in_tws():
    tws_grid = np.array([0.0, 3.0, 6.0, 8.0, 10.0, 12.0, 14.0, 16.0, 20.0])
    up = polar.upwind_vmgmax_kt(tws_grid)
    down = polar.downwind_vmgmax_kt(tws_grid)
    assert np.all(np.isfinite(up)) and np.all(np.isfinite(down))
    assert up[0] == 0.0
    assert down[0] == 0.0
    assert np.all(np.diff(up) > 0)
    assert np.all(np.diff(down) > 0)


def test_speed_at_grid_points_matches_table():
    for i, twa in enumerate(polar.TWA_GRID):
        for j, tws in enumerate(polar.TWS_GRID):
            v = float(polar.speed_kt(twa, tws))
            assert abs(v - float(polar.SPEED_TABLE_KT[i, j])) < 1e-9


def test_lookup_returns_nan_outside_domain():
    assert np.isnan(float(polar.speed_kt(150.0, -1.0)))
    assert np.isnan(float(polar.speed_kt(150.0, 25.0)))
    assert np.isnan(float(polar.speed_kt(190.0, 12.0)))


def test_speed_below_table_domain_scales_to_zero():
    at_six = float(polar.speed_kt(150.0, 6.0))
    assert float(polar.speed_kt(150.0, 0.0)) == 0.0
    assert abs(float(polar.speed_kt(150.0, 3.0)) - 0.5 * at_six) < 1e-9
    assert abs(float(polar.speed_kt(150.0, 4.0)) - (4.0 / 6.0) * at_six) < 1e-9


def test_vectorized_lookup_shape():
    twa = np.array([45.0, 90.0, 150.0])
    tws = np.array([10.0, 12.0, 14.0])
    out = polar.speed_kt(twa, tws)
    assert out.shape == (3,)
    assert np.all(np.isfinite(out))


def test_vmgmax_at_12_kt_sane():
    up = float(polar.upwind_vmgmax_kt(12.0))
    down = float(polar.downwind_vmgmax_kt(12.0))
    # J/105 polars at 12 kt TWS: VMG ~ 4.8 up, ~ 6.4 down.
    assert 4.0 <= up <= 5.5
    assert 5.5 <= down <= 7.5


def test_vmgmax_fit_matches_table_derived_reference():
    tws_grid = polar.TWS_GRID
    up_table = []
    down_table = []
    up_twa = np.linspace(32.0, 90.0, 117)
    down_twa = np.linspace(90.0, 180.0, 181)
    for tws in tws_grid:
        up_table.append(float(np.nanmax(polar.upwind_vmg_kt(up_twa, tws))))
        down_table.append(float(np.nanmax(polar.downwind_vmg_kt(down_twa, tws))))
    up_fit = polar.upwind_vmgmax_kt(tws_grid)
    down_fit = polar.downwind_vmgmax_kt(tws_grid)
    assert np.max(np.abs(up_fit - np.array(up_table))) < 0.10
    assert np.max(np.abs(down_fit - np.array(down_table))) < 0.15


def test_directional_vmg_helpers_match_signed_vmg():
    # downwind_vmg_kt is the magnitude on the downwind side.
    twa = 150.0
    assert abs(float(polar.downwind_vmg_kt(twa, 12.0)) + float(polar.vmg_kt(twa, 12.0))) < 1e-9
    twa = 45.0
    assert abs(float(polar.upwind_vmg_kt(twa, 12.0)) - float(polar.vmg_kt(twa, 12.0))) < 1e-9
