#!/usr/bin/env python
"""Stage A of the .slog v2 plan: compare smoothing candidates against raw + v1 on
a day's worth of parquet data, centered on detected tacks.

Run:
    conda run -n sailing --no-capture-output python Analyze/slog_experiments.py --date 2026-04-19

Outputs under Analyze/Plots/slog_experiments/YYYYMMDD/:
    <channel>/tack_NN.png       overlaid smoothers, ±N s around each tack
    summary.csv + summary.md    noise floor + settle time per (channel, candidate)
"""
import argparse
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from Libraries import race_logs
from Libraries import process as pr

FS = 10  # Hz — matches process.butterworth_filter's hardcoded sampling rate


# ---- smoothers --------------------------------------------------------------

def _filtfilt_scalar(cutoff, order=3):
    coeff = pr.butterworth_filter(cutoff, order)
    return lambda sig: pr.smooth(coeff, sig, causal=False)


def _filtfilt_angle(cutoff, order=3):
    coeff = pr.butterworth_filter(cutoff, order)
    return lambda sig: pr.smooth_angle(coeff, sig, causal=False)


def _adaptive_scalar(alpha, max_err):
    def f(sig):
        out, _ = pr.exponential_filter(np.asarray(sig, dtype=float), alpha, max_err)
        return out
    return f


def _adaptive_angle(alpha, max_err):
    def f(sig):
        out, _ = pr.exponential_filter_angle(np.asarray(sig, dtype=float), alpha, max_err)
        return out
    return f


def _composite_scalar(filtfilt_cutoff, adaptive_alpha, adaptive_max_err, order=3):
    """filtfilt base + adaptive filter applied to the residual, added back.

    Zero-phase where the signal is steady; adaptive sharpness on transients.
    Only defined for scalar channels — angle residual addition is wrap-sensitive.
    """
    base = _filtfilt_scalar(filtfilt_cutoff, order)
    resid = _adaptive_scalar(adaptive_alpha, adaptive_max_err)

    def f(sig):
        sig = np.asarray(sig, dtype=float)
        b = base(sig)
        r = resid(sig - b)
        return b + r
    return f


# ---- channel table ----------------------------------------------------------
# (display name, parquet raw column, parquet v1-smoothed column, kind, v1_note,
#  extra_refs: list of (label, parquet_col) plotted as additional dashed reference
#  curves — useful when "raw" isn't actually sensor-raw, e.g. twd/tws where the
#  "raw" column is the boat's on-board NMEA true-wind computation.)
CHANNELS = [
    ('spd',        'rspd',       'spd',        'scalar', None,                   []),
    ('sog',        'rsog',       'sog',        'scalar', None,                   []),
    ('aws',        'raws',       'aws',        'scalar', None,                   []),
    # For tws: raw=on-boat rtws (NMEA). v1=estimate_true_wind(ε=1e-4).
    # stws=estimate_true_wind(ε=1e-5), the heavier Python estimate.
    ('tws',        'rtws',       'tws',        'scalar', 'on-boat rtws vs py est', [('stws (ε=1e-5)', 'stws')]),
    ('zg100_roll', 'zg100_roll', 'zg100_roll', 'scalar', 'no v1 smoother (raw)', []),
    ('hdg',        'rhdg',       'hdg',        'angle',  None,                   []),
    ('cog',        'rcog',       'cog',        'angle',  None,                   []),
    ('awa',        'rawa',       'awa',        'angle',  None,                   []),
    # For twd: same story as tws — raw=on-boat rtwd (uses stale boat variation),
    # v1=estimate_true_wind(ε=1e-4), stwd=ε=1e-5.
    ('twd',        'rtwd',       'twd',        'angle',  'on-boat rtwd vs py est', [('stwd (ε=1e-5)', 'stwd')]),
]


def candidates_for(kind, raw):
    """Return ordered dict of candidate_name -> smoothed array.

    Narrowed to filtfilt 0.2/0.3/0.4 Hz based on Stage-A-round-1 finding that
    these are the interesting band; dropped composite and the too-loose 0.5/1.0
    comparisons. Kept adaptive as a reference for the causal option.
    """
    if kind == 'scalar':
        return {
            'raw':                        np.asarray(raw, dtype=float),
            'filtfilt 0.2 Hz':            _filtfilt_scalar(0.2, 3)(raw),
            'filtfilt 0.3 Hz':            _filtfilt_scalar(0.3, 3)(raw),
            'filtfilt 0.4 Hz':            _filtfilt_scalar(0.4, 3)(raw),
            'adaptive α=0.9 me=0.3':      _adaptive_scalar(0.9, 0.3)(raw),
        }
    else:
        return {
            'raw':                        np.asarray(raw, dtype=float),
            'filtfilt 0.2 Hz':            _filtfilt_angle(0.2, 3)(raw),
            'filtfilt 0.3 Hz':            _filtfilt_angle(0.3, 3)(raw),
            'filtfilt 0.4 Hz':            _filtfilt_angle(0.4, 3)(raw),
            'adaptive α=0.9 me=3°':       _adaptive_angle(0.9, 3.0)(raw),
        }


# ---- tack detection ---------------------------------------------------------

def detect_tacks(df, min_spacing_s=45, min_spd_kt=3.0):
    """Return row indices of tack centers.

    A tack is identified by three conjoint conditions:
      1) raw awa (`rawa`) sign change
      2) unwrapped heading range over a ±10 s window > 60°
      3) median boat speed over a ±30 s window ≥ `min_spd_kt` (skip drifting)

    Both make a proper tack/gybe as opposed to a simple awa nudge.
    """
    awa = df['rawa'].values
    hdg = df['rhdg'].values
    spd = df['rspd'].values if 'rspd' in df.columns else df['spd'].values
    sign = np.sign(awa)
    sign[sign == 0] = 1
    flips = np.where(np.diff(sign) != 0)[0] + 1

    min_spacing = int(min_spacing_s * FS)
    hdg_win = int(10 * FS)
    spd_win = int(30 * FS)

    good = []
    last = -min_spacing
    for i in flips:
        if i - last < min_spacing:
            continue
        lo = max(0, i - int(3 * FS))
        hi = min(len(awa), i + int(3 * FS))
        if np.max(np.abs(awa[lo:i])) < 20 or np.max(np.abs(awa[i:hi])) < 20:
            continue
        lo2 = max(0, i - hdg_win)
        hi2 = min(len(hdg), i + hdg_win)
        if np.degrees(np.ptp(np.unwrap(np.radians(hdg[lo2:hi2])))) < 60:
            continue
        lo3 = max(0, i - spd_win)
        hi3 = min(len(spd), i + spd_win)
        if np.median(spd[lo3:hi3]) < min_spd_kt:
            continue
        good.append(i)
        last = i
    return np.array(good, dtype=int)


# ---- metrics ----------------------------------------------------------------

def straight_leg(tacks, n_samples):
    """Pick a straight-leg window far from the tack set, for noise-floor estimation."""
    if len(tacks) >= 2:
        mid = (tacks[0] + tacks[-1]) // 2
        # find the nearest tack to this midpoint, then step 15 s away
        offset = int(15 * FS)
        lo = max(0, mid - int(10 * FS))
        hi = min(n_samples, mid + int(10 * FS))
        return lo, hi
    return n_samples // 2 - 100, n_samples // 2 + 100


def _unwrap_to_reference(sig_deg, ref_deg):
    """Unwrap `sig_deg` onto the same branch as `ref_deg`.

    `np.unwrap` alone picks an arbitrary branch per-signal, so two independently-
    unwrapped variants of the same channel can differ by a 360° offset and plot
    as parallel curves 360° apart. We anchor `sig`'s branch to `ref`'s by
    unwrapping both, then shifting `sig_un` by the nearest 360° multiple so its
    first sample is within ±180° of `ref_un`'s first sample.
    """
    ref_un = np.degrees(np.unwrap(np.radians(np.asarray(ref_deg, dtype=float))))
    sig_un = np.degrees(np.unwrap(np.radians(np.asarray(sig_deg, dtype=float))))
    k = round((ref_un[0] - sig_un[0]) / 360.0)
    return sig_un + 360.0 * k


def output_roughness(sig, lo, hi):
    """σ of first difference over a straight leg — how jittery the displayed curve is."""
    return float(np.std(np.diff(sig[lo:hi])))


def raw_to_smoothed_lag_s(raw, smoothed, tack_idx, win_s=15, max_lag_s=5.0, is_angle=False):
    """Cross-correlate a ±win_s window around the tack; return the lag (s) at which
    the smoothed signal best matches raw. Positive = smoothed trails raw.

    For angle channels, use np.unwrap before correlating so wraps don't corrupt it.
    """
    win = int(win_s * FS)
    a = max(0, tack_idx - win)
    b = min(len(raw), tack_idx + win)
    r = raw[a:b].astype(float)
    s = smoothed[a:b].astype(float)
    if is_angle:
        r = np.degrees(np.unwrap(np.radians(r)))
        s = np.degrees(np.unwrap(np.radians(s)))
    r = r - np.mean(r)
    s = s - np.mean(s)
    if np.std(r) < 1e-6 or np.std(s) < 1e-6:
        return np.nan
    max_lag = int(max_lag_s * FS)
    corr = np.correlate(r, s, mode='full')  # idx 0 corresponds to lag = -(len(r)-1)
    mid = len(r) - 1
    win_corr = corr[mid - max_lag: mid + max_lag + 1]
    best = int(np.argmax(win_corr)) - max_lag
    return best / FS  # positive -> smoothed lags raw


# ---- main -------------------------------------------------------------------

def _find_parquet(date_str):
    pl = '/Users/viola/Data/SailingData/PandasLogs'
    matches = sorted(f for f in os.listdir(pl)
                     if f.startswith(date_str) and f.endswith('.parquet'))
    if not matches:
        print(f'ERROR: no parquet matching {date_str} in {pl}', file=sys.stderr)
        sys.exit(2)
    return os.path.join(pl, matches[0])


def _write_summary_md(path, date_str, parquet, n_tacks, summary):
    lines = [
        f'# Stage A smoothing experiments — {date_str}',
        '',
        f'Parquet: `{parquet}`',
        f'Tacks auto-detected (≥45 s spacing, ≥60° heading turn, ≥3 kt median spd): **{n_tacks}**',
        '',
        'Metrics:',
        '- `out_roughness`: σ of the smoothed channel\'s first difference on a 20 s straight-leg '
        'window far from any tack. Measures how jumpy the displayed line is. '
        'For reference, `raw` shows the raw channel\'s roughness.',
        '- `median_lag_s`: median lag (in seconds) between smoothed and raw inside a ±15 s '
        'window centered on each tack, found by argmax of cross-correlation within ±5 s. '
        'Positive = smoothed trails raw (old signal). filtfilt should be ≈0.',
        '',
    ]
    for ch in summary['channel'].unique():
        lines.append(f'## {ch}')
        lines.append('')
        sub = summary[summary.channel == ch][
            ['candidate', 'out_roughness', 'median_lag_s']]
        lines.append('| candidate | out_roughness | median_lag_s |')
        lines.append('|---|---:|---:|')
        for _, r in sub.iterrows():
            lag = f'{r.median_lag_s:+.2f}' if not np.isnan(r.median_lag_s) else 'n/a'
            lines.append(f'| {r.candidate} | {r.out_roughness:.3f} | {lag} |')
        lines.append('')
    Path(path).write_text('\n'.join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', required=True, help='YYYY-MM-DD')
    ap.add_argument('--parquet', default=None, help='Override parquet path')
    ap.add_argument('--out', default=None, help='Output directory (default under Analyze/Plots/)')
    ap.add_argument('--limit-tacks', type=int, default=8)
    ap.add_argument('--window-s', type=float, default=60.0)
    args = ap.parse_args()

    parquet = args.parquet or _find_parquet(args.date)
    print(f'Loading {parquet}')
    df = race_logs.read_log_file(parquet)
    print(f'Loaded {df.shape[0]} rows, {df.shape[1]} cols')

    tacks_all = detect_tacks(df)
    print(f'Detected {len(tacks_all)} tacks')
    if len(tacks_all) == 0:
        print('No tacks detected; nothing to plot.', file=sys.stderr)
        sys.exit(3)
    if len(tacks_all) > args.limit_tacks:
        sel = np.linspace(0, len(tacks_all) - 1, args.limit_tacks).astype(int)
        tacks = tacks_all[sel]
        print(f'  sampled to {len(tacks)} evenly spaced')
    else:
        tacks = tacks_all

    ymd = args.date.replace('-', '')
    out_dir = args.out or str(REPO / 'Analyze' / 'Plots' / 'slog_experiments' / ymd)
    os.makedirs(out_dir, exist_ok=True)

    summary_rows = []

    for (name, raw_col, v1_col, kind, v1_note, extra_refs) in CHANNELS:
        ch_dir = os.path.join(out_dir, name)
        os.makedirs(ch_dir, exist_ok=True)

        raw_full = df[raw_col].values.astype(float)
        v1_full = df[v1_col].values.astype(float)
        extra_ref_data = [(label, df[col].values.astype(float))
                          for (label, col) in (extra_refs or [])
                          if col in df.columns]

        cands = candidates_for(kind, raw_full)
        # Inject v1 in-place after 'raw' so it plots in the second slot
        v1_label = 'v1' + (f' ({v1_note})' if v1_note else '')
        items = list(cands.items())
        cands = dict(items[:1] + [(v1_label, v1_full)] + items[1:])

        leg_lo, leg_hi = straight_leg(tacks, len(raw_full))
        is_angle = (kind == 'angle')

        for cand_name, cand_sig in cands.items():
            roughness = output_roughness(cand_sig, leg_lo, leg_hi)
            lags = [raw_to_smoothed_lag_s(raw_full, cand_sig, t, is_angle=is_angle)
                    for t in tacks]
            lags = [l for l in lags if not np.isnan(l)]
            med_lag = float(np.median(lags)) if lags else np.nan
            summary_rows.append(dict(
                channel=name, candidate=cand_name,
                out_roughness=roughness,
                median_lag_s=med_lag,
            ))

        # ---- plots ----
        win = int(args.window_s * FS)
        for ti_idx, ti in enumerate(tacks):
            a = max(0, ti - win // 2)
            b = min(len(raw_full), ti + win // 2)
            t_s = (np.arange(a, b) - ti) / FS

            # For angle channels, unwrap within the plot window using the raw
            # signal's phase continuation as the reference so every candidate
            # stays on the same branch (no wrap artifacts in display).
            def _display(sig):
                if not is_angle:
                    return sig[a:b]
                # unwrap raw, then snap each other curve to the same branch
                return _unwrap_to_reference(sig[a:b], raw_full[a:b])

            fig, (ax1, ax2) = plt.subplots(
                2, 1, sharex=True, figsize=(11, 7),
                gridspec_kw={'height_ratios': [3, 1]})
            for cand_name, cand_sig in cands.items():
                disp = _display(cand_sig)
                if cand_name == 'raw':
                    ax1.plot(t_s, disp, label=cand_name,
                             linewidth=1.0, alpha=0.35, color='0.35')
                    continue
                lw = 2.0 if cand_name.startswith('v1') else 1.3
                ax1.plot(t_s, disp, label=cand_name, linewidth=lw, alpha=0.9)
                ax2.plot(t_s, disp - _display(raw_full),
                         label=cand_name, linewidth=0.9, alpha=0.85)
            # Extra reference curves (e.g. stwd/stws) plotted as dashed black
            for ref_label, ref_sig in extra_ref_data:
                disp_ref = _display(ref_sig)
                ax1.plot(t_s, disp_ref, label=ref_label,
                         linewidth=1.3, linestyle='--', color='k', alpha=0.7)
                ax2.plot(t_s, disp_ref - _display(raw_full),
                         label=ref_label, linewidth=0.9, linestyle='--',
                         color='k', alpha=0.7)
            ax1.axvline(0, color='k', linestyle=':', alpha=0.6)
            ax1.set_title(f'{name} — tack at {df.row_times.iloc[ti]}')
            ax1.set_ylabel(name + (' (unwrapped)' if is_angle else ''))
            ax1.legend(loc='upper right', fontsize=8, ncol=2)
            ax1.grid(True, alpha=0.3)
            ax2.axvline(0, color='k', linestyle=':', alpha=0.6)
            ax2.axhline(0, color='k', linewidth=0.5, alpha=0.5)
            ax2.set_ylabel('smoothed − raw')
            ax2.set_xlabel('seconds from tack')
            ax2.grid(True, alpha=0.3)
            fig.tight_layout()
            fig.savefig(os.path.join(ch_dir, f'tack_{ti_idx:02d}.png'), dpi=110)
            plt.close(fig)
        print(f'  {name}: wrote {len(tacks)} tack plots')

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(os.path.join(out_dir, 'summary.csv'), index=False)
    _write_summary_md(os.path.join(out_dir, 'summary.md'),
                      args.date, parquet, len(tacks), summary)
    print(f'Wrote {out_dir}/summary.csv and summary.md')


if __name__ == '__main__':
    main()
