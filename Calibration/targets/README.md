# Calibration Targets

Use a Charuco board for dual-fisheye calibration capture.

## Why Charuco

Charuco is more reliable than plain chessboards for fisheye lenses because marker IDs
allow robust partial-board detection near the frame edges.

## Generate A Printable Target

From repo root:

```bash
python Calibration/scripts/generate_charuco_target.py \
  --squares-x 10 \
  --squares-y 7 \
  --square-length-mm 40 \
  --marker-length-mm 28 \
  --output Calibration/targets/charuco_10x7_40mm_28mm.png
```

Recommended board:

- `10 x 7` squares
- `40 mm` square size
- `28 mm` marker size

Print at **100% scale** (no fit-to-page).

## Print Checklist

- Use matte paper if possible (reduces reflections).
- Verify one square with calipers/ruler after printing.
- Mount to rigid backing board (foamcore or thin plywood).
- Keep board flat (no curl).

## Where To Stand / How To Move The Target

- Keep camera fixed where it is normally mounted.
- One operator moves the target; avoid moving the camera during calibration capture.
- Start about `1.5-2.0 m` from the camera, then sweep closer (`0.6-1.0 m`) and farther (`3-6 m`).
- Visit all major directions around the boat so both fisheye lenses get broad angular coverage.
- During each hold, rotate the board (left/right tilt, up/down tilt, roll).
- Spend extra time crossing the seam overlap regions with the board at different angles.

## How Long To Record

- Minimum useful clip: `90 seconds`
- Recommended clip: `2-4 minutes`
- If detection count is low or heavily clustered, record another `2-3 minute` clip from different positions.

## Common Failure Modes

- Motion blur from moving the board too quickly
- Glare/wet board causing marker washout
- Only center coverage, not enough edge/overlap coverage
- All frames from one distance band (for example only near range)
