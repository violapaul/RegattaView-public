"""Validate the single global leeway model end-to-end on sample days:
compare its current estimate against (a) the per-session best fit and (b) SSCOFS.

  conda run -n sailing python Analyze/validate_global_leeway.py 2026-05-09T08_36 ...
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from Analyze import current_analysis as ca
from Analyze import investigate_current as ic

DEFAULT_DAYS = ['2026-04-25T08_40', '2026-05-09T08_36', '2026-05-30T08_08']


def _rolling(a, win):
    return ic._rolling_mean(a, win)


def bias_vs_sscofs(df, ce, cn, sub_idx, scof):
    de = ce[sub_idx] - scof['e']
    dn = cn[sub_idx] - scof['n']
    g = np.isfinite(de) & np.isfinite(dn)
    if not g.any():
        return None
    return dict(n=int(g.sum()),
                mE=float(np.mean(de[g])), mN=float(np.mean(dn[g])),
                rms=float(np.sqrt(np.mean(de[g]**2 + dn[g]**2))))


def main(argv):
    days = argv[1:] or DEFAULT_DAYS
    m = ca.load_leeway_model()
    if not m:
        raise SystemExit("leeway_model.json not found — run leeway_report.py first")
    K = m['leeway']['K']
    print(f"Global model: stw_scale={m['calibration']['stw_scale']:.3f}, "
          f"classic K={K:.2f}  (CV rms {m['cv_rms_kt']:.3f} kt)\n")

    smooth = int(round(30 * ca._FS))
    hdr = (f"{'day':18s} {'model':18s} {'tackRMS':>8s} "
           f"{'|cur|mean':>9s} {'vsSSCOFS_RMS':>12s} {'biasN':>7s}")
    print(hdr); print('-' * len(hdr))

    for day in days:
        name = day if day.endswith('.parquet') else day + '.parquet'
        # per-session best (display calibration)
        df_d = ca.load_calibrated_log(name, calibration='display')
        wins = ca.tack_windows(df_d, ca.detect_tacks(df_d))
        fits, best = ca.rank_models(df_d, wins)
        ce_s, cn_s, _ = ca.estimate_current(df_d, best, fits[best]['params_tuple'])
        ce_s, cn_s = _rolling(ce_s, smooth), _rolling(cn_s, smooth)

        # global single model (global calibration + global K)
        df_g = ca.load_calibrated_log(name, calibration='global')
        ce_g, cn_g, _ = ca.estimate_current(df_g, 'classic', (K,))
        ce_g, cn_g = _rolling(ce_g, smooth), _rolling(cn_g, smooth)
        # global model's own cross-tack discontinuity on this day
        gloss, _ = ca.symmetry_loss(df_g, 'classic', (K,), wins)
        grms = float(np.sqrt(gloss / len(wins)))

        # SSCOFS (subsampled), shared bridge (cached)
        step = max(1, int(round(10 * ca._FS)))
        sub_idx = np.arange(0, len(df_d), step)
        e_kt, n_kt = ic.query_sscofs(df_d, sub_idx)
        scof = ({'e': e_kt, 'n': n_kt}
                if e_kt is not None and np.isfinite(e_kt).any() else None)

        for label, ce, cn, rms in (
                (f'per-session({best})', ce_s, cn_s, fits[best]['rms_kt']),
                ('global', ce_g, cn_g, grms)):
            sp = ca.current_speed_dir(ce, cn)[0]
            b = bias_vs_sscofs(None, ce, cn, sub_idx, scof) if scof else None
            srms = f"{b['rms']:.3f}" if b else 'n/a'
            sbn = f"{b['mN']:+.2f}" if b else 'n/a'
            print(f"{day:18s} {label:18s} {rms:8.3f} {np.nanmean(sp):9.2f} "
                  f"{srms:>12s} {sbn:>7s}")
        print()
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
