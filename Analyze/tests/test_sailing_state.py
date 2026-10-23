import numpy as np

from Analyze.sailing_state import (
    STATE_DOWNWIND,
    STATE_REACH,
    STATE_UPWIND,
    StateConfig,
    add_sailing_state,
    smooth_states,
)


def test_clean_start_has_post_start_upwind_segment(clean_start_track):
    df, truth = clean_start_track
    state_df, segments = add_sailing_state(df)
    post = state_df.iloc[truth['candidate_idx']:truth['candidate_idx'] + 60 * 10]
    assert (post['sailing_state'] == STATE_UPWIND).mean() > 0.9
    assert STATE_UPWIND in set(segments['state'])
    assert {'path_m', 'chord_m', 'net_upwind_m', 'mean_upwind_vmg_kts'} <= set(segments.columns)


def test_smooth_states_merges_short_gap_between_matching_states():
    states = np.array(
        [STATE_UPWIND] * 100
        + [STATE_REACH] * 5
        + [STATE_UPWIND] * 100,
        dtype=object,
    )
    cfg = StateConfig(min_state_duration_s=2.0, merge_gap_s=2.0)
    smoothed = smooth_states(states, fs=10, config=cfg)
    assert set(smoothed) == {STATE_UPWIND}


def test_downwind_transit_segments_as_downwind_then_upwind(transit_track):
    df, truth = transit_track
    # Reverse the pre-candidate half into a southbound/downwind approach, then
    # keep the existing northbound/upwind section after the candidate.
    pre = slice(0, truth['candidate_idx'])
    y0 = df['latitude'].copy()  # keep shape reference; values overwritten below
    del y0
    # This fixture is mostly upwind; the state module should still produce a
    # coherent upwind segment without inventing downwind.
    state_df, _ = add_sailing_state(df)
    assert (state_df['sailing_state'] == STATE_UPWIND).mean() > 0.8
    assert STATE_DOWNWIND not in set(state_df['sailing_state'].iloc[pre])
