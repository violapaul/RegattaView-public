"""Build a pooled cross-tack dataset over ALL sailing days for global leeway fitting.

For every parquet in the logs dir we detect upwind tacks and, for each, extract a
short steady window just *before* and just *after* the maneuver. We store the
per-sample signals **pre-calibration** (smoothed m/s speeds + raw heel + magnetic
heading) so the downstream fitter can apply any calibration — including jointly
fitting `stw_scale` / `heading_offset` — without re-extracting.

Outputs (under --out, default Analyze/TackData/):
  tack_samples.parquet : one row per (tack, side, downsampled-sample)
  tack_index.parquet   : one row per tack (day, conditions, fold key)

Run:
  conda run -n sailing python Analyze/build_tack_dataset.py            # all days
  conda run -n sailing python Analyze/build_tack_dataset.py --limit 5  # quick test
"""
import argparse
import glob
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import numpy as np
import pandas as pd

from Libraries.global_variables import G
from Libraries.race_logs import read_log_file
from Analyze import current_analysis as ca

_FS = ca._FS
MS_TO_KT = G.MS_2_KNOTS

# Window geometry (seconds): skip a guard band over the turn, then a steady window.
GUARD_S = 15.0
WIN_S = 40.0
DOWNSAMPLE_HZ = 2.0          # store at 2 Hz; current is smooth, 10 Hz is wasteful
SAILING_MIN_KT = 3.0         # both windows must be genuinely sailing (upwind)


def _compass(a):
    return np.asarray(a, float) % 360.0


def extract_session(name, logs_dir):
    """Return (samples_rows, index_rows) for one parquet, or ([],[]) on failure."""
    try:
        df = read_log_file(name, path=logs_dir, discard_columns=False)
    except Exception as e:  # noqa: BLE001
        print(f"  {name}: load failed ({e})", file=sys.stderr)
        return [], []
    if df is None or len(df) < _FS * 600:
        return [], []
    need = ('spd', 'sog', 'hdg', 'cog', 'awa', 'aws', 'zg100_roll',
            'latitude', 'longitude', 'unixtime')
    if any(c not in df.columns for c in need):
        print(f"  {name}: missing columns", file=sys.stderr)
        return [], []

    # Calibrated view only to detect tacks (detection is insensitive to small cal).
    cal = ca.load_calibrated_log(name, path=logs_dir)
    wins = ca.tack_windows(cal, ca.detect_tacks(cal), guard_s=GUARD_S, win_s=WIN_S)
    if not wins:
        return [], []

    # Pre-calibration per-sample signals (smoothed m/s + raw heel + magnetic hdg).
    spd_ms = df['spd'].to_numpy(float)
    sog_ms = df['sog'].to_numpy(float)
    hdg_mag = _compass(df['hdg'].to_numpy(float))
    cog_true = _compass(df['cog'].to_numpy(float))
    heel_raw = df['zg100_roll'].to_numpy(float)
    aws_ms = df['aws'].to_numpy(float)
    awa = df['awa'].to_numpy(float)
    tws_ms = (df['tws'].to_numpy(float) if 'tws' in df.columns
              else np.full(len(df), np.nan))
    lat = df['latitude'].to_numpy(float)
    lon = df['longitude'].to_numpy(float)
    ut = df['unixtime'].to_numpy(float)

    day = os.path.basename(name)[:10]
    dstep = max(1, int(round(_FS / DOWNSAMPLE_HZ)))

    srows, irows = [], []
    for k, (idx, pre, post) in enumerate(wins):
        # quality gate: both windows actually sailing upwind
        if (np.nanmean(spd_ms[pre]) * MS_TO_KT < SAILING_MIN_KT or
                np.nanmean(spd_ms[post]) * MS_TO_KT < SAILING_MIN_KT):
            continue
        tack_id = f"{day}#{k:02d}"
        for side, sl in (('pre', pre), ('post', post)):
            sel = np.arange(sl.start, sl.stop, dstep)
            for i in sel:
                srows.append((tack_id, day, side,
                              ut[i], lat[i], lon[i],
                              spd_ms[i], sog_ms[i], hdg_mag[i], cog_true[i],
                              heel_raw[i], aws_ms[i], awa[i], tws_ms[i]))
        irows.append((tack_id, day,
                      float(ut[idx]), float(lat[idx]), float(lon[idx]),
                      float(np.nanmean(spd_ms[pre]) * MS_TO_KT),
                      float(np.nanmean(np.abs(heel_raw[pre] - (-4.0)))),
                      float(np.nanmean(tws_ms[pre]) * MS_TO_KT) if np.isfinite(tws_ms[pre]).any() else np.nan,
                      float(np.nanmean(np.abs(awa[pre]))),
                      float(np.nanmean(hdg_mag[pre])), float(np.nanmean(hdg_mag[post]))))
    return srows, irows


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument('--logs-dir', default=None)
    ap.add_argument('--out', default=str(Path(__file__).with_name('TackData')))
    ap.add_argument('--limit', type=int, default=0, help='only first N sessions (test)')
    args = ap.parse_args(argv)

    logs_dir = args.logs_dir or ca.default_logs_dir()
    files = sorted(glob.glob(os.path.join(logs_dir, '*.parquet')))
    if args.limit:
        files = files[-args.limit:]
    os.makedirs(args.out, exist_ok=True)

    all_s, all_i = [], []
    for n, p in enumerate(files):
        name = os.path.basename(p)
        s, i = extract_session(name, logs_dir)
        all_s.extend(s); all_i.extend(i)
        print(f"[{n+1}/{len(files)}] {name}: {len(i)} tacks "
              f"(cumulative {len(all_i)})", file=sys.stderr)

    scols = ['tack_id', 'day', 'side', 'unixtime', 'lat', 'lon',
             'spd_ms', 'sog_ms', 'hdg_mag', 'cog_true', 'heel_raw',
             'aws_ms', 'awa', 'tws_ms']
    icols = ['tack_id', 'day', 'unixtime', 'lat', 'lon', 'spd_kt',
             'abs_heel', 'tws_kt', 'abs_awa', 'hdg_pre', 'hdg_post']
    s_df = pd.DataFrame(all_s, columns=scols)
    i_df = pd.DataFrame(all_i, columns=icols)
    s_path = os.path.join(args.out, 'tack_samples.parquet')
    i_path = os.path.join(args.out, 'tack_index.parquet')
    s_df.to_parquet(s_path)
    i_df.to_parquet(i_path)

    print(f"\nWrote {s_path} ({len(s_df)} samples)")
    print(f"Wrote {i_path} ({len(i_df)} tacks across {i_df['day'].nunique()} days)")
    if len(i_df):
        print("\nCondition coverage:")
        for c in ['spd_kt', 'abs_heel', 'tws_kt', 'abs_awa']:
            q = i_df[c].quantile([0.1, 0.5, 0.9]).round(2).tolist()
            print(f"  {c:9s} p10/50/90 = {q}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
