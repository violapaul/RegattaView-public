"""Corpus retrospective: offline-calibrated triangle current vs SSCOFS.

Runs the single *global* leeway model (``Analyze/leeway_model.json``) over every
session that SSCOFS can be sampled for (live S3 within retention, or already in
the RaceRouting surface cache), then aggregates the triangle-vs-SSCOFS gap to
answer one question: **where are the biggest gaps?**

The gap is decomposed three ways, because "RMS 0.5 kt" alone hides the structure:

* **Systematic** — the per-session *mean* vector difference (triangle − SSCOFS).
  A constant (E,N) offset. The known ~-0.1..-0.2 kt North bias lives here.
* **Scatter** — RMS of the difference *after* removing that per-session mean.
  Point-to-point disagreement that a constant calibration can't fix.
  ``total_rms² ≈ |mean_bias|² + scatter²``.
* **Regime** — the gap split by where/when: sailing vs slow/at-dock (the marina
  basin is a known SSCOFS weak spot), and by current magnitude (does the gap
  grow with the current, i.e. a scale error, or sit flat, i.e. an offset?).

Usage
-----
    conda run -n sailing python Analyze/current_retrospective.py
    conda run -n sailing python Analyze/current_retrospective.py --since 2026-04-01
    conda run -n sailing python Analyze/current_retrospective.py --sessions 2026-05-09T08_36 2026-05-30T08_08

Outputs (``--out``, default ``Analyze/CurrentOut``):
    retrospective.json            per-session + pooled stats
    retrospective_bias.png        per-session mean-bias vectors + total/scatter RMS
    retrospective_scatter.png     triangle vs SSCOFS component scatter, pooled
"""
import argparse
import datetime as dt
import glob
import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from Analyze import current_analysis as ca
from Analyze import investigate_current as ic

_FS = ca._FS


# ---------------------------------------------------------------------------
# Per-session evaluation
# ---------------------------------------------------------------------------
def eval_session(name, K, logs_dir, subsample_s=10.0, smooth_s=30.0,
                 sscofs_env='currents'):
    """Evaluate one session. Returns a dict of stats, or None if unusable /
    no SSCOFS coverage."""
    try:
        df = ca.load_calibrated_log(name, path=logs_dir, calibration='global')
    except Exception as e:  # noqa: BLE001
        print(f"  (skip {name}: {e})", file=sys.stderr)
        return None

    smooth_win = int(round(smooth_s * _FS))
    ce, cn, _ = ca.estimate_current(df, 'classic', (K,))
    ce = ic._rolling_mean(ce, smooth_win)
    cn = ic._rolling_mean(cn, smooth_win)

    step = max(1, int(round(subsample_s * _FS)))
    sub_idx = np.arange(0, len(df), step)
    e_kt, n_kt = ic.query_sscofs(df, sub_idx, env=sscofs_env)
    if e_kt is None or not np.isfinite(e_kt).any():
        print(f"  (skip {name}: no SSCOFS coverage)", file=sys.stderr)
        return None

    # Triangle current at the SSCOFS sample points.
    te = ce[sub_idx]
    tn = cn[sub_idx]
    spd = df['spd_kt'].to_numpy(float)[sub_idx]

    de = te - e_kt
    dn = tn - n_kt
    good = np.isfinite(de) & np.isfinite(dn)
    if good.sum() < 5:
        print(f"  (skip {name}: only {int(good.sum())} comparable pts)", file=sys.stderr)
        return None

    de, dn, spd = de[good], dn[good], spd[good]
    te_g, tn_g = te[good], tn[good]
    se_g, sn_g = e_kt[good], n_kt[good]

    mE, mN = float(np.mean(de)), float(np.mean(dn))           # systematic vector bias
    total_rms = float(np.sqrt(np.mean(de**2 + dn**2)))        # total disagreement
    # scatter = RMS after removing the per-session mean bias
    scatter = float(np.sqrt(np.mean((de - mE)**2 + (dn - mN)**2)))

    # magnitudes / set
    tri_mag = np.hypot(te_g, tn_g)
    scf_mag = np.hypot(se_g, sn_g)

    # regime split: sailing vs slow (dock / drifting)
    sailing = spd > ca.SAILING_KT
    def _rms(mask):
        if mask.sum() < 3:
            return None
        return float(np.sqrt(np.mean(de[mask]**2 + dn[mask]**2)))

    date = dt.date.fromisoformat(os.path.basename(name)[:10])
    return {
        'session': os.path.basename(name),
        'date': date.isoformat(),
        'n_pts': int(good.sum()),
        'frac_on_water': float(good.sum() / len(sub_idx)),
        'mean_bias_E_kt': mE,
        'mean_bias_N_kt': mN,
        'mean_bias_mag_kt': float(np.hypot(mE, mN)),
        'total_rms_kt': total_rms,
        'scatter_rms_kt': scatter,
        'tri_mag_mean_kt': float(np.mean(tri_mag)),
        'scf_mag_mean_kt': float(np.mean(scf_mag)),
        'rms_sailing_kt': _rms(sailing),
        'rms_slow_kt': _rms(~sailing),
        'n_sailing': int(sailing.sum()),
        'n_slow': int((~sailing).sum()),
        # raw arrays kept for pooled aggregation (not serialized per-session)
        '_de': de, '_dn': dn, '_spd': spd,
        '_te': te_g, '_tn': tn_g, '_se': se_g, '_sn': sn_g,
    }


# ---------------------------------------------------------------------------
# Pooled aggregation
# ---------------------------------------------------------------------------
def pooled_stats(results):
    de = np.concatenate([r['_de'] for r in results])
    dn = np.concatenate([r['_dn'] for r in results])
    spd = np.concatenate([r['_spd'] for r in results])

    mE, mN = float(np.mean(de)), float(np.mean(dn))
    total_rms = float(np.sqrt(np.mean(de**2 + dn**2)))
    # scatter after removing GLOBAL mean bias
    scatter_global = float(np.sqrt(np.mean((de - mE)**2 + (dn - mN)**2)))
    # scatter after removing each session's OWN mean bias (how much is left if we
    # allowed a free per-session offset — i.e. irreducible point scatter)
    de_c = np.concatenate([r['_de'] - r['mean_bias_E_kt'] for r in results])
    dn_c = np.concatenate([r['_dn'] - r['mean_bias_N_kt'] for r in results])
    scatter_persession = float(np.sqrt(np.mean(de_c**2 + dn_c**2)))

    # gap vs current magnitude (SSCOFS magnitude bins) — scale vs offset error?
    se = np.concatenate([r['_se'] for r in results])
    sn = np.concatenate([r['_sn'] for r in results])
    scf_mag = np.hypot(se, sn)
    edges = [0, 0.2, 0.4, 0.6, 0.9, 99]
    by_mag = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        m = (scf_mag >= lo) & (scf_mag < hi)
        if m.sum() < 10:
            by_mag.append({'bin': f'{lo:.1f}-{hi:.1f}', 'n': int(m.sum()),
                           'rms_kt': None, 'mean_bias_mag_kt': None})
            continue
        by_mag.append({
            'bin': f'{lo:.1f}-{hi:.1f}', 'n': int(m.sum()),
            'rms_kt': float(np.sqrt(np.mean(de[m]**2 + dn[m]**2))),
            'mean_bias_E_kt': float(np.mean(de[m])),
            'mean_bias_N_kt': float(np.mean(dn[m])),
            'mean_bias_mag_kt': float(np.hypot(np.mean(de[m]), np.mean(dn[m]))),
        })

    # per-component agreement: regression slope (triangle = a*sscofs + b) and
    # Pearson r. Slope < 1 = triangle under-responds to SSCOFS variation (or
    # heavy independent scatter / regression dilution); r = shared signal.
    te = np.concatenate([r['_te'] for r in results])
    tn = np.concatenate([r['_tn'] for r in results])
    def _fit(x, y):
        m = np.isfinite(x) & np.isfinite(y)
        if m.sum() < 10:
            return None
        A = np.vstack([x[m], np.ones(m.sum())]).T
        slope, intcpt = np.linalg.lstsq(A, y[m], rcond=None)[0]
        r = float(np.corrcoef(x[m], y[m])[0, 1])
        return {'slope': float(slope), 'intercept': float(intcpt), 'pearson_r': r}
    agree = {'East': _fit(se, te), 'North': _fit(sn, tn)}

    # regime split pooled
    sailing = spd > ca.SAILING_KT
    def _rms(mask):
        return (float(np.sqrt(np.mean(de[mask]**2 + dn[mask]**2)))
                if mask.sum() else None)
    def _bias(mask):
        return ([float(np.mean(de[mask])), float(np.mean(dn[mask]))]
                if mask.sum() else None)

    return {
        'n_pts': int(len(de)),
        'mean_bias_E_kt': mE,
        'mean_bias_N_kt': mN,
        'mean_bias_mag_kt': float(np.hypot(mE, mN)),
        'total_rms_kt': total_rms,
        'scatter_after_global_bias_kt': scatter_global,
        'scatter_after_persession_bias_kt': scatter_persession,
        'component_agreement': agree,
        'by_sscofs_magnitude': by_mag,
        'sailing': {'n': int(sailing.sum()), 'rms_kt': _rms(sailing),
                    'mean_bias': _bias(sailing)},
        'slow': {'n': int((~sailing).sum()), 'rms_kt': _rms(~sailing),
                 'mean_bias': _bias(~sailing)},
    }


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def make_bias_figure(results, pooled, out_path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(1, 2, figsize=(15, 6.5))

    # (0) per-session mean-bias vectors (E,N), arrow from origin
    a = ax[0]
    for r in results:
        a.annotate('', xy=(r['mean_bias_E_kt'], r['mean_bias_N_kt']),
                   xytext=(0, 0),
                   arrowprops=dict(arrowstyle='->', color='tab:blue', alpha=0.7))
        a.text(r['mean_bias_E_kt'], r['mean_bias_N_kt'], '  ' + r['date'][5:],
               fontsize=7, va='center')
    a.annotate('', xy=(pooled['mean_bias_E_kt'], pooled['mean_bias_N_kt']),
               xytext=(0, 0),
               arrowprops=dict(arrowstyle='->', color='tab:red', lw=2.5))
    a.text(pooled['mean_bias_E_kt'], pooled['mean_bias_N_kt'], '  POOLED',
           fontsize=9, color='tab:red', fontweight='bold')
    lim = max(0.35, 1.15 * max(abs(r['mean_bias_E_kt']) for r in results),
              1.15 * max(abs(r['mean_bias_N_kt']) for r in results))
    a.set_xlim(-lim, lim); a.set_ylim(-lim, lim)
    a.axhline(0, color='0.7', lw=0.6); a.axvline(0, color='0.7', lw=0.6)
    a.set_aspect('equal')
    a.set_xlabel('mean bias East (kt)  [triangle − SSCOFS]')
    a.set_ylabel('mean bias North (kt)')
    a.set_title('Per-session systematic bias vector')
    a.grid(alpha=0.3)

    # (1) total vs scatter RMS per session, sorted by total
    a = ax[1]
    rs = sorted(results, key=lambda r: r['total_rms_kt'])
    y = np.arange(len(rs))
    a.barh(y, [r['total_rms_kt'] for r in rs], color='tab:orange',
           alpha=0.55, label='total RMS')
    a.barh(y, [r['scatter_rms_kt'] for r in rs], color='tab:blue',
           alpha=0.8, label='scatter (bias-removed)')
    a.set_yticks(y); a.set_yticklabels([r['date'] for r in rs], fontsize=8)
    a.axvline(pooled['total_rms_kt'], color='tab:red', ls='--', lw=1.5,
              label=f"pooled total {pooled['total_rms_kt']:.2f}")
    a.set_xlabel('RMS gap (kt)')
    a.set_title('Total gap vs irreducible scatter, by session')
    a.legend(fontsize=8, loc='lower right'); a.grid(alpha=0.3, axis='x')

    fig.suptitle('Triangle (global model) vs SSCOFS — corpus retrospective',
                 fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.97])
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def make_scatter_figure(results, out_path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    te = np.concatenate([r['_te'] for r in results])
    tn = np.concatenate([r['_tn'] for r in results])
    se = np.concatenate([r['_se'] for r in results])
    sn = np.concatenate([r['_sn'] for r in results])

    fig, ax = plt.subplots(1, 2, figsize=(14, 6.5))
    for a, (s, t, comp) in zip(ax, [(se, te, 'East'), (sn, tn, 'North')]):
        a.scatter(s, t, s=4, alpha=0.25, color='tab:blue')
        lim = 1.5
        a.plot([-lim, lim], [-lim, lim], 'k--', lw=1, label='y=x')
        # least-squares slope/intercept (triangle = a*sscofs + b)
        m = np.isfinite(s) & np.isfinite(t)
        if m.sum() > 10:
            A = np.vstack([s[m], np.ones(m.sum())]).T
            slope, intcpt = np.linalg.lstsq(A, t[m], rcond=None)[0]
            xs = np.array([-lim, lim])
            a.plot(xs, slope * xs + intcpt, color='tab:red', lw=1.5,
                   label=f'fit: {slope:.2f}·x{intcpt:+.2f}')
        a.set_xlim(-lim, lim); a.set_ylim(-lim, lim); a.set_aspect('equal')
        a.axhline(0, color='0.7', lw=0.6); a.axvline(0, color='0.7', lw=0.6)
        a.set_xlabel(f'SSCOFS current {comp} (kt)')
        a.set_ylabel(f'triangle current {comp} (kt)')
        a.set_title(f'{comp} component')
        a.legend(fontsize=8); a.grid(alpha=0.3)
    fig.suptitle('Per-point: triangle vs SSCOFS current components (all sessions)',
                 fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.96])
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def discover_sessions(logs_dir, since=None, until=None):
    out = []
    for p in sorted(glob.glob(os.path.join(logs_dir, '*.parquet'))):
        base = os.path.basename(p)
        try:
            d = dt.date.fromisoformat(base[:10])
        except ValueError:
            continue
        if since and d < since:
            continue
        if until and d > until:
            continue
        out.append(base)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--sessions', nargs='*', help='explicit parquet names/stems')
    ap.add_argument('--since', type=dt.date.fromisoformat, default=None,
                    help='only sessions on/after YYYY-MM-DD')
    ap.add_argument('--until', type=dt.date.fromisoformat, default=None)
    ap.add_argument('--logs-dir', default=None)
    ap.add_argument('--out', default=str(Path(__file__).with_name('CurrentOut')))
    ap.add_argument('--subsample-s', type=float, default=10.0)
    ap.add_argument('--smooth-s', type=float, default=30.0)
    ap.add_argument('--sscofs-env', default='currents')
    args = ap.parse_args(argv)

    logs_dir = args.logs_dir or ca.default_logs_dir()
    m = ca.load_leeway_model()
    if not m:
        raise SystemExit("leeway_model.json not found — run leeway_report.py first")
    K = m['leeway']['K']
    cal = m['calibration']
    print(f"Global model: stw_scale={cal['stw_scale']:.3f}, "
          f"heading_offset={cal['heading_offset_deg']:.2f}deg, "
          f"classic K={K:.2f}  (CV rms {m['cv_rms_kt']:.3f} kt)", file=sys.stderr)

    if args.sessions:
        sessions = [s if s.endswith('.parquet') else s + '.parquet'
                    for s in args.sessions]
    else:
        sessions = discover_sessions(logs_dir, args.since, args.until)
    print(f"Evaluating {len(sessions)} candidate sessions "
          f"(SSCOFS coverage will filter further)\n", file=sys.stderr)

    results = []
    for name in sessions:
        print(f"--- {name}", file=sys.stderr)
        r = eval_session(name, K, logs_dir, args.subsample_s, args.smooth_s,
                         args.sscofs_env)
        if r:
            results.append(r)

    if not results:
        raise SystemExit("No sessions had SSCOFS coverage — nothing to report.")

    pooled = pooled_stats(results)

    # ---- console report ----
    print("\n" + "=" * 92)
    print("CORPUS RETROSPECTIVE — offline-calibrated triangle vs SSCOFS")
    print("=" * 92)
    hdr = (f"{'date':11s} {'n':>5s} {'|tri|':>6s} {'|scf|':>6s} "
           f"{'biasE':>6s} {'biasN':>6s} {'|bias|':>7s} "
           f"{'total':>6s} {'scat':>6s} {'sail':>6s} {'slow':>6s}")
    print(hdr); print('-' * len(hdr))
    for r in sorted(results, key=lambda x: x['date']):
        sail = f"{r['rms_sailing_kt']:.2f}" if r['rms_sailing_kt'] is not None else '  -'
        slow = f"{r['rms_slow_kt']:.2f}" if r['rms_slow_kt'] is not None else '  -'
        print(f"{r['date']:11s} {r['n_pts']:5d} "
              f"{r['tri_mag_mean_kt']:6.2f} {r['scf_mag_mean_kt']:6.2f} "
              f"{r['mean_bias_E_kt']:+6.2f} {r['mean_bias_N_kt']:+6.2f} "
              f"{r['mean_bias_mag_kt']:7.2f} "
              f"{r['total_rms_kt']:6.2f} {r['scatter_rms_kt']:6.2f} "
              f"{sail:>6s} {slow:>6s}")

    print("\nPOOLED")
    print(f"  points compared        : {pooled['n_pts']}")
    print(f"  mean bias (E,N)        : ({pooled['mean_bias_E_kt']:+.3f}, "
          f"{pooled['mean_bias_N_kt']:+.3f}) kt  "
          f"|bias|={pooled['mean_bias_mag_kt']:.3f} kt")
    print(f"  total RMS gap          : {pooled['total_rms_kt']:.3f} kt")
    print(f"  scatter (− global bias): {pooled['scatter_after_global_bias_kt']:.3f} kt")
    print(f"  scatter (− per-session): {pooled['scatter_after_persession_bias_kt']:.3f} kt "
          f"  <- irreducible point scatter")
    ag = pooled['component_agreement']
    print("  per-component agreement (triangle = slope·SSCOFS + b):")
    for comp in ('East', 'North'):
        a = ag[comp]
        if a:
            print(f"    {comp:5s}: slope={a['slope']:.2f}  r={a['pearson_r']:.2f}  "
                  f"intercept={a['intercept']:+.3f} kt")
    s, sl = pooled['sailing'], pooled['slow']
    print(f"  sailing  (spd>{ca.SAILING_KT}): n={s['n']:6d}  RMS={s['rms_kt']:.3f} kt")
    print(f"  slow/dock(spd<={ca.SAILING_KT}): n={sl['n']:6d}  RMS={sl['rms_kt']:.3f} kt")
    print("\n  gap vs SSCOFS current magnitude:")
    print(f"    {'bin(kt)':>9s} {'n':>6s} {'RMS':>6s} {'|bias|':>7s}")
    for b in pooled['by_sscofs_magnitude']:
        if b['rms_kt'] is None:
            print(f"    {b['bin']:>9s} {b['n']:6d}    -      -")
        else:
            print(f"    {b['bin']:>9s} {b['n']:6d} {b['rms_kt']:6.2f} "
                  f"{b['mean_bias_mag_kt']:7.2f}")

    # ---- figures + JSON ----
    os.makedirs(args.out, exist_ok=True)
    bias_png = os.path.join(args.out, 'retrospective_bias.png')
    scat_png = os.path.join(args.out, 'retrospective_scatter.png')
    make_bias_figure(results, pooled, bias_png)
    make_scatter_figure(results, scat_png)

    serial = []
    for r in results:
        serial.append({k: v for k, v in r.items() if not k.startswith('_')})
    json_path = os.path.join(args.out, 'retrospective.json')
    with open(json_path, 'w') as f:
        json.dump({'model': {'K': K, 'calibration': cal,
                             'cv_rms_kt': m['cv_rms_kt']},
                   'n_sessions': len(results),
                   'sessions': serial, 'pooled': pooled}, f, indent=2)

    print(f"\nwrote:\n  {json_path}\n  {bias_png}\n  {scat_png}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
