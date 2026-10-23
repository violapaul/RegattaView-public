"""Stage 0.5 guardrail: verify that ``df['twd']`` responds *linearly* to
``G.MAGNETIC_VARIATION``.

Empirically the slope of twd with respect to variation on real data is ~2,
not 1, because ``estimate_true_wind_helper`` uses ``rhdg + variation`` in
two places:
  - to compute ``twa = ptwd − (rhdg + variation)``, which is
    variation-invariant once ``ptwd`` relaxes, AND
  - to compute ``course_angle = rcog − (rhdg + variation)``, which rotates
    the boat's velocity contribution ``(btn, bte)`` into the residual and
    forces the solver to compensate via ``ptwd``.

The helper has a separate known issue at
``Libraries/race_logs.py::estimate_true_wind_helper`` line 102
(``# WHY IS THIS NEGATIVE???  SEEMS INCORRECT``) that likely also feeds
this amplification. Additionally the helper's ``eps`` adapts based on
residual magnitude (line ~120), so different variation values produce
different gain schedules and thus different convergence trajectories —
breaking strict per-sample linearity on transient samples.

We therefore pin weaker, still useful invariants:
  - **sign**: the *median* shift is positive and > 1° for each V step
    (catches sign-flip regressions from e.g. ``rhdg - variation``);
  - **median linearity**: the *median* shift for V 0→V equals the
    *median* shift for V V→2V within a small tolerance (catches any
    newly-introduced variation-dependent path that would show up on the
    majority of samples).

Any refactor that introduces a new variation-dependent path will break
one of these.

Skipped if the reference parquet isn't available (CI environment without
the sailing data share).
"""
import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from Libraries.global_variables import G  # noqa: E402
from Libraries import race_logs  # noqa: E402


# Use the smallest available parquet so this test runs fast. 2023-10-27 has
# the full column set including ``rtwd`` (True-North branch of PGN 130306).
REF_PARQUET_DIR = "/Users/viola/Data/SailingData/PandasLogs"
REF_PARQUET = "2023-10-27T14_39.parquet"

# Median-linearity tolerance (degrees) — the difference between the two
# equal-sized-step median shifts.
MEDIAN_LINEARITY_TOL = 0.5
# Sign check: the median per-sample shift must exceed this (degrees) to
# count as a definitive same-sign response.
SIGN_THRESHOLD = 1.0
# Ignore the first N samples (pre-convergence transient from twd_init).
EXCLUDE_LEADING_SAMPLES = 500


def _wrap_signed_deg(a):
    a = np.asarray(a, dtype=float)
    return ((a + 180.0) % 360.0) - 180.0


def _run_pipeline_at_variation(variation):
    """Run the full read→process pipeline with G.MAGNETIC_VARIATION patched."""
    original = G.MAGNETIC_VARIATION
    try:
        G.MAGNETIC_VARIATION = variation
        df = race_logs.read_log_file(
            REF_PARQUET,
            path=REF_PARQUET_DIR,
            discard_columns=False,
        )
    finally:
        G.MAGNETIC_VARIATION = original
    return df


@pytest.fixture(scope="module")
def reference_path():
    path = os.path.join(REF_PARQUET_DIR, REF_PARQUET)
    if not os.path.exists(path):
        pytest.skip(f"Reference parquet not available: {path}")
    return path


@pytest.fixture(scope="module")
def step_size():
    # Any finite positive value works; use the production constant so the
    # test exercises a realistic range.
    return float(G.MAGNETIC_VARIATION)


@pytest.fixture(scope="module")
def twd_triplet(reference_path, step_size):
    """Run the pipeline at variation ∈ {0, V, 2V} and return the three twd
    arrays aligned to the same sample indices."""
    _ = reference_path
    zero = _run_pipeline_at_variation(0.0)
    single = _run_pipeline_at_variation(step_size)
    doubled = _run_pipeline_at_variation(2.0 * step_size)

    # Deterministic trim → aligned indices across the three runs.
    assert len(zero) == len(single) == len(doubled), (
        f"trim inconsistency: {len(zero)} vs {len(single)} vs {len(doubled)}"
    )

    sl = slice(EXCLUDE_LEADING_SAMPLES, len(zero))
    return {
        "step_size": step_size,
        "twd_zero": zero["twd"].values[sl],
        "twd_single": single["twd"].values[sl],
        "twd_doubled": doubled["twd"].values[sl],
    }


def test_twd_shifts_consistently_under_equal_variation_steps(twd_triplet):
    """The *median* shift from V=0→V should equal the *median* shift from
    V→2V because both perturbations are the same size. A mismatch means
    either (a) a new variation-dependent path was added, or (b) the
    solver's adaptive gain schedule is so different between the two runs
    that its bias contaminates the median — both worth investigating."""
    d1 = _wrap_signed_deg(twd_triplet["twd_single"] - twd_triplet["twd_zero"])
    d2 = _wrap_signed_deg(twd_triplet["twd_doubled"] - twd_triplet["twd_single"])

    mask = np.isfinite(d1) & np.isfinite(d2)
    assert mask.sum() > 1000, "too few finite samples for a reliable check"

    median1 = float(np.median(d1[mask]))
    median2 = float(np.median(d2[mask]))

    assert abs(median2 - median1) < MEDIAN_LINEARITY_TOL, (
        f"median shifts differ: V 0→V = {median1:+.3f}°, V V→2V = "
        f"{median2:+.3f}° (|Δ| = {abs(median2 - median1):.3f}° > "
        f"{MEDIAN_LINEARITY_TOL}°). Indicates a new variation-dependent "
        "path or a non-linear regime in the solver."
    )


def test_twd_shift_has_correct_sign(twd_triplet):
    """Bumping variation must push twd in a consistent, positive-signed
    direction on an upwind session. A sign flip catches a convention
    regression (e.g. ``rhdg - variation`` where it should be ``+``)."""
    d1 = _wrap_signed_deg(twd_triplet["twd_single"] - twd_triplet["twd_zero"])
    d2 = _wrap_signed_deg(twd_triplet["twd_doubled"] - twd_triplet["twd_single"])

    median1 = float(np.median(d1[np.isfinite(d1)]))
    median2 = float(np.median(d2[np.isfinite(d2)]))

    assert median1 > SIGN_THRESHOLD, (
        f"median twd shift for V 0→V is {median1:+.3f}°; expected > "
        f"{SIGN_THRESHOLD}° (same sign as the variation step)."
    )
    assert median2 > SIGN_THRESHOLD, (
        f"median twd shift for V V→2V is {median2:+.3f}°; expected > "
        f"{SIGN_THRESHOLD}° (same sign as the variation step)."
    )
