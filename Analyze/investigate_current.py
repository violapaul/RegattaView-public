"""Investigate, validate, and compare current estimates for one sailing session.

Pipeline
--------
1. Load a session log (``--log NAME`` or auto-pick a recent multi-tack day).
2. Build the instrument "current triangle" under three leeway models
   (``none`` / ``classic`` / ``wind``) and calibrate each by cross-tack symmetry.
3. Optionally sample the SSCOFS model current along the track (``--sscofs``),
   via the standalone ``sscofs_bridge.py`` run in the ``currents`` conda env.
4. Emit diagnostic PNGs + a per-sample CSV + a JSON fit summary.

Examples
--------
    conda run -n sailing python Analyze/investigate_current.py            # auto-pick
    conda run -n sailing python Analyze/investigate_current.py --log 2026-05-30T08_08.parquet --sscofs

Outputs land under ``--out`` (default ``Analyze/CurrentOut``):
    current_<YYYYMMDD>_overview.png   time series + leeway + symmetry diagnostics
    current_<YYYYMMDD>_map.png        track with estimated (+ SSCOFS) current quiver
    current_<YYYYMMDD>.csv            per-(subsampled)-sample comparison table
    current_<YYYYMMDD>.json           fit summary (models, params, losses, bias)
"""
import argparse
import datetime as dt
import glob
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np

from Analyze import current_analysis as ca

_FS = ca._FS
MS_TO_KT = ca.MS_TO_KT


# ---------------------------------------------------------------------------
# Session selection
# ---------------------------------------------------------------------------
def _file_date(path):
    base = os.path.basename(path)
    try:
        return dt.date.fromisoformat(base[:10])
    except ValueError:
        return None


def autopick_log(logs_dir, max_age_days=32, min_tacks=6, today=None):
    """Pick a recent, long, multi-tack session. Tries newest large sessions
    first and returns the first whose tack count clears ``min_tacks``."""
    today = today or dt.date.today()
    cutoff = today - dt.timedelta(days=max_age_days)
    files = []
    for p in glob.glob(os.path.join(logs_dir, '*.parquet')):
        d = _file_date(p)
        if d and d >= cutoff:
            files.append((os.path.getsize(p), p))
    # Largest (longest) sessions first — more upwind tacks = better symmetry fit.
    files.sort(reverse=True)
    for _, p in files:
        name = os.path.basename(p)
        try:
            df = ca.load_calibrated_log(name, path=logs_dir)
            wins = ca.tack_windows(df, ca.detect_tacks(df))
        except Exception as e:  # noqa: BLE001
            print(f"  (skip {name}: {e})", file=sys.stderr)
            continue
        if len(wins) >= min_tacks:
            print(f"Auto-picked {name}: {len(wins)} tack windows", file=sys.stderr)
            return name, df, wins
        print(f"  (skip {name}: only {len(wins)} tack windows)", file=sys.stderr)
    raise SystemExit(
        f"autopick: no session within {max_age_days}d had >= {min_tacks} tacks")


# ---------------------------------------------------------------------------
# Smoothing + subsampling helpers
# ---------------------------------------------------------------------------
def _rolling_mean(a, win):
    """Centered moving average, NaN-tolerant, same length."""
    a = np.asarray(a, float)
    if win <= 1:
        return a.copy()
    k = np.ones(win)
    valid = np.isfinite(a).astype(float)
    filled = np.where(np.isfinite(a), a, 0.0)
    num = np.convolve(filled, k, mode='same')
    den = np.convolve(valid, k, mode='same')
    with np.errstate(invalid='ignore', divide='ignore'):
        return np.where(den > 0, num / den, np.nan)


def _iso(unixtime):
    return dt.datetime.fromtimestamp(float(unixtime), tz=dt.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# SSCOFS bridge invocation
# ---------------------------------------------------------------------------
def query_sscofs(df, sub_idx, env='currents', script=None):
    """Run sscofs_bridge in ``env`` for the subsampled track points.

    Returns ``(e_kt, n_kt)`` arrays aligned to ``sub_idx`` (NaN on failure /
    land), or ``(None, None)`` if the model couldn't be loaded at all."""
    script = script or str(Path(__file__).with_name('sscofs_bridge.py'))
    with tempfile.TemporaryDirectory() as td:
        in_csv = os.path.join(td, 'track.csv')
        out_csv = os.path.join(td, 'out.csv')
        with open(in_csv, 'w') as f:
            f.write('lat,lon,unixtime\n')
            for i in sub_idx:
                f.write(f"{df['lat'].iloc[i]:.6f},{df['lon'].iloc[i]:.6f},"
                        f"{df['unixtime'].iloc[i]:.1f}\n")
        cmd = ['conda', 'run', '--no-capture-output', '-n', env,
               'python', script, in_csv, out_csv]
        print(f"SSCOFS: {' '.join(cmd[:6])} ... (downloads model on first run)",
              file=sys.stderr)
        try:
            subprocess.run(cmd, check=True)
        except subprocess.CalledProcessError as e:
            print(f"SSCOFS bridge failed ({e}); continuing triangle-only.",
                  file=sys.stderr)
            return None, None
        e_kt = np.full(len(sub_idx), np.nan)
        n_kt = np.full(len(sub_idx), np.nan)
        import csv as _csv
        with open(out_csv, newline='') as f:
            for j, r in enumerate(_csv.DictReader(f)):
                e_kt[j] = float(r['cur_sscofs_e_kt']) if r['cur_sscofs_e_kt'] else np.nan
                n_kt[j] = float(r['cur_sscofs_n_kt']) if r['cur_sscofs_n_kt'] else np.nan
        return e_kt, n_kt


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------
def make_overview_figure(df, fits, best, cur, t_rel, sub, scof, out_path):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(3, 2, figsize=(15, 12))
    tmin = t_rel / 60.0  # minutes

    # (0,0) East component: models vs sscofs
    a = ax[0, 0]
    a.plot(tmin, cur['none'][0], color='0.6', lw=0.8, label='none')
    a.plot(tmin, cur['classic'][0], color='tab:blue', lw=1.0, label='classic')
    if best == 'wind':
        a.plot(tmin, cur['wind'][0], color='tab:green', lw=1.0, label='wind')
    if scof is not None:
        a.plot(sub['tmin'], scof['e'], 'o-', color='tab:red', ms=3, lw=1.0,
               label='SSCOFS')
    a.set_title('Current East (kt)'); a.set_xlabel('min'); a.legend(fontsize=8)
    a.grid(alpha=0.3)

    # (1,0) North component
    a = ax[1, 0]
    a.plot(tmin, cur['none'][1], color='0.6', lw=0.8, label='none')
    a.plot(tmin, cur['classic'][1], color='tab:blue', lw=1.0, label='classic')
    if best == 'wind':
        a.plot(tmin, cur['wind'][1], color='tab:green', lw=1.0, label='wind')
    if scof is not None:
        a.plot(sub['tmin'], scof['n'], 'o-', color='tab:red', ms=3, lw=1.0,
               label='SSCOFS')
    a.set_title('Current North (kt)'); a.set_xlabel('min'); a.legend(fontsize=8)
    a.grid(alpha=0.3)

    # (2,0) Speed (and set as twin) best vs sscofs
    a = ax[2, 0]
    spd_b, set_b = ca.current_speed_dir(cur[best][0], cur[best][1])
    a.plot(tmin, spd_b, color='tab:blue', lw=1.0, label=f'|cur| {best}')
    if scof is not None:
        spd_s, _ = ca.current_speed_dir(scof['e'], scof['n'])
        a.plot(sub['tmin'], spd_s, 'o-', color='tab:red', ms=3, lw=1.0,
               label='|cur| SSCOFS')
    a.set_title('Current speed (kt)'); a.set_xlabel('min'); a.legend(fontsize=8)
    a.grid(alpha=0.3)

    # (0,1) Per-tack cross-tack discontinuity by model
    a = ax[0, 1]
    width = 0.27
    for k, (m, color) in enumerate([('none', '0.6'), ('classic', 'tab:blue'),
                                    ('wind', 'tab:green')]):
        per = fits[m]['per_tack']
        xs = np.arange(len(per)) + (k - 1) * width
        a.bar(xs, [p[1] for p in per], width=width, color=color,
              label=f"{m} (rms {fits[m]['rms_kt']:.2f})")
    a.set_title('Cross-tack current discontinuity (kt) — lower is better')
    a.set_xlabel('tack #'); a.legend(fontsize=8); a.grid(alpha=0.3, axis='y')

    # (1,1) Loss vs K curve (classic)
    a = ax[1, 1]
    wins = ca.tack_windows(df, ca.detect_tacks(df))
    Ks = np.linspace(-5, 35, 81)
    losses = [ca.symmetry_loss(df, 'classic', (K,), wins)[0] for K in Ks]
    a.plot(Ks, losses, color='tab:blue')
    Kbest = fits['classic']['params']['K']
    a.axvline(Kbest, color='k', ls='--', lw=1)
    a.plot([Kbest], [fits['classic']['loss']], 'o', color='tab:red')
    a.set_title(f"Tack-symmetry loss vs leeway K  (best K={Kbest:.1f})")
    a.set_xlabel('K  [lambda = K·heel/spd²]'); a.set_ylabel('Σ |Δcur|²')
    a.grid(alpha=0.3)

    # (2,1) Leeway angle (best), sailing samples only
    a = ax[2, 1]
    lam = cur[best][2].copy()
    sailing = df['spd_kt'].to_numpy() > ca.SAILING_KT
    lam_plot = np.where(sailing, lam, np.nan)
    a.plot(tmin, lam_plot, color='tab:purple', lw=0.8)
    pj = ', '.join(f"{k}={v:.2f}" for k, v in fits[best]['params'].items()) or '—'
    a.set_title(f"Leeway angle (deg), model={best} [{pj}]")
    a.set_xlabel('min'); a.grid(alpha=0.3)
    a.set_ylim(-25, 25)

    fig.suptitle(f"Current investigation — {df.attrs.get('name','')}  "
                 f"(best leeway model: {best})", fontsize=13)
    fig.tight_layout(rect=[0, 0, 1, 0.98])
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


def make_map_figure(df, cur, best, scof, sub_idx, out_path, quiver_s=180.0):
    """Track with a *coarse* current quiver (~quiver_s spacing), drawn only
    where the boat is actually sailing so arrows are legible."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    lon = df['lon'].to_numpy(); lat = df['lat'].to_numpy()
    spd = df['spd_kt'].to_numpy()
    mlat = 111320.0
    mlon = 111320.0 * np.cos(np.radians(np.nanmean(lat)))

    fig, ax = plt.subplots(figsize=(11, 11))
    ax.plot(lon, lat, color='0.75', lw=0.6, zorder=1)

    ce, cn, _ = cur[best]
    ce_s = _rolling_mean(ce, int(30 * _FS))
    cn_s = _rolling_mean(cn, int(30 * _FS))

    # coarse quiver indices, sailing only
    qstep = max(1, int(round(quiver_s * _FS)))
    qi = np.arange(0, len(df), qstep)
    qi = qi[spd[qi] > ca.SAILING_KT]

    sc = 0.022  # degrees per knot (arrow visual scale)
    ax.quiver(lon[qi], lat[qi],
              ce_s[qi] * sc / (mlon / mlat), cn_s[qi] * sc,
              angles='xy', scale_units='xy', scale=1.0,
              color='tab:blue', width=0.004,
              label='estimated current (boat)', zorder=4)
    if scof is not None:
        # map subsampled-SSCOFS values back to the nearest coarse quiver point
        sub_idx = np.asarray(sub_idx)
        pos = np.searchsorted(sub_idx, qi).clip(0, len(sub_idx) - 1)
        se, sn = scof['e'][pos], scof['n'][pos]
        good = np.isfinite(se) & np.isfinite(sn)
        ax.quiver(lon[qi][good], lat[qi][good],
                  se[good] * sc / (mlon / mlat), sn[good] * sc,
                  angles='xy', scale_units='xy', scale=1.0,
                  color='tab:red', width=0.004,
                  label='SSCOFS current (model)', zorder=3)
    # reference 1-kt arrow
    x0, y0 = lon.min(), lat.min()
    ax.quiver([x0], [y0], [1.0 * sc / (mlon / mlat)], [0.0],
              angles='xy', scale_units='xy', scale=1.0, color='k', width=0.004)
    ax.text(x0, y0 + 0.004, '1 kt', fontsize=9)

    ax.set_aspect(mlat / mlon)
    ax.set_xlabel('lon'); ax.set_ylabel('lat')
    ax.set_title(f"Current: boat estimate vs SSCOFS — {df.attrs.get('name','')}\n"
                 f"(leeway model: {best}; quiver every {quiver_s:g}s)")
    ax.legend(loc='best')
    ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=110)
    plt.close(fig)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--log', help='parquet filename (default: auto-pick recent day)')
    ap.add_argument('--logs-dir', default=None,
                    help='PandasLogs directory (default: auto-resolve)')
    ap.add_argument('--sscofs', action='store_true',
                    help='also sample SSCOFS model current along the track')
    ap.add_argument('--sscofs-env', default='currents',
                    help='conda env with SSCOFS deps (default: currents)')
    ap.add_argument('--out', default=str(Path(__file__).with_name('CurrentOut')),
                    help='output directory')
    ap.add_argument('--subsample-s', type=float, default=10.0,
                    help='seconds between SSCOFS / quiver / CSV samples')
    ap.add_argument('--smooth-s', type=float, default=30.0,
                    help='centered moving-average window for the estimated current')
    args = ap.parse_args(argv)

    logs_dir = args.logs_dir or ca.default_logs_dir()

    # --- load + tacks ---
    if args.log:
        df = ca.load_calibrated_log(args.log, path=logs_dir)
        wins = ca.tack_windows(df, ca.detect_tacks(df))
        if not wins:
            raise SystemExit(f"{args.log}: no usable tack windows for the symmetry fit")
        name = args.log
    else:
        name, df, wins = autopick_log(logs_dir)

    date_tag = os.path.basename(name)[:10].replace('-', '')
    os.makedirs(args.out, exist_ok=True)

    # --- fit + estimate ---
    fits, best = ca.rank_models(df, wins)
    smooth_win = int(round(args.smooth_s * _FS))
    cur = {}
    for m in ca.MODELS:
        ce, cn, lam = ca.estimate_current(df, m, fits[m]['params_tuple'])
        cur[m] = (_rolling_mean(ce, smooth_win), _rolling_mean(cn, smooth_win), lam)

    t0 = float(df['unixtime'].iloc[0])
    t_rel = df['unixtime'].to_numpy() - t0

    # --- subsample indices ---
    step = max(1, int(round(args.subsample_s * _FS)))
    sub_idx = np.arange(0, len(df), step)
    sub = {'idx': sub_idx, 'tmin': t_rel[sub_idx] / 60.0}

    # --- SSCOFS ---
    scof = None
    if args.sscofs:
        e_kt, n_kt = query_sscofs(df, sub_idx, env=args.sscofs_env)
        if e_kt is not None and np.isfinite(e_kt).any():
            scof = {'e': e_kt, 'n': n_kt}
        else:
            print("SSCOFS unavailable for this session; triangle-only outputs.",
                  file=sys.stderr)

    # --- figures ---
    overview = os.path.join(args.out, f'current_{date_tag}_overview.png')
    mapfig = os.path.join(args.out, f'current_{date_tag}_map.png')
    make_overview_figure(df, fits, best, cur, t_rel, sub, scof, overview)
    make_map_figure(df, cur, best, scof, sub_idx, mapfig)

    # --- CSV (subsampled comparison) ---
    csv_path = os.path.join(args.out, f'current_{date_tag}.csv')
    ce_b, cn_b, lam_b = cur[best]
    ce_0, cn_0, _ = cur['none']
    import csv as _csv
    with open(csv_path, 'w', newline='') as f:
        w = _csv.writer(f)
        w.writerow(['unixtime', 'iso', 'lat', 'lon', 'hdg_true', 'cog_true',
                    'spd_kt', 'sog_kt', 'heel_deg', 'awa_deg', 'aws_kt',
                    'leeway_deg', 'cur_none_e', 'cur_none_n',
                    f'cur_{best}_e', f'cur_{best}_n',
                    'cur_sscofs_e', 'cur_sscofs_n'])
        for j, i in enumerate(sub_idx):
            row = [f"{df['unixtime'].iloc[i]:.1f}", _iso(df['unixtime'].iloc[i]),
                   f"{df['lat'].iloc[i]:.6f}", f"{df['lon'].iloc[i]:.6f}",
                   f"{df['hdg_true'].iloc[i]:.1f}", f"{df['cog_true'].iloc[i]:.1f}",
                   f"{df['spd_kt'].iloc[i]:.2f}", f"{df['sog_kt'].iloc[i]:.2f}",
                   f"{df['heel'].iloc[i]:.1f}", f"{df['awa'].iloc[i]:.1f}",
                   f"{df['aws_kt'].iloc[i]:.1f}", f"{lam_b[i]:.2f}",
                   f"{ce_0[i]:.3f}", f"{cn_0[i]:.3f}",
                   f"{ce_b[i]:.3f}", f"{cn_b[i]:.3f}"]
            if scof is not None:
                row += ['' if not np.isfinite(scof['e'][j]) else f"{scof['e'][j]:.3f}",
                        '' if not np.isfinite(scof['n'][j]) else f"{scof['n'][j]:.3f}"]
            else:
                row += ['', '']
            w.writerow(row)

    # --- bias stats vs SSCOFS ---
    bias = None
    if scof is not None:
        de = ce_b[sub_idx] - scof['e']
        dn = cn_b[sub_idx] - scof['n']
        good = np.isfinite(de) & np.isfinite(dn)
        if good.any():
            bias = {
                'n_points': int(good.sum()),
                'mean_diff_e_kt': float(np.mean(de[good])),
                'mean_diff_n_kt': float(np.mean(dn[good])),
                'rms_diff_kt': float(np.sqrt(np.mean(de[good]**2 + dn[good]**2))),
                'mean_abs_diff_kt': float(np.mean(np.hypot(de[good], dn[good]))),
            }

    # --- JSON summary ---
    summary = {
        'session': name,
        'rows': int(len(df)),
        'n_tacks_used': len(wins),
        'best_model': best,
        'models': {m: {'params': fits[m]['params'], 'loss': fits[m]['loss'],
                       'rms_discontinuity_kt': fits[m]['rms_kt']} for m in ca.MODELS},
        'current_kt': {
            'mean': float(np.nanmean(ca.current_speed_dir(ce_b, cn_b)[0])),
            'median': float(np.nanmedian(ca.current_speed_dir(ce_b, cn_b)[0])),
            'p95': float(np.nanpercentile(ca.current_speed_dir(ce_b, cn_b)[0], 95)),
        },
        'sscofs_bias': bias,
        'outputs': {'overview': overview, 'map': mapfig, 'csv': csv_path},
    }
    json_path = os.path.join(args.out, f'current_{date_tag}.json')
    with open(json_path, 'w') as f:
        json.dump(summary, f, indent=2)

    # --- console report ---
    print("\n=== Current investigation summary ===")
    print(f"session         : {name}  ({len(df)} rows, {len(wins)} tacks used)")
    for m in ca.MODELS:
        pj = ', '.join(f"{k}={v:.3f}" for k, v in fits[m]['params'].items()) or '—'
        star = '  <-- best' if m == best else ''
        print(f"  {m:8s} rms Δcur = {fits[m]['rms_kt']:.3f} kt   [{pj}]{star}")
    sp = ca.current_speed_dir(ce_b, cn_b)[0]
    print(f"current ({best}): mean {np.nanmean(sp):.2f} kt, "
          f"median {np.nanmedian(sp):.2f} kt, p95 {np.nanpercentile(sp,95):.2f} kt")
    if bias:
        print(f"vs SSCOFS       : {bias['n_points']} pts, "
              f"mean diff (E,N)=({bias['mean_diff_e_kt']:+.2f},"
              f"{bias['mean_diff_n_kt']:+.2f}) kt, "
              f"RMS {bias['rms_diff_kt']:.2f} kt")
    elif args.sscofs:
        print("vs SSCOFS       : unavailable (see messages above)")
    print(f"\nwrote:\n  {overview}\n  {mapfig}\n  {csv_path}\n  {json_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
