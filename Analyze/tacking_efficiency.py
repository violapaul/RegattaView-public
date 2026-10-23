"""Tacking-efficiency viz — step 1: pre/post-tack course line fits.

For every modern parquet we detect upwind tacks (reusing
``current_analysis.detect_tacks``), keep the clean **port -> starboard** ones
where the boat held a steady heading both before and after, fit a straight line
to the boat's ground track on each side (the curved "in-tack" section in between
is excluded), and draw:

  fig A  one representative tack: track colored pre / in-tack / post, with the
         two linear course fits and their intersection.
  fig B  the 10 best port->starboard tacks aligned on the pre-tack course angle
         and on the pre/post intersection point, overlaid.

Exclusion zone (the curved turn we don't fit) is, per the working hypothesis,
20 s before the detected tack instant through 40 s after. The pre fit uses a
steady ``--fit-s`` window ending at t-20 s; the post fit uses one starting at
t+40 s.

Course == ground track (lat/lon positions in a local meters frame), so current
and leeway are included by construction.

Run (from repo root or the worktree):
  conda run --no-capture-output -n sailing \
      python Analyze/tacking_efficiency.py --n 10
Outputs land in Analyze/Plots/ (gitignored).
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
from Analyze.race_geometry import offset_en

_FS = ca._FS  # 10 Hz parquet rate

# --- window geometry (seconds) ------------------------------------------------
PRE_GUARD_S = 10.0   # exclude this much *before* the tack instant (curved entry)
POST_GUARD_S = 20.0  # exclude this much *after* (curved exit + initial settle)
FIT_S = 25.0         # length of the pre-tack straight-line fit window
POST_FIT_S = 80.0    # length of the post-tack fit window (out to +100 s settled)
EVAL_T = 35.0        # measure windward loss directly at this many s after the tack
EVAL_TAUS = (30.0, 35.0, 40.0, 60.0)  # climb sampled at all of these (fixed-bar
                     # loss at 35/60 s + the 30/40 s jackknife in --metric noise)
RAY_MAX_M = 250.0    # cap fitted-course rays in the aligned overlay (display only;
                     # sized to show the full ~80 s / ~250 m post-tack leg)

# --- quality gates ------------------------------------------------------------
STEADY_HDG_STD_MAX = 6.0   # deg; circular std of hdg_true within a fit window
SAILING_MIN_KT = 3.0       # both windows must be genuinely sailing upwind


_LEEWAY_MODEL = None


def _leeway_model():
    """Cached global leeway model dict (classic K) from leeway_model.json."""
    global _LEEWAY_MODEL
    if _LEEWAY_MODEL is None:
        _LEEWAY_MODEL = ca.load_leeway_model() or {}
    return _LEEWAY_MODEL


def water_track(df):
    """Integrate speed-through-water along (heading + leeway) to get the boat's
    track *relative to the water* (meters, arbitrary origin).

    This removes tidal current: where the ground track (GPS) is bent by the
    stream, the water track follows the heading the boat actually steered (plus
    leeway). Calibration is already baked into the 'display' columns, and the
    global leeway K was fit at that same calibration, so the two are consistent.
    """
    spd_ms = df['spd_kt'].to_numpy(float) / ca.MS_TO_KT
    hdg = df['hdg_true'].to_numpy(float)
    m = _leeway_model().get('leeway', {})
    if m.get('model') == 'classic' and 'K' in m:
        heel = df['heel'].to_numpy(float)
        aws = df['aws_kt'].to_numpy(float)
        lam = ca.leeway_angle('classic', (float(m['K']),), spd_ms * ca.MS_TO_KT,
                              heel, aws)
    else:
        lam = np.zeros_like(spd_ms)
    wb = np.radians(hdg + lam)               # water-track bearing
    ve, vn = spd_ms * np.sin(wb), spd_ms * np.cos(wb)
    dt = 1.0 / _FS
    return np.nancumsum(ve) * dt, np.nancumsum(vn) * dt


def _circmean(deg):
    """Circular mean (deg) of an angle sample."""
    r = np.radians(np.asarray(deg, float))
    return float(np.degrees(np.arctan2(np.nanmean(np.sin(r)),
                                       np.nanmean(np.cos(r)))))


def circ_std_deg(deg):
    """Circular standard deviation (deg) of an angle sample."""
    r = np.radians(np.asarray(deg, float))
    c, s = np.nanmean(np.cos(r)), np.nanmean(np.sin(r))
    R = np.hypot(c, s)
    if R <= 0:
        return np.inf
    return float(np.degrees(np.sqrt(-2.0 * np.log(R))))


def fit_line_pca(e, n):
    """Total-least-squares line through points (e, n).

    Returns (center[2], direction[2]) with ``direction`` a unit vector along the
    track's principal axis, oriented in time order (first->last sample).
    """
    e = np.asarray(e, float)
    n = np.asarray(n, float)
    P = np.column_stack([e, n])
    c = P.mean(axis=0)
    U, S, Vt = np.linalg.svd(P - c, full_matrices=False)
    v = Vt[0]
    v = v / np.linalg.norm(v)
    # orient along direction of travel (first sample -> last sample)
    travel = P[-1] - P[0]
    if np.dot(v, travel) < 0:
        v = -v
    return c, v


def line_intersection(c1, v1, c2, v2):
    """Intersection of infinite lines (c1 + t v1) and (c2 + s v2).

    Returns the 2-D point, or the midpoint of centers if near-parallel.
    """
    A = np.column_stack([v1, -v2])
    b = np.asarray(c2, float) - np.asarray(c1, float)
    if abs(np.linalg.det(A)) < 1e-9:
        return (np.asarray(c1, float) + np.asarray(c2, float)) / 2.0
    t, _ = np.linalg.solve(A, b)
    return np.asarray(c1, float) + t * np.asarray(v1, float)


def angle_between(v1, v2):
    """Unsigned angle (deg) between two 2-D vectors."""
    d = np.clip(np.dot(v1, v2) / (np.linalg.norm(v1) * np.linalg.norm(v2)), -1, 1)
    return float(np.degrees(np.arccos(d)))


def windward_metrics(e, n, ut, idx, pre, post, spd_kt, eval_t=EVAL_T):
    """Windward-distance-lost metric for one tack in one frame (ground or water).

    Windward distance ``w`` is the track projected onto the up-the-beat axis =
    bisector of the two steady courses, ``u = normalise(v_pre + v_post)``
    (robust, from the GPS tracks; twd_true is too biased by upwash to use as the
    axis). Loss is the classic tack loss: the windward distance you'd cover at
    your pre-tack VMG over ``eval_t`` seconds, minus the distance you **actually**
    cover from the tack instant to +eval_t::

        loss_m = vmg_pre * T  -  [ w(idx+T) - w(idx) ]      T = eval_t

    Anchored at the tack instant (so the pre-tack luff-up isn't double-counted),
    no line extrapolation. Stall (slow recovery) and footing (low pointing) both
    reduce the actual windward climb -> larger loss. Returns a dict (or None).
    """
    _, vp = fit_line_pca(e[pre], n[pre])
    _, vq = fit_line_pca(e[post], n[post])
    u = vp + vq
    nu = np.linalg.norm(u)
    if nu < 1e-9:
        return None
    u = u / nu
    w = e * u[0] + n * u[1]
    t = ut - ut[idx]
    sp, a = np.polyfit(t[pre], w[pre], 1)     # vmg_pre (m/s) on the beat axis
    if sp <= 0:
        return None

    def wavg(tau):
        m = (t >= tau - 2) & (t <= tau + 2)
        return float(np.nanmean(w[m])) if m.any() else np.nan
    w0 = wavg(0.0)
    if not np.isfinite(w0):
        return None
    # actual windward climb from the tack instant to each sample time; the
    # caller picks a bar (own vmg_pre here; fixed session/polar bar later)
    climb = {tau: wavg(tau) - w0 for tau in EVAL_TAUS}
    if not np.isfinite(climb.get(eval_t, np.nan)):
        return None
    loss = float(sp * eval_t - climb[eval_t])  # expected climb - actual climb
    # settled post VMG (for reporting) and settle time
    sq, _ = np.polyfit(t[post], w[post], 1)
    vmg = np.gradient(w) * _FS
    k = max(1, int(round(2.0 * _FS)))
    vmg_s = np.convolve(vmg, np.ones(k) / k, mode='same')
    win = (t >= -15) & (t <= 40)
    imin = int(np.argmin(np.where(win, vmg_s, np.inf)))
    settle = np.nan
    for i in range(imin, post.stop):
        if vmg_s[i] >= 0.95 * sp:
            settle = float(t[i])
            break

    span = slice(pre.start, post.stop)
    return dict(loss=loss, vmg_pre=float(sp), vmg_post=float(sq), climb=climb,
                settle=settle, min_spd=float(np.nanmin(spd_kt[win])),
                t=t[span], wdetr=(w - w0 - sp * t)[span],  # 0 at tack, =-loss at eval_t
                vmg_kt=(vmg_s * ca.MS_TO_KT)[span])   # windward velocity (kt)


def extract_tacks(name, logs_dir, frame='ground', tack_dir='p2s'):
    """Yield clean port->starboard tack records for one parquet.

    Each record is a dict with the local-meters track for the whole
    pre+in+post span plus the two fitted course lines.

    ``frame``: 'ground' fits the GPS track (current included); 'water'
    integrates speed-through-water along heading+leeway, removing tidal current
    so tacks compare on a current-free basis.
    """
    try:
        df = ca.load_calibrated_log(name, path=logs_dir)
    except Exception as e:  # noqa: BLE001
        print(f"  {name}: load failed ({e})", file=sys.stderr)
        return []
    if df is None or len(df) < _FS * 600:
        return []

    ut = df['unixtime'].to_numpy(float)
    lat = df['lat'].to_numpy(float)
    lon = df['lon'].to_numpy(float)
    hdg = df['hdg_true'].to_numpy(float)
    awa = df['awa'].to_numpy(float)
    spd = df['spd_kt'].to_numpy(float)
    tws = (df['tws_kt'].to_numpy(float) if 'tws_kt' in df.columns
           else np.full(len(df), np.nan))
    twd = (df['twd_true'].to_numpy(float) if 'twd_true' in df.columns
           else np.full(len(df), np.nan))
    rudder = (df['rudder'].to_numpy(float) if 'rudder' in df.columns
              else np.full(len(df), np.nan))
    heel = (df['heel'].to_numpy(float) if 'heel' in df.columns
            else np.full(len(df), np.nan))

    # both frames' absolute meters tracks (chosen one drives the plots; both
    # feed the windward-loss metric)
    eg, ng = offset_en(lat, lon, lat[0], lon[0])
    ew, nw = water_track(df)
    e_abs, n_abs = (ew, nw) if frame == 'water' else (eg, ng)

    tacks = ca.detect_tacks(df)
    n = len(df)
    pg = int(round(PRE_GUARD_S * _FS))
    qg = int(round(POST_GUARD_S * _FS))
    fw = int(round(FIT_S * _FS))
    fwq = int(round(POST_FIT_S * _FS))

    recs = []
    for j, idx in enumerate(sorted(tacks)):
        pre0, pre1 = idx - pg - fw, idx - pg      # steady pre window
        post0, post1 = idx + qg, idx + qg + fwq   # steady post window
        if pre0 < 0 or post1 > n:
            continue
        # don't let a fit window swallow a neighbouring maneuver
        prev_t = tacks[j - 1] if j > 0 else -10**9
        next_t = tacks[j + 1] if j < len(tacks) - 1 else 10**9
        if pre0 < prev_t + pg or post1 > next_t - pg:
            continue

        pre = slice(pre0, pre1)
        post = slice(post0, post1)

        # tack direction from AWA sign flip: p2s = wind port->stbd (awa -ve->+ve),
        # s2p = the reverse. Anything that doesn't flip sign isn't a tack.
        awa_pre = np.nanmean(awa[pre])
        awa_post = np.nanmean(awa[post])
        is_p2s = awa_pre < 0 < awa_post
        is_s2p = awa_pre > 0 > awa_post
        if not (is_p2s or is_s2p):
            continue
        direction = 'p2s' if is_p2s else 's2p'
        if tack_dir != 'both' and direction != tack_dir:
            continue

        # quality: steady heading + genuinely sailing on both sides
        if (circ_std_deg(hdg[pre]) > STEADY_HDG_STD_MAX or
                circ_std_deg(hdg[post]) > STEADY_HDG_STD_MAX):
            continue
        if (np.nanmean(spd[pre]) < SAILING_MIN_KT or
                np.nanmean(spd[post]) < SAILING_MIN_KT):
            continue

        # local meters frame, referenced to the tack instant's position
        e, n_m = e_abs - e_abs[idx], n_abs - n_abs[idx]

        c_pre, v_pre = fit_line_pca(e[pre], n_m[pre])
        c_post, v_post = fit_line_pca(e[post], n_m[post])

        # turn direction from the *track* (robust to AWA noise near head-to-wind):
        # p2s turns to port (cross>0, CCW); s2p turns to starboard (cross<0, CW).
        # Requiring the turn sense to match the AWA flip drops shift/noise cases.
        cross = v_pre[0] * v_post[1] - v_pre[1] * v_post[0]
        if (is_p2s and cross <= 0) or (is_s2p and cross >= 0):
            continue

        xpt = line_intersection(c_pre, v_pre, c_post, v_post)

        # windward-distance-lost metric, both frames (None if degenerate)
        mg = windward_metrics(eg, ng, ut, idx, pre, post, spd)
        mw = windward_metrics(ew, nw, ut, idx, pre, post, spd)
        if mg is None or mw is None:
            continue

        # full span we draw: start of pre window .. end of post window
        span = slice(pre0, post1)
        # steering profile: heading turned from the entry (pre) heading, flipped
        # so every tack reads as a positive turn (p2s and s2p overlay)
        entry_hdg = _circmean(hdg[pre])
        rel = ((hdg[span] - entry_hdg + 180) % 360) - 180
        turn_sign = 1.0 if (((np.nanmean(hdg[post]) - np.nanmean(hdg[pre])
                              + 180) % 360) - 180) > 0 else -1.0
        steer_ang = rel * turn_sign
        # actual steering input: rudder angle relative to the steady pre-tack
        # helm (removes weather-helm/offset), flipped to match the turn sign so
        # +ve = helm into the tack
        rudder_rel = (rudder[span] - np.nanmean(rudder[pre])) * turn_sign

        # --- per-tack scalars for the noise / paired analyses ---------------
        # in-tack window in the *span-relative* time base
        t_span = ut[span] - ut[idx]
        turn_m = (t_span >= -PRE_GUARD_S) & (t_span <= POST_GUARD_S)
        with np.errstate(all='ignore'):
            rudder_p90 = (float(np.nanpercentile(rudder_rel[turn_m], 90))
                          if np.isfinite(rudder_rel[turn_m]).any() else np.nan)
            rudder_work = (float(np.nansum(np.abs(rudder_rel[turn_m])) / _FS)
                           if np.isfinite(rudder_rel[turn_m]).any() else np.nan)
        # peak yaw (turn) rate through the maneuver — the mechanical confound
        # for roll rate (a faster turn compresses the heel flip); kept so the
        # paired analysis can test roll-rate with turn-rate controlled
        k1 = max(1, int(round(1.0 * _FS)))
        # steer_ang is computed below; derive yaw from raw hdg here (deg/s,
        # wrap-safe via the +-180 fold, 1 s smoothed)
        dh = (((np.diff(hdg[span], prepend=hdg[span][:1]) + 180) % 360) - 180)
        yw = np.convolve(np.nan_to_num(dh * _FS), np.ones(k1) / k1, 'same')
        yaw_peak = (float(np.nanmax(np.abs(yw[turn_m])))
                    if np.isfinite(yw[turn_m]).any() else np.nan)

        # over/under-shoot of the exit course: settled = mean turn over the
        # steady post window; +ve overshoot = bow swung past the settled exit
        # heading during the maneuver and had to come back up. Measured from
        # heading (not loss), so it's a technique variable safe to group by.
        post_m = t_span >= POST_GUARD_S
        swing_m = (t_span >= 0) & (t_span <= 45.0)
        if np.isfinite(steer_ang[post_m]).any() and np.isfinite(steer_ang[swing_m]).any():
            overshoot = float(np.nanmax(steer_ang[swing_m])
                              - np.nanmean(steer_ang[post_m]))
        else:
            overshoot = np.nan

        # roll through the turn: heel change from the steady pre-tack heel, and
        # peak roll rate (1 s smoothed) — the crew-coachable channel
        heel_rel = heel[span] - np.nanmean(heel[pre])
        if np.isfinite(heel_rel[turn_m]).any():
            roll_range = float(np.nanmax(heel_rel[turn_m])
                               - np.nanmin(heel_rel[turn_m]))
            k1 = max(1, int(round(1.0 * _FS)))
            hr = np.convolve(np.nan_to_num(np.gradient(heel[span]) * _FS),
                             np.ones(k1) / k1, mode='same')
            roll_rate_max = float(np.nanmax(np.abs(hr[turn_m])))
        else:
            roll_range = roll_rate_max = np.nan
        # TWD change across the tack, signed so +ve = LIFT on the new tack.
        # p2s ends on starboard (hdg = twd - twa): a veer (dtwd>0) lets the boat
        # point closer to the old beat axis = lift; s2p mirrors.
        if np.isfinite(twd[pre]).any() and np.isfinite(twd[post]).any():
            dtwd = ((_circmean(twd[post]) - _circmean(twd[pre]) + 180) % 360) - 180
            shift_lift = dtwd if is_p2s else -dtwd
        else:
            dtwd = shift_lift = np.nan

        recs.append(dict(
            name=name, day=os.path.basename(name)[:10], idx=idx, direction=direction,
            t=float(ut[idx]),
            e=e[span], n=n_m[span],
            t_rel=ut[span] - ut[idx],
            e_pre=e[pre], n_pre=n_m[pre], e_post=e[post], n_post=n_m[post],
            c_pre=c_pre, v_pre=v_pre, c_post=c_post, v_post=v_post, xpt=xpt,
            tack_angle=angle_between(v_pre, v_post),
            hdg_std_pre=circ_std_deg(hdg[pre]),
            hdg_std_post=circ_std_deg(hdg[post]),
            spd_pre=float(np.nanmean(spd[pre])),
            spd_post=float(np.nanmean(spd[post])),
            tws_pre=float(np.nanmean(tws[pre])),
            awa_pre=float(np.nanmean(awa[pre])),
            awa_post=float(np.nanmean(awa[post])),
            hdg_change=float(((np.nanmean(hdg[post]) - np.nanmean(hdg[pre])
                               + 180) % 360) - 180),
            # windward loss (m); +ve == lost ground to windward in the tack
            loss_ground=mg['loss'], loss_water=mw['loss'],
            climb_ground=mg['climb'], climb_water=mw['climb'],
            vmg_pre=mw['vmg_pre'], vmg_post=mw['vmg_post'],
            vmg_pre_ground=mg['vmg_pre'],
            settle_s=mw['settle'], min_spd=mw['min_spd'],
            rudder_p90=rudder_p90, rudder_work=rudder_work,
            roll_range=roll_range, roll_rate_max=roll_rate_max,
            yaw_peak=yaw_peak, overshoot=overshoot,
            dtwd=dtwd, shift_lift=shift_lift,
            curve_t=mw['t'], curve_w=mw['wdetr'],   # water, pre-line-detrended
            steer_ang=steer_ang,                    # heading from entry (flipped)
            rudder_curve=rudder_rel,                # rudder angle (deg, flipped)
            spd_curve=spd[span],                    # boat speed over span (kt)
            vmg_curve=mw['vmg_kt'],                 # windward velocity (kt)
        ))
    return recs


def add_fixed_bar_losses(recs):
    """Attach fixed-target-VMG losses to every record (in place).

    The own-bar loss (note §3.4) scores a tack against its *own* pre-tack VMG,
    so a slow entry lowers the bar and flatters the tack (corr(spd_pre, loss)
    = +0.43). Here the bar is independent of the tack itself:

      * sess  — session median ``vmg_pre`` over that session's clean tacks
                (needs >=3 in the session, else the global median of the
                analyzed set). Per frame: the ground bar then includes the
                session-median current ("vs the mark on that day").
      * polar — upwind VMG-max at the tack's TWS from the J/105 polar
                (water frame only; absolute, condition-independent).

    Adds ``loss_fix_{sess,polar}_{frame}_{35,60}`` keys (meters). Call AFTER
    any TWS-band filtering so the medians describe the analyzed population.
    """
    if not recs:
        return
    for frame in ('water', 'ground'):
        vkey = 'vmg_pre' if frame == 'water' else 'vmg_pre_ground'
        glob_med = float(np.nanmedian([r[vkey] for r in recs]))
        by_sess = {}
        for r in recs:
            by_sess.setdefault(r['name'], []).append(r[vkey])
        sess_med = {s: (float(np.nanmedian(v)) if len(v) >= 3 else glob_med)
                    for s, v in by_sess.items()}
        for r in recs:
            bar = sess_med[r['name']]
            r[f'bar_sess_{frame}'] = bar          # m/s, for ad-hoc taus later
            for T in (35.0, 60.0):
                c = r[f'climb_{frame}'].get(T, np.nan)
                r[f'loss_fix_sess_{frame}_{int(T)}'] = (
                    float(bar * T - c) if np.isfinite(c) else np.nan)
    try:
        from Analyze import polar
        bar_of = lambda tws: (float(polar.upwind_vmgmax_kt(tws)) / ca.MS_TO_KT
                              if np.isfinite(tws) else np.nan)
    except Exception as e:  # noqa: BLE001
        print(f"polar bar unavailable ({e}); loss_fix_polar_* = NaN",
              file=sys.stderr)
        bar_of = lambda tws: np.nan
    for r in recs:
        bar = bar_of(r['tws_pre'])
        for T in (35.0, 60.0):
            c = r['climb_water'].get(T, np.nan)
            r[f'loss_fix_polar_water_{int(T)}'] = (
                float(bar * T - c)
                if np.isfinite(c) and np.isfinite(bar) else np.nan)


# ---------------------------------------------------------------------------
# --metric noise: validate the metric + budget the per-tack noise
# ---------------------------------------------------------------------------

BEAT_GAP_S = 600.0   # consecutive clean tacks closer than this = same beat


def _fit_residual_params(recs, n_max=400):
    """AR(1) GPS-noise parameters (sigma_m, phi at 10 Hz) from real data.

    Residual = perpendicular distance of each pre-window sample to its own TLS
    course line. Smoothed/filtered GPS gives strongly correlated residuals;
    the synthetic injector must reproduce that correlation or it will be
    wildly optimistic (white noise averages away in the +/-2 s wavg).
    """
    sig, phi = [], []
    for r in recs[:n_max]:
        e = np.asarray(r['e_pre'], float)
        n = np.asarray(r['n_pre'], float)
        c, v = r['c_pre'], r['v_pre']
        perp = (e - c[0]) * (-v[1]) + (n - c[1]) * v[0]
        if len(perp) < 20 or not np.isfinite(perp).all():
            continue
        perp = perp - perp.mean()
        s = float(perp.std())
        if s <= 1e-6:
            continue
        sig.append(s)
        phi.append(float(np.corrcoef(perp[:-1], perp[1:])[0, 1]))
    if not sig:
        return 0.5, 0.95
    return float(np.median(sig)), float(np.clip(np.median(phi), 0.0, 0.999))


def _ar1(rng, n, sigma, phi):
    x = np.empty(n)
    x[0] = rng.normal(0.0, sigma)
    innov = sigma * np.sqrt(max(1e-12, 1.0 - phi * phi))
    x[1:] = rng.normal(0.0, innov, n - 1)
    for i in range(1, n):
        x[i] += phi * x[i - 1]
    return x


def _smoothstep(u):
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


def synth_tack(rng, loss_m, sigma, phi, vbar=2.1, shift_deg=0.0,
               turn_s=10.0, settle_s=25.0):
    """One synthetic tack with KNOWN injected loss, through the real metric.

    Courses are +/-45 deg off the beat axis at constant axis speed ``vbar``
    (m/s). The loss ramps in (smoothstep) over ``settle_s`` after the tack, so
    truth at +35 s is exactly ``loss_m``. ``shift_deg`` rotates the *post*
    course toward the axis (a lift on the new tack). AR(1) noise on both axes.
    Returns (own-bar loss, fixed-bar loss at the true vbar, climb dict).
    """
    t = np.arange(-45.0, 100.0, 1.0 / _FS)
    idx = int(np.argmin(np.abs(t)))
    r = _smoothstep(t / turn_s)                  # pre->post course blend
    sp = _smoothstep(t / settle_s)               # loss ramp (0 -> 1)
    dr = np.radians(shift_deg)
    wdot = vbar * (1.0 + r * dr)                 # axis speed (lift adds)
    xdot = vbar * (1.0 - 2.0 * r + r * dr)       # cross: +vbar -> -vbar(1-dr)
    dt = 1.0 / _FS
    w = np.cumsum(wdot) * dt - loss_m * sp
    x = np.cumsum(xdot) * dt
    e = x + _ar1(rng, len(t), sigma, phi)
    n = w + _ar1(rng, len(t), sigma, phi)
    spd_kt = (vbar * np.sqrt(2.0)) * ca.MS_TO_KT * (1.0 - 0.35 * np.exp(
        -((t - turn_s / 2) / (turn_s / 2)) ** 2))
    pg, qg = int(PRE_GUARD_S * _FS), int(POST_GUARD_S * _FS)
    fw, fwq = int(FIT_S * _FS), int(POST_FIT_S * _FS)
    pre = slice(idx - pg - fw, idx - pg)
    post = slice(idx + qg, idx + qg + fwq)
    m = windward_metrics(e, n, t, idx, pre, post, spd_kt)
    if m is None:
        return np.nan, np.nan, {}
    loss_fix = vbar * EVAL_T - m['climb'][EVAL_T]
    return m['loss'], float(loss_fix), m['climb']


def run_synthetic(recs, loa, n_trials=200, seed=20260609):
    """Gate check: the metric must recover known injected losses unbiased
    (<0.05 BL) and we measure its GPS-noise-only sigma. Then the response to a
    pure post-tack wind shift, in BL/deg."""
    sigma, phi = _fit_residual_params(recs)
    print(f"\n=== Synthetic injection (metric validation) ===", file=sys.stderr)
    print(f"GPS noise from real pre-window fits: sigma={sigma:.2f} m, "
          f"AR1 phi={phi:.3f} (10 Hz)", file=sys.stderr)
    rng = np.random.default_rng(seed)
    ok = True
    print(f"{'inj(BL)':>8} {'own bias':>9} {'own sd':>7} {'fix bias':>9} "
          f"{'fix sd':>7}   (BL, n={n_trials})", file=sys.stderr)
    for L_bl in (0.0, 0.5, 1.0, 2.0):
        own, fix = [], []
        for _ in range(n_trials):
            o, f, _c = synth_tack(rng, L_bl * loa, sigma, phi)
            own.append(o / loa)
            fix.append(f / loa)
        own, fix = np.array(own), np.array(fix)
        bo, bf = np.nanmean(own) - L_bl, np.nanmean(fix) - L_bl
        print(f"{L_bl:8.1f} {bo:+9.3f} {np.nanstd(own):7.3f} "
              f"{bf:+9.3f} {np.nanstd(fix):7.3f}", file=sys.stderr)
        if abs(bf) > 0.05:
            ok = False
    sd_meas = np.nanstd(fix)   # GPS-only sigma at the last (2 BL) level
    # response to a pure wind shift on the new tack (no extra loss injected)
    resp = []
    for d in (-5.0, 5.0):
        v = [synth_tack(rng, 1.0 * loa, sigma, phi, shift_deg=d)[1] / loa
             for _ in range(n_trials)]
        resp.append(np.nanmean(v))
    bl_per_deg = (resp[1] - resp[0]) / 10.0
    print(f"shift response: {bl_per_deg:+.3f} BL/deg lift "
          f"(physics est ~ -0.12 BL/deg before bisector absorption)",
          file=sys.stderr)
    print(("GATE PASS: fixed-bar metric unbiased within 0.05 BL" if ok else
           "GATE FAIL: fixed-bar metric biased > 0.05 BL — fix before "
           "interpreting corpus numbers"), file=sys.stderr)
    return ok, sd_meas, bl_per_deg


def report_noise(recs, loa):
    """Per-tack noise budget for the fixed-bar loss (water frame, 35 s)."""
    ok, sd_gps, bl_deg_synth = run_synthetic(recs, loa)

    lf = np.array([r['loss_fix_sess_water_35'] for r in recs]) / loa
    lg = np.array([r['loss_fix_sess_ground_35'] for r in recs]) / loa
    own = np.array([r['loss_water'] for r in recs]) / loa
    sp = np.array([r['spd_pre'] for r in recs])
    shift = np.array([r['shift_lift'] for r in recs])
    m = np.isfinite(lf)

    print(f"\n=== Fixed-bar metric on the corpus (n={m.sum()}) ===",
          file=sys.stderr)
    print(f"own-bar  loss: mean {np.nanmean(own):.2f} BL  sd {np.nanstd(own):.2f}",
          file=sys.stderr)
    print(f"fixed-bar loss: mean {np.nanmean(lf):.2f} BL  sd {np.nanstd(lf):.2f}",
          file=sys.stderr)
    c_own = np.corrcoef(sp[m & np.isfinite(own)], own[m & np.isfinite(own)])[0, 1]
    c_fix = np.corrcoef(sp[m], lf[m])[0, 1]
    print(f"corr(spd_pre, loss): own-bar {c_own:+.2f}  ->  fixed-bar {c_fix:+.2f} "
          f"(bias check; was +0.43 in round 1)", file=sys.stderr)

    # water vs ground: same tack, two measurements
    d = lg - lf
    dm = np.isfinite(d)
    print(f"\n=== Water vs ground (same tack, two frames) ===", file=sys.stderr)
    print(f"ground - water: mean {np.nanmean(d[dm]):+.2f} BL (current tax), "
          f"sd {np.nanstd(d[dm]):.2f} BL -> per-frame sigma <= "
          f"{np.nanstd(d[dm])/np.sqrt(2):.2f} BL", file=sys.stderr)

    # EVAL_T jackknife: loss_fix at 30/35/40 s should agree once settled
    jk = []
    for r in recs:
        bar = r.get('bar_sess_water', np.nan)
        ls = [bar * T - r['climb_water'].get(T, np.nan) for T in (30.0, 35.0, 40.0)]
        ls = np.array(ls) / loa
        if np.isfinite(ls).all():
            jk.append(np.std(ls))
    jk = np.array(jk)
    print(f"\n=== EVAL_T jackknife (30/35/40 s) ===", file=sys.stderr)
    print(f"per-tack sd across taus: median {np.median(jk):.2f} BL, "
          f"90th pct {np.percentile(jk, 90):.2f} BL", file=sys.stderr)

    # wind-shift regression
    sm = m & np.isfinite(shift)
    dirs = np.array([r['direction'] for r in recs])
    print(f"\n=== Wind shift across the tack (n={sm.sum()}) ===", file=sys.stderr)
    mu_p = np.nanmean(shift[sm & (dirs == 'p2s')])
    mu_s = np.nanmean(shift[sm & (dirs == 's2p')])
    print(f"shift_lift: sd {np.nanstd(shift[sm]):.1f} deg; mean p2s {mu_p:+.1f} "
          f"/ s2p {mu_s:+.1f} deg (if these diverge, twd upwash bias is NOT "
          f"tack-symmetric -> treat dtwd as exclusion gate, not covariate)",
          file=sys.stderr)
    if sm.sum() > 20:
        b, a = np.polyfit(shift[sm], lf[sm], 1)
        r_ = np.corrcoef(shift[sm], lf[sm])[0, 1]
        expl = abs(b) * np.nanstd(shift[sm])
        print(f"loss_fix vs shift_lift: slope {b:+.3f} BL/deg (r={r_:+.2f}); "
              f"synthetic predicts {bl_deg_synth:+.3f} BL/deg", file=sys.stderr)
        print(f"shift-explained sigma: {expl:.2f} BL", file=sys.stderr)
    else:
        b, expl = np.nan, np.nan

    # budget
    tot = np.nanstd(lf[m])
    parts = dict(gps_model=sd_gps, shift=expl)
    known = np.nansum([v * v for v in parts.values()])
    resid = np.sqrt(max(0.0, tot * tot - known))
    print(f"\n=== Noise budget: fixed-bar water loss @35 s ===", file=sys.stderr)
    print(f"total sd          {tot:5.2f} BL", file=sys.stderr)
    print(f"  GPS/model (synthetic) {sd_gps:5.2f} BL", file=sys.stderr)
    print(f"  wind shift (regress)  {expl:5.2f} BL", file=sys.stderr)
    print(f"  residual (conditions+technique+...) {resid:5.2f} BL",
          file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# --metric paired: within-beat pairing + mixed-effects re-test
# ---------------------------------------------------------------------------

def build_tack_table(recs, loa):
    """One row per clean tack, losses in BL, with session/beat grouping ids."""
    import pandas as pd
    rows = []
    for r in recs:
        retain = (r['min_spd'] / r['spd_pre']) if r['spd_pre'] else np.nan
        rows.append(dict(
            session=r['name'], day=r['day'], t=r['t'], direction=r['direction'],
            loss_own=r['loss_water'] / loa,
            loss_fix=r['loss_fix_sess_water_35'] / loa,
            loss_fix_g=r['loss_fix_sess_ground_35'] / loa,
            loss_fix_polar=r.get('loss_fix_polar_water_35', np.nan) / loa,
            loss_fix_60=r['loss_fix_sess_water_60'] / loa,
            spd_pre=r['spd_pre'], tws=r['tws_pre'], retain=retain,
            shift_lift=r['shift_lift'], tack_angle=r['tack_angle'],
            rudder_p90=r['rudder_p90'], rudder_work=r['rudder_work'],
            roll_range=r['roll_range'], roll_rate_max=r['roll_rate_max'],
            yaw_peak=r.get('yaw_peak', np.nan),
            overshoot=r.get('overshoot', np.nan),
            settle_s=r['settle_s'],
        ))
    df = pd.DataFrame(rows).sort_values(['session', 't']).reset_index(drop=True)
    # shift-adjusted loss: take out the (measurable) wind-shift term so the
    # paired contrasts compare technique, not luck of the shift
    sm = df['loss_fix'].notna() & df['shift_lift'].notna()
    if sm.sum() > 20:
        b, a = np.polyfit(df.loc[sm, 'shift_lift'], df.loc[sm, 'loss_fix'], 1)
        df['loss_adj'] = df['loss_fix'] - b * (df['shift_lift']
                                               - df.loc[sm, 'shift_lift'].mean())
    else:
        df['loss_adj'] = df['loss_fix']
    beat, k, last = [], 0, None
    for s, tt in zip(df['session'], df['t']):
        if last is None or s != last[0] or (tt - last[1]) > BEAT_GAP_S:
            k += 1
        beat.append(k)
        last = (s, tt)
    df['beat'] = beat
    return df


def report_paired(recs, loa, out_dir):
    """Within-beat paired contrasts + mixed-effects model + MDE statement."""
    import pandas as pd
    df = build_tack_table(recs, loa)
    csv = os.path.join(out_dir, 'tack_table.csv')
    df.to_csv(csv, index=False)
    nb = df.groupby('beat').size()
    print(f"\n=== Tack table ===", file=sys.stderr)
    print(f"{len(df)} tacks, {df['session'].nunique()} sessions, "
          f"{df['beat'].nunique()} beats; beats with >=2 tacks: "
          f"{(nb >= 2).sum()}  -> {csv}", file=sys.stderr)

    # ---- mixed-effects: loss ~ technique + conditions, beat random effect.
    # Run for all loss variants: conclusions must agree across the 35/60 s
    # horizons, the session-median/polar bars, AND the ground frame.
    # Direction is a fixed effect: s2p tacks differ on both sides (loss AND
    # rudder/spd), and consecutive pairs mostly alternate direction, so
    # omitting it inflates the technique coefficients.
    preds = ['rudder_p90', 'spd_pre', 'shift_lift', 'tws', 'roll_rate_max']
    import statsmodels.formula.api as smf
    df['dir_s2p'] = (df['direction'] == 's2p').astype(float)
    print(f"\n=== MixedLM; coefs = BL per 1 sd of predictor "
          f"(dir_s2p = BL, raw) ===", file=sys.stderr)
    for outcome in ('loss_fix', 'loss_fix_60', 'loss_fix_polar', 'loss_fix_g'):
        dm = df.dropna(subset=[outcome] + preds).copy()
        if len(dm) < 40:
            print(f"  {outcome}: skipped ({len(dm)} complete rows)",
                  file=sys.stderr)
            continue
        for c in preds:
            dm[c + '_z'] = (dm[c] - dm[c].mean()) / dm[c].std()
        f = (outcome + ' ~ ' + ' + '.join(c + '_z' for c in preds)
             + ' + dir_s2p')
        for grp, gname in ((dm['beat'], 'beat'), (dm['session'], 'session')):
            try:
                res = smf.mixedlm(f, dm, groups=grp).fit(reml=True)
            except Exception as e:  # noqa: BLE001
                print(f"  {outcome}/{gname}: MixedLM failed: {e}",
                      file=sys.stderr)
                continue
            terms = '  '.join(
                f"{c}={res.params[c + '_z']:+.2f}(p={res.pvalues[c + '_z']:.3f})"
                for c in preds)
            terms += (f"  dir_s2p={res.params['dir_s2p']:+.2f}"
                      f"(p={res.pvalues['dir_s2p']:.3f})")
            print(f"  {outcome} [groups={gname}] (n={len(dm)}): {terms}",
                  file=sys.stderr)
            print(f"    group var {res.cov_re.iloc[0, 0]:.3f}  residual "
                  f"{res.scale:.3f}  (BL^2)", file=sys.stderr)

    # ---- within-beat consecutive pairs: conditions held constant ----
    pairs = []
    for _, g in df.groupby('beat'):
        g = g.sort_values('t')
        for i in range(len(g) - 1):
            a, b = g.iloc[i], g.iloc[i + 1]
            pairs.append(dict(dloss=b['loss_adj'] - a['loss_adj'],
                              dloss_raw=b['loss_fix'] - a['loss_fix'],
                              dloss_g=b['loss_fix_g'] - a['loss_fix_g'],
                              drud=b['rudder_p90'] - a['rudder_p90'],
                              droll=b['roll_rate_max'] - a['roll_rate_max'],
                              dspd=b['spd_pre'] - a['spd_pre'],
                              ddir=b['dir_s2p'] - a['dir_s2p'],
                              dyaw=b['yaw_peak'] - a['yaw_peak'],
                              dtws=b['tws'] - a['tws'],
                              dover=b['overshoot'] - a['overshoot'],
                              dshift=b['shift_lift'] - a['shift_lift']))
    pr = pd.DataFrame(pairs)
    pv = pr.dropna(subset=['dloss'])
    print(f"\n=== Within-beat consecutive pairs (n={len(pv)}; "
          f"loss = shift-adjusted fixed-bar) ===", file=sys.stderr)
    if len(pv) >= 10:
        sd_d = pv['dloss'].std()
        print(f"sd(delta loss) within beat: raw {pv['dloss_raw'].std():.2f} -> "
              f"shift-adjusted {sd_d:.2f} BL "
              f"-> per-tack within-beat sigma ~ {sd_d/np.sqrt(2):.2f} BL",
              file=sys.stderr)
        for key, lab in (('drud', 'rudder p90 (deg)'),
                         ('droll', 'roll rate (deg/s)'),
                         ('dspd', 'entry speed (kt)')):
            q = pv.dropna(subset=[key])
            if len(q) < 10:
                continue
            slope = (q['dloss'] * q[key]).sum() / (q[key] ** 2).sum()
            boot = []
            rng = np.random.default_rng(7)
            for _ in range(2000):
                s = q.iloc[rng.integers(0, len(q), len(q))]
                boot.append((s['dloss'] * s[key]).sum() / (s[key] ** 2).sum())
            lo, hi = np.percentile(boot, [2.5, 97.5])
            print(f"  dloss/d[{lab:18s}] = {slope:+.4f} BL/unit "
                  f"[{lo:+.4f}, {hi:+.4f}] (n={len(q)})", file=sys.stderr)
        # joint paired regression: the technique deltas are correlated with
        # each other AND with direction (consecutive tacks mostly alternate
        # p2s/s2p), so test them together with a ddir term (no intercept: a
        # zero delta in everything should predict zero delta in loss). Run in
        # both frames — a technique effect that vanishes on the ground track
        # is suspect (water-frame paddlewheel coupling).
        # plain-language sign test: of each back-to-back pair, did the
        # worse-technique tack (higher rudder peak / slower roll) lose more?
        # Countable evidence for readers who (rightly) distrust regressions.
        from scipy import stats as _st
        print("sign test (back-to-back pairs):", file=sys.stderr)
        for key, ths, bsign, lab in (('drud', (5.0, 8.0), +1.0, 'higher-rudder'),
                                     ('droll', (3.0,), -1.0, 'slower-roll'),
                                     ('dover', (10.0,), +1.0, 'bigger-overshoot')):
            for th in ths:
                q = pv.dropna(subset=[key])
                q = q[np.abs(q[key]) >= th]
                if not len(q):
                    continue
                k = int((np.sign(q['dloss']) == bsign * np.sign(q[key])).sum())
                pbin = _st.binomtest(k, len(q), 0.5).pvalue
                mu = (q.loc[bsign * q[key] > 0, 'dloss'].mean()
                      - q.loc[bsign * q[key] < 0, 'dloss'].mean())
                print(f"  |{key}|>={th:g}: {lab} tack lost more in "
                      f"{k}/{len(q)} = {100*k/len(q):.0f}% (binom p={pbin:.3f}); "
                      f"mean extra loss {abs(mu)/2:.2f} BL", file=sys.stderr)

        # roll rate vs yaw (turn) rate: the heel flip is mechanically coupled
        # to the turn, so report their correlation and re-fit with dyaw added
        # — if droll dies when dyaw enters, "roll fast" was just "turn fast".
        cy = df.dropna(subset=['roll_rate_max', 'yaw_peak'])
        if len(cy) > 30:
            print(f"corr(roll_rate_max, yaw_peak) = "
                  f"{np.corrcoef(cy['roll_rate_max'], cy['yaw_peak'])[0, 1]:+.2f}"
                  f"   corr(rudder_p90, yaw_peak) = "
                  f"{np.corrcoef(*cy.dropna(subset=['rudder_p90'])[['rudder_p90', 'yaw_peak']].to_numpy().T)[0, 1]:+.2f}",
                  file=sys.stderr)
        # pressure check: in more wind the boat turns on sails/heel and needs
        # less wheel — if gust/lull differences within a pair drive both
        # rudder and loss, the technique terms would die when dtws enters
        cd_ = pv.dropna(subset=['drud', 'droll', 'dtws'])
        if len(cd_) > 30:
            print(f"within-pair pressure coupling: corr(drud, dtws) = "
                  f"{np.corrcoef(cd_['drud'], cd_['dtws'])[0, 1]:+.2f}   "
                  f"corr(droll, dtws) = "
                  f"{np.corrcoef(cd_['droll'], cd_['dtws'])[0, 1]:+.2f}",
                  file=sys.stderr)
        base = ('rudder p90  (BL/deg)', 'roll rate   (BL/(deg/s))',
                'entry speed (BL/kt)', 'direction   (BL, s2p-p2s)')
        specs = (
            (['drud', 'droll', 'dspd', 'ddir'], base),
            (['drud', 'droll', 'dspd', 'ddir', 'dyaw', 'dtws'],
             base + ('yaw peak    (BL/(deg/s))', 'pressure    (BL/kt tws)')),
            (['drud', 'droll', 'dspd', 'ddir', 'dtws', 'dover'],
             base + ('pressure    (BL/kt tws)', 'overshoot   (BL/deg)')),
            # overshoot is measured against the SETTLED exit heading, so a
            # lift on the new tack both shrinks measured overshoot and
            # cheapens the tack -- dshift tests whether dover is a shift
            # proxy (matters most in the ground frame, where dloss_g is the
            # raw, not shift-adjusted, loss)
            (['drud', 'droll', 'dspd', 'ddir', 'dtws', 'dover', 'dshift'],
             base + ('pressure    (BL/kt tws)', 'overshoot   (BL/deg)',
                     'shift lift  (BL/deg)')),
        )
        for cols_, labs in specs:
            for ycol, ftag in (('dloss', 'water'), ('dloss_g', 'ground')):
                q = pv.dropna(subset=cols_ + [ycol])
                if len(q) < 15:
                    continue
                X = q[cols_].to_numpy()
                y = q[ycol].to_numpy()
                coef, *_ = np.linalg.lstsq(X, y, rcond=None)
                rng = np.random.default_rng(11)
                B = []
                for _ in range(2000):
                    ii = rng.integers(0, len(q), len(q))
                    c, *_ = np.linalg.lstsq(X[ii], y[ii], rcond=None)
                    B.append(c)
                B = np.array(B)
                extra = (', +turn-rate control' if 'dyaw' in cols_ else
                         ', +overshoot+shift' if 'dshift' in cols_ else
                         ', +overshoot' if 'dover' in cols_ else '')
                print(f"joint paired regression [{ftag}{extra}] "
                      f"(n={len(q)}):", file=sys.stderr)
                for j, lab in enumerate(labs):
                    lo, hi = np.percentile(B[:, j], [2.5, 97.5])
                    print(f"  {lab:26s} {coef[j]:+.4f} [{lo:+.4f}, {hi:+.4f}]",
                          file=sys.stderr)
        # minimum detectable effect for a paired-mean design
        n = len(pv)
        mde = 2.80 * sd_d / np.sqrt(n)
        print(f"\nMDE (paired mean diff, alpha=.05, power=.8, n={n} pairs): "
              f"{mde:.2f} BL", file=sys.stderr)
        print("-> effects smaller than this are UNDETECTABLE in this corpus "
              "with the paired design; an on-water A/B session needs "
              f"~{int(np.ceil((2.80*sd_d/0.10)**2))} alternating tacks to "
              "resolve 0.10 BL.", file=sys.stderr)
        fig_path = os.path.join(out_dir, 'tack_paired_contrasts.png')
        draw_paired_contrasts(pv, fig_path)
        print(f"\nWrote {fig_path}", file=sys.stderr)
    return 0


def draw_paired_contrasts(pv, out_path):
    """Within-beat consecutive-pair deltas: the conditions-controlled view.
    Each point = two back-to-back tacks on one beat; the slope is the
    technique effect with the day/beat conditions differenced out."""
    panels = (('drud', 'delta rudder p90 (deg)'),
              ('droll', 'delta peak roll rate (deg/s)'),
              ('dspd', 'delta entry speed (kt)'))
    fig, axes = plt.subplots(1, 3, figsize=(13.5, 4.6), sharey=True)
    for ax, (key, xl) in zip(axes, panels):
        q = pv.dropna(subset=[key])
        ax.plot(q[key], q['dloss'], 'o', ms=4, alpha=0.55, color='#1f77b4')
        if len(q) >= 10:
            slope = (q['dloss'] * q[key]).sum() / (q[key] ** 2).sum()
            xs = np.array([q[key].min(), q[key].max()])
            ax.plot(xs, slope * xs, '-', color='#d62728', lw=1.8,
                    label=f'{slope:+.3f} BL/unit (n={len(q)})')
            ax.legend(loc='upper left', fontsize=8)
        ax.axhline(0, color='0.6', lw=0.8)
        ax.axvline(0, color='0.6', lw=0.8)
        ax.grid(alpha=0.3)
        ax.set_xlabel(xl)
    axes[0].set_ylabel('delta loss, shift-adjusted fixed-bar (BL)')
    fig.suptitle('Within-beat consecutive tack pairs: technique deltas vs '
                 'loss delta (conditions differenced out)')
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def rot_matrix(theta):
    c, s = np.cos(theta), np.sin(theta)
    return np.array([[c, -s], [s, c]])


def aligned_R(rec, target_deg=90.0):
    """2x2 map from this tack's local frame to the aligned frame: pre-course ->
    ``target_deg`` (90 == up). For an s2p tack we also reflect the cross-track
    axis so its post leg fans to the same side as a p2s tack (overlays cleanly
    in 'both' mode)."""
    a_pre = np.arctan2(rec['v_pre'][1], rec['v_pre'][0])
    M = rot_matrix(np.radians(target_deg) - a_pre)
    if rec.get('direction') == 's2p':
        M = np.array([[-1.0, 0.0], [0.0, 1.0]]) @ M   # reflect cross-track
    return M


def aligned_transform(rec, target_deg=90.0):
    """Closure xy->XY into the aligned frame (intersection -> origin)."""
    M = aligned_R(rec, target_deg)
    o = np.asarray(rec['xpt'], float)

    def tf(e, n):
        P = np.column_stack([np.asarray(e, float) - o[0],
                             np.asarray(n, float) - o[1]])
        Q = P @ M.T
        return Q[:, 0], Q[:, 1]
    return tf


def fit_ray(rec, side):
    """(t-range, dir) of one fitted course line in the aligned frame.

    Both lines pass through the origin (the intersection) by construction, so a
    line is just origin + t*dir; ``ts`` spans the actual data extent on the side
    the window sits.  Returns (ts[2], dir[2]).
    """
    M = aligned_R(rec)
    tf = aligned_transform(rec)
    e_w, n_w, v = ((rec['e_pre'], rec['n_pre'], rec['v_pre']) if side == 'pre'
                   else (rec['e_post'], rec['n_post'], rec['v_post']))
    vv = M @ v
    Ew, Nw = tf(e_w, n_w)
    proj = Ew * vv[0] + Nw * vv[1]
    ts = np.array([min(proj.min(), 0.0), max(proj.max(), 0.0)])
    return ts, vv


def draw_single(rec, out_path, frame='ground'):
    """One tack in the aligned frame (same coordinates as the overlay): pre-tack
    course points up, the pre/post intersection is the origin."""
    ftag = 'water track (current removed)' if frame == 'water' else 'ground track (GPS)'
    tf = aligned_transform(rec)
    E, N = tf(rec['e'], rec['n'])
    tr = rec['t_rel']
    pre_m = tr <= -PRE_GUARD_S
    post_m = tr >= POST_GUARD_S
    in_m = ~(pre_m | post_m)

    fig, ax = plt.subplots(figsize=(7.5, 8.5))
    ax.plot(E[pre_m], N[pre_m], '.', color='#1f77b4', ms=5,
            label='pre-tack course (port) — fit data')
    ax.plot(E[in_m], N[in_m], '.', color='#bbbbbb', ms=4,
            label='in-tack (excluded from fit)')
    ax.plot(E[post_m], N[post_m], '.', color='#d62728', ms=5,
            label='post-tack course (stbd) — fit data')

    for side, col in (('pre', '#1f77b4'), ('post', '#d62728')):
        ts, vv = fit_ray(rec, side)
        ax.plot(ts * vv[0], ts * vv[1], '-', color=col, lw=2)

    ax.plot(0, 0, 'kx', ms=12, mew=2.5, label='pre/post intersection (origin)')

    ax.set_aspect('equal')
    ax.grid(alpha=0.3)
    ax.set_xlabel('cross-track (m)   (post-tack to port / left)')
    ax.set_ylabel('along pre-tack course (m)   (boat approaches from below)')
    ax.set_title(f"Port->starboard tack  {rec['day']}  [{ftag}]\n"
                 f"turn {rec['tack_angle']:.0f}deg   pre-spd {rec['spd_pre']:.1f}kt   "
                 f"hdg std pre/post {rec['hdg_std_pre']:.1f}/{rec['hdg_std_post']:.1f}deg")
    ax.legend(loc='best', fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def draw_aligned(recs, out_path, frame='ground'):
    """Overlay tacks in a frame where every pre/post intersection sits at the
    origin and every pre-tack course points straight up.

    Both fitted course lines pass through the intersection *by construction*, so
    we draw them as rays anchored at the origin out to the extent of their data
    window: pre goes down (boat approaches from below), post fans up-left by the
    tack angle. The actual turn track is overlaid thin to show the rounding.
    """
    ftag = 'water track (current removed)' if frame == 'water' else 'ground track (GPS)'
    fig, ax = plt.subplots(figsize=(9.0, 9.5))
    cmap = plt.get_cmap('turbo')
    for k, rec in enumerate(recs):
        tf = aligned_transform(rec)
        col = cmap((k + 0.5) / len(recs))

        # FAINT, THIN = the fitted courses (reference). Drawn first/underneath so
        # the real track reads on top. Straight, through the origin by
        # construction; pre rays all land on the same vertical, post rays fan by
        # each tack's turn angle.
        for side in ('pre', 'post'):
            ts, vv = fit_ray(rec, side)
            ts = np.clip(ts, -RAY_MAX_M, RAY_MAX_M)
            ax.plot(ts * vv[0], ts * vv[1], '-', color=col, lw=0.8,
                    alpha=0.30, zorder=2)

        # BOLD, OPAQUE = the actual boat track (the data): the whole drawn span,
        # clipped to the 100 m view. This is what we want to see -- where it hugs
        # the faint line the fit is good; where it bulges (the +/-10 s turn near
        # the origin) is the maneuver itself.
        E, N = tf(rec['e'], rec['n'])
        d = np.hypot(E, N) <= RAY_MAX_M
        ax.plot(E[d], N[d], '-', color=col, lw=1.6, alpha=0.95, zorder=3)

    ax.plot(0, 0, 'k+', ms=16, mew=3, zorder=5, label='pre/post intersection')
    # legend proxies for the two line styles
    ax.plot([], [], '-', color='0.2', lw=1.8, label='actual track (data)')
    ax.plot([], [], '-', color='0.55', lw=0.9, label='fitted course (reference)')
    ax.set_aspect('equal')
    ax.grid(alpha=0.3)
    ax.set_xlabel('cross-track (m)   (post-tack fans to port / left)')
    ax.set_ylabel('along pre-tack course (m)   (boat approaches from below)')
    ax.set_title(f"{len(recs)} port->starboard tacks, aligned on pre-tack "
                 f"course angle + pre/post intersection  [{ftag}]\n"
                 f"(pre-tack course points up; boat turns to port onto starboard)")
    ax.legend(loc='upper right', fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def draw_tackloss_curves(recs, out_path, loa=None):
    """Windward distance vs time, detrended by each tack's pre-tack VMG line
    (water frame). Pre-tack rides flat at 0; the curve dips through the
    maneuver and settles at -loss. Thin/high tacks vs fat/fast tacks show
    different shapes; the settled depth is the cost."""
    fig, ax = plt.subplots(figsize=(9.5, 7.0))
    cmap = plt.get_cmap('turbo')
    yl = 'windward distance vs pre-tack VMG line (m)'
    sc = 1.0
    if loa:
        sc = 1.0 / loa
        yl = 'windward distance vs pre-tack VMG line (boatlengths)'
    big = len(recs) > 15
    for k, rec in enumerate(recs):
        col = cmap((k + 0.5) / len(recs))
        lab = None if big else f"{rec['day']}  {rec['loss_water']:+.0f}m"
        ax.plot(rec['curve_t'], rec['curve_w'] * sc, '-', color=col,
                lw=0.8 if big else 1.4, alpha=0.55 if big else 0.9, label=lab)
    # mean curve on a common time grid (where curves overlap)
    tg = np.arange(-PRE_GUARD_S - FIT_S, POST_GUARD_S + POST_FIT_S, 1.0)
    stack = np.array([np.interp(tg, r['curve_t'], r['curve_w'] * sc,
                                left=np.nan, right=np.nan) for r in recs])
    ax.plot(tg, np.nanmean(stack, 0), 'k-', lw=2.5, zorder=5, label='mean')
    ax.axhline(0, color='0.4', lw=1.0)
    ax.axvspan(-PRE_GUARD_S, POST_GUARD_S, color='0.85', alpha=0.4, zorder=0,
               label='in-tack (excluded)')
    ax.grid(alpha=0.3)
    ax.set_xlabel('time from tack (s)')
    ax.set_ylabel(yl)
    ax.set_title(f"{len(recs)} tacks: windward distance lost  [water track]\n"
                 "flat=0 before; settles at -loss after recovery")
    ax.legend(loc='lower left', fontsize=7, ncol=2)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def draw_tackloss_scatter(recs, out_path, loa=None):
    """Loss vs a 'fatness' proxy (minimum speed in the tack). Tests #1 (thin,
    slow, high) vs #2 (fat, holds speed): if holding speed pays, loss falls as
    min-speed rises."""
    sc = (1.0 / loa) if loa else 1.0
    unit = 'boatlengths' if loa else 'm'
    ms = np.array([r['min_spd'] for r in recs])
    lw = np.array([r['loss_water'] for r in recs]) * sc
    lg = np.array([r['loss_ground'] for r in recs]) * sc
    fig, ax = plt.subplots(figsize=(8.0, 7.0))
    ax.scatter(ms, lw, s=60, c='#1f77b4', label='water (technique)', zorder=3)
    ax.scatter(ms, lg, s=40, c='#d62728', marker='x', label='ground (vs mark)', zorder=3)
    if len(recs) <= 20:
        for r, x, y in zip(recs, ms, lw):
            ax.annotate(r['day'][5:], (x, y), fontsize=6, alpha=0.6,
                        xytext=(3, 3), textcoords='offset points')
    if len(ms) >= 3:
        m, b = np.polyfit(ms, lw, 1)
        xs = np.array([ms.min(), ms.max()])
        ax.plot(xs, m * xs + b, '-', color='#1f77b4', lw=1.2, alpha=0.6,
                label=f'water trend: {m:+.1f} {unit}/kt')
    ax.grid(alpha=0.3)
    ax.set_xlabel('minimum speed through the tack (kt)   <- thin/slow | fat/fast ->')
    ax.set_ylabel(f'windward distance lost ({unit})')
    ax.set_title(f"{len(recs)} tacks: loss vs how much speed was carried\n"
                 "negative trend => holding speed (fat) costs less")
    ax.legend(loc='best', fontsize=8)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def report_turnrate(recs, out_dir):
    """Turn rate vs rudder angle through tacks, three honest views.

    The per-sample in-tack relation is a hysteresis LOOP, not a function: yaw
    responds to the integrated rudder history (~1 s lag + hull inertia), so
    binned per-sample medians mix turn-entry samples (fast, yaw still
    building) with mid-tack samples (slow, rotation established) and fake a
    speed dependence. Panels:
      A: per-tack PEAK turn rate vs PEAK rudder, split by entry-speed band --
         the maneuver-level transfer ("does more wheel buy a faster turn?").
      B: mean (rudder, yaw) phase trajectories by rudder tercile, time
         markers every 5 s -- the loop itself.
      C: raw per-sample cloud by instantaneous speed band, for context.
    """
    LAG_S = 1.0     # rudder leads the measured heading rate (hull ~0.5 s +
                    # race_logs hdg filter ~0.5 s): pair rudder(t-LAG), yaw(t)
    k = int(round(LAG_S * _FS))
    ks = max(1, int(round(1.0 * _FS)))
    rr = [r for r in recs if np.isfinite(r['rudder_curve']).any()
          and np.isfinite(r.get('rudder_p90', np.nan))
          and np.isfinite(r.get('yaw_peak', np.nan))]
    if not rr:
        print("No rudder data (use --modern).", file=sys.stderr)
        return 1
    R, Y, V = [], [], []
    tg = np.arange(-15.0, 30.0, 0.2)
    RM, YM = [], []     # per-tack curves on the common grid (for panel B)
    for r in rr:
        t = r['t_rel']
        yaw = np.convolve(np.nan_to_num(np.gradient(r['steer_ang']) * _FS),
                          np.ones(ks) / ks, 'same')
        ii = np.where((t >= -PRE_GUARD_S) & (t <= POST_GUARD_S))[0]
        ii = ii[ii >= k]
        R.append(r['rudder_curve'][ii - k])
        Y.append(yaw[ii])
        V.append(r['spd_curve'][ii])
        RM.append(np.interp(tg, t - LAG_S, r['rudder_curve'],
                            left=np.nan, right=np.nan))
        YM.append(np.interp(tg, t, yaw, left=np.nan, right=np.nan))
    R, Y, V = (np.concatenate(x) for x in (R, Y, V))
    ok = np.isfinite(R) & np.isfinite(Y) & np.isfinite(V) & (V > 2.0)
    R, Y, V = R[ok], Y[ok], V[ok]
    RM, YM = np.array(RM), np.array(YM)
    print(f"\n=== Turn rate vs rudder: {len(rr)} tacks, {len(R)} in-tack "
          f"samples, rudder shifted {LAG_S:.1f} s ===", file=sys.stderr)

    fig, (aA, aB, aC) = plt.subplots(1, 3, figsize=(16.0, 5.6))

    # --- A: per-tack peaks, entry-speed bands -----------------------------
    rp = np.array([r['rudder_p90'] for r in rr])
    yp = np.array([r['yaw_peak'] for r in rr])
    sp = np.array([r['spd_pre'] for r in rr])
    s1, s2 = np.nanpercentile(sp, [33.3, 66.7])
    bandsA = ((-np.inf, s1, '#d62728', f'entry < {s1:.1f} kt'),
              (s1, s2, '#ff7f0e', f'{s1:.1f}-{s2:.1f} kt'),
              (s2, np.inf, '#1f77b4', f'entry > {s2:.1f} kt'))
    edges = np.arange(15.0, 60.1, 5.0)
    mid = 0.5 * (edges[:-1] + edges[1:])
    print("median yaw_peak (deg/s) per rudder_p90 bin, by entry speed:",
          file=sys.stderr)
    for lo, hi, col, lab in bandsA:
        m = (sp >= lo) & (sp < hi)
        aA.plot(rp[m], yp[m], '.', ms=3, color=col, alpha=0.35, zorder=1)
        med = np.full(len(mid), np.nan)
        for j in range(len(mid)):
            mm = m & (rp >= edges[j]) & (rp < edges[j + 1])
            if mm.sum() >= 8:
                med[j] = np.median(yp[mm])
        aA.plot(mid, med, '-o', ms=4, lw=2.4, color=col, zorder=4,
                label=f'{lab} (n={m.sum()})')
        print(f"  {lab:>16}: " + '  '.join(
            f'{e:.0f}-{e2:.0f}:{v:.1f}' for e, e2, v in
            zip(edges[:-1], edges[1:], med) if np.isfinite(v)),
            file=sys.stderr)
    aA.set_xlabel('rudder peak p90 (deg into tack)')
    aA.set_ylabel('peak turn rate (deg/s)')
    aA.set_title('A: per-tack peaks -- does more wheel\nbuy a faster turn?',
                 fontsize=10)
    aA.legend(loc='upper left', fontsize=8)
    aA.grid(alpha=0.3)

    # --- B: mean phase trajectories by rudder tercile ----------------------
    r1, r2 = np.nanpercentile(rp, [33.3, 66.7])
    bandsB = ((-np.inf, r1, '#1f77b4', f'soft helm (<{r1:.0f} deg)'),
              (r1, r2, '#2ca02c', f'mid ({r1:.0f}-{r2:.0f} deg)'),
              (r2, np.inf, '#d62728', f'hard helm (>{r2:.0f} deg)'))
    marks_t = (0.0, 5.0, 10.0, 15.0, 20.0)
    for lo, hi, col, lab in bandsB:
        m = (rp >= lo) & (rp < hi)
        mr = np.nanmean(RM[m], axis=0)
        my = np.nanmean(YM[m], axis=0)
        aB.plot(mr, my, '-', lw=2.2, color=col, zorder=3,
                label=f'{lab} (n={m.sum()})')
        for mt in marks_t:
            j = int(round((mt - tg[0]) / 0.2))
            if 0 <= j < len(tg):
                aB.plot(mr[j], my[j], 'o', ms=5, color=col, zorder=4)
                if col == '#d62728':
                    aB.annotate(f'{mt:.0f}s', (mr[j], my[j]),
                                textcoords='offset points', xytext=(5, 4),
                                fontsize=7, color='0.3')
    aB.axhline(0, color='0.5', lw=0.6)
    aB.axvline(0, color='0.5', lw=0.6)
    aB.set_xlabel('rudder into tack (deg, 1 s earlier)')
    aB.set_ylabel('turn rate (deg/s)')
    aB.set_title('B: mean phase trajectory (-15..+30 s)\n-- the relation is '
                 'a loop, dots every 5 s', fontsize=10)
    aB.legend(loc='upper left', fontsize=8)
    aB.grid(alpha=0.3)

    # --- C: raw per-sample cloud by instantaneous speed --------------------
    bandsC = ((3.0, 5.0, '#d62728'), (5.0, 6.0, '#ff7f0e'),
              (6.0, 8.5, '#1f77b4'))
    for lo, hi, col in bandsC:
        m = (V >= lo) & (V < hi)
        aC.plot(R[m][::4], Y[m][::4], '.', ms=1.5, color=col, alpha=0.05,
                zorder=1)
        aC.plot([], [], '.', ms=6, color=col, label=f'{lo:g}-{hi:g} kt')
    aC.axhline(0, color='0.5', lw=0.6)
    aC.axvline(0, color='0.5', lw=0.6)
    aC.set_xlim(-30, 50)
    aC.set_ylim(-5, 9)
    aC.set_xlabel('rudder into tack (deg, 1 s earlier)')
    aC.set_ylabel('turn rate (deg/s)')
    aC.set_title('C: all in-tack samples by instantaneous\nboat speed '
                 '(dynamics, not steady-state)', fontsize=10)
    aC.legend(loc='upper left', fontsize=8)
    aC.grid(alpha=0.3)

    fig.suptitle('Turn rate vs rudder through tacks: maneuver peaks, phase '
                 'loop, raw cloud')
    fig.tight_layout()
    out = os.path.join(out_dir, 'turnrate_vs_rudder.png')
    fig.savefig(out, dpi=140)
    plt.close(fig)
    print(f"Wrote {out}", file=sys.stderr)
    return 0


# Nearly every tack swings past its settled exit heading (corpus median
# ~+15 deg: bear away through/below course, then squeeze back up), so the
# groups are "low swing / typical / big overshoot", at roughly the 20th and
# 75th percentiles of the distribution.
OV_UNDER = 8.0   # deg: barely crossed the settled exit course
OV_OVER = 20.0   # deg: swung far past, then had to come back up


def report_overshoot(recs, out_dir, loa, frame):
    """Under- vs over-turned tacks.

    overshoot = max heading swing past the settled exit course (deg) within
    45 s of the tack. Grouped by the cue itself (like --rank rudder), so the
    panels show the consequence of turning too far vs not crossing the line.
    """
    rr = [r for r in recs if np.isfinite(r.get('overshoot', np.nan))
          and np.isfinite(r.get('loss_fix_sess_water_35', np.nan))
          and np.isfinite(r.get('loss_fix_sess_ground_35', np.nan))]
    ov = np.array([r['overshoot'] for r in rr])
    print(f"\n=== Overshoot of the exit course (n={len(rr)}) ===",
          file=sys.stderr)
    qs = np.percentile(ov, [5, 25, 50, 75, 95])
    print("overshoot percentiles 5/25/50/75/95: "
          + '  '.join(f'{v:+.1f}' for v in qs) + ' deg', file=sys.stderr)

    # loss profile vs overshoot, before grouping: is it monotone?
    prof_edges = (-90, 4, 8, 12, 16, 20, 25, 90)
    print("loss (water, BL) vs overshoot bin:", file=sys.stderr)
    for lo, hi in zip(prof_edges[:-1], prof_edges[1:]):
        g = [r for r in rr if lo <= r['overshoot'] < hi]
        if len(g) < 5:
            continue
        lw_ = np.array([r['loss_fix_sess_water_35'] for r in g]) / loa
        print(f"  [{lo:>3}..{hi:>3}) deg: n={len(g):>3}  "
              f"loss {lw_.mean():.2f} +- {lw_.std()/np.sqrt(len(g)):.2f}",
              file=sys.stderr)

    groups = (
        ([r for r in rr if r['overshoot'] < OV_UNDER], '#1f77b4',
         f'low swing (<{OV_UNDER:g} deg past)'),
        ([r for r in rr if OV_UNDER <= r['overshoot'] < OV_OVER], '#2ca02c',
         f'typical ({OV_UNDER:g}-{OV_OVER:g} deg)'),
        ([r for r in rr if r['overshoot'] >= OV_OVER], '#d62728',
         f'big overshoot (>={OV_OVER:g} deg past)'),
    )
    print(f"{'group':>34} {'n':>4} {'lossW':>6} {'+-SE':>5} {'lossG':>6} "
          f"{'rudP90':>6} {'roll':>5} {'yawPk':>5} {'spdPre':>6} "
          f"{'tws':>5} {'vmgPre':>6} {'vmgPost':>7} {'lift':>5} "
          f"{'angle':>5} {'s2p%':>4}", file=sys.stderr)
    for grp, _, lab in groups:
        if not grp:
            continue
        lw_ = np.array([r['loss_fix_sess_water_35'] for r in grp]) / loa
        lg_ = np.array([r['loss_fix_sess_ground_35'] for r in grp]) / loa
        print(f"{lab:>34} {len(grp):>4} {lw_.mean():>6.2f} "
              f"{lw_.std()/np.sqrt(len(grp)):>5.2f} {lg_.mean():>6.2f} "
              f"{np.nanmean([r['rudder_p90'] for r in grp]):>6.1f} "
              f"{np.nanmean([r['roll_rate_max'] for r in grp]):>5.1f} "
              f"{np.nanmean([r['yaw_peak'] for r in grp]):>5.1f} "
              f"{np.mean([r['spd_pre'] for r in grp]):>6.2f} "
              f"{np.nanmean([r['tws_pre'] for r in grp]):>5.1f} "
              f"{np.nanmean([r['vmg_pre'] for r in grp])*ca.MS_TO_KT:>6.2f} "
              f"{np.nanmean([r['vmg_post'] for r in grp])*ca.MS_TO_KT:>7.2f} "
              f"{np.nanmean([r['shift_lift'] for r in grp]):>5.1f} "
              f"{np.mean([r['tack_angle'] for r in grp]):>5.1f} "
              f"{100*np.mean([r['direction'] == 's2p' for r in grp]):>4.0f}",
              file=sys.stderr)

    # how the cue couples to the other technique variables and to loss
    def corr(key):
        x = np.array([r[key] for r in rr], float)
        m = np.isfinite(x)
        return np.corrcoef(ov[m], x[m])[0, 1]
    print("corr(overshoot, .): "
          f"rudder_p90 {corr('rudder_p90'):+.2f}  "
          f"roll_rate {corr('roll_rate_max'):+.2f}  "
          f"yaw_peak {corr('yaw_peak'):+.2f}  "
          f"spd_pre {corr('spd_pre'):+.2f}  "
          f"tws {corr('tws_pre'):+.2f}  "
          f"shift_lift {corr('shift_lift'):+.2f}  "
          f"lossW {corr('loss_fix_sess_water_35'):+.2f}  "
          f"lossG {corr('loss_fix_sess_ground_35'):+.2f}", file=sys.stderr)

    out = os.path.join(out_dir, f'tack_overshoot_{frame}.png')
    draw_steering(groups, out, loa=loa,
                  title='Low-swing vs typical vs big-overshoot tacks '
                        '(grouped by swing past the settled exit course)')
    print(f"\nWrote {out}", file=sys.stderr)
    return 0


def draw_steering(groups, out_path, title=None, loa=None):
    """Steering + performance, time-aligned at the tack and flipped to a positive
    turn. Five panels: rudder angle (the actual steering input), heading turned
    from entry (the response), boat speed, VMG, and cumulative windward
    distance vs the pre-tack pace line (the integral of the VMG hole — the
    settled gap between the group means IS the loss difference). ``groups`` is
    a sequence of (recs, color, label); each shown as faint individuals plus a
    bold GROUP MEAN."""
    sc = (1.0 / loa) if loa else 1.0
    wunit = 'BL' if loa else 'm'
    fig, (a0, a1, a2, a3, a4) = plt.subplots(5, 1, figsize=(9.5, 13.5),
                                             sharex=True)
    tg = np.arange(-20.0, 70.0, 0.2)

    def meanof(recs, key, scale=1.0):
        M = np.array([np.interp(tg, r['t_rel'], r[key] * scale,
                                left=np.nan, right=np.nan)
                      for r in recs])
        n_ok = np.sum(np.isfinite(M), axis=0)
        mu = np.nanmean(M, axis=0)
        sem = np.nanstd(M, axis=0) / np.sqrt(np.maximum(n_ok, 1))
        return mu, sem

    panels = ((a0, 'rudder_curve', 1.0), (a1, 'steer_ang', 1.0),
              (a2, 'spd_curve', 1.0), (a3, 'vmg_curve', 1.0),
              (a4, 'curve_w', sc))
    for grp, col, lab in groups:
        ind_alpha = min(0.20, 8.0 / max(1, len(grp)))
        for ax, key, scale in panels:
            for r in grp:                                   # faint individuals
                ax.plot(r['t_rel'], r[key] * scale, color=col, lw=0.6,
                        alpha=ind_alpha, zorder=1)
            mu, sem = meanof(grp, key, scale)
            if ax is a4:                                    # +/-1 SE band on the sum
                ax.fill_between(tg, mu - sem, mu + sem, color=col,
                                alpha=0.25, lw=0, zorder=3)
            ax.plot(tg, mu, color=col, lw=2.6, zorder=4,
                    label=f"{lab} (n={len(grp)})")
    for ax in (a0, a1, a2, a3, a4):
        ax.axvspan(-PRE_GUARD_S, POST_GUARD_S, color='0.9', alpha=0.5, zorder=0)
        ax.axvline(0, color='0.5', lw=0.8)
        ax.grid(alpha=0.3)
        ax.set_xlim(-20, 70)
    a0.axhline(0, color='0.5', lw=0.6)
    a4.axhline(0, color='0.5', lw=0.6)
    a4.set_ylim((-4.0, 1.5) if loa else (-40.0, 15.0))  # clip outlier individuals
    a0.set_ylabel('rudder into tack (deg)\n[steering input]')
    a1.set_ylabel('turn from entry\nheading (deg) [response]')
    a2.set_ylabel('boat speed (kt)')
    a3.set_ylabel('VMG to windward (kt)')
    a4.set_ylabel(f'windward progress vs\npre-tack pace ({wunit})\n[the sum]')
    a4.set_xlabel('time from tack (s)')
    a0.set_title(title or "Steering input (rudder) + response + performance "
                 "(group means bold; aligned & flipped to positive turn)")
    a0.legend(loc='upper right', fontsize=9)
    fig.tight_layout()
    fig.savefig(out_path, dpi=140)
    plt.close(fig)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--logs-dir', default=None)
    ap.add_argument('--n', type=int, default=10, help='tacks in the aligned plot')
    ap.add_argument('--per-day', type=int, default=2, help='max tacks per day in the overlay')
    ap.add_argument('--frame', choices=('ground', 'water'), default='ground',
                    help="'ground' = GPS track (current in); 'water' = "
                         "current-removed (STW along heading+leeway)")
    ap.add_argument('--tws', type=float, default=None,
                    help='target true wind speed (kt); keep only tacks near it')
    ap.add_argument('--tws-tol', type=float, default=1.5,
                    help='+/- band (kt) around --tws')
    ap.add_argument('--tacks', choices=('p2s', 's2p', 'both'), default='p2s',
                    help='which tack direction(s) to include')
    ap.add_argument('--metric',
                    choices=('aligned', 'tackloss', 'steering', 'noise',
                             'paired', 'turnrate', 'overshoot'),
                    default='aligned',
                    help="'aligned' = course overlay; 'tackloss' = windward "
                         "distance lost + thin/fat scatter; 'steering' = "
                         "time-aligned steering + speed + VMG of the best "
                         "tacks; 'noise' = synthetic metric validation + "
                         "per-tack noise budget; 'paired' = within-beat "
                         "pairs + mixed-effects re-test + MDE; 'turnrate' = "
                         "per-sample turn rate vs rudder angle, speed-"
                         "resolved; 'overshoot' = under/just-right/over-"
                         "turned group contrast")
    ap.add_argument('--modern', action='store_true',
                    help='only modern .parquet sessions (rudder available)')
    ap.add_argument('--loa', type=float, default=None,
                    help='boat LOA (m); report tack loss in boatlengths')
    ap.add_argument('--rank', choices=('retain', 'loss', 'rudder'),
                    default='retain',
                    help='steering: rank best/worst by speed-retained, '
                         'fixed-bar loss, or rudder peak (groups by the cue '
                         'itself; cleanest contrast)')
    ap.add_argument('--trim', type=int, default=0,
                    help='steering: drop this many extreme tacks from each '
                         'end of the ranking before taking best/worst N '
                         '(excludes fluke negatives and disaster outliers)')
    ap.add_argument('--limit', type=int, default=0, help='only last N sessions')
    ap.add_argument('--out', default=str(PROJECT_ROOT / 'Analyze' / 'Plots'))
    args = ap.parse_args(argv)

    logs_dir = args.logs_dir or ca.default_logs_dir()
    # modern parquet (2024+) + legacy .pd.gz (2019-2023); sorted by date string
    files = sorted(glob.glob(os.path.join(logs_dir, '*.parquet')) +
                   ([] if args.modern else
                    glob.glob(os.path.join(logs_dir, '*.pd.gz'))))
    if args.limit:
        files = files[-args.limit:]
    os.makedirs(args.out, exist_ok=True)

    all_recs = []
    for i, p in enumerate(files):
        name = os.path.basename(p)
        recs = extract_tacks(name, logs_dir, frame=args.frame, tack_dir=args.tacks)
        if recs:
            print(f"[{i+1}/{len(files)}] {name}: {len(recs)} clean port->stbd tacks",
                  file=sys.stderr)
        all_recs.extend(recs)

    print(f"\nTotal clean port->starboard tacks: {len(all_recs)}", file=sys.stderr)
    if not all_recs:
        print("No qualifying tacks found.", file=sys.stderr)
        return 1

    # optional wind-speed band
    if args.tws is not None:
        lo, hi = args.tws - args.tws_tol, args.tws + args.tws_tol
        all_recs = [r for r in all_recs
                    if np.isfinite(r['tws_pre']) and lo <= r['tws_pre'] <= hi]
        print(f"Within TWS {args.tws:.1f}+/-{args.tws_tol:.1f} kt: "
              f"{len(all_recs)} tacks", file=sys.stderr)
        if not all_recs:
            print("No tacks in that wind band.", file=sys.stderr)
            return 1

    # fixed-target-VMG losses (after the band filter so medians match the
    # analyzed population)
    add_fixed_bar_losses(all_recs)
    loa = args.loa or 10.5   # J/105 default for the BL-denominated modes

    if args.metric == 'noise':
        return report_noise(all_recs, loa)
    if args.metric == 'paired':
        return report_paired(all_recs, loa, args.out)
    if args.metric == 'turnrate':
        return report_turnrate(all_recs, args.out)
    if args.metric == 'overshoot':
        return report_overshoot(all_recs, args.out, loa, args.frame)

    # steering: rank the tacks, then contrast the N best vs N worst group means.
    if args.metric == 'steering':
        if args.rank == 'retain':
            key = lambda r: r['min_spd'] / r['spd_pre']   # speed retained
            blab, wlab = 'best: high speed-retained', 'worst: low speed-retained'
        elif args.rank == 'rudder':
            # group by the CUE itself: lowest rudder peak = best. Entry speed
            # and roll rate come out naturally matched between groups, so the
            # panels isolate the helm's consequence; outcome-ranked groupings
            # (retain/loss) mix entry-speed and roll confounds into the
            # contrast.
            key = lambda r: -r['rudder_p90']
            blab, wlab = 'smooth helm: lowest rudder peak', 'jammed helm: highest rudder peak'
            all_recs = [r for r in all_recs if np.isfinite(r['rudder_p90'])]
        else:
            # least FIXED-BAR loss (the headline metric). NB ranking by
            # retention biases the "worst" group toward fast entries (high
            # denominator), which misleads in the group-mean plots.
            key = lambda r: -r['loss_fix_sess_water_35']
            blab, wlab = 'best: least windward loss', 'worst: most loss'
            all_recs = [r for r in all_recs
                        if np.isfinite(r['loss_fix_sess_water_35'])]
        ranked = sorted(all_recs, key=key, reverse=True)
        t = args.trim
        if t and len(ranked) < 2 * (t + args.n):
            print(f"--trim {t} leaves too few tacks; ignoring", file=sys.stderr)
            t = 0
        best = ranked[t:t + args.n]
        worst = ranked[len(ranked) - t - args.n:len(ranked) - t]
        print(f"\nSteering: best vs worst {args.n} by {args.rank}"
              f"{f' (trimmed {t}/end)' if t else ''}", file=sys.stderr)
        for tag, grp in (('BEST', best), ('WORST', worst)):
            ms = np.mean([r['min_spd'] / r['spd_pre'] for r in grp])
            ls = np.mean([r['loss_fix_sess_water_35'] for r in grp]) / (args.loa or 1)
            sp = np.mean([r['spd_pre'] for r in grp])
            ta = np.mean([r['tack_angle'] for r in grp])
            rd = np.nanmean([r['rudder_p90'] for r in grp])
            rl = np.nanmean([r['roll_rate_max'] for r in grp])
            u = 'BL' if args.loa else 'm'
            print(f"  {tag}: loss_fix={ls:.2f}{u}  retain={ms:.2f}  "
                  f"spd_pre={sp:.2f}kt  angle={ta:.1f}  rudP90={rd:.1f}  "
                  f"rollRate={rl:.1f}", file=sys.stderr)
        out = os.path.join(args.out,
                           f'tack_steering_{args.rank}_{args.frame}.png')
        draw_steering(((best, '#1f77b4', blab), (worst, '#d62728', wlab)),
                      out, loa=args.loa)
        print(f"\nWrote {out}", file=sys.stderr)
        return 0

    # rank by steadiness (lowest combined heading std), prefer realistic tack angles
    def quality(r):
        return r['hdg_std_pre'] + r['hdg_std_post'] + 0.1 * abs(r['tack_angle'] - 85)
    all_recs.sort(key=quality)

    # take the best, but cap per-day so one day doesn't dominate the overlay
    per_day = {}
    best = []
    for r in all_recs:
        if per_day.get(r['day'], 0) >= args.per_day:
            continue
        per_day[r['day']] = per_day.get(r['day'], 0) + 1
        best.append(r)
        if len(best) >= args.n:
            break
    print("\nSelected tacks:", file=sys.stderr)
    for r in best:
        print(f"  {r['day']}  turn={r['tack_angle']:5.1f}  "
              f"hdgStd pre/post={r['hdg_std_pre']:.1f}/{r['hdg_std_post']:.1f}  "
              f"spd pre/post={r['spd_pre']:.1f}/{r['spd_post']:.1f}  "
              f"tws={r['tws_pre']:.1f}  "
              f"awa pre/post={r['awa_pre']:+.0f}/{r['awa_post']:+.0f}  "
              f"dHdg={r['hdg_change']:+.0f}", file=sys.stderr)
    awp = np.mean([r['awa_pre'] for r in best])
    aws = np.mean([r['awa_post'] for r in best])
    print(f"\nMean AWA  pre={awp:+.1f}  post={aws:+.1f}  "
          f"(|pre|={abs(awp):.1f}, |post|={abs(aws):.1f})", file=sys.stderr)

    if args.metric == 'tackloss':
        u = 'bl' if args.loa else 'm'
        sc = (1.0 / args.loa) if args.loa else 1.0
        print(f"\nTack loss (windward distance lost; +ve = lost ground):",
              file=sys.stderr)
        print(f"  {'day':10} {'lossWat':>8} {'lossGnd':>8} {'settle_s':>8} "
              f"{'minSpd':>6} {'vmgPre':>6}", file=sys.stderr)
        for r in best:
            st = f"{r['settle_s']:.0f}" if np.isfinite(r['settle_s']) else '  >win'
            print(f"  {r['day']:10} {r['loss_water']*sc:7.1f}{u} "
                  f"{r['loss_ground']*sc:7.1f}{u} {st:>8} "
                  f"{r['min_spd']:6.1f} {r['vmg_pre']*ca.MS_TO_KT:6.1f}",
                  file=sys.stderr)
        lw = np.array([r['loss_water'] for r in best]) * sc
        lg = np.array([r['loss_ground'] for r in best]) * sc
        print(f"  mean loss: water={lw.mean():.1f}{u}  ground={lg.mean():.1f}{u}"
              f"   (current tax = {lg.mean()-lw.mean():+.1f}{u})", file=sys.stderr)
        curves = os.path.join(args.out, f'tackloss_curves_{args.frame}.png')
        scatter = os.path.join(args.out, f'tackloss_scatter_{args.frame}.png')
        draw_tackloss_curves(best, curves, loa=args.loa)
        draw_tackloss_scatter(best, scatter, loa=args.loa)
        print(f"\nWrote {curves}\nWrote {scatter}", file=sys.stderr)
        return 0

    single_path = os.path.join(args.out, f'tack_single_{args.frame}.png')
    aligned_path = os.path.join(args.out, f'tacks_aligned_10_{args.frame}.png')
    draw_single(best[0], single_path, frame=args.frame)
    draw_aligned(best, aligned_path, frame=args.frame)
    print(f"\nWrote {single_path}\nWrote {aligned_path}", file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
