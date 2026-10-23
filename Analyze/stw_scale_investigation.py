"""Adjudicate the knotmeter STW scale: tack-symmetry (~1.25, GPS-physical) vs
water-ref (~1.107, reproduces the onboard instrument).

Three tests:
  1. Same-7-sessions: fit tack-symmetry scale on the EXACT sessions the water-ref
     calibration used (controls for any time/fouling drift).
  2. Temporal trend: per-session tack-symmetry scale across the year (fouling?).
  3. GPS zero-current gold standard: where SSCOFS current ~ 0, calibrated STW must
     equal SOG, so SOG / raw_STW is an instrument-independent scale estimate.

  conda run -n sailing python Analyze/stw_scale_investigation.py
"""
import datetime as dt
import os
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

from Analyze import current_analysis as ca
from Analyze import fit_leeway_global as F
from Analyze import investigate_current as ic

DATA = str(Path(__file__).with_name('TackData'))
OUT = str(Path(__file__).with_name('CurrentOut'))
WATERREF_SESSIONS = ['2026-03-07', '2026-03-29', '2026-04-11', '2026-04-13',
                     '2026-04-18', '2026-04-19', '2026-04-20']
SSCOFS_DAYS = ['2026-04-25T08_40', '2026-05-09T08_36', '2026-05-30T08_08']
# display calibration constants (to invert spd_kt back to scale=1.0 STW)
DISP_SCALE, DISP_OFF = 1.183908, 0.1933


def joint_scale_K(bundle, base, mask):
    theta, _ = F.fit_model('classic', bundle, base, train_mask=mask, free_stw=True)
    return theta[0], theta[1], int(mask.sum()) if mask is not None else bundle.n_tacks


def main():
    samples = pd.read_parquet(os.path.join(DATA, 'tack_samples.parquet'))
    index = pd.read_parquet(os.path.join(DATA, 'tack_index.parquet'))
    bundle = F.TackBundle(samples, index)
    base = dict(F.DEFAULT_CAL)

    # ---- Test 1: same 7 sessions ----
    in7 = np.array([d[:10] in WATERREF_SESSIONS for d in bundle.tack_day])
    s7, k7, n7 = joint_scale_K(bundle, base, in7)
    sall, kall, nall = joint_scale_K(bundle, base, np.ones(bundle.n_tacks, bool))
    print("=== Test 1: tack-symmetry scale on the water-ref's own 7 sessions ===")
    print(f"  water-ref 7 sessions ({n7} tacks): stw_scale={s7:.3f}, K={k7:.2f}")
    print(f"  all 76 days        ({nall} tacks): stw_scale={sall:.3f}, K={kall:.2f}")
    print(f"  water-ref reported stw_scale = 1.107  (vs tack-symmetry {s7:.3f} on same days)")

    # ---- Test 2: per-session trend ----
    rows = []
    for day in sorted(set(bundle.tack_day)):
        mask = bundle.tack_day == day
        if mask.sum() < 8:
            continue
        s, k, n = joint_scale_K(bundle, base, mask)
        if 0.9 < s < 1.45:  # keep physically plausible fits
            rows.append((dt.date.fromisoformat(day[:10]), s, k, n))
    trend = pd.DataFrame(rows, columns=['date', 'scale', 'K', 'n'])
    print(f"\n=== Test 2: per-session scale, {len(trend)} days with >=8 tacks ===")
    print(f"  scale median={trend['scale'].median():.3f}  "
          f"IQR=[{trend['scale'].quantile(.25):.3f}, {trend['scale'].quantile(.75):.3f}]")
    early = trend[trend['date'] < dt.date(2026, 4, 25)]
    late = trend[trend['date'] >= dt.date(2026, 4, 25)]
    print(f"  before 4/25 ({len(early)}d): median scale={early['scale'].median():.3f}")
    print(f"  on/after 4/25 ({len(late)}d): median scale={late['scale'].median():.3f}")

    # ---- Test 3: GPS zero-current gold standard ----
    print("\n=== Test 3: GPS zero-current check (SOG vs raw STW where SSCOFS~0) ===")
    gps_ratios = []
    for day in SSCOFS_DAYS:
        name = day + '.parquet'
        df = ca.load_calibrated_log(name, calibration='display')
        step = max(1, int(round(10 * ca._FS)))
        sub = np.arange(0, len(df), step)
        e, n = ic.query_sscofs(df, sub)
        if e is None:
            print(f"  {day}: SSCOFS unavailable"); continue
        cur = np.hypot(e, n)
        spd_kt = df['spd_kt'].to_numpy()[sub]
        sog_kt = df['sog_kt'].to_numpy()[sub]
        raw_stw_scale1 = (spd_kt - DISP_OFF) / DISP_SCALE  # STW at scale=1.0
        sailing = spd_kt > 3.0
        for lo, hi in [(0.0, 0.1), (0.1, 0.2), (0.2, 0.3)]:
            sel = sailing & (cur >= lo) & (cur < hi) & (raw_stw_scale1 > 0.5)
            if sel.sum() >= 5:
                r = np.median(sog_kt[sel] / raw_stw_scale1[sel])
                if lo == 0.0:
                    gps_ratios.append(r)
                print(f"  {day}  SSCOFS|cur|∈[{lo},{hi}) n={int(sel.sum()):3d}  "
                      f"median SOG/rawSTW = {r:.3f}")
    if gps_ratios:
        print(f"\n  GPS zero-current implied scale (|cur|<0.1): "
              f"median {np.median(gps_ratios):.3f}  -> compare 1.107 vs ~1.25")

    # ---- plot temporal trend ----
    fig, ax = plt.subplots(figsize=(12, 6))
    sc = ax.scatter(trend['date'], trend['scale'], c=trend['n'], cmap='viridis',
                    s=30, zorder=3)
    plt.colorbar(sc, label='# tacks in session')
    for val, name, col in [(1.107, 'water-ref (onboard)', 'tab:red'),
                           (1.184, 'display', 'tab:orange'),
                           (sall, f'global tack-fit {sall:.3f}', 'k')]:
        ax.axhline(val, color=col, ls='--', lw=1.2, label=f'{name} {val:.3f}')
    for d in WATERREF_SESSIONS:
        ax.axvline(dt.date.fromisoformat(d), color='tab:red', alpha=0.15, lw=4, zorder=0)
    ax.set_ylabel('per-session tack-symmetry stw_scale')
    ax.set_xlabel('session date')
    ax.set_title('Knotmeter STW scale over the season (red bands = water-ref sessions)')
    ax.legend(fontsize=8); ax.grid(alpha=0.3); ax.set_ylim(0.9, 1.45)
    fig.autofmt_xdate()
    fig.tight_layout()
    p = os.path.join(OUT, 'stw_scale_trend.png')
    fig.savefig(p, dpi=110); plt.close(fig)
    print(f"\nwrote {p}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
