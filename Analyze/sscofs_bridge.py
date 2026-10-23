"""Sample the SSCOFS model current along a boat track.

Standalone bridge so the heavy SSCOFS dependency stack (xarray / h5netcdf /
s3fs / boto3 / pyproj) lives in the RaceRouting ``currents`` conda env and never
has to coexist with RegattaView's ``sailing`` env. ``investigate_current.py``
invokes this as a subprocess::

    conda run -n currents python Analyze/sscofs_bridge.py IN.csv OUT.csv

Input CSV  : columns ``lat,lon,unixtime`` (one row per track sample to query;
             the caller should subsample — querying every 10 Hz point is wasteful).
Output CSV : ``lat,lon,unixtime,cur_sscofs_e_kt,cur_sscofs_n_kt`` where the two
             current components are TRUE east/north in **knots**, NaN over land
             or outside the loaded model window.

Exit non-zero (with a message on stderr) if the model data can't be loaded —
e.g. the dated NOAA S3 cycle has aged out — so the caller can degrade to a
triangle-only run.
"""
import csv
import datetime as dt
import os
import sys
from pathlib import Path

import numpy as np

MS_TO_KT = 1.9438444924406046


def _add_racerouting_to_path():
    """Locate the RaceRouting ``race_routing`` package and put it on sys.path.

    Order: ``REGATTA_RACEROUTING_DIR`` env override, then the sibling checkout
    next to this repo (``<Code>/RaceRouting/WaysWaterMoves/race_routing``).
    """
    here = Path(__file__).resolve()
    # worktree or main checkout -> parents[1] is the repo root, parents[2] is <Code>
    code_dir = here.parents[2]
    candidates = [
        os.environ.get('REGATTA_RACEROUTING_DIR'),
        str(code_dir / 'RaceRouting' / 'WaysWaterMoves' / 'race_routing'),
        os.path.expanduser(
            '~/Resilio Sync/Dropbox/Sailboat/Code/RaceRouting/'
            'WaysWaterMoves/race_routing'),
    ]
    for c in candidates:
        if c and (Path(c) / 'routing' / 'sail_routing.py').exists():
            sys.path.insert(0, c)
            return c
    raise SystemExit(
        "sscofs_bridge: could not locate RaceRouting race_routing package. "
        "Set REGATTA_RACEROUTING_DIR to .../RaceRouting/WaysWaterMoves/race_routing")


def main(argv):
    if len(argv) != 3:
        raise SystemExit("usage: sscofs_bridge.py IN.csv OUT.csv")
    in_path, out_path = argv[1], argv[2]

    rows = []
    with open(in_path, newline='') as f:
        for r in csv.DictReader(f):
            rows.append((float(r['lat']), float(r['lon']), float(r['unixtime'])))
    if not rows:
        raise SystemExit("sscofs_bridge: input CSV has no rows")

    lats = np.array([r[0] for r in rows])
    lons = np.array([r[1] for r in rows])
    times = np.array([r[2] for r in rows])

    _add_racerouting_to_path()
    from routing.sail_routing import load_current_field

    t0 = float(times.min())
    t1 = float(times.max())
    depart_utc = dt.datetime.fromtimestamp(t0, tz=dt.timezone.utc)
    duration_hours = int(np.ceil((t1 - t0) / 3600.0)) + 1

    print(f"sscofs_bridge: loading SSCOFS for {depart_utc:%Y-%m-%d %H:%M UTC} "
          f"+{duration_hours}h, {len(rows)} query points", file=sys.stderr)

    cf, transformer, start_time_s, depart_utc_ret = load_current_field(
        depart_dt=depart_utc, duration_hours=duration_hours, use_cache=True)

    # elapsed_s for a query time t (relative to the base/first-loaded frame):
    #   start_time_s = depart_utc - base_frame_utc
    #   elapsed(t)   = (t - depart_utc) + start_time_s
    base_offset = start_time_s  # seconds from base frame to depart_utc

    x, y = transformer.transform(lons, lats)  # always_xy -> (lon, lat) order

    e_kt = np.full(len(rows), np.nan)
    n_kt = np.full(len(rows), np.nan)
    for i in range(len(rows)):
        elapsed = (times[i] - t0) + base_offset
        try:
            u, v = cf.query(float(x[i]), float(y[i]), elapsed_s=elapsed)
        except Exception:  # noqa: BLE001 - per-point robustness
            continue
        if u is None or (isinstance(u, float) and np.isnan(u)):
            continue
        e_kt[i] = float(u) * MS_TO_KT
        n_kt[i] = float(v) * MS_TO_KT

    with open(out_path, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['lat', 'lon', 'unixtime', 'cur_sscofs_e_kt', 'cur_sscofs_n_kt'])
        for i in range(len(rows)):
            w.writerow([f"{lats[i]:.7f}", f"{lons[i]:.7f}", f"{times[i]:.3f}",
                        '' if np.isnan(e_kt[i]) else f"{e_kt[i]:.4f}",
                        '' if np.isnan(n_kt[i]) else f"{n_kt[i]:.4f}"])

    n_ok = int(np.isfinite(e_kt).sum())
    print(f"sscofs_bridge: wrote {out_path} ({n_ok}/{len(rows)} points on water)",
          file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
