"""Diagnose + persist the global leeway model.

The sensor calibration (STW scale, heading offset, heel offset) is the single
source of truth in ``Src/src/assets/calibration/sensor_calibration.json`` — this
script reads it and fits ONLY the leeway K at that FIXED calibration, so the
global current model and the viewer agree. (We deliberately do NOT jointly fit
the STW scale: tack symmetry prefers ~1.25, but that is the objective absorbing
leeway/heading error into the scale; the physical STW magnitude is 1.22 — see
Analyze/SPEEDO_CALIBRATION.md. Likewise heading is the real RC42 offset -2.3°,
not a free parameter — see Analyze/HEADING_CALIBRATION.md.)

  conda run -n sailing python Analyze/leeway_report.py
"""
import json
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
from scipy.optimize import minimize_scalar

from Analyze import fit_leeway_global as F
from Analyze import current_analysis as ca

DATA = str(Path(__file__).with_name('TackData'))
OUT = str(Path(__file__).with_name('CurrentOut'))


def base_cal_from_sensor_json():
    """Build fit_leeway_global's cal dict from the committed sensor calibration."""
    stw_scale, stw_offset, heel_offset, heading_offset = ca.load_sensor_calibration()
    return dict(stw_scale=stw_scale, stw_offset=stw_offset,
                heading_offset=heading_offset, heel_offset=heel_offset)


def main():
    samples = pd.read_parquet(os.path.join(DATA, 'tack_samples.parquet'))
    index = pd.read_parquet(os.path.join(DATA, 'tack_index.parquet'))
    bundle = F.TackBundle(samples, index)
    base = base_cal_from_sensor_json()
    print(f"{bundle.n_tacks} tacks / {len(set(bundle.tack_day))} days")
    print(f"FIXED calibration (from sensor_calibration.json): {base}")

    # --- fit classic leeway K at the FIXED calibration ---
    (K_star,), _ = F.fit_model('classic', bundle, base)        # fixed stw
    cv_rms, cv_med = F.cv_by_day('classic', bundle, base)
    in_rms, _ = F.eval_disc('classic', [K_star], bundle, base, None)
    print(f"classic K={K_star:.3f}  in-sample rms={in_rms:.4f}  "
          f"by-day CV rms={cv_rms:.4f} kt")

    c = bundle.apply_cal(base)
    x = c['x']

    # --- figure: 3 diagnostics ---
    fig, ax = plt.subplots(1, 3, figsize=(20, 6))

    # (0) stw_scale sweep — shows tacks "want" ~1.25 but we fix 1.22 (physical)
    a = ax[0]
    scales = np.linspace(1.00, 1.40, 41)
    losses = []
    for s in scales:
        cc = dict(base); cc['stw_scale'] = s
        cs = bundle.apply_cal(cc)
        r = minimize_scalar(
            lambda K: F.robust_mean(bundle.tack_discontinuity(cs, F.leeway('classic', [K], cs))[0]),
            bounds=(-40, 40), method='bounded')
        losses.append(np.sqrt(r.fun))
    a.plot(scales, losses, color='tab:blue')
    for val, name, col in [(1.107, 'water-ref', 'tab:red'),
                           (1.25, 'tack-optimal (confounded)', 'tab:orange'),
                           (base['stw_scale'], 'committed/physical', 'k')]:
        a.axvline(val, color=col, ls='--', lw=1.2, label=f'{name} {val:.3f}')
    a.set_xlabel('stw_scale'); a.set_ylabel('RMS discontinuity (kt), K refit')
    a.set_title('STW scale: tacks prefer ~1.25 (confound); we fix 1.22 (SOG=STW magnitude)')
    a.legend(fontsize=8); a.grid(alpha=0.3)

    # (1) leeway curve linearity at fixed cal: classic vs spline
    a = ax[1]
    knots = F._spline_knots(x)
    th_spline, knots = F.fit_model('spline', bundle, base, knots=knots)
    lam_classic = F.leeway('classic', [K_star], c)
    lam_spline = F.leeway('spline', th_spline, c, knots)
    bins = np.linspace(np.percentile(x, 1), np.percentile(x, 99), 30)
    bc = 0.5 * (bins[:-1] + bins[1:])
    def binned(v):
        idx = np.digitize(x, bins)
        return np.array([np.mean(v[idx == k]) if np.any(idx == k) else np.nan
                         for k in range(1, len(bins))])
    a.plot(bc, binned(lam_classic), 'o-', color='tab:blue', label=f'classic (K={K_star:.1f})')
    a.plot(bc, binned(lam_spline), '^:', color='tab:red', label='spline (non-param)')
    a.set_xlabel('x = heel / spd²  (deg·kt⁻²)'); a.set_ylabel('leeway λ (deg)')
    a.set_title('Leeway ~linear in heel/spd² → classic suffices')
    a.legend(fontsize=8); a.grid(alpha=0.3)

    # (2) per-day optimal K spread → residual day-to-day leeway variation
    a = ax[2]
    perday = []
    for day in sorted(set(bundle.tack_day)):
        mask = bundle.tack_day == day
        if mask.sum() < 6:
            continue
        r = minimize_scalar(
            lambda K: F.robust_mean(bundle.tack_discontinuity(c, F.leeway('classic', [K], c), mask)[0]),
            bounds=(-10, 40), method='bounded')
        perday.append(r.x)
    perday = np.array(perday)
    iqr = np.percentile(perday, 75) - np.percentile(perday, 25)
    a.hist(perday, bins=20, color='tab:blue', alpha=0.8)
    a.axvline(K_star, color='k', ls='--', lw=1.5, label=f'global K={K_star:.1f}')
    a.axvline(np.median(perday), color='tab:red', ls=':', lw=1.5,
              label=f'per-day median={np.median(perday):.1f}')
    a.set_xlabel('per-day optimal leeway K'); a.set_ylabel('# days')
    a.set_title(f'Per-day K spread ({len(perday)} days): IQR={iqr:.1f} '
                f'→ genuine day-to-day LEEWAY variation')
    a.legend(fontsize=8); a.grid(alpha=0.3, axis='y')

    fig.suptitle(f"Global leeway model — {bundle.n_tacks} tacks / "
                 f"{len(set(bundle.tack_day))} days  (FIXED cal: stw={base['stw_scale']:.2f}, "
                 f"heading={base['heading_offset']:.1f}°, λ=K·heel/spd², K={K_star:.2f})",
                 fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.95])
    os.makedirs(OUT, exist_ok=True)
    figpath = os.path.join(OUT, 'leeway_global_model.png')
    fig.savefig(figpath, dpi=110)
    plt.close(fig)

    # --- persist the chosen single model ---
    model = {
        'fitted_from': {'n_tacks': bundle.n_tacks,
                        'n_days': len(set(bundle.tack_day)),
                        'trim': F.TRIM},
        'calibration': {
            'stw_scale': float(base['stw_scale']),
            'stw_offset': float(base['stw_offset']),
            'heading_offset_deg': float(base['heading_offset']),
            'heel_offset_deg': float(base['heel_offset']),
        },
        'leeway': {'model': 'classic',
                   'formula': 'lambda_deg = K * heel_deg / max(spd_kt, 2)**2',
                   'K': float(K_star)},
        'cv_rms_kt': float(cv_rms),
        'note': ('Calibration is FIXED from sensor_calibration.json (physical STW '
                 'scale 1.22, RC42 heading offset -2.3deg, heel -4.0deg) — NOT jointly '
                 'fit; only leeway K is fit here. K trades off with the STW scale, so '
                 'K~9.8 at 1.22 (vs ~7.6 at the confounded 1.25). Spline/power/wind do '
                 'not beat classic in by-day CV. A signed b*heel STW refinement '
                 '(~-0.001/deg) exists but is not wired into the consumer. See '
                 'Analyze/SPEEDO_CALIBRATION.md, HEADING_CALIBRATION.md.'),
    }
    mpath = os.path.join(str(PROJECT_ROOT), 'Analyze', 'leeway_model.json')
    with open(mpath, 'w') as f:
        json.dump(model, f, indent=2)
    print(f"wrote {figpath}")
    print(f"wrote {mpath}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
