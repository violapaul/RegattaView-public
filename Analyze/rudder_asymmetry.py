"""Rudder sensor asymmetry investigation.

Motivated by the tacking round-2 finding (TACKING_EFFICIENCY.md §5.4/§8):
s2p tacks show a +4.7 deg higher rudder p90 and ~0.25 BL more loss than p2s.
Is that real technique/boat handedness, or is the rudder sensor asymmetric?
Three independent probes, all per-session then pooled:

1. **Static offset** — steady upwind helm on port vs starboard tack. Weather
   helm is mirror-imaged between tacks, so the midpoint of the two steady
   medians estimates the sensor zero offset ``r0`` and their half-difference
   the true weather helm. (A constant offset cancels in the tack analysis's
   ``rudder_rel`` — it canNOT explain the s2p excess — but it should agree
   with probe 2's intercept if the sensor model is right.)
2. **Turn-rate gain** — during genuine turning, rudder demanded per deg/s of
   yaw should mirror between left and right turns at matched speed. Piecewise
   fit ``rudder = c + bP*yaw+  + bN*yaw-`` (rudder shifted by its measured
   lead over yaw). Gain ratio |bP/bN| != 1 is sign-dependent sensor gain (or
   genuinely asymmetric helm response) — exactly the kind of error that
   WOULD inflate one tack direction's rudder_p90.
3. **Whole-day average** while sailing — crude, but a drifting/offset sensor
   shows up here over the corpus timeline.

Outputs: per-session table + pooled estimates on stderr, figure
``Analyze/Plots/rudder_asymmetry.png``, and the implied contribution of any
gain asymmetry to the observed s2p-p2s rudder_p90 gap.

Run:
  conda run --no-capture-output -n sailing python Analyze/rudder_asymmetry.py
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

from Analyze import current_analysis as ca

_FS = ca._FS

SPD_MIN_KT = 3.5         # genuinely sailing
SPD_MAX_KT = 8.0         # keep the speed band tight (rudder authority ~ V^2)
STEADY_YAW_MAX = 0.5     # deg/s: "sailing straight"
TURN_YAW_MIN = 1.5       # deg/s: "genuinely turning"
TURN_YAW_MAX = 8.0       # deg/s: beyond this it's a crash maneuver
UPWIND_AWA = (22.0, 60.0)  # |awa| band for steady weather-helm states
LAG_SCAN_S = (0.0, 3.0)  # rudder leads yaw by this much (scan)


def smooth(x, sec):
    k = max(1, int(round(sec * _FS)))
    return np.convolve(np.nan_to_num(x, nan=0.0), np.ones(k) / k, 'same')


def yaw_rate(hdg):
    """deg/s from hdg_true, wrap-safe, ~1.5 s smoothed."""
    d = np.diff(hdg, prepend=hdg[:1])
    d = ((d + 180) % 360) - 180
    return smooth(d * _FS, 1.5)


def best_lag_s(rud, yaw, mask):
    """Lag (s) maximizing corr(rudder(t), yaw(t+lag)) over the turn samples.
    Positive = rudder leads yaw (causality)."""
    best, blag = -np.inf, 0.0
    for lag in np.arange(LAG_SCAN_S[0], LAG_SCAN_S[1] + 0.01, 0.2):
        k = int(round(lag * _FS))
        if k == 0:
            r, y = rud[mask], yaw[mask]
        else:
            m = mask[:-k] if k else mask
            r, y = rud[:-k][mask[:-k]], yaw[k:][mask[:-k]]
        if len(r) < 200:
            continue
        c = np.corrcoef(r, y)[0, 1]
        if abs(c) > best:
            best, blag = abs(c), lag
    return blag


def session_stats(name, logs_dir):
    try:
        df = ca.load_calibrated_log(name, path=logs_dir)
    except Exception as e:  # noqa: BLE001
        print(f"  {name}: load failed ({e})", file=sys.stderr)
        return None
    if df is None or 'rudder' not in df.columns or len(df) < _FS * 1200:
        return None
    rud = df['rudder'].to_numpy(float)
    if not np.isfinite(rud).any():
        return None
    spd = df['spd_kt'].to_numpy(float)
    awa = df['awa'].to_numpy(float)
    hdg = df['hdg_true'].to_numpy(float)
    yaw = yaw_rate(hdg)
    rud_s = smooth(rud, 1.0)

    sailing = (spd >= SPD_MIN_KT) & (spd <= SPD_MAX_KT) & np.isfinite(rud)
    if sailing.sum() < _FS * 600:
        return None

    # --- probe 1: steady upwind helm, port vs starboard tack --------------
    steady = sailing & (np.abs(yaw) < STEADY_YAW_MAX)
    up = (np.abs(awa) >= UPWIND_AWA[0]) & (np.abs(awa) <= UPWIND_AWA[1])
    port = steady & up & (awa < 0)
    stbd = steady & up & (awa > 0)
    if port.sum() < _FS * 60 or stbd.sum() < _FS * 60:
        m_p = m_s = np.nan
    else:
        m_p = float(np.nanmedian(rud[port]))
        m_s = float(np.nanmedian(rud[stbd]))
    r0_steady = (m_p + m_s) / 2.0
    helm = (m_p - m_s) / 2.0

    # --- probe 2: piecewise rudder ~ yaw during turns ----------------------
    turning = sailing & (np.abs(yaw) >= TURN_YAW_MIN) & (np.abs(yaw) <= TURN_YAW_MAX)
    bP = bN = c0 = lag = np.nan
    if turning.sum() >= _FS * 60:
        lag = best_lag_s(rud_s, yaw, turning)
        k = int(round(lag * _FS))
        if k > 0:
            r_al, y_al = rud_s[:-k], yaw[k:]
            t_al = turning[:-k]
        else:
            r_al, y_al, t_al = rud_s, yaw, turning
        y_t, r_t = y_al[t_al], r_al[t_al]
        X = np.column_stack([np.ones_like(y_t),
                             np.maximum(y_t, 0.0), np.minimum(y_t, 0.0)])
        coef, *_ = np.linalg.lstsq(X, r_t, rcond=None)
        c0, bP, bN = (float(v) for v in coef)

    # --- probe 3: whole-day average while sailing --------------------------
    day_mean = float(np.nanmean(rud[sailing]))
    day_median = float(np.nanmedian(rud[sailing]))

    return dict(name=name, day=os.path.basename(name)[:10],
                m_port=m_p, m_stbd=m_s, r0_steady=r0_steady, helm=helm,
                c0=c0, bP=bP, bN=bN, lag=lag,
                day_mean=day_mean, day_median=day_median,
                # raw turn samples for the pooled binned curve
                _yaw=yaw[turning][::5], _rud=rud_s[turning][::5],
                rng_lo=float(np.nanpercentile(rud[sailing], 0.5)),
                rng_hi=float(np.nanpercentile(rud[sailing], 99.5)))


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--logs-dir', default=None)
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--out', default=str(PROJECT_ROOT / 'Analyze' / 'Plots'))
    args = ap.parse_args(argv)

    logs_dir = args.logs_dir or ca.default_logs_dir()
    files = sorted(glob.glob(os.path.join(logs_dir, '*.parquet')))
    if args.limit:
        files = files[-args.limit:]
    os.makedirs(args.out, exist_ok=True)

    rows = []
    for i, p in enumerate(files):
        st = session_stats(os.path.basename(p), logs_dir)
        if st:
            rows.append(st)
            print(f"[{i+1}/{len(files)}] {st['day']}: r0_steady="
                  f"{st['r0_steady']:+.1f} helm={st['helm']:+.1f} "
                  f"c0={st['c0']:+.1f} bP={st['bP']:+.2f} bN={st['bN']:+.2f} "
                  f"lag={st['lag']:.1f}s dayMean={st['day_mean']:+.1f}",
                  file=sys.stderr)
    if not rows:
        print("no usable sessions", file=sys.stderr)
        return 1

    def arr(k):
        return np.array([r[k] for r in rows], float)

    r0s, helm, c0 = arr('r0_steady'), arr('helm'), arr('c0')
    bP, bN, lag = arr('bP'), arr('bN'), arr('lag')
    dmean = arr('day_mean')
    ratio = np.abs(bP / bN)

    def med_iqr(x):
        x = x[np.isfinite(x)]
        if not len(x):
            return 'n/a'
        q1, q2, q3 = np.percentile(x, [25, 50, 75])
        return f"{q2:+.2f} [{q1:+.2f}, {q3:+.2f}] (n={len(x)})"

    print(f"\n=== Rudder sensor asymmetry, {len(rows)} modern sessions ===",
          file=sys.stderr)
    print(f"probe 1  r0 (steady-helm midpoint, deg): {med_iqr(r0s)}",
          file=sys.stderr)
    print(f"         weather helm +/- (deg):         {med_iqr(helm)}",
          file=sys.stderr)
    print(f"probe 2  c0 (turn-fit intercept, deg):   {med_iqr(c0)}",
          file=sys.stderr)
    print(f"         bP (deg rudder per +deg/s yaw): {med_iqr(bP)}",
          file=sys.stderr)
    print(f"         bN (deg rudder per -deg/s yaw): {med_iqr(bN)}",
          file=sys.stderr)
    print(f"         gain ratio |bP/bN|:             {med_iqr(ratio)}",
          file=sys.stderr)
    print(f"         rudder->yaw lead (s):           {med_iqr(lag)}",
          file=sys.stderr)
    print(f"probe 3  day mean while sailing (deg):   {med_iqr(dmean)}",
          file=sys.stderr)
    print(f"         sailing range used (deg):       "
          f"[{np.nanmedian(arr('rng_lo')):+.1f}, "
          f"{np.nanmedian(arr('rng_hi')):+.1f}]", file=sys.stderr)

    # implication for the tacking analysis: p2s turns one way, s2p the other.
    # rudder_rel subtracts the pre-tack mean, so r0 cancels there; a gain
    # ratio g rescales one direction's peak: expected p90 gap from gain alone
    # ~= (g - 1) * typical p90 (~37 deg). Compare to the observed +4.7 deg.
    g = np.nanmedian(ratio)
    print(f"\nimplication: gain ratio {g:.3f} alone predicts an s2p-p2s "
          f"rudder_p90 gap of ~{(g - 1) * 37:+.1f} deg "
          f"(observed: +4.7 deg)", file=sys.stderr)

    # ---- figure -----------------------------------------------------------
    fig, (a0, a1, a2) = plt.subplots(1, 3, figsize=(14.5, 4.6))
    days = [r['day'] for r in rows]
    x = np.arange(len(rows))
    a0.plot(x, r0s, 'o-', ms=3, lw=0.8, label='r0 steady-helm midpoint')
    a0.plot(x, c0, 's-', ms=3, lw=0.8, alpha=0.7, label='c0 turn-fit intercept')
    a0.plot(x, dmean, '.-', ms=3, lw=0.8, alpha=0.5, label='day mean (sailing)')
    a0.axhline(0, color='0.5', lw=0.8)
    step = max(1, len(rows) // 10)
    a0.set_xticks(x[::step])
    a0.set_xticklabels([days[i] for i in range(0, len(rows), step)],
                       rotation=45, ha='right', fontsize=7)
    a0.set_ylabel('rudder (sensor deg)')
    a0.set_title('offset estimates per session')
    a0.legend(fontsize=8)
    a0.grid(alpha=0.3)

    yy = np.concatenate([r['_yaw'] for r in rows])
    rr = np.concatenate([r['_rud'] for r in rows])
    bins = np.arange(-TURN_YAW_MAX, TURN_YAW_MAX + 0.5, 0.5)
    bc = 0.5 * (bins[1:] + bins[:-1])
    med = np.array([np.nanmedian(rr[(yy >= a) & (yy < b)])
                    if ((yy >= a) & (yy < b)).sum() > 30 else np.nan
                    for a, b in zip(bins[:-1], bins[1:])])
    a1.plot(yy[::7], rr[::7], '.', ms=1, alpha=0.08, color='#1f77b4')
    a1.plot(bc, med, 'r-o', ms=3, lw=1.6, label='binned median')
    a1.plot(bc, -med[::-1], 'g--', lw=1.2,
            label='mirror of opposite side')
    a1.axhline(0, color='0.5', lw=0.8)
    a1.axvline(0, color='0.5', lw=0.8)
    a1.set_xlabel('yaw rate (deg/s)')
    a1.set_ylabel('rudder (sensor deg, 1 s smoothed)')
    a1.set_title('rudder vs turn rate, pooled\n(gap between red and green = asymmetry)')
    a1.legend(fontsize=8)
    a1.grid(alpha=0.3)

    a2.hist(r0s[np.isfinite(r0s)], bins=20, alpha=0.7, label='r0 steady')
    a2.hist(c0[np.isfinite(c0)], bins=20, alpha=0.5, label='c0 turn fit')
    a2.axvline(0, color='0.5', lw=0.8)
    a2.set_xlabel('offset estimate (deg)')
    a2.set_title('offset distributions')
    a2.legend(fontsize=8)
    a2.grid(alpha=0.3)

    fig.tight_layout()
    out = os.path.join(args.out, 'rudder_asymmetry.png')
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"\nWrote {out}", file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
