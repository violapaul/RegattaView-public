"""Simple, independent compass/heading-offset estimate from COG vs HDG.

Mirrors the SOG-vs-STW speedo calibration: use steady, moving data and let the
nuisance terms average out.

    COG_true - HDG_true = compass_offset + leeway + current_drift

where HDG_true = HDG_mag + magnetic_variation. The three terms separate by their
symmetry:
  * leeway        — antisymmetric across a tack (port heels one way, starboard the
                    other) -> cancels in 0.5*(port + starboard).
  * current_drift — depends on boat heading vs the fixed current vector; averages
                    toward zero over many headings / many days.
  * compass_offset— constant; the quantity we want. It also absorbs any error in
                    the assumed magnetic_variation, so it is exactly the
                    `heading_offset` used by the calibrations.

Per day we therefore report
    offset_day = 0.5*(mean_delta|starboard + mean_delta|port)   (leeway cancels)
    leeway_day = 0.5*(mean_delta|starboard - mean_delta|port)   (bonus cross-check)

and the global offset is the across-day mean. The SPREAD of offset_day is an
independent estimate of the day-to-day heading variation that the tack-symmetry
analysis inferred (sd ~2 deg) -- if the two independent methods agree, the per-day
offset is real.

Data: the steady upwind pre/post windows already pooled in TackData (balanced
port/starboard by construction, boat moving, |AWA|<90). No reprocessing needed.

Run:
  conda run --no-capture-output -n sailing python Analyze/compass_offset_simple.py
"""
import argparse
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from Libraries.global_variables import G
G.init_seattle()

MS_TO_KT = G.MS_2_KNOTS
VARIATION = G.MAGNETIC_VARIATION
HEEL_OFFSET = -4.0
MOVE_KT = 2.5          # both SOG and STW must exceed this (genuinely sailing)


def wrap180(a):
    return (a + 180.0) % 360.0 - 180.0


def circ_mean_small(d):
    """Mean of small wrapped angle differences (deltas cluster near the offset,
    so a plain wrapped mean is fine; use median too for robustness)."""
    d = d[np.isfinite(d)]
    if len(d) == 0:
        return np.nan, np.nan, 0
    return float(np.mean(d)), float(np.median(d)), len(d)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=str(Path(__file__).with_name('TackData')))
    ap.add_argument('--out', default=str(Path(__file__).with_name('CurrentOut')))
    ap.add_argument('--min-tacks-per-day', type=int, default=4)
    args = ap.parse_args(argv)

    s = pd.read_parquet(os.path.join(args.data, 'tack_samples.parquet'))
    spd_kt = s['spd_ms'].to_numpy(float) * MS_TO_KT
    sog_kt = s['sog_ms'].to_numpy(float) * MS_TO_KT
    heel = s['heel_raw'].to_numpy(float) - HEEL_OFFSET
    hdg_true = s['hdg_mag'].to_numpy(float) + VARIATION
    cog_true = s['cog_true'].to_numpy(float)
    delta = wrap180(cog_true - hdg_true)          # crab angle (COG right of HDG +)
    day = s['day'].to_numpy()

    move = (spd_kt > MOVE_KT) & (sog_kt > MOVE_KT) & np.isfinite(delta)
    stbd = move & (heel < 0)     # heel sign = which tack
    port = move & (heel > 0)
    # (sign(heel)->tack mapping is just a label here; offset is symmetric in it)

    print(f"samples: {move.sum()} moving  "
          f"(port-heel {port.sum()}, stbd-heel {stbd.sum()})")

    days = np.array(sorted(set(day)))
    rows = []
    for d in days:
        md = day == d
        dp, _, np_ = circ_mean_small(delta[md & port])
        ds, _, ns_ = circ_mean_small(delta[md & stbd])
        if np_ < 50 or ns_ < 50:        # need both tacks well-sampled
            continue
        offset = 0.5 * (dp + ds)
        leeway = 0.5 * (ds - dp)
        rows.append((str(d), offset, leeway, np_ + ns_))

    rows = [r for r in rows]
    offs = np.array([r[1] for r in rows])
    lews = np.array([r[2] for r in rows])

    print(f"\nper-day (n={len(rows)} days with both tacks well-sampled):")
    print(f"  offset = 0.5*(delta_stbd + delta_port)  [leeway-cancelled]")
    print(f"  global heading offset:  mean={offs.mean():+.2f}  "
          f"median={np.median(offs):+.2f}  sd={offs.std():.2f}  "
          f"range=[{offs.min():+.2f},{offs.max():+.2f}] deg")
    print(f"  (this is the residual on top of assumed variation {VARIATION:.2f} deg)")
    print(f"\n  bonus leeway proxy = 0.5*(delta_stbd - delta_port):  "
          f"mean={lews.mean():+.2f} sd={lews.std():.2f} deg "
          f"(expect a few deg, ~K*heel/spd^2)")

    # pooled (all moving samples, both tacks together) -- leeway only cancels if
    # the two tacks are balanced; report as a cross-check of the per-day mean.
    dp_all, _, _ = circ_mean_small(delta[port])
    ds_all, _, _ = circ_mean_small(delta[stbd])
    print(f"\n  pooled-all-days offset = 0.5*(port {dp_all:+.2f} + stbd {ds_all:+.2f}) "
          f"= {0.5*(dp_all+ds_all):+.2f} deg")

    # low-heel cross-check: where |heel|<5 deg leeway is tiny, so delta ~ offset
    # directly (still needs current to average out across the pooled set).
    low = move & (np.abs(heel) < 5.0)
    dl, dlm, nl = circ_mean_small(delta[low])
    print(f"\n  low-heel (|heel|<5) direct delta: mean={dl:+.2f} median={dlm:+.2f} "
          f"n={nl}  (leeway~0, least-confounded single number)")

    print(f"\nCompare to tack-symmetry fit: it wanted heading_offset ~ -1.6 deg "
          f"(global) and sd~2 deg day-to-day. Agreement here = independent "
          f"confirmation the per-day offset is real, not a leeway artifact.")

    os.makedirs(args.out, exist_ok=True)
    out = dict(variation=VARIATION, move_kt=MOVE_KT, n_days=len(rows),
               global_offset_mean=float(offs.mean()),
               global_offset_median=float(np.median(offs)),
               global_offset_sd=float(offs.std()),
               leeway_proxy_mean=float(lews.mean()),
               pooled_offset=float(0.5 * (dp_all + ds_all)),
               low_heel_delta=float(dl),
               per_day=[dict(day=r[0], offset=r[1], leeway=r[2], n=int(r[3]))
                        for r in rows])
    with open(os.path.join(args.out, 'compass_offset_simple.json'), 'w') as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {os.path.join(args.out, 'compass_offset_simple.json')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
