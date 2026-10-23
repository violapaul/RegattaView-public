"""Validate heel-dependent speedo (STW) models against the cross-tack structural
step, across the whole pooled tack corpus (1099 tacks / 76 days).

Motivation
----------
The paddlewheel is mounted to PORT of the keel. Two physical effects could make
its scale depend on heel:

  * sign(heel)  — which side is down. On starboard tack the boat heels to port and
    the port wheel sits DEEPER (cleaner flow); on port tack it is shallower (nearer
    the ventilated surface). This is ANTISYMMETRIC across a tack, so it is a
    candidate explanation for the residual tack-direction-correlated "current"
    (the structural step) that a single leeway K leaves behind.
  * |heel|      — how hard she heels changes immersion depth / waterline / local
    flow angle at the wheel. This is tack-SYMMETRIC, so it mostly affects STW
    accuracy rather than the structural step (it only leaks into the step insofar
    as heel magnitude differs between tacks).

STW model fit here::

    scale(heel) = s0 + a*sign(heel) + b*|heel|        (offset stays 0)
    spd_kt = raw_ms * MS_TO_KT * scale(heel)

The competing explanation for the structural step is a per-day HEADING offset
(compass / install / sea-state), which rotates the water vector (cross-track,
orthogonal in the boat frame to the along-track STW-scale error).

Crucial validation asymmetry
----------------------------
``a`` and ``b`` are fixed BOAT constants -> shared across days -> honestly
by-day cross-validatable (fit on train days, score on held-out days). A per-day
heading offset is a per-day nuisance parameter and CANNOT be cross-validated by
holding a day out (no data to fit that day's offset), so we report it in-sample
only, as an upper bound on what day-level rotation could buy.

We score every model by GroupKFold-on-day CV of:
  * disc_rms   — robust RMS cross-tack current discontinuity (the fit objective)
  * struct     — |antisymmetric mean jump| = the tack-direction-correlated step,
                 split by tack direction on the held-out tacks. THIS is the
                 structural-soundness number.

Run:
  conda run --no-capture-output -n sailing python Analyze/validate_speedo_heel_models.py
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
from scipy.optimize import minimize

from Libraries.global_variables import G
G.init_seattle()

MS_TO_KT = G.MS_2_KNOTS
VARIATION = G.MAGNETIC_VARIATION
SPD_FLOOR = 2.0
HEEL_OFFSET = -4.0      # heel = heel_raw - HEEL_OFFSET  (== roll + 4)
TRIM = 0.15             # robust trim, matches fit_leeway_global


# ---------------------------------------------------------------------------
# Flat sample bundle with per-tack direction + day bookkeeping
# ---------------------------------------------------------------------------
class Bundle:
    def __init__(self, samples, index):
        s = samples.copy()
        s['post'] = (s['side'] == 'post').astype(int)
        tack_ids = list(index['tack_id'])
        code = {t: k for k, t in enumerate(tack_ids)}
        s = s[s['tack_id'].isin(code)]
        self.n_tacks = len(tack_ids)
        self.gid = s['tack_id'].map(code).to_numpy() * 2 + s['post'].to_numpy()
        self.ngroup = 2 * self.n_tacks
        self.counts = np.bincount(self.gid, minlength=self.ngroup).astype(float)
        self.counts[self.counts == 0] = np.nan
        self.pre_gid = np.arange(self.n_tacks) * 2
        self.post_gid = self.pre_gid + 1

        self.spd_ms = s['spd_ms'].to_numpy(float)
        self.sog_ms = s['sog_ms'].to_numpy(float)
        self.hdg_mag = s['hdg_mag'].to_numpy(float)
        self.cog_true = s['cog_true'].to_numpy(float)
        self.heel = s['heel_raw'].to_numpy(float) - HEEL_OFFSET
        self.sgn = np.sign(self.heel)
        self.absheel = np.abs(self.heel)

        # tack -> day (from index), and per-sample day code for per-day heading
        self.tack_day = index['day'].to_numpy()
        day_of_tack = dict(zip(index['tack_id'], index['day']))
        s_day = s['tack_id'].map(day_of_tack).to_numpy()
        days = np.array(sorted(set(self.tack_day)))
        self.days = days
        dcode = {d: k for k, d in enumerate(days)}
        self.samp_daycode = np.array([dcode[d] for d in s_day])
        self.tack_daycode = np.array([dcode[d] for d in self.tack_day])
        self.n_days = len(days)

        # tack direction = sign of heel on the PRE side (port vs starboard entry)
        pre_heel = (np.bincount(self.gid, weights=self.heel, minlength=self.ngroup)
                    / self.counts)[self.pre_gid]
        self.dirn = np.sign(pre_heel)   # +1 / -1

    # -- current per sample given a parameter dict ------------------------
    def current(self, p, day_head=None):
        # scale(heel) = s0 + a*sign(heel) + b*|heel| + bh*heel
        #   a  : tack step (antisymmetric, binary in tack)
        #   b  : |heel| magnitude (SYMMETRIC across tack)
        #   bh : signed heel (ANTISYMMETRIC, grows with heel angle ~ sin(heel))
        scale = (p['s0'] + p.get('a', 0.0) * self.sgn
                 + p.get('b', 0.0) * self.absheel + p.get('bh', 0.0) * self.heel)
        spd_kt = self.spd_ms * MS_TO_KT * scale
        sog_kt = self.sog_ms * MS_TO_KT
        hdg = self.hdg_mag + VARIATION + p.get('head', 0.0)
        if day_head is not None:
            hdg = hdg + day_head[self.samp_daycode]
        lam = p.get('K', 0.0) * self.heel / np.maximum(np.abs(spd_kt), SPD_FLOOR) ** 2
        wb = np.radians(hdg + lam)
        cog = np.radians(self.cog_true)
        cur_e = sog_kt * np.sin(cog) - spd_kt * np.sin(wb)
        cur_n = sog_kt * np.cos(cog) - spd_kt * np.cos(wb)
        return cur_e, cur_n

    def jumps(self, p, day_head=None):
        """Per-tack (de, dn) jump = mean(post)-mean(pre)."""
        cur_e, cur_n = self.current(p, day_head)
        me = np.bincount(self.gid, weights=cur_e, minlength=self.ngroup) / self.counts
        mn = np.bincount(self.gid, weights=cur_n, minlength=self.ngroup) / self.counts
        de = me[self.post_gid] - me[self.pre_gid]
        dn = mn[self.post_gid] - mn[self.pre_gid]
        return de, dn

    def disc(self, p, tack_mask=None, day_head=None):
        de, dn = self.jumps(p, day_head)
        d2 = de * de + dn * dn
        ok = np.isfinite(d2)
        if tack_mask is not None:
            ok &= tack_mask
        return d2[ok]

    def struct_step(self, p, tack_mask=None, day_head=None):
        """|antisymmetric mean jump| over the (masked) tacks = structural step."""
        de, dn = self.jumps(p, day_head)
        ok = np.isfinite(de) & np.isfinite(dn)
        if tack_mask is not None:
            ok &= tack_mask
        d = self.dirn
        ps = ok & (d > 0); sp = ok & (d < 0)
        if ps.sum() < 3 or sp.sum() < 3:
            return np.nan
        mps = np.array([de[ps].mean(), dn[ps].mean()])
        msp = np.array([de[sp].mean(), dn[sp].mean()])
        return float(np.hypot(*(0.5 * (mps - msp))))


def robust_mean(d2):
    if len(d2) == 0:
        return 1e9
    keep = max(1, int(round(len(d2) * (1.0 - TRIM))))
    return float(np.mean(np.sort(d2)[:keep]))


# ---------------------------------------------------------------------------
# Model specs: which params are free + bounds + x0
# ---------------------------------------------------------------------------
BASE = dict(s0=1.22, a=0.0, b=0.0, bh=0.0, K=8.0, head=-0.528)

MODELS = {
    # name: list of free param names (the rest held at BASE)
    'base (K, s0, head)':         ['s0', 'K', 'head'],
    '+ sign(heel) a':             ['s0', 'K', 'head', 'a'],
    '+ |heel| b (symmetric)':     ['s0', 'K', 'head', 'b'],
    '+ b*heel (signed)':          ['s0', 'K', 'head', 'bh'],
    '+ a*sign + b*heel':          ['s0', 'K', 'head', 'a', 'bh'],
}
BOUNDS = dict(s0=(1.0, 1.5), K=(-40, 40), head=(-8, 8),
              a=(-0.3, 0.3), b=(-0.05, 0.05), bh=(-0.05, 0.05))


def fit(bundle, free, train_mask=None):
    x0 = [BASE[k] for k in free]
    bnds = [BOUNDS[k] for k in free]

    def loss(x):
        p = dict(BASE); p.update(dict(zip(free, x)))
        return robust_mean(bundle.disc(p, train_mask))

    r = minimize(loss, x0, method='L-BFGS-B', bounds=bnds,
                 options={'maxiter': 4000, 'ftol': 1e-12})
    p = dict(BASE); p.update(dict(zip(free, r.x)))
    return p


def cv_by_day(bundle, free, n_splits=5):
    days = np.arange(bundle.n_days)
    folds = np.array_split(days, n_splits)
    rms, struct = [], []
    for fold in folds:
        test_t = np.isin(bundle.tack_daycode, fold)
        train_t = ~test_t
        if train_t.sum() < 20 or test_t.sum() < 6:
            continue
        p = fit(bundle, free, train_t)
        rms.append(np.sqrt(robust_mean(bundle.disc(p, test_t))))
        struct.append(bundle.struct_step(p, test_t))
    return (float(np.nanmean(rms)) if rms else np.nan,
            float(np.nanmean(struct)) if struct else np.nan)


def fit_per_day_heading(bundle, base_free):
    """Upper-bound reference: fit shared params + one heading offset per day,
    in-sample (NOT cross-validatable). Returns (params, day_head, struct)."""
    p = fit(bundle, base_free)             # shared params first
    # then per-day heading by 1-D solve each day (current is ~linear in small dpsi,
    # but just grid+refine for robustness)
    day_head = np.zeros(bundle.n_days)
    for d in range(bundle.n_days):
        m = bundle.tack_daycode == d
        if m.sum() < 4:
            continue
        smp = bundle.samp_daycode == d

        def loss(dh):
            dharr = np.zeros(bundle.n_days); dharr[d] = dh[0]
            return robust_mean(bundle.disc(p, m, dharr))
        r = minimize(loss, [0.0], method='Nelder-Mead',
                     options={'xatol': 1e-3, 'fatol': 1e-10, 'maxiter': 800})
        day_head[d] = float(r.x[0])
    struct = bundle.struct_step(p, None, day_head)
    return p, day_head, struct


def within_day_cv_heading(bundle, base_free):
    """Honest test of the per-day heading offset: split each day's tacks into two
    halves, fit that day's offset on one half, score the structural step on the
    pooled held-out halves. If a per-day offset is a real day property it
    generalizes within the day; if it was fitting tack noise it will not."""
    p = fit(bundle, base_free)
    # rank within day -> even/odd half assignment
    half = np.zeros(bundle.n_tacks, dtype=int)
    for d in range(bundle.n_days):
        idx = np.where(bundle.tack_daycode == d)[0]
        half[idx[np.argsort(idx)][1::2]] = 1   # every other tack -> half 1
    held = []
    for train_half in (0, 1):
        day_head = np.zeros(bundle.n_days)
        for d in range(bundle.n_days):
            m_train = (bundle.tack_daycode == d) & (half == train_half)
            if m_train.sum() < 3:
                continue

            def loss(dh):
                a = np.zeros(bundle.n_days); a[d] = dh[0]
                return robust_mean(bundle.disc(p, m_train, a))
            r = minimize(loss, [0.0], method='Nelder-Mead',
                         options={'xatol': 1e-3, 'fatol': 1e-10, 'maxiter': 800})
            day_head[d] = float(r.x[0])
        test_mask = half == (1 - train_half)
        held.append(bundle.struct_step(p, test_mask, day_head))
    return float(np.nanmean(held))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--data', default=str(Path(__file__).with_name('TackData')))
    ap.add_argument('--out', default=str(Path(__file__).with_name('CurrentOut')))
    args = ap.parse_args(argv)

    samples = pd.read_parquet(os.path.join(args.data, 'tack_samples.parquet'))
    index = pd.read_parquet(os.path.join(args.data, 'tack_index.parquet'))
    b = Bundle(samples, index)
    print(f"{b.n_tacks} tacks / {b.n_days} days / {len(b.gid)} samples")
    print(f"tack directions: port-entry={int((b.dirn>0).sum())} "
          f"stbd-entry={int((b.dirn<0).sum())}\n")

    print(f"{'model':24s} {'s0':>6s} {'a':>7s} {'b':>8s} {'bh':>8s} {'K':>6s} "
          f"{'head':>6s}  {'CVdisc':>7s} {'CVstruct':>8s} {'INstruct':>8s}")
    rows = {}
    for name, free in MODELS.items():
        p = fit(b, free)
        cvd, cvs = cv_by_day(b, free)
        ins = b.struct_step(p)
        rows[name] = dict(params={k: p[k] for k in free}, cv_disc=cvd,
                          cv_struct=cvs, in_struct=ins)
        print(f"{name:24s} {p['s0']:6.3f} {p.get('a',0):7.4f} {p.get('b',0):8.5f} "
              f"{p.get('bh',0):8.5f} {p['K']:6.2f} {p['head']:6.2f}  "
              f"{cvd:7.4f} {cvs:8.4f} {ins:8.4f}")

    # per-day heading reference (in-sample only)
    p_pd, day_head, struct_pd = fit_per_day_heading(b, MODELS['base (K, s0, head)'])
    finite = day_head[np.abs(day_head) > 1e-6]
    wd = within_day_cv_heading(b, MODELS['base (K, s0, head)'])
    print(f"\nper-day heading offset:")
    print(f"  IN  struct (in-sample, 76 free params)  = {struct_pd:.4f} kt")
    print(f"  CV  struct (within-day held-out half)   = {wd:.4f} kt   "
          f"<- honest number")
    print(f"  day dpsi mean={np.mean(finite):+.2f} sd={np.std(finite):.2f} "
          f"range=[{finite.min():+.2f},{finite.max():+.2f}] deg")

    # interpret the signed b*heel term physically (the heel-proportional asymmetry)
    if '+ b*heel (signed)' in rows:
        pp = rows['+ b*heel (signed)']['params']
        s0 = pp['s0']; bh = pp.get('bh', 0)
        d15 = bh * 15.0
        print(f"\nInterpretation of '+ b*heel (signed)' (physical: immersion ~ sin heel):")
        print(f"  base scale s0   = {s0:.3f}")
        print(f"  bh              = {bh:+.5f} /deg")
        print(f"  at 15 deg heel  -> scale delta {d15:+.4f}  "
              f"(one tack {s0+d15:.3f}, other {s0-d15:.3f}, {200*d15/s0:+.1f}% spread)")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, 'speedo_heel_validation.json'), 'w') as f:
        json.dump(dict(n_tacks=b.n_tacks, n_days=b.n_days, models=rows,
                       per_day_heading=dict(in_struct=struct_pd,
                                            mean=float(np.mean(finite)),
                                            sd=float(np.std(finite)))), f, indent=2)
    print(f"\nwrote {os.path.join(args.out, 'speedo_heel_validation.json')}")
    print("\nKey question: does a GLOBAL sign(heel) term (a) cut CVstruct as much "
          "as a per-day heading offset? If yes, the structural step is the "
          "port-side speedo depth asymmetry (one boat constant), not daily compass drift.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
