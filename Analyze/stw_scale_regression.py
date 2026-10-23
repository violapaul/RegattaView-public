"""Assumption-light STW-scale estimate: regress SOG against raw STW over the whole
corpus. Current averages out across many headings/days, so the slope ≈ the true
knotmeter scale -- no tacks, no symmetry, no SSCOFS.

Two estimators:
  scalar      : SOG ~ scale * raw_STW                 (small +bias ~|cur|²/4·STW)
  projection  : SOG*cos(COG-HDG) ~ scale * raw_STW    (removes the magnitude bias)

raw_STW = df['spd'](m/s) * MS_TO_KT  (i.e. the paddlewheel reading at scale=1.0).

  conda run -n sailing python Analyze/stw_scale_regression.py
"""
import glob
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

from Libraries.global_variables import G
G.init_seattle()
from Libraries.race_logs import read_log_file
from Analyze import current_analysis as ca

MS_TO_KT = G.MS_2_KNOTS
OUT = str(Path(__file__).with_name('CurrentOut'))
SUBSAMPLE_S = 5.0
SPD_MIN, SPD_MAX = 2.5, 9.0   # sailing, exclude dock + glitches


def gather():
    logs = ca.default_logs_dir()
    files = sorted(glob.glob(os.path.join(logs, '*.parquet')))
    raw, sog, proj, days = [], [], [], []
    step = max(1, int(round(SUBSAMPLE_S * G.SAMPLES_PER_SECOND)))
    for p in files:
        name = os.path.basename(p)
        try:
            df = read_log_file(name, path=logs, discard_columns=False)
        except Exception:
            continue
        if df is None or any(c not in df.columns
                             for c in ('spd', 'sog', 'hdg_true', 'cog_true')):
            continue
        spd_ms = df['spd'].to_numpy(float)[::step]
        sog_ms = df['sog'].to_numpy(float)[::step]
        hdg = df['hdg_true'].to_numpy(float)[::step]
        cog = df['cog_true'].to_numpy(float)[::step]
        raw_kt = spd_ms * MS_TO_KT
        sog_kt = sog_ms * MS_TO_KT
        ok = (raw_kt > SPD_MIN) & (raw_kt < SPD_MAX) & (sog_kt < 12) & np.isfinite(cog)
        d = np.radians(cog - hdg)
        raw.append(raw_kt[ok]); sog.append(sog_kt[ok])
        proj.append(sog_kt[ok] * np.cos(d[ok]))
        days.append([name[:10]] * int(ok.sum()))
    return (np.concatenate(raw), np.concatenate(sog),
            np.concatenate(proj), np.concatenate(days))


def fit_origin(x, y):
    return float(np.sum(x * y) / np.sum(x * x))


def fit_affine(x, y):
    A = np.vstack([x, np.ones_like(x)]).T
    m, b = np.linalg.lstsq(A, y, rcond=None)[0]
    return float(m), float(b)


def main():
    raw, sog, proj, days = gather()
    print(f"{len(raw)} sailing samples across {len(set(days))} days\n")

    print("=== Scalar  SOG ~ scale*rawSTW ===")
    print(f"  through-origin slope : {fit_origin(raw, sog):.4f}")
    m, b = fit_affine(raw, sog)
    print(f"  affine               : scale={m:.4f}, offset={b:+.4f} kt")
    print(f"  median SOG/rawSTW     : {np.median(sog/raw):.4f}")

    print("\n=== Projection  SOG·cos(COG-HDG) ~ scale*rawSTW  (bias-free) ===")
    print(f"  through-origin slope : {fit_origin(raw, proj):.4f}")
    mp, bp = fit_affine(raw, proj)
    print(f"  affine               : scale={mp:.4f}, offset={bp:+.4f} kt")

    # per-day slope distribution (projection, origin)
    dser = pd.Series(days)
    per = []
    for day in sorted(set(days)):
        sel = (dser == day).to_numpy()
        if sel.sum() >= 50:
            per.append(fit_origin(raw[sel], proj[sel]))
    per = np.array(per)
    print(f"\nPer-day projection slope: median={np.median(per):.4f}  "
          f"IQR=[{np.percentile(per,25):.4f}, {np.percentile(per,75):.4f}]  "
          f"({len(per)} days)")
    print("\nReference: water-ref 1.107 | display 1.184 | tack-symmetry 1.251")

    # ---- plot ----
    fig, ax = plt.subplots(1, 2, figsize=(15, 6))
    a = ax[0]
    hb = a.hexbin(raw, sog, gridsize=60, cmap='Blues', mincnt=1, bins='log')
    xs = np.linspace(SPD_MIN, SPD_MAX, 50)
    a.plot(xs, 1.251 * xs, 'k-', lw=1.5, label='tack-symmetry 1.251')
    a.plot(xs, 1.107 * xs, 'r--', lw=1.5, label='water-ref 1.107')
    a.plot(xs, fit_origin(raw, sog) * xs, 'g:', lw=2,
           label=f'OLS slope {fit_origin(raw,sog):.3f}')
    a.set_xlabel('raw STW (kt, scale=1.0)'); a.set_ylabel('SOG (kt)')
    a.set_title('SOG vs raw STW (all sailing samples)')
    a.legend(fontsize=8); a.set_aspect('equal'); a.grid(alpha=0.3)

    a = ax[1]
    a.hist(per, bins=25, color='tab:blue', alpha=0.8)
    for v, n, c in [(1.107, 'water-ref', 'r'), (1.184, 'display', 'tab:orange'),
                    (1.251, 'tack-sym', 'k'), (np.median(per), 'this method', 'tab:green')]:
        a.axvline(v, ls='--', color=c, lw=1.5, label=f'{n} {v:.3f}')
    a.set_xlabel('per-day SOG-projection slope'); a.set_ylabel('# days')
    a.set_title('Per-day regression slope (current averages out)')
    a.legend(fontsize=8); a.grid(alpha=0.3, axis='y')
    fig.tight_layout()
    pth = os.path.join(OUT, 'stw_scale_regression.png')
    fig.savefig(pth, dpi=110); plt.close(fig)
    print(f"\nwrote {pth}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
