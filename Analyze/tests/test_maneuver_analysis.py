from Analyze.maneuver_analysis import _maneuver_quality


def test_gybe_quality_requires_downwind_twa_sign_switch():
    reviewable, score, reasons = _maneuver_quality(
        'gybe', 42.0, 5.0, 0.2,
        pre_twa_deg=150.0, post_twa_deg=-145.0,
    )
    assert reviewable
    assert score >= 0.85
    assert reasons == []


def test_gybe_quality_rejects_rounding_into_upwind():
    reviewable, score, reasons = _maneuver_quality(
        'gybe', 105.0, 5.0, 0.2,
        pre_twa_deg=150.0, post_twa_deg=40.0,
    )
    assert not reviewable
    assert 'not downwind before/after' in reasons
    assert 'no TWA sign switch' in reasons
