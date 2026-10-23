"""Estimate the water current under the boat from its own instruments — the
"current triangle" — and calibrate the leeway term by cross-tack symmetry.

Physics
-------
Velocity over ground (GPS) is the vector sum of velocity through the water and
the current::

    V_ground = V_water + V_current   =>   V_current = V_ground - V_water

``V_ground`` comes from GPS SOG/COG. ``V_water`` has magnitude STW (speed through
water) and points along the boat's *water track*, which is the heading rotated to
leeward by the **leeway** angle ``lambda``. Leeway is the dominant unknown.

Conventions (kept consistent throughout):

* All bearings are TRUE north, degrees, compass convention (clockwise from N).
  ``E = V*sin(theta)``, ``N = V*cos(theta)``.
* Speeds are knots; the returned current is knots.
* Components are returned as ``(East, North)``.

The validator/fitter
--------------------
The true current is continuous across a tack (same body of water, seconds apart).
A correct leeway model is therefore the one whose *estimated* current does not
jump when the boat tacks. We detect tacks, take a short steady window just before
and just after each, and fit the leeway parameters that minimise the summed
squared discontinuity ``sum_tacks || cur_post - cur_after ||^2``.

Leeway models (all odd across a tack via the signed ``heel`` factor, so the
symmetry fit can actually constrain them):

* ``none``    : lambda = 0                              (baseline)
* ``classic`` : lambda = K * heel / spd^2               (1 param: K)
* ``wind``    : lambda = (a + b*AWS) * heel / spd^2      (2 params: a, b)

``wind`` nests ``classic`` (b=0); a non-zero fitted ``b`` is direct evidence that
leeway depends on wind speed *beyond* what heel already captures.

Calibration / units
-------------------
``Libraries.race_logs.read_log_file`` returns processed columns with bearings
already frame-aliased (``hdg_true``, ``cog_true``, ``twd_true``) and speeds in
**m/s**. The display calibration in ``Src/src/assets/calibration/sensor_calibration.json``
is authored against the **knots** stored in the ``.slog`` binary (see
``Libraries/sailview_log_v1.py`` ``knots_convert`` + ``Src/src/js/viewer360.js``
``applySensorCalibration``). So we convert m/s -> kt *first*, then apply
``spd_kt*scale + offset`` and ``heel = zg100_roll - heel.offset`` exactly as the
viewer does.
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
from scipy.optimize import minimize, minimize_scalar

from Libraries.global_variables import G
from Libraries.race_logs import read_log_file
from Libraries.event_detectors import HeadingManeuverDetector

MS_TO_KT = 1.9438444924406046
_FS = G.SAMPLES_PER_SECOND  # 10 Hz parquet rate
_SPD_FLOOR_KT = 2.0         # clamp STW in the leeway denominator (avoid /~0 blow-up).
                            # NB: the leeway *angle* is meaningless at low STW,
                            # but there it multiplies a ~0 water-speed so the
                            # current estimate is unaffected. Report leeway stats
                            # only over sailing samples (spd_kt > SAILING_KT).
SAILING_KT = 2.5            # speed threshold for "actually sailing" sample masks

_CAL_PATH = os.path.join(
    str(PROJECT_ROOT), 'Src', 'src', 'assets', 'calibration',
    'sensor_calibration.json',
)

MODELS = ('none', 'classic', 'wind')
MODEL_PARAM_NAMES = {'none': (), 'classic': ('K',), 'wind': ('a', 'b')}


# ---------------------------------------------------------------------------
# Loading + calibration
# ---------------------------------------------------------------------------
def default_logs_dir():
    """Resolve the PandasLogs directory without depending on cwd.

    ``G.PANDAS_LOGS_DIRECTORY`` is relative ('Data/PandasLogs') and only
    resolves from a particular working dir, so we probe, in order: the
    ``REGATTA_PANDAS_LOGS`` env override, the relative ``G`` path if it happens
    to exist, then the standard data location under the user's home.
    """
    env = os.environ.get('REGATTA_PANDAS_LOGS')
    candidates = [
        env,
        getattr(G, 'PANDAS_LOGS_DIRECTORY', None),
        os.path.expanduser('~/Data/SailingData/PandasLogs'),
    ]
    for c in candidates:
        if c and os.path.isdir(c):
            return c
    # Fall back to the home-relative path even if missing, for a clear error.
    return os.path.expanduser('~/Data/SailingData/PandasLogs')


def load_sensor_calibration(path=_CAL_PATH):
    """Return ``(spd_scale, spd_offset, heel_offset, heading_offset)`` from the
    shared JSON.

    Mirrors ``viewer360.js::applySensorCalibration`` for spd/heel, and adds the
    compass ``heading.offset`` (degrees, added to true heading). On Peer Gynt the
    heading source is the B&G RC42 rate compass, which carries a real, heel-independent
    offset of ≈ -2.3° (the EVEN part of COG-HDG; the leeway is the ODD part, which is
    separated out — see ``Analyze/HEADING_CALIBRATION.md``). NB: the live viewer does
    not apply this offset (its wind angle is derived from HDG at parse time); that
    wiring is a documented follow-up, so for now this only affects offboard analysis.
    """
    try:
        with open(path) as f:
            cal = json.load(f) or {}
    except (OSError, ValueError, TypeError):
        cal = {}
    spd = cal.get('spd') or {}
    heel = cal.get('heel') or {}
    heading = cal.get('heading') or {}
    return (float(spd.get('scale', 1.0)),
            float(spd.get('offset', 0.0)),
            float(heel.get('offset', 0.0)),
            float(heading.get('offset', 0.0)))


_LEEWAY_MODEL_PATH = os.path.join(str(PROJECT_ROOT), 'Analyze', 'leeway_model.json')


def load_leeway_model(path=_LEEWAY_MODEL_PATH):
    """Return the persisted global leeway model dict, or None if absent.

    Produced by ``leeway_report.py``: a single calibration (stw_scale /
    heading_offset / heel_offset) + classic leeway K fit across all days.
    """
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return None


def load_calibrated_log(parquet_name, path=None, calibration='display'):
    """Load one parquet through ``race_logs`` and return a tidy, calibrated,
    knots/true DataFrame ready for the current triangle.

    ``calibration``:
      'display' — the per-display sensor_calibration.json (spd scale/offset,
                  heel offset). Matches the helmsman's instruments.
      'global'  — the calibration block from ``leeway_model.json`` (tack-fit
                  stw_scale, heading_offset, heel_offset; no spd offset). Use
                  this together with the global leeway K for the single model.

    Columns: unixtime, lat, lon, hdg_true, cog_true, spd_kt, sog_kt, aws_kt,
    awa, heel, twd_true, tws_kt.
    """
    if path is None:
        path = default_logs_dir()
    # discard_columns=False keeps unixtime + zg100_roll, which the default
    # whitelist would otherwise drop.
    df = read_log_file(parquet_name, path=path, discard_columns=False)
    if df is None:
        raise ValueError(
            f"{parquet_name}: read_log_file returned None "
            "(boat never left the dock, or file missing).")

    # Older (VIRB-era) logs lack the derived 'unixtime' column but carry a
    # UTC 'timestamp' (or tz-aware 'row_times'); synthesize unixtime from it.
    if 'unixtime' not in df.columns:
        for tc in ('timestamp', 'row_times'):
            if tc in df.columns:
                df = df.copy()
                df['unixtime'] = df[tc].astype('int64') / 1e9
                break

    hdg_true = df['hdg_true'].to_numpy(float)
    if calibration == 'global':
        m = load_leeway_model()
        if not m:
            raise ValueError("calibration='global' but leeway_model.json not found")
        gc = m['calibration']
        spd_scale = float(gc.get('stw_scale', 1.0))
        spd_off = float(gc.get('stw_offset', 0.0))
        heel_off = float(gc.get('heel_offset_deg', -4.0))
        hdg_true = hdg_true + float(gc.get('heading_offset_deg', 0.0))
    else:
        spd_scale, spd_off, heel_off, head_off = load_sensor_calibration()
        hdg_true = hdg_true + head_off

    spd_kt = df['spd'].to_numpy(float) * MS_TO_KT * spd_scale + spd_off
    sog_kt = df['sog'].to_numpy(float) * MS_TO_KT
    aws_kt = df['aws'].to_numpy(float) * MS_TO_KT
    heel = df['zg100_roll'].to_numpy(float) - heel_off  # == roll + 4.0 deg

    out = pd.DataFrame({
        'unixtime': df['unixtime'].to_numpy(float),
        'lat': df['latitude'].to_numpy(float),
        'lon': df['longitude'].to_numpy(float),
        'hdg_true': hdg_true,
        'cog_true': df['cog_true'].to_numpy(float),
        'spd_kt': spd_kt,
        'sog_kt': sog_kt,
        'aws_kt': aws_kt,
        'awa': df['awa'].to_numpy(float),
        'heel': heel,
        'twd_true': (df['twd_true'].to_numpy(float)
                     if 'twd_true' in df.columns else np.full(len(df), np.nan)),
        'tws_kt': (df['tws'].to_numpy(float) * MS_TO_KT
                   if 'tws' in df.columns else np.full(len(df), np.nan)),
        'rudder': (df['rudder'].to_numpy(float)
                   if 'rudder' in df.columns else np.full(len(df), np.nan)),
    })
    out.attrs['name'] = str(parquet_name)
    return out


# ---------------------------------------------------------------------------
# Leeway models + current triangle
# ---------------------------------------------------------------------------
def leeway_angle(model, params, spd_kt, heel, aws_kt):
    """Signed leeway angle (degrees, +ve = water track rotated clockwise of
    heading). Sign convention is *not* asserted physically; the fitted
    parameters absorb it (a negative K just means the heel sign runs the other
    way). All models are odd in ``heel`` so a tack flips the leeway."""
    s2 = np.maximum(np.abs(spd_kt), _SPD_FLOOR_KT) ** 2
    if model == 'none':
        return np.zeros_like(spd_kt)
    if model == 'classic':
        (K,) = params
        return K * heel / s2
    if model == 'wind':
        a, b = params
        return (a + b * aws_kt) * heel / s2
    raise ValueError(f"unknown leeway model: {model!r}")


def estimate_current(df, model='classic', params=()):
    """Return ``(cur_E, cur_N, leeway_deg)`` arrays in knots/degrees.

    ``cur = V_ground(GPS) - V_water(STW along hdg_true + leeway)``.
    """
    hdg = np.radians(df['hdg_true'].to_numpy(float))
    cog = np.radians(df['cog_true'].to_numpy(float))
    spd = df['spd_kt'].to_numpy(float)
    sog = df['sog_kt'].to_numpy(float)
    heel = df['heel'].to_numpy(float)
    aws = df['aws_kt'].to_numpy(float)

    lam_deg = leeway_angle(model, params, spd, heel, aws)
    wb = hdg + np.radians(lam_deg)  # water-track bearing

    vw_e, vw_n = spd * np.sin(wb), spd * np.cos(wb)
    vg_e, vg_n = sog * np.sin(cog), sog * np.cos(cog)
    return vg_e - vw_e, vg_n - vw_n, lam_deg


# ---------------------------------------------------------------------------
# Tack detection + cross-tack symmetry fit
# ---------------------------------------------------------------------------
def detect_tacks(df, min_turn=55, steady_std_max=10, steady_seconds=20,
                 min_gap_seconds=25):
    """Return sorted sample indices of tacks (gybes excluded).

    Reuses the repo's shared ``HeadingManeuverDetector`` (same detector used by
    ``Analyze/maneuver_analysis.py``). A maneuver is a tack when the boat is
    upwind (``|AWA| < 90``) and the turn is < 120 deg.
    """
    det = HeadingManeuverDetector(
        min_turn=min_turn, steady_std_max=steady_std_max,
        steady_seconds=steady_seconds, min_gap_seconds=min_gap_seconds,
        sample_rate=_FS)
    raw = det._find_maneuvers(df['hdg_true'].to_numpy(float))
    awa = np.abs(df['awa'].to_numpy(float))
    tacks = []
    for idx, change in raw:
        idx = int(idx)
        upwind = (0 <= idx < len(awa)) and awa[idx] < 90
        if abs(change) < 120 and upwind:
            tacks.append(idx)
    return sorted(tacks)


def tack_windows(df, tacks, guard_s=15.0, win_s=40.0):
    """For each tack, a (idx, pre_slice, post_slice) triple of steady windows
    straddling the maneuver. Windows that run off the ends or collide with a
    neighbouring maneuver are dropped."""
    n = len(df)
    g = int(round(guard_s * _FS))
    w = int(round(win_s * _FS))
    tacks = sorted(tacks)
    out = []
    for j, idx in enumerate(tacks):
        pre0, pre1 = idx - g - w, idx - g
        post0, post1 = idx + g, idx + g + w
        if pre0 < 0 or post1 > n:
            continue
        if j > 0 and pre0 < tacks[j - 1] + g:
            continue
        if j < len(tacks) - 1 and post1 > tacks[j + 1] - g:
            continue
        out.append((idx, slice(pre0, pre1), slice(post0, post1)))
    return out


def symmetry_loss(df, model, params, windows):
    """Return ``(total_sq_loss, per_tack)`` where per_tack is a list of
    ``(idx, discontinuity_kt)``. Discontinuity = |mean current after - before|."""
    cur_e, cur_n, _ = estimate_current(df, model, params)
    total = 0.0
    per = []
    for idx, pre, post in windows:
        de = float(np.nanmean(cur_e[post]) - np.nanmean(cur_e[pre]))
        dn = float(np.nanmean(cur_n[post]) - np.nanmean(cur_n[pre]))
        total += de * de + dn * dn
        per.append((idx, float(np.hypot(de, dn))))
    return total, per


def fit_leeway_by_tack_symmetry(df, model, windows):
    """Fit a leeway model by minimising cross-tack current discontinuity.

    Returns a dict: ``model``, ``params`` (named), ``loss`` (sum of squares),
    ``rms_kt`` (RMS per-tack discontinuity), ``per_tack``, ``n_tacks``.
    """
    if not windows:
        raise ValueError("no usable tack windows — cannot fit leeway")

    if model == 'none':
        params = ()
    elif model == 'classic':
        res = minimize_scalar(
            lambda K: symmetry_loss(df, 'classic', (K,), windows)[0],
            bounds=(-40.0, 40.0), method='bounded')
        params = (float(res.x),)
    elif model == 'wind':
        res = minimize(
            lambda p: symmetry_loss(df, 'wind', (p[0], p[1]), windows)[0],
            x0=np.array([8.0, 0.0]), method='Nelder-Mead',
            options={'xatol': 1e-3, 'fatol': 1e-6, 'maxiter': 2000})
        params = (float(res.x[0]), float(res.x[1]))
    else:
        raise ValueError(f"unknown model {model!r}")

    total, per = symmetry_loss(df, model, params, windows)
    return {
        'model': model,
        'params': dict(zip(MODEL_PARAM_NAMES[model], params)),
        'params_tuple': params,
        'loss': total,
        'rms_kt': float(np.sqrt(total / len(windows))),
        'per_tack': per,
        'n_tacks': len(windows),
    }


def select_best(fits, rel_margin=0.05):
    """Pick the headline model, preferring the *simpler* one.

    A more complex model is only chosen if it cuts the tack-symmetry loss by
    more than ``rel_margin`` relative to the simpler model it nests. Since
    ``wind`` nests ``classic`` (and both nest ``none``), raw min-loss would
    almost always crown ``wind`` even when its extra wind term is meaningless;
    this keeps the interpretable ``classic`` model as the default unless wind
    dependence genuinely helps.
    """
    order = [m for m in ('none', 'classic', 'wind') if m in fits]
    best = order[0]
    for m in order[1:]:
        if fits[m]['loss'] < fits[best]['loss'] * (1.0 - rel_margin):
            best = m
    return best


def rank_models(df, windows, models=MODELS):
    """Fit every model and return ``{model: fit_dict}`` plus the headline key
    (simpler model preferred — see :func:`select_best`)."""
    fits = {m: fit_leeway_by_tack_symmetry(df, m, windows) for m in models}
    return fits, select_best(fits)


# ---------------------------------------------------------------------------
# Convenience: current speed/direction helpers
# ---------------------------------------------------------------------------
def current_speed_dir(cur_e, cur_n):
    """Return ``(speed_kt, set_deg_true)`` — set is the direction the current
    flows TOWARD, compass convention."""
    speed = np.hypot(cur_e, cur_n)
    set_deg = (np.degrees(np.arctan2(cur_e, cur_n))) % 360.0
    return speed, set_deg
