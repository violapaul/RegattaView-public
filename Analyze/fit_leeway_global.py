"""Fit ONE global leeway model across all sailing days by pooled cross-tack symmetry.

Consumes the cached dataset from ``build_tack_dataset.py``. The objective is the
total cross-tack current discontinuity over every tack in the corpus:

    loss(theta) = mean_tacks || mean(cur_post) - mean(cur_pre) ||^2

where the current at each sample is the triangle
``cur = V_ground(GPS) - V_water(STW along hdg_true + leeway(theta))``.

Models compared (a complexity ladder):
    none      lambda = 0
    classic   lambda = K * heel / spd^2
    power     lambda = K * heel / spd^n              (fit n)
    intercept lambda = K * heel / spd^2 + c*sign(heel)
    wind      lambda = (a + b*TWS) * heel / spd^2
    spline    lambda = sign(heel) * f(|heel|/spd^2)  (monotone, non-parametric)

Each is scored by **by-day cross-validation** (GroupKFold on calendar day) so we
measure generalization to unseen days and keep the flexible spline honest.

Calibration: STW scale / heading offset / heel offset are applied here, not baked
into the cache, so we can also *jointly* fit ``stw_scale`` from the tacks and
compare it to the two existing calibrations (display 1.184 vs water-ref 1.107).

Run:
  conda run -n sailing python Analyze/fit_leeway_global.py
  conda run -n sailing python Analyze/fit_leeway_global.py --joint-stw
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
from scipy.optimize import minimize, minimize_scalar

from Libraries.global_variables import G
G.init_seattle()  # populates MAGNETIC_VARIATION + venue constants (cf. calibrate_water_ref.py)

MS_TO_KT = G.MS_2_KNOTS
VARIATION = G.MAGNETIC_VARIATION
SPD_FLOOR = 2.0  # kt, clamp in leeway denominator

# Default calibration: water-ref fit (more rigorous than the display JSON).
DEFAULT_CAL = dict(stw_scale=1.1066, stw_offset=0.0,
                   heading_offset=-0.528, heel_offset=-4.0)


# ---------------------------------------------------------------------------
# Vectorised sample bundle (built once from the cache)
# ---------------------------------------------------------------------------
class TackBundle:
    """Flat per-sample arrays + group bookkeeping for fast objective evals."""

    def __init__(self, samples, index):
        # stable group id per (tack, side): tack*2 + (side=='post')
        s = samples.copy()
        s['side_post'] = (s['side'] == 'post').astype(int)
        tack_ids = list(index['tack_id'])
        tcode = {t: k for k, t in enumerate(tack_ids)}
        s = s[s['tack_id'].isin(tcode)]
        self.gid = (s['tack_id'].map(tcode).to_numpy() * 2
                    + s['side_post'].to_numpy())
        self.ngroup = 2 * len(tack_ids)
        self.counts = np.bincount(self.gid, minlength=self.ngroup).astype(float)
        self.counts[self.counts == 0] = np.nan

        self.spd_ms = s['spd_ms'].to_numpy(float)
        self.sog_ms = s['sog_ms'].to_numpy(float)
        self.hdg_mag = s['hdg_mag'].to_numpy(float)
        self.cog_true = s['cog_true'].to_numpy(float)
        self.heel_raw = s['heel_raw'].to_numpy(float)
        self.aws_ms = s['aws_ms'].to_numpy(float)
        self.tws_ms = s['tws_ms'].to_numpy(float)

        # tack -> (pre_gid, post_gid) and day labels
        self.pre_gid = np.arange(len(tack_ids)) * 2
        self.post_gid = self.pre_gid + 1
        self.tack_day = index['day'].to_numpy()
        self.n_tacks = len(tack_ids)

    # -- calibration ------------------------------------------------------
    def apply_cal(self, cal):
        spd_kt = self.spd_ms * MS_TO_KT * cal['stw_scale'] + cal['stw_offset']
        sog_kt = self.sog_ms * MS_TO_KT
        hdg_true = self.hdg_mag + VARIATION + cal['heading_offset']
        heel = self.heel_raw - cal['heel_offset']
        aws_kt = self.aws_ms * MS_TO_KT
        tws_kt = self.tws_ms * MS_TO_KT
        x = heel / np.maximum(np.abs(spd_kt), SPD_FLOOR) ** 2
        return dict(spd_kt=spd_kt, sog_kt=sog_kt, hdg_true=hdg_true,
                    cog_true=self.cog_true, heel=heel,
                    aws_kt=aws_kt, tws_kt=tws_kt, x=x)

    # -- per-group current means -----------------------------------------
    def group_current(self, c, lam_deg):
        wb = np.radians(c['hdg_true'] + lam_deg)
        cur_e = c['sog_kt'] * np.sin(np.radians(c['cog_true'])) - c['spd_kt'] * np.sin(wb)
        cur_n = c['sog_kt'] * np.cos(np.radians(c['cog_true'])) - c['spd_kt'] * np.cos(wb)
        me = np.bincount(self.gid, weights=cur_e, minlength=self.ngroup) / self.counts
        mn = np.bincount(self.gid, weights=cur_n, minlength=self.ngroup) / self.counts
        return me, mn

    def tack_discontinuity(self, c, lam_deg, tack_mask=None):
        me, mn = self.group_current(c, lam_deg)
        de = me[self.post_gid] - me[self.pre_gid]
        dn = mn[self.post_gid] - mn[self.pre_gid]
        d2 = de * de + dn * dn
        ok = np.isfinite(d2)
        if tack_mask is not None:
            ok &= tack_mask
        return d2[ok], np.hypot(de[ok], dn[ok])


# Robust loss: pooled tacks include genuine current jumps + bad data. Trim the
# worst TRIM fraction of squared discontinuities so the fit isn't dragged by a
# few outliers (real current shear that no leeway model can remove).
TRIM = 0.15


def robust_mean(d2):
    if len(d2) == 0:
        return 1e9
    keep = max(1, int(round(len(d2) * (1.0 - TRIM))))
    return float(np.mean(np.sort(d2)[:keep]))


# ---------------------------------------------------------------------------
# Leeway models — each returns lambda(deg) given calibrated feature dict
# ---------------------------------------------------------------------------
def _spline_knots(x, n=7):
    return np.quantile(np.abs(x[np.isfinite(x)]), np.linspace(0, 1, n))


def leeway(model, theta, c, knots=None):
    x = c['x']
    if model == 'none':
        return np.zeros_like(x)
    if model == 'classic':
        return theta[0] * x
    if model == 'power':
        K, n = theta
        return K * c['heel'] / np.maximum(np.abs(c['spd_kt']), SPD_FLOOR) ** n
    if model == 'intercept':
        K, cc = theta
        return K * x + cc * np.sign(c['heel'])
    if model == 'wind':
        a, b = theta
        return (a + b * c['tws_kt']) * x
    if model == 'spline':
        heights = np.asarray(theta)
        return np.sign(x) * np.interp(np.abs(x), knots, heights)
    raise ValueError(model)


MODEL_SPECS = {
    'none':      dict(x0=[], names=[]),
    'classic':   dict(x0=[8.0], names=['K']),
    'power':     dict(x0=[8.0, 2.0], names=['K', 'n']),
    'intercept': dict(x0=[8.0, 0.0], names=['K', 'c']),
    'wind':      dict(x0=[8.0, 0.0], names=['a', 'b']),
    'spline':    dict(x0=None, names=None),  # sized at fit time
}


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------
def _split_theta(theta, free_stw, base_cal):
    """Peel an optional leading stw_scale off theta; return (cal, leeway_theta)."""
    if free_stw:
        cal = dict(base_cal); cal['stw_scale'] = theta[0]
        return cal, list(theta[1:])
    return base_cal, list(theta)


def fit_model(model, bundle, base_cal, train_mask=None, knots=None, reg=2.0,
              free_stw=False):
    """Fit theta on the (optionally masked) training tacks. When ``free_stw``,
    theta[0] is a global stw_scale fit jointly with the leeway params.
    Returns (theta, knots)."""

    def loss(theta):
        cal, lt = _split_theta(theta, free_stw, base_cal)
        c = bundle.apply_cal(cal)
        lam = leeway(model, lt, c, knots)
        d2, _ = bundle.tack_discontinuity(c, lam, train_mask)
        base = robust_mean(d2)
        if model == 'spline' and reg:  # penalize curvature (2nd differences)
            base += reg * float(np.mean(np.diff(np.asarray(lt), 2) ** 2))
        return base

    spec = MODEL_SPECS[model]
    stw0 = [1.18] if free_stw else []
    stw_bnd = [(0.9, 1.45)] if free_stw else []

    if model == 'none' and not free_stw:
        return [], knots
    if model == 'classic' and not free_stw:
        r = minimize_scalar(lambda K: loss([K]), bounds=(-40, 40), method='bounded')
        return [float(r.x)], knots
    if model == 'spline':
        if knots is None:
            knots = _spline_knots(bundle.apply_cal(base_cal)['x'])
        x0 = np.r_[stw0, np.full(len(knots), 8.0) * knots]
        bnds = stw_bnd + [(-30, 30)] * len(knots)
        r = minimize(loss, x0, method='L-BFGS-B', bounds=bnds)
        return list(map(float, r.x)), knots
    x0 = np.r_[stw0, spec['x0']]
    r = minimize(loss, x0, method='Nelder-Mead',
                 options={'xatol': 1e-3, 'fatol': 1e-7, 'maxiter': 6000})
    return list(map(float, r.x)), knots


def eval_disc(model, theta, bundle, base_cal, mask, knots=None, free_stw=False):
    """Return (rms, median) held-out per-tack discontinuity (kt)."""
    cal, lt = _split_theta(theta, free_stw, base_cal)
    c = bundle.apply_cal(cal)
    lam = leeway(model, lt, c, knots)
    d2, disc = bundle.tack_discontinuity(c, lam, mask)
    if len(d2) == 0:
        return np.nan, np.nan
    return float(np.sqrt(robust_mean(d2))), float(np.median(disc))


def cv_by_day(model, bundle, base_cal, n_splits=5, knots_all=None, free_stw=False):
    """GroupKFold over calendar day; return mean held-out (rms, median)."""
    days = np.array(sorted(set(bundle.tack_day)))
    folds = np.array_split(days, min(n_splits, len(days)))
    rms_list, med_list = [], []
    for fold_days in folds:
        test_day = np.isin(bundle.tack_day, fold_days)
        train_mask = ~test_day
        if train_mask.sum() < 10 or test_day.sum() < 3:
            continue
        theta, knots = fit_model(model, bundle, base_cal, train_mask,
                                 knots=knots_all, free_stw=free_stw)
        rms, med = eval_disc(model, theta, bundle, base_cal, test_day,
                             knots, free_stw=free_stw)
        rms_list.append(rms); med_list.append(med)
    return (float(np.nanmean(rms_list)) if rms_list else np.nan,
            float(np.nanmean(med_list)) if med_list else np.nan)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=str(Path(__file__).with_name('TackData')))
    ap.add_argument('--out', default=str(Path(__file__).with_name('CurrentOut')))
    ap.add_argument('--cal', choices=['waterref', 'display'], default='waterref')
    ap.add_argument('--joint-stw', action='store_true',
                    help='also jointly fit a global stw_scale with the classic model')
    args = ap.parse_args(argv)

    samples = pd.read_parquet(os.path.join(args.data, 'tack_samples.parquet'))
    index = pd.read_parquet(os.path.join(args.data, 'tack_index.parquet'))
    bundle = TackBundle(samples, index)
    print(f"Loaded {bundle.n_tacks} tacks across {len(set(bundle.tack_day))} days, "
          f"{len(bundle.gid)} samples")

    if args.cal == 'display':
        cal = dict(stw_scale=1.183908, stw_offset=0.1933,
                   heading_offset=0.0, heel_offset=-4.0)
    else:
        cal = dict(DEFAULT_CAL)
    print(f"Calibration ({args.cal}): {cal}")

    ladder = ['none', 'classic', 'power', 'intercept', 'wind', 'spline']
    knots_all = _spline_knots(bundle.apply_cal(cal)['x'])
    results = {}

    for free_stw in (False, True):
        tag = 'FREE stw_scale (jointly fit)' if free_stw else f'FIXED stw_scale={cal["stw_scale"]:.3f}'
        print(f"\n=== Leeway ladder, {tag} ===")
        print(f"{'model':10s} {'train_rms':>9s} {'cv_rms':>7s} {'cv_med':>7s}   params")
        for model in ladder:
            if model == 'none' and free_stw:
                continue
            ka = knots_all if model == 'spline' else None
            theta, knots = fit_model(model, bundle, cal, knots=ka, free_stw=free_stw)
            tr, _ = eval_disc(model, theta, bundle, cal, None, knots, free_stw)
            cv_rms, cv_med = cv_by_day(model, bundle, cal, knots_all=ka, free_stw=free_stw)
            key = f"{model}{'+stw' if free_stw else ''}"
            results[key] = dict(model=model, free_stw=free_stw, theta=theta,
                                train_rms=tr, cv_rms=cv_rms, cv_median=cv_med,
                                knots=(list(knots) if model == 'spline' else None),
                                names=(['stw_scale'] if free_stw else []) + (MODEL_SPECS[model]['names'] or []))
            allnames = (['stw'] if free_stw else []) + (MODEL_SPECS[model]['names'] or [])
            if model == 'spline':
                lead = f"stw={theta[0]:.3f} " if free_stw else ''
                ps = lead + '[' + ', '.join(f'{t:.1f}' for t in theta[(1 if free_stw else 0):]) + ']'
            else:
                ps = ', '.join(f"{nm}={v:.3f}" for nm, v in zip(allnames, theta)) or '-'
            print(f"{model:10s} {tr:9.4f} {cv_rms:7.4f} {cv_med:7.4f}   {ps}")

    best = min((k for k in results if np.isfinite(results[k]['cv_rms'])),
               key=lambda k: results[k]['cv_rms'])
    print(f"\nBest by CV rms: {best}  "
          f"(cv_rms={results[best]['cv_rms']:.4f} kt, cv_median={results[best]['cv_median']:.4f} kt)")

    os.makedirs(args.out, exist_ok=True)
    out = dict(n_tacks=bundle.n_tacks, n_days=len(set(bundle.tack_day)),
               base_calibration=cal, trim=TRIM, models=results, best=best,
               spline_knots=list(knots_all))
    with open(os.path.join(args.out, 'leeway_fit.json'), 'w') as f:
        json.dump(out, f, indent=2)
    print(f"\nwrote {os.path.join(args.out, 'leeway_fit.json')}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
