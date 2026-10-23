"""Shared pytest fixtures for the calibration test suite.

All expensive fixtures (image generation, detection) use ``scope="session"``
so they run once per pytest invocation and are shared across all test modules.

Synthetic test data is generated into pytest's temporary directory at runtime.
That keeps rebuildable PNGs / JSON artifacts out of git while preserving a
fully deterministic test dataset (fixed seed, fixed camera model).

To inspect or manually regenerate sample images outside pytest:

    conda run -n calibration python Calibration/scripts/generate_synthetic_dual_fisheye.py \\
        --output-dir Calibration/tests/fixtures --num-images 40
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

# ---------------------------------------------------------------------------
# Make the scripts directory importable without installation
# ---------------------------------------------------------------------------
_SCRIPTS_DIR = Path(__file__).parent.parent / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import detect_charuco  # noqa: E402  (after sys.path manipulation)
import fit_dual_fisheye  # noqa: E402
import generate_synthetic_dual_fisheye as gen  # noqa: E402


# ---------------------------------------------------------------------------
# Session-level constants (must match the generator defaults)
# ---------------------------------------------------------------------------
MIN_CORNERS = 14

# ---------------------------------------------------------------------------
# Fixture: synthetic images + ground truth
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def synth_data(tmp_path_factory: pytest.TempPathFactory) -> Dict:
    """Generate a deterministic synthetic dataset for this pytest session."""
    image_dir = tmp_path_factory.mktemp("calibration_synth")
    gen.generate(image_dir, num_images=40, seed=42)

    gt_path = image_dir / "ground_truth.json"
    with gt_path.open("r", encoding="utf-8") as f:
        gt = json.load(f)
    return {"image_dir": image_dir, "gt": gt, "gt_path": gt_path}


# ---------------------------------------------------------------------------
# Fixture: CharucoBoard + 3D corners (re-created from generator constants)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def board_and_corners():
    """Return (CharucoBoard, corners_3d) matching the synthetic images."""
    board, corners_3d = gen.make_charuco_board()
    return board, corners_3d


# ---------------------------------------------------------------------------
# Fixture: detection results on all synthetic images
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def observations(synth_data, board_and_corners) -> List[Dict]:
    """Run detect_charuco over all synthetic images and return the full
    observations list (same structure as detect_charuco.py output)."""
    board, corners_3d = board_and_corners
    image_dir: Path = synth_data["image_dir"]

    all_obs = []
    image_files = sorted(image_dir.glob("synth_*.png"))
    for idx, img_path in enumerate(image_files):
        import cv2
        image = cv2.imread(str(img_path))
        if image is None:
            continue
        pair_key = img_path.stem
        views = detect_charuco.split_dual_half(image)
        for side, half_img in views.items():
            det = detect_charuco.detect_charuco(half_img, board, corners_3d, MIN_CORNERS)
            if det is None:
                continue
            h, w = half_img.shape[:2]
            all_obs.append({
                "source_type": "image",
                "source_path": str(img_path),
                "pair_key": pair_key,
                "frame_index": idx,
                "side": side,
                "image_size": [int(w), int(h)],
                **det,
            })
    return all_obs


@pytest.fixture(scope="session")
def projected_observations() -> List[Dict]:
    """Return noise-free observations generated via direct projectPoints.

    Used by the back-euler estimation test, which needs stereo-paired
    observations with a known ground-truth relative rotation.  See
    ``gen.generate_projected_observations`` for the construction details.
    """
    return gen.generate_projected_observations(n_pairs=30, seed=42)
