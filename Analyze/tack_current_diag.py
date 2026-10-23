"""Structural-soundness diagnostic for the on-boat current triangle.

The claim under test: the estimated current must be *continuous across a tack*.
This tool makes that visible, per tack, three ways for one session:

  A) MAP  — the GPS track with, at each tack, two current arrows drawn from the
            same origin: the mean current in the steady window just BEFORE the
            tack (blue) and just AFTER (red). If the triangle is sound the two
            arrows overlie each other; a fan = a discontinuity the leeway model
            failed to remove.
  B) TIME — cur_E and cur_N vs time, tacks marked, with the pre/post window means
            drawn as bold bars so continuity (or a sawtooth locked to the tacks)
            is obvious.
  C) JUMP — each tack's discontinuity vector (cur_post - cur_pre) in the E-N
            plane, COLORED BY TACK DIRECTION (port->stbd vs stbd->port). This is
            the structural tell: real current shear is uncorrelated with which
            way you tacked, so a clean split by colour means a heading / leeway /
            STW-scale error is leaking into "current" as a tack-synchronous
            artifact, NOT water moving.

Run (in the worktree, sailing env)::

    conda run --no-capture-output -n sailing \
        python Analyze/tack_current_diag.py --log 2026-05-09T08_36 \
        --models none classic --out Analyze/CurrentOut

Omitting --log auto-picks the largest recent parquet. Calibration defaults to
'display' (the committed sensor_calibration.json, STW scale 1.22); classic-K is
fit per session by cross-tack symmetry so each session is judged on its own terms.
"""
import argparse
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

import Analyze.current_analysis as ca


def _latlon_to_local_m(lat, lon):
    """Equirectangular projection to metres about the track centroid (good to
    <0.1% over a race course at this latitude)."""
    lat0 = float(np.nanmean(lat))
    lon0 = float(np.nanmean(lon))
    mlat = 111_132.0
    mlon = 111_320.0 * np.cos(np.radians(lat0))
    return (lon - lon0) * mlon, (lat - lat0) * mlat


def _tack_direction(df, idx, half=80):
    """+1 if the boat went port->starboard (heading increased through the tack),
    -1 if starboard->port, by comparing mean heading a few seconds either side.
    Uses unwrapped heading to be robust to the 0/360 wrap."""
    n = len(df)
    a0, a1 = max(0, idx - half - 40), max(0, idx - 40)
    b0, b1 = min(n, idx + 40), min(n, idx + half + 40)
    hdg = np.radians(df['hdg_true'].to_numpy(float))
    # circular means
    pre = np.arctan2(np.nanmean(np.sin(hdg[a0:a1])), np.nanmean(np.cos(hdg[a0:a1])))
    post = np.arctan2(np.nanmean(np.sin(hdg[b0:b1])), np.nanmean(np.cos(hdg[b0:b1])))
    d = np.degrees((post - pre + np.pi) % (2 * np.pi) - np.pi)
    return 1 if d >= 0 else -1


def _fit(df, model, windows):
    if model == 'none':
        return ca.fit_leeway_by_tack_symmetry(df, 'none', windows)
    return ca.fit_leeway_by_tack_symmetry(df, model, windows)


def _window_means(df, fit, windows):
    """Per-tack pre/post mean current (E,N) and tack location (x,y in metres)."""
    cur_e, cur_n, _ = ca.estimate_current(df, fit['model'], fit['params_tuple'])
    x, y = _latlon_to_local_m(df['lat'].to_numpy(float), df['lon'].to_numpy(float))
    rows = []
    for idx, pre, post in windows:
        rows.append(dict(
            idx=idx,
            xt=float(np.nanmean(x[max(0, idx - 5):idx + 5])),
            yt=float(np.nanmean(y[max(0, idx - 5):idx + 5])),
            pre_e=float(np.nanmean(cur_e[pre])), pre_n=float(np.nanmean(cur_n[pre])),
            post_e=float(np.nanmean(cur_e[post])), post_n=float(np.nanmean(cur_n[post])),
            dirn=_tack_direction(df, idx),
        ))
    return rows, (cur_e, cur_n), (x, y)


def _struct_step(df, model, params, windows):
    """Antisymmetric (tack-direction-correlated) component of the per-tack
    current jump = the structural artifact magnitude (kt)."""
    cur_e, cur_n, _ = ca.estimate_current(df, model, params)
    jp, dr = [], []
    for idx, pre, post in windows:
        jp.append([float(np.nanmean(cur_e[post]) - np.nanmean(cur_e[pre])),
                   float(np.nanmean(cur_n[post]) - np.nanmean(cur_n[pre]))])
        dr.append(_tack_direction(df, idx))
    jp = np.array(jp); dr = np.array(dr)
    if not (dr > 0).any() or not (dr < 0).any():
        return 1e9
    struct = 0.5 * (jp[dr > 0].mean(0) - jp[dr < 0].mean(0))
    return float(np.hypot(*struct))


def _apply_heading(df, dpsi):
    if dpsi == 0.0:
        return df
    d = df.copy()
    d['hdg_true'] = df['hdg_true'].to_numpy(float) + dpsi
    return d


def _fit_session(df, model, guard_s, win_s, fit_heading):
    """Return (df_corrected, fit, windows, dpsi). When ``fit_heading`` and the
    model carries leeway, jointly fit a constant heading offset that minimises
    the antisymmetric structural step, then refit the leeway on the corrected
    heading. Tacks/windows are recomputed on the corrected heading."""
    dpsi = 0.0
    if fit_heading and model != 'none':
        from scipy.optimize import minimize

        def obj(p):
            dd = _apply_heading(df, p[0])
            w = ca.tack_windows(dd, ca.detect_tacks(dd), guard_s=guard_s, win_s=win_s)
            if not w:
                return 1e9
            f = _fit(dd, model, w)
            return _struct_step(dd, model, f['params_tuple'], w) ** 2

        r = minimize(obj, x0=[0.0], method='Nelder-Mead',
                     options={'xatol': 1e-3, 'fatol': 1e-9, 'maxiter': 2000})
        dpsi = float(r.x[0])
    dd = _apply_heading(df, dpsi)
    windows = ca.tack_windows(dd, ca.detect_tacks(dd), guard_s=guard_s, win_s=win_s)
    fit = _fit(dd, model, windows)
    return dd, fit, windows, dpsi


def make_figure(df, models, out_path, title, guard_s=15.0, win_s=40.0,
                fit_heading=False):
    base_windows = ca.tack_windows(df, ca.detect_tacks(df),
                                   guard_s=guard_s, win_s=win_s)
    if not base_windows:
        raise SystemExit("no usable tack windows in this session")

    nrows = len(models)
    fig, axes = plt.subplots(nrows, 3, figsize=(20, 6.2 * nrows), squeeze=False)

    # consistent arrow scale across panels: metres-per-knot for the map
    course_span = None
    metrics = {}

    for r, model in enumerate(models):
        df_m, fit, windows, dpsi = _fit_session(df, model, guard_s, win_s, fit_heading)
        t = (df_m['unixtime'].to_numpy(float) - df_m['unixtime'].to_numpy(float)[0]) / 60.0
        rows, (cur_e, cur_n), (x, y) = _window_means(df_m, fit, windows)

        # ---- A) MAP -----------------------------------------------------
        axm = axes[r][0]
        axm.plot(x, y, color='0.78', lw=0.8, zorder=1)
        if course_span is None:
            course_span = max(np.nanmax(x) - np.nanmin(x), np.nanmax(y) - np.nanmin(y))
        # 1 kt -> ~6% of the course span, so arrows are visible but local
        kt2m = 0.06 * course_span
        for rr in rows:
            axm.annotate('', xy=(rr['xt'] + rr['pre_e'] * kt2m, rr['yt'] + rr['pre_n'] * kt2m),
                         xytext=(rr['xt'], rr['yt']),
                         arrowprops=dict(arrowstyle='->', color='tab:blue', lw=1.6), zorder=3)
            axm.annotate('', xy=(rr['xt'] + rr['post_e'] * kt2m, rr['yt'] + rr['post_n'] * kt2m),
                         xytext=(rr['xt'], rr['yt']),
                         arrowprops=dict(arrowstyle='->', color='tab:red', lw=1.6), zorder=3)
            axm.plot(rr['xt'], rr['yt'], 'k.', ms=4, zorder=4)
        axm.set_aspect('equal')
        _hdg = f"  dψ={dpsi:+.2f}°" if fit_heading and model != 'none' else ""
        axm.set_title(f"[{model}] track + per-tack current  "
                      f"(blue=before, red=after)\n"
                      f"K={fit['params'].get('K', 0):.2f}{_hdg}  "
                      f"RMS jump={fit['rms_kt']:.3f} kt  n={fit['n_tacks']}")
        axm.set_xlabel('east (m)'); axm.set_ylabel('north (m)')
        # scale bar: 0.5 kt
        x0 = np.nanmin(x); y0 = np.nanmin(y)
        axm.annotate('', xy=(x0 + 0.5 * kt2m, y0), xytext=(x0, y0),
                     arrowprops=dict(arrowstyle='->', color='k', lw=2))
        axm.text(x0, y0 - 0.03 * course_span, '0.5 kt', fontsize=9)

        # ---- structural decomposition of the per-tack jump --------------
        # jump = post - pre. The part that FLIPS sign with tack direction is the
        # structural artifact (water can't know which way you tacked); the part
        # COMMON to both directions is just net current drift between windows.
        jp = np.array([[rr['post_e'] - rr['pre_e'], rr['post_n'] - rr['pre_n']]
                       for rr in rows])
        dirn = np.array([rr['dirn'] for rr in rows])
        mean_ps = jp[dirn > 0].mean(0) if (dirn > 0).any() else np.zeros(2)
        mean_sp = jp[dirn < 0].mean(0) if (dirn < 0).any() else np.zeros(2)
        struct = 0.5 * (mean_ps - mean_sp)   # antisymmetric = tack-induced step
        drift = 0.5 * (mean_ps + mean_sp)    # symmetric    = background drift
        struct_mag = float(np.hypot(*struct))
        axis = struct / (struct_mag + 1e-9)  # data-derived worst-case axis
        metrics[model] = dict(K=fit['params'].get('K', 0.0), rms_jump=fit['rms_kt'],
                              struct_step=struct_mag, drift=float(np.hypot(*drift)),
                              dpsi=dpsi, n=fit['n_tacks'])

        # ---- B) TACK-ALIGNED OVERLAY (does the tack inject a step?) ------
        # For each tack, current in +/-150 s, pre-subtracted so every trace sits
        # at 0 before the tack, then projected onto the structural axis. A sound
        # triangle => no systematic step at t=0; an artifact => the two tack
        # directions separate at t=0 AND stay separated (persistent, not a swing).
        axt = axes[r][1]
        H = int(round(150 * ca._FS))
        tr = np.arange(-H, H + 1) / ca._FS
        n = len(df_m)
        means = {1: [], -1: []}
        for rr in rows:
            i = rr['idx']
            if i - H < 0 or i + H + 1 > n:
                continue
            seg_e = cur_e[i - H:i + H + 1] - rr['pre_e']
            seg_n = cur_n[i - H:i + H + 1] - rr['pre_n']
            proj = seg_e * axis[0] + seg_n * axis[1]
            col = 'tab:orange' if rr['dirn'] > 0 else 'tab:cyan'
            axt.plot(tr, proj, color=col, lw=0.4, alpha=0.25)
            means[rr['dirn']].append(proj)
        for sgn, col, lbl in ((1, 'tab:orange', 'port->stbd'), (-1, 'tab:cyan', 'stbd->port')):
            if means[sgn]:
                m = np.nanmean(np.vstack(means[sgn]), axis=0)
                axt.plot(tr, m, color=col, lw=2.8, label=f'{lbl} mean ({len(means[sgn])})')
        axt.axvline(0, color='0.4', lw=1.0)
        axt.axhline(0, color='0.6', lw=0.6)
        axt.set_title(f"[{model}] tack-aligned current on structural axis "
                      f"(pre-subtracted)\nstep at t=0 that persists = artifact; "
                      f"struct step={struct_mag:.3f} kt")
        axt.set_xlabel('seconds relative to tack'); axt.set_ylabel('Δcurrent · axis (kt)')
        axt.set_ylim(-1.0, 1.0)
        axt.legend(loc='upper right', fontsize=8)

        # ---- C) JUMP VECTORS BY TACK DIRECTION -------------------------
        axj = axes[r][2]
        for rr in rows:
            de = rr['post_e'] - rr['pre_e']
            dn = rr['post_n'] - rr['pre_n']
            col = 'tab:orange' if rr['dirn'] > 0 else 'tab:cyan'
            axj.annotate('', xy=(de, dn), xytext=(0, 0),
                         arrowprops=dict(arrowstyle='->', color=col, lw=1.3, alpha=0.8))
        # group-mean jump per direction — the structural tell
        for sgn, col, lbl, mvec in ((1, 'tab:orange', 'port->stbd', mean_ps),
                                    (-1, 'tab:cyan', 'stbd->port', mean_sp)):
            if (dirn == sgn).any():
                axj.annotate('', xy=(mvec[0], mvec[1]), xytext=(0, 0),
                             arrowprops=dict(arrowstyle='-|>', color=col, lw=3.5))
                axj.plot([], [], color=col, lw=3, label=f'{lbl} mean ({int((dirn==sgn).sum())})')
        axj.axhline(0, color='0.7', lw=0.6); axj.axvline(0, color='0.7', lw=0.6)
        axj.set_aspect('equal')
        lim = 1.0
        axj.set_xlim(-lim, lim); axj.set_ylim(-lim, lim)
        axj.set_title(f"[{model}] per-tack JUMP (post-pre)\n"
                      f"struct(antisym)={struct_mag:.3f} kt  drift(sym)={np.hypot(*drift):.3f} kt")
        axj.set_xlabel('Δcur E (kt)'); axj.set_ylabel('Δcur N (kt)')
        axj.legend(loc='upper right', fontsize=8)

    fig.suptitle(title, fontsize=14)
    fig.tight_layout(rect=[0, 0, 1, 0.99])
    fig.savefig(out_path, dpi=120)
    plt.close(fig)
    return out_path, metrics


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--log', default=None, help='parquet basename (no .parquet)')
    ap.add_argument('--models', nargs='+', default=['none', 'classic'])
    ap.add_argument('--calibration', default='display', choices=['display', 'global'])
    ap.add_argument('--out', default=str(Path(__file__).with_name('CurrentOut')))
    ap.add_argument('--guard-s', type=float, default=15.0,
                    help='seconds skipped over the turn itself (each side)')
    ap.add_argument('--win-s', type=float, default=40.0,
                    help='length of the steady pre/post window in seconds')
    ap.add_argument('--fit-heading', action='store_true',
                    help='also jointly fit a per-session heading offset that '
                         'minimises the antisymmetric structural step')
    args = ap.parse_args(argv)

    logs_dir = ca.default_logs_dir()
    name = args.log
    if name is None:
        cands = sorted(Path(logs_dir).glob('2026-*.parquet'))
        if not cands:
            cands = sorted(Path(logs_dir).glob('*.parquet'))
        name = cands[-1].name
    if not name.endswith('.parquet'):
        name = name + '.parquet'
    stem = name[:-len('.parquet')]
    print(f"session: {stem}  (calibration={args.calibration})")

    df = ca.load_calibrated_log(name, path=logs_dir, calibration=args.calibration)
    os.makedirs(args.out, exist_ok=True)
    tag = f"_w{int(args.win_s)}" if args.win_s != 40.0 else ""
    tag += "_hdg" if args.fit_heading else ""
    out_path = os.path.join(args.out, f"tack_current_{stem.replace('T', '_')}{tag}.png")
    _, metrics = make_figure(
        df, args.models, out_path,
        title=f"Cross-tack current continuity — {stem} "
              f"(cal={args.calibration}, guard={args.guard_s:g}s, win={args.win_s:g}s"
              f"{', +heading fit' if args.fit_heading else ''})",
        guard_s=args.guard_s, win_s=args.win_s, fit_heading=args.fit_heading)
    print(f"\n{'model':9s} {'K':>7s} {'dpsi':>6s} {'n':>4s} {'RMS_jump':>9s} "
          f"{'struct(antisym)':>16s} {'drift(sym)':>11s}")
    for m in args.models:
        mm = metrics[m]
        print(f"{m:9s} {mm['K']:7.2f} {mm['dpsi']:+6.2f} {mm['n']:4d} {mm['rms_jump']:9.3f} "
              f"{mm['struct_step']:16.3f} {mm['drift']:11.3f}")
    print(f"\n[struct(antisym)] = the tack-direction-correlated step = the "
          f"structural artifact (kt).\n[drift(sym)] = background current change "
          f"common to both tack directions (not a tack artifact).")
    print(f"wrote {out_path}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
