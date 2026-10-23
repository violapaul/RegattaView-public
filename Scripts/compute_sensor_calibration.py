#!/usr/bin/env /Users/viola/miniforge3/envs/sailing/bin/python

"""
Compute sensor calibration factors from historical sailing data.

Calibrations computed:
  - SPD (boat speed): through-origin linear scale against SOG
  - Heading (compass): COG-vs-HDG offset, leeway-cancelled across tacks
  - Heel (zg100_roll): zero-offset from stationary periods
  - AWA (apparent wind angle): alignment offset from port/starboard tack symmetry

Output is a JSON file loadable by the sailview JavaScript viewer, which applies
calibrations at display time.

Usage:
    python compute_sensor_calibration.py [--output PATH] [--year YYYY] [--plot]
"""

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd
from scipy.stats import theilslopes

from Libraries.log_helper import LOG
from Libraries.global_variables import G

G.init_seattle()
LOG.set_logging_level('INFO')

import Libraries.insta360 as insta360

MS_2_KNOTS = G.MS_2_KNOTS
DEFAULT_OUTPUT = os.path.join(
    os.path.dirname(__file__), '..', 'Src', 'src', 'assets', 'calibration', 'sensor_calibration.json'
)


def load_data(year=None):
    """Load all parquet files and optionally filter to a specific year."""
    LOG.logger.info("Loading parquet files...")
    df = insta360.read_parquet_files()
    LOG.logger.info(f"Loaded {len(df)} rows total")

    if year and 'row_times' in df.columns:
        df = df[df.row_times.dt.year.isin(year)]
        LOG.logger.info(f"Filtered to year(s) {year}: {len(df)} rows")

    return df


def calibrate_spd(df, window_seconds=60):
    """
    Calibrate boat speed (SPD) against GPS speed over ground (SOG).

    Model is a single LINEAR scale through the origin: ``SOG ≈ scale * SPD``
    (offset forced to 0). The paddlewheel sensor under-reads multiplicatively;
    a pure scale (~1.22) is what four independent estimates converge on, and a
    fixed offset is not identifiable over the narrow ~3-7 kt racing band — see
    Analyze/SPEEDO_CALIBRATION.md. Windowed medians average current out within
    each window. Through-origin OLS is the reported scale; Theil-Sen (affine)
    is kept only as a robustness cross-check in the metadata.

    Returns dict with scale (knots/knot) and offset (=0.0).
    """
    mask = (
        (df.rsog * MS_2_KNOTS > 1.5) &
        (df.rspd * MS_2_KNOTS > 0.5) &
        (df.turn_rate.abs() < 3.0) &
        df.rsog.notna() &
        df.rspd.notna()
    )
    sdf = df[mask].copy()
    LOG.logger.info(f"SPD calibration: {len(sdf)} rows after filtering (of {len(df)} total)")

    if len(sdf) < 100:
        LOG.logger.warning("Not enough data for SPD calibration")
        return {"scale": 1.0, "offset": 0.0, "n_points": 0, "n_windows": 0}

    spd_kts = sdf.rspd.values * MS_2_KNOTS
    sog_kts = sdf.rsog.values * MS_2_KNOTS

    # Group into time windows to reduce autocorrelation and average out current
    samples_per_window = window_seconds * G.SAMPLES_PER_SECOND
    n_windows = len(sdf) // samples_per_window
    if n_windows < 10:
        # Fall back to using all points if not enough windows
        LOG.logger.info("Few time windows, using all filtered points directly")
        x, y = spd_kts, sog_kts
    else:
        # Trim to exact multiple and reshape
        n_use = n_windows * samples_per_window
        x = np.median(spd_kts[:n_use].reshape(n_windows, samples_per_window), axis=1)
        y = np.median(sog_kts[:n_use].reshape(n_windows, samples_per_window), axis=1)
        LOG.logger.info(f"Using {n_windows} time windows of {window_seconds}s each")

    # Through-origin linear scale (offset forced to 0): the physical scale.
    scale = float(np.sum(x * y) / np.sum(x * x))
    # Theil-Sen affine kept only as a robustness cross-check (NOT used as cal).
    ts_slope, ts_intercept, low_slope, high_slope = theilslopes(y, x)

    result = {
        "scale": round(scale, 6),
        "offset": 0.0,
        "n_points": int(len(sdf)),
        "n_windows": int(n_windows) if n_windows >= 10 else 0,
        "theilsen_affine_xcheck": {
            "slope": round(float(ts_slope), 6),
            "intercept_kt": round(float(ts_intercept), 4),
            "slope_ci": [round(float(low_slope), 6), round(float(high_slope), 6)],
        },
    }
    LOG.logger.info(
        f"SPD calibration (linear, through-origin): scale={result['scale']} "
        f"(Theil-Sen affine x-check: slope={ts_slope:.4f}, "
        f"intercept={ts_intercept:.4f} kt)"
    )
    return result


def calibrate_heel(df, sog_threshold_ms=0.15):
    """
    Compute heel (zg100_roll) zero-offset from stationary periods.

    When stationary, median heel should be zero. Any nonzero median is the
    mounting/weight bias.

    Returns dict with offset in degrees.
    """
    mask = (
        (df.rsog < sog_threshold_ms) &
        df.zg100_roll.notna()
    )
    sdf = df[mask]
    LOG.logger.info(f"Heel calibration: {len(sdf)} stationary rows (SOG < {sog_threshold_ms} m/s)")

    if len(sdf) < 50:
        LOG.logger.warning("Not enough stationary data for heel calibration")
        return {"offset": 0.0, "n_points": 0}

    offset = float(sdf.zg100_roll.median())
    std = float(sdf.zg100_roll.std())

    result = {
        "offset": round(offset, 3),
        "std": round(std, 3),
        "n_points": int(len(sdf)),
    }
    LOG.logger.info(f"Heel calibration: offset={result['offset']}° (std={result['std']}°, n={result['n_points']})")
    return result


def _wrap180(a):
    return (a + 180.0) % 360.0 - 180.0


def calibrate_heading(df, heel_offset=-4.0, move_kt=2.5):
    """Compute the compass/heading offset from COG vs HDG via the EVEN/ODD split.

    ``COG_true - HDG_true = compass_offset + leeway + current_drift``. Decompose by
    tack across matched heel: with ``heel = zg100_roll - heel_offset`` (the
    CALIBRATED heel — essential; splitting on raw roll misclassifies near-zero
    samples and fabricates a false heel trend),

        even = 0.5*(median|port + median|stbd) = compass_offset (+ current, ~0 avg)
        odd  = 0.5*(median|port - median|stbd) = leeway

    The EVEN part is heel-INDEPENDENT (the compass offset); the ODD part grows ~linearly
    with heel (leeway). We take the compass offset from LOW heel (|heel|<5), where leeway
    is smallest so the even part is least contaminated, and log the heel-trend so the
    flat-even / rising-odd signature is visible.

    On Peer Gynt the heading we use is the **B&G RC42 rate compass** (canboat routes the
    inferior Airmar ZG100 compass to ``zg100_heading``; ``race_logs`` maps the unprefixed
    RC42 ``heading`` to ``hdg``). The RC42 has a real, heel-independent offset of ≈ -2.3°
    (likely magnetic deviation; WMM variation would predict +0.6°). The ZG100, by
    contrast, is ~8° off — do not use it. See Analyze/HEADING_CALIBRATION.md.

    Returns dict with ``offset`` (deg, added to true heading) plus the even/odd heel-trend.
    """
    heel = df.zg100_roll.to_numpy(float) - heel_offset   # CALIBRATED heel (== roll + 4)
    move = (
        (df.rsog * MS_2_KNOTS > move_kt) &
        (df.turn_rate.abs() < 3.0) &
        df.cog_true.notna() & df.hdg_true.notna() & df.zg100_roll.notna()
    ).to_numpy()
    delta = _wrap180(df.cog_true.to_numpy(float) - df.hdg_true.to_numpy(float))

    def even_odd(mask):
        p = mask & (heel > 0) & np.isfinite(delta)
        s = mask & (heel < 0) & np.isfinite(delta)
        if p.sum() < 200 or s.sum() < 200:
            return float('nan'), float('nan'), int(p.sum()), int(s.sum())
        mp, ms = float(np.median(delta[p])), float(np.median(delta[s]))
        return 0.5 * (mp + ms), 0.5 * (mp - ms), int(p.sum()), int(s.sum())

    ah = np.abs(heel)
    low = move & (ah < 5.0)                 # least leeway: cleanest compass offset
    mid = move & (ah >= 5.0) & (ah < 12.0)
    high = move & (ah >= 12.0)
    e_low, o_low, np_, ns_ = even_odd(low)
    e_mid, o_mid, _, _ = even_odd(mid)
    e_high, o_high, _, _ = even_odd(high)

    if np.isnan(e_low):
        LOG.logger.warning("Not enough low-heel data for heading calibration")
        return {"offset": 0.0, "n_port": np_, "n_starboard": ns_}

    result = {
        "offset": round(e_low, 1),          # heel-independent compass offset (even part)
        "even_compass_deg": {"low_<5": round(e_low, 2), "mid_5_12": round(e_mid, 2),
                             "high_>12": round(e_high, 2)},   # ~flat -> real compass
        "odd_leeway_deg": {"low_<5": round(o_low, 2), "mid_5_12": round(o_mid, 2),
                           "high_>12": round(o_high, 2)},     # grows -> leeway
        "n_port": np_, "n_starboard": ns_,
    }
    LOG.logger.info(
        f"Heading calibration (RC42): offset={result['offset']}° | "
        f"even/compass low/mid/high = {e_low:+.2f}/{e_mid:+.2f}/{e_high:+.2f}° (flat) | "
        f"odd/leeway = {o_low:+.2f}/{o_mid:+.2f}/{o_high:+.2f}° (grows with heel)"
    )
    return result


def calibrate_awa(df, awa_min=20, awa_max=55):
    """
    Compute AWA alignment offset from port/starboard tack symmetry.

    Uses upwind sailing data where signed AWA is in [-awa_max, -awa_min] (port)
    or [awa_min, awa_max] (starboard).  If the sensor has no offset,
    median(port) + median(starboard) ≈ 0.

    Returns dict with offset in degrees.
    """
    mask = (
        (df.rsog * MS_2_KNOTS > 4.5) &
        df.rawa.notna() &
        (df.rawa.abs() >= awa_min) &
        (df.rawa.abs() <= awa_max)
    )
    sdf = df[mask]

    port = sdf[sdf.rawa < 0].rawa
    starboard = sdf[sdf.rawa > 0].rawa

    LOG.logger.info(
        f"AWA calibration (upwind {awa_min}°-{awa_max}°): "
        f"{len(port)} port rows, {len(starboard)} starboard rows"
    )

    if len(port) < 100 or len(starboard) < 100:
        LOG.logger.warning("Not enough upwind tacking data for AWA calibration")
        return {"offset": 0.0, "n_port": 0, "n_starboard": 0}

    median_port = float(port.median())
    median_starboard = float(starboard.median())
    offset = (median_port + median_starboard) / 2.0

    result = {
        "offset": round(offset, 3),
        "median_port": round(median_port, 2),
        "median_starboard": round(median_starboard, 2),
        "n_port": int(len(port)),
        "n_starboard": int(len(starboard)),
    }
    LOG.logger.info(
        f"AWA calibration: offset={result['offset']}° "
        f"(port median={result['median_port']}°, starboard median={result['median_starboard']}°)"
    )
    return result


def generate_plots(df, spd_cal, heel_cal, output_dir):
    """Generate diagnostic plots for the calibration results."""
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt

    os.makedirs(output_dir, exist_ok=True)

    # SPD vs SOG scatter with fitted line
    mask = (
        (df.rsog * MS_2_KNOTS > 1.5) &
        (df.rspd * MS_2_KNOTS > 0.5) &
        (df.turn_rate.abs() < 3.0) &
        df.rsog.notna() & df.rspd.notna()
    )
    sdf = df[mask]
    spd = sdf.rspd.values * MS_2_KNOTS
    sog = sdf.rsog.values * MS_2_KNOTS

    spd_cal_values = spd * spd_cal['scale'] + spd_cal['offset']
    plot_max = np.percentile(np.concatenate([spd, sog, spd_cal_values]), 99.5)
    plot_mask = (spd < plot_max) & (sog < plot_max)
    spd_p, sog_p = spd[plot_mask], sog[plot_mask]
    spd_cal_p = spd_cal_values[plot_mask]
    step = max(1, len(spd_p) // 3000)

    fig, axes = plt.subplots(1, 2, figsize=(14, 6))

    # Left: raw SPD vs SOG
    ax = axes[0]
    ax.scatter(spd_p[::step], sog_p[::step], alpha=0.15, s=4, color='C0')
    x_line = np.array([0, plot_max])
    ax.plot(x_line, x_line, 'k--', alpha=0.5, label='1:1 (ideal)')
    ax.set_xlabel('Raw SPD (kts)')
    ax.set_ylabel('SOG (kts)')
    ax.set_title('Before Calibration')
    ax.legend(loc='upper left')
    ax.set_xlim(0, plot_max * 1.05)
    ax.set_ylim(0, plot_max * 1.05)
    ax.set_aspect('equal')

    # Right: calibrated SPD vs SOG
    ax = axes[1]
    ax.scatter(spd_cal_p[::step], sog_p[::step], alpha=0.15, s=4, color='C1')
    ax.plot(x_line, x_line, 'k--', alpha=0.5, label='1:1 (ideal)')
    ax.set_xlabel('Calibrated SPD (kts)')
    ax.set_ylabel('SOG (kts)')
    ax.set_title(f'After Calibration  (scale={spd_cal["scale"]:.4f}, offset={spd_cal["offset"]:.3f})')
    ax.legend(loc='upper left')
    ax.set_xlim(0, plot_max * 1.05)
    ax.set_ylim(0, plot_max * 1.05)
    ax.set_aspect('equal')

    fig.suptitle('Boat Speed Calibration: SPD vs SOG', fontsize=14)
    fig.tight_layout()
    plot_path = os.path.join(output_dir, 'spd_calibration.png')
    fig.savefig(plot_path, dpi=150)
    plt.close(fig)
    LOG.logger.info(f"Saved SPD plot to {plot_path}")

    # Heel histogram at rest
    mask_rest = (df.rsog < 0.15) & df.zg100_roll.notna()
    heel_rest = df[mask_rest].zg100_roll

    fig, ax = plt.subplots(1, 1, figsize=(8, 5))
    ax.hist(heel_rest, bins=80, edgecolor='black', alpha=0.7)
    ax.axvline(heel_cal['offset'], color='r', linewidth=2,
               label=f"median offset = {heel_cal['offset']:.2f}°")
    ax.axvline(0, color='k', linestyle='--', alpha=0.4)
    ax.set_xlabel('zg100_roll (degrees)')
    ax.set_ylabel('Count')
    ax.set_title('Heel Zero-Offset (stationary data)')
    ax.legend()
    fig.tight_layout()
    fig.savefig(os.path.join(output_dir, 'heel_calibration.png'), dpi=150)
    plt.close(fig)
    LOG.logger.info(f"Saved heel plot to {output_dir}/heel_calibration.png")


def main():
    parser = argparse.ArgumentParser(description='Compute sensor calibration from sailing data')
    parser.add_argument('--output', default=DEFAULT_OUTPUT,
                        help='Output JSON path (default: Src/src/assets/calibration/sensor_calibration.json)')
    parser.add_argument('--year', type=int, nargs='+', default=None,
                        help='Filter data to specific year(s) (e.g. --year 2025 2026)')
    parser.add_argument('--plot', action='store_true',
                        help='Generate diagnostic plots in Calibration/tmp/')
    args = parser.parse_args()

    df = load_data(year=args.year)

    if len(df) == 0:
        LOG.logger.error("No data loaded, aborting")
        sys.exit(1)

    spd_cal = calibrate_spd(df)
    heel_cal = calibrate_heel(df)
    heading_cal = calibrate_heading(df, heel_offset=heel_cal["offset"])
    awa_cal = calibrate_awa(df)

    # Build the output calibration structure.
    # Only include the fields consumers need; diagnostics go to console/plots.
    calibration = {
        "spd": {"scale": spd_cal["scale"], "offset": spd_cal["offset"]},
        "heading": {"offset": heading_cal["offset"]},
        "heel": {"offset": heel_cal["offset"]},
        "awa": {"offset": awa_cal["offset"]},
        "depth": {"offset": 0.0},
    }

    output_path = os.path.normpath(args.output)
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, 'w') as f:
        json.dump(calibration, f, indent=2)
    LOG.logger.info(f"Wrote calibration to {output_path}")
    print(json.dumps(calibration, indent=2))

    if args.plot:
        plot_dir = os.path.join(os.path.dirname(__file__), '..', 'Calibration', 'tmp')
        generate_plots(df, spd_cal, heel_cal, plot_dir)


if __name__ == "__main__":
    main()
