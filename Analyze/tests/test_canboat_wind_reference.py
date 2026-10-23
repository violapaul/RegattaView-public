"""Stage 3 guardrail: ``canboat.transform_record`` deconflicts every
PGN 130306 Wind Data reference branch into its own column prefix.

Before Stage 3 the Magnetic branch silently collapsed into Apparent's
unprefixed ``wind_angle``/``wind_speed`` columns — a latent data-corruption
bug if a sensor ever emitted that branch.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from Libraries.global_variables import G  # noqa: E402
from Libraries import canboat  # noqa: E402


WIND_PGN = G.PGN_CODE["Wind Data"]


def _make_wind_record(reference):
    return {
        'pgn': WIND_PGN,
        'src': 14,
        'fields': {
            'Reference': reference,
            'Wind Angle': 42.5,
            'Wind Speed': 7.8,
        },
    }


@pytest.fixture(autouse=True)
def reset_warn_flag():
    # Reset the module-level "have we warned?" flag so each test
    # gets a clean slate.
    canboat._MAGNETIC_WIND_WARNED = False
    yield


def test_apparent_wind_stays_unprefixed():
    rec = _make_wind_record("Apparent")
    canboat.transform_record(rec)
    fields = rec['fields']
    assert 'wind_angle' in fields
    assert 'wind_speed' in fields
    # No branch-prefixed variants.
    assert 'true_wind_angle' not in fields
    assert 'magnetic_wind_angle' not in fields


def test_true_boat_referenced_gets_true_prefix():
    rec = _make_wind_record("True (boat referenced)")
    canboat.transform_record(rec)
    fields = rec['fields']
    assert 'true_wind_angle' in fields
    assert 'true_wind_speed' in fields
    # Does NOT collide into the Apparent unprefixed columns.
    assert 'wind_angle' not in fields


def test_true_ground_referenced_gets_true_north_prefix():
    rec = _make_wind_record("True (ground referenced to North)")
    canboat.transform_record(rec)
    fields = rec['fields']
    assert 'true_north_wind_angle' in fields
    assert 'true_north_wind_speed' in fields
    assert 'wind_angle' not in fields


def test_magnetic_reference_gets_its_own_prefix():
    """The Stage 3 fix: Magnetic must not silently collapse into Apparent."""
    rec = _make_wind_record("Magnetic")
    canboat.transform_record(rec)
    fields = rec['fields']
    assert 'magnetic_wind_angle' in fields
    assert 'magnetic_wind_speed' in fields
    # Explicitly NOT in the Apparent unprefixed columns (the pre-Stage-3 bug).
    assert 'wind_angle' not in fields
    assert 'wind_speed' not in fields


def test_magnetic_reference_emits_single_warning(caplog):
    import logging
    caplog.set_level(logging.WARNING, logger='regatta_analysis')
    rec1 = _make_wind_record("Magnetic")
    rec2 = _make_wind_record("Magnetic")
    canboat.transform_record(rec1)
    canboat.transform_record(rec2)
    magnetic_warnings = [r for r in caplog.records
                         if r.levelname == 'WARNING'
                         and 'Magnetic' in r.getMessage()]
    assert len(magnetic_warnings) == 1, (
        f"expected one warning for magnetic wind, got {len(magnetic_warnings)}"
    )


def test_non_wind_pgns_are_untouched_by_the_branch():
    """Regression guard: the Magnetic-reference handling must not fire on
    unrelated PGNs that happen to carry a 'Reference' field."""
    # PGN 127250 (Vessel Heading) has a Reference field with value
    # 'Magnetic'. That must not get the magnetic_wind_* prefix.
    rec = {
        'pgn': G.PGN_CODE["Vessel Heading"],
        'src': 4,
        'fields': {
            'Reference': 'Magnetic',
            'Heading': 120.0,
        },
    }
    canboat.transform_record(rec)
    fields = rec['fields']
    assert 'heading' in fields
    assert 'magnetic_wind_heading' not in fields
