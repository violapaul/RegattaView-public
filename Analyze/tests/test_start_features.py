import math

import numpy as np
import pytest

from Analyze.start_features import (
    FEATURE_KEYS,
    FEATURE_SCHEMA_VERSION,
    compute_candidate_features,
    compute_derived_channels,
    compute_features_bulk,
)
from Analyze.start_line import StartLine, infer_start_line


def _line_at_origin() -> StartLine:
    return StartLine(
        pin_xy=(-100.0, 0.0),
        committee_xy=(100.0, 0.0),
        bearing_deg=90.0,
        course_bearing_deg=0.0,   # course side = north
        source='user',
    )


def test_feature_schema_stability():
    # Guards Phase D: training-row builder assumes stable keys per version.
    assert FEATURE_SCHEMA_VERSION == 1
    assert 'd_line_at_cross_m' in FEATURE_KEYS
    assert 'post60_best_single_tack_upwind_m' in FEATURE_KEYS
    assert 'reverse_cross_within_90s' in FEATURE_KEYS


def test_all_features_nan_without_line(clean_start_track):
    df, truth = clean_start_track
    df_e = compute_derived_channels(df)
    assert 'rel_cog_to_twd_deg' in df_e.columns
    assert 'upwind_vmg_kts' in df_e.columns
    # Back-compat aliases should stay exactly aligned with canonical columns.
    np.testing.assert_allclose(df_e['rel_cog'], df_e['rel_cog_to_twd_deg'])
    np.testing.assert_allclose(df_e['upwind_vmg'], df_e['upwind_vmg_kts'])
    feats = compute_candidate_features(df_e, truth['candidate_idx'], line=None)
    for key in (
        'd_line_at_cross_m', 'cross_dir_sign', 'cross_speed_kts',
        'd_line_min_prestart_m', 'time_within_30m_prestart_s',
        'along_line_at_cross_m',
    ):
        assert math.isnan(feats[key]), f"{key} should be NaN when line is None"
    # Non-line features should still be finite.
    assert np.isfinite(feats['post60_upwind_m'])
    assert np.isfinite(feats['prestart_tortuosity'])


def test_clean_start_features(clean_start_track):
    df, truth = clean_start_track
    df_e = compute_derived_channels(df)
    line = _line_at_origin()
    feats = compute_candidate_features(df_e, truth['candidate_idx'], line=line)

    # Crossing happens at the candidate index in this fixture, so distance
    # at cross should be near zero and the direction should be +1 (toward
    # course side).
    assert abs(feats['d_line_at_cross_m']) < 5.0
    assert feats['cross_dir_sign'] == 1.0
    assert feats['cross_speed_kts'] > 1.0

    # Post-start upwind progress: ~5 kt for 90 s ~= 231 m. Allow wide margin.
    assert feats['post60_upwind_m'] > 100.0

    # Loiter prestart should be highly tortuous.
    assert feats['prestart_tortuosity'] > 2.0

    # Approach was circling, not settled.
    assert feats['approach_hdg_settled'] == 0.0

    # No OCS pattern in this clean fixture.
    assert feats['reverse_cross_within_90s'] == 0.0


def test_transit_features(transit_track):
    df, truth = transit_track
    df_e = compute_derived_channels(df)
    line = _line_at_origin()
    feats = compute_candidate_features(df_e, truth['candidate_idx'], line=line)

    # Straight line through the line = near-unity tortuosity.
    assert feats['prestart_tortuosity'] < 1.1

    # Heading has been steady for the whole prestart.
    assert feats['approach_hdg_settled'] == 1.0


def test_bulk_feature_extraction(clean_start_track):
    df, truth = clean_start_track
    line = _line_at_origin()
    indices = np.array([truth['candidate_idx']])
    out = compute_features_bulk(df, indices, line_resolver=lambda i: line)
    assert len(out) == 1
    assert 'idx' in out.columns
    for key in FEATURE_KEYS:
        assert key in out.columns
    assert int(out.iloc[0]['idx']) == truth['candidate_idx']
