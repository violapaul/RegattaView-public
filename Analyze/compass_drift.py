"""Is the RC42 compass deviation drifting over time? (Answer: no.)

Per session, compute the clean compass offset = the EVEN part of ``COG - HDG`` at
LOW heel (where leeway ~ 0), splitting port/starboard on CALIBRATED heel
(``roll + 4`` — essential; raw roll misclassifies near-zero samples). Then
regress that offset against calendar date.

Result over Oct-2023 .. Jun-2026 (88 sessions): slope -0.02 deg/yr (p=0.91;
Theil-Sen -0.05 deg/yr) — flat. The committed heading offset -2.3 deg is a stable,
permanent constant, not a drifting one. The ~1.2 deg session-to-session scatter is
current-sampling noise. See Analyze/HEADING_CALIBRATION.md.

  conda run --no-capture-output -n sailing python Analyze/compass_drift.py
"""
import glob
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from scipy.stats import theilslopes, linregress

from Libraries.global_variables import G
G.init_seattle()

import Analyze.current_analysis as ca

VARIATION = G.MAGNETIC_VARIATION
HEEL_OFFSET = -4.0
MOVE_KT = 2.5
LOW_HEEL = 6.0   # |heel| below this ~ leeway-free for the even-part compass read


def wrap180(a):
    return (a + 180.0) % 360.0 - 180.0


def session_offset(parquet_path):
    """Clean compass offset for one session, or None if under-sampled."""
    try:
        d = pd.read_parquet(parquet_path, columns=['cog', 'sog', 'zg100_roll', 'heading'])
    except Exception:
        return None
    sog = d['sog'].to_numpy(float) * G.MS_2_KNOTS
    heel = d['zg100_roll'].to_numpy(float) - HEEL_OFFSET   # CALIBRATED heel
    cog = d['cog'].to_numpy(float)
    hdg = d['heading'].to_numpy(float)
    move = (sog > MOVE_KT) & np.isfinite(cog) & np.isfinite(hdg) & (np.abs(heel) < LOW_HEEL)
    delta = wrap180(cog - hdg - VARIATION)
    port = move & (heel > 0)
    stbd = move & (heel < 0)
    if port.sum() < 100 or stbd.sum() < 100:
        return None
    # even part = 0.5*(port + stbd) cancels leeway -> compass offset
    return 0.5 * (float(np.median(delta[port])) + float(np.median(delta[stbd]))), \
        int(port.sum() + stbd.sum())


def main():
    logs_dir = ca.default_logs_dir()
    rows = []
    for f in sorted(glob.glob(os.path.join(logs_dir, '*.parquet'))):
        m = re.search(r'(\d{4})-(\d{2})-(\d{2})T', os.path.basename(f))
        if not m:
            continue
        r = session_offset(f)
        if r is None:
            continue
        off, n = r
        t = pd.Timestamp(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        dec = t.year + (t.dayofyear - 1) / 365.25
        rows.append((dec, off, t, n))
    rows.sort()
    if len(rows) < 10:
        print(f"only {len(rows)} sessions — not enough for a trend")
        return 1

    dec = np.array([r[0] for r in rows])
    off = np.array([r[1] for r in rows])
    lr = linregress(dec, off)
    ts = theilslopes(off, dec)
    print(f"{len(rows)} sessions, {rows[0][2].date()} .. {rows[-1][2].date()}")
    print(f"OLS       slope = {lr.slope:+.3f} deg/yr  (r={lr.rvalue:+.2f}, p={lr.pvalue:.3f})")
    print(f"Theil-Sen slope = {ts[0]:+.3f} deg/yr (robust)")
    for y in sorted(set(int(x) for x in dec)):
        sel = (dec >= y) & (dec < y + 1)
        if sel.sum():
            print(f"  {y}: n={sel.sum():2d}  mean={off[sel].mean():+.2f}  sd={off[sel].std():.2f}")
    verdict = "DRIFTING" if lr.pvalue < 0.05 and abs(lr.slope) > 0.3 else "STABLE (no drift)"
    print(f"verdict: {verdict}")

    out = os.path.join(str(PROJECT_ROOT), 'Analyze', 'CurrentOut')
    os.makedirs(out, exist_ok=True)
    fig, ax = plt.subplots(figsize=(11, 6))
    ax.scatter(dec, off, s=[max(8, r[3] / 2000) for r in rows], alpha=0.6,
               color='tab:blue', label='per-session clean offset (size ~ #samples)')
    xs = np.linspace(dec.min(), dec.max(), 50)
    ax.plot(xs, lr.intercept + lr.slope * xs, 'r-', lw=2,
            label=f'OLS {lr.slope:+.2f}°/yr (p={lr.pvalue:.2f})')
    ax.plot(xs, ts[1] + ts[0] * xs, 'k--', lw=1.5, label=f'Theil-Sen {ts[0]:+.2f}°/yr')
    ax.axhline(-2.3, color='tab:green', ls=':', label='committed -2.3°')
    ax.set_xlabel('year')
    ax.set_ylabel('clean compass offset COG-HDG, low heel (deg)')
    ax.set_title('Compass offset over time — is the RC42 deviation drifting?')
    ax.legend(); ax.grid(alpha=0.3); ax.invert_yaxis()
    path = os.path.join(out, 'compass_drift.png')
    fig.savefig(path, dpi=120)
    plt.close(fig)
    print(f"wrote {path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
