"""Stage 1: Charuco detection tests on synthetic dual-fisheye images.

Tests verify that:
  - detect_charuco finds corners in the expected fraction of visible poses
  - detected corner positions are close to the ground-truth projected positions
  - split_dual_half correctly splits a side-by-side dual frame
  - detect_charuco returns None on a blank / feature-free image
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np
import pytest

import detect_charuco
import generate_synthetic_dual_fisheye as gen


# ---------------------------------------------------------------------------
# Helper: build a lookup from (pair_key, side) -> list of GT corner positions
# keyed by charuco_id
# ---------------------------------------------------------------------------

def _build_gt_corner_map(gt: Dict) -> Dict:
    """Return {(pair_key, side): {charuco_id: (u, v)}} from ground_truth.json.

    ``pair_key`` matches the image stem used in conftest (e.g. ``synth_000``).
    The projected corners are for *all* charuco interior corners; the charuco_id
    is the index into that array (which matches board.getChessboardCorners()).
    """
    corner_map = {}
    for entry in gt["images"]:
        stem = entry["filename"].replace(".png", "")
        for side in ("front", "back"):
            pts = entry.get(f"{side}_projected_corners")
            if pts is None:
                continue
            corner_map[(stem, side)] = {i: tuple(p) for i, p in enumerate(pts)}
    return corner_map


# ---------------------------------------------------------------------------
# Test 1: Detection count -- >= 80 % of GT-visible (side, image) pairs detected
# ---------------------------------------------------------------------------

def test_detection_count(synth_data: Dict, observations: List[Dict]) -> None:
    """At least 80 % of GT-visible (side, image) pairs must yield a detection.

    This validates that the synthetic images are realistic enough for the aruco
    detector and that detect_charuco is functioning end-to-end.
    """
    gt = synth_data["gt"]

    # Count expected visible pairs from GT
    expected = 0
    for entry in gt["images"]:
        if entry.get("front_projected_corners") is not None:
            expected += 1
        if entry.get("back_projected_corners") is not None:
            expected += 1

    detected = len(observations)

    assert expected > 0, "No GT-visible pairs found; generator may have failed"
    rate = detected / expected
    assert rate >= 0.80, (
        f"Detection rate {rate:.1%} is below 80 % "
        f"({detected} detected out of {expected} GT-visible pairs)"
    )


# ---------------------------------------------------------------------------
# Test 2: Reprojection accuracy -- detected corners close to GT positions
# ---------------------------------------------------------------------------

def test_detection_reprojection_error(synth_data: Dict, observations: List[Dict]) -> None:
    """Detected corner positions must match GT projected positions.

    Tolerances:
        median error < 1.5 pixels
        95th-percentile error < 3.0 pixels

    These account for sub-pixel rendering interpolation and the charuco
    sub-pixel corner refinement step.
    """
    gt = synth_data["gt"]
    corner_map = _build_gt_corner_map(gt)

    errors = []
    for obs in observations:
        key = (obs["pair_key"], obs["side"])
        gt_corners = corner_map.get(key)
        if gt_corners is None:
            continue
        det_ids = obs["charuco_ids"]
        det_pts = obs["image_points"]
        for cid, det_pt in zip(det_ids, det_pts):
            gt_pt = gt_corners.get(int(cid))
            if gt_pt is None:
                continue
            err = np.hypot(det_pt[0] - gt_pt[0], det_pt[1] - gt_pt[1])
            errors.append(err)

    assert len(errors) > 50, (
        f"Too few matched corners to evaluate reprojection ({len(errors)}); "
        "check generator and detection."
    )

    errors_arr = np.array(errors)
    median_err = float(np.median(errors_arr))
    p95_err = float(np.percentile(errors_arr, 95))

    assert median_err < 1.5, (
        f"Median reprojection error {median_err:.3f} px >= 1.5 px tolerance"
    )
    assert p95_err < 3.0, (
        f"95th-percentile reprojection error {p95_err:.3f} px >= 3.0 px tolerance"
    )


# ---------------------------------------------------------------------------
# Test 3: split_dual_half -- correctly splits a side-by-side dual frame
# ---------------------------------------------------------------------------

def test_split_dual_half() -> None:
    """split_dual_half should return left and right halves independently."""
    import numpy as np

    H, W_half = gen.HALF_HEIGHT, gen.HALF_WIDTH
    # Left half red (BGR: 0,0,255), right half blue (BGR: 255,0,0)
    frame = np.zeros((H, W_half * 2, 3), dtype=np.uint8)
    frame[:, :W_half] = (0, 0, 255)    # red (BGR)
    frame[:, W_half:] = (255, 0, 0)    # blue (BGR)

    views = detect_charuco.split_dual_half(frame)

    assert set(views.keys()) == {"front", "back"}
    assert views["front"].shape == (H, W_half, 3)
    assert views["back"].shape == (H, W_half, 3)
    assert np.all(views["front"] == (0, 0, 255)), "Front half should be red"
    assert np.all(views["back"] == (255, 0, 0)), "Back half should be blue"


# ---------------------------------------------------------------------------
# Test 4: No false detections on blank image
# ---------------------------------------------------------------------------

def test_detection_on_blank_image(board_and_corners) -> None:
    """detect_charuco must return None on a uniform gray image (no board)."""
    import numpy as np

    board, corners_3d = board_and_corners
    gray_image = np.full((gen.HALF_HEIGHT, gen.HALF_WIDTH, 3), 128, dtype=np.uint8)

    result = detect_charuco.detect_charuco(gray_image, board, corners_3d, min_corners=4)
    assert result is None, (
        "detect_charuco should return None on a featureless image, "
        f"but returned: {result}"
    )
