#!/usr/bin/env python
"""Dead-simple speed comparison on the starting tack: OUR SOG vs CREATIVE's SOG.

SOG is the one thing AIS gives cleanly (COG is often null), and SOG-vs-SOG is the
honest apples-to-apples — both boats are in the same current, so it cancels. Our
STW ('spd') is NOT comparable to their SOG, so we use our SOG too.

Window = true start (02:57:02.9Z, video 1214.18) -> our tack (02:58:55.1Z, video
1326.44), both fixed from the .log row_times. One panel. Read-only except the PNG.
"""
from __future__ import annotations
import json, os, sys
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

HERE = os.path.dirname(os.path.abspath(__file__))
NOTE_DIR = os.path.dirname(HERE)
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, REPO_ROOT)
from Analyze.current_analysis import load_calibrated_log
from Analyze.race_geometry import nearest_row, offset_en, course_frame

# Generated data (AIS slices, day file) lives only in the MAIN checkout, not a worktree.
MAIN_REPO = "/Users/viola/Resilio Sync/Dropbox/Sailboat/Code/RegattaView"
START = 1780541822.897      # video 1214.18
TACK  = 1780541935.105      # video 1326.44  (our tack away)
LOA   = 10.5
FIGURES = os.path.join(NOTE_DIR, "figures"); os.makedirs(FIGURES, exist_ok=True)

df = load_calibrated_log("2026-06-03T17_14.parquet", calibration="display")
ut = df["unixtime"].to_numpy(float)
m = (ut >= START) & (ut <= TACK)
ot = ut[m] - START
osog = df["sog_kt"].to_numpy(float)[m]
oheel = np.abs(df["heel"].to_numpy(float)[m])   # our heel magnitude (AIS has no heel for CREATIVE)

day = json.load(open(os.path.join(MAIN_REPO, "Analyze", "AISDays", "2026-06-03.json")))
slc = json.load(open(os.path.join(MAIN_REPO, "Analyze", "AIS", "2026-06-04T02_56.json")))
cb = next(b for b in day["boats"] if b["name"] == "CREATIVE")
cs = np.array([[p[2], p[3]] for p in cb["track"] if len(p) > 3 and p[3] is not None], float)
sel = (cs[:, 0] >= START - 3) & (cs[:, 0] <= TACK + 3)
cst, css = cs[sel, 0] - START, cs[sel, 1]          # native AIS cadence

# net lead change (along our course) from positions, for one annotation
ct = np.array(next(b for b in slc["boats"] if b["name"] == "CREATIVE")["track"], float)
COURSE = float(np.median([nearest_row(df, ut, START+s)["hdg_true"] for s in range(20,100,5)]))
def lead_bl(t):
    o = nearest_row(df, ut, t)
    e, n = offset_en(np.interp(t, ct[:,2], ct[:,0]), np.interp(t, ct[:,2], ct[:,1]), o.lat, o.lon)
    ahead, _ = course_frame(e, n, COURSE)
    return ahead / LOA
lead0, lead1 = lead_bl(START+10), lead_bl(TACK-5)

fig, ax = plt.subplots(figsize=(11, 5.2))
ax.plot(ot, osog, color="navy", lw=2.2, label=f"US (SOG)  mean {np.nanmean(osog):.1f} kt")
# CREATIVE: only ~4 independent AIS moments (8 fixes in 4 near-duplicate pairs, ~30 s apart).
# Show them as discrete points — NO connecting line, which would fake a continuous history.
ax.scatter(cst, css, color="crimson", s=60, zorder=5, edgecolor="white", linewidth=.6,
           label=f"CREATIVE SOG — {len(css)} AIS fixes ({len(np.unique(np.round(cst/5)))} moments, ~30 s apart)")
ax.set_xlim(0, TACK-START); ax.set_ylim(0, 8)
ax.set_xlabel("seconds from start"); ax.set_ylabel("SOG (kt)")
ax.set_title("Starting tack speed: our SOG vs CREATIVE's SOG (02:57:02.9 → 02:58:55.1 Z)")
ax.axvline(0, color="k", lw=1); ax.text(1, 7.6, "start", fontsize=9)
ax.axvline(TACK-START, color="purple", lw=1.4, ls="--"); ax.text(TACK-START-1, 7.6, "our tack", ha="right", color="purple", fontsize=9)
ax.annotate("CREATIVE = only ~4 AIS moments (~30 s apart): held ~2 BL ahead & to leeward,\n"
            "never broke away. SOG within ~0.5 kt of ours — too sparse to read a speed trend.",
            xy=(0.5, 0.93), xycoords="axes fraction", ha="center", fontsize=9,
            bbox=dict(boxstyle="round", fc="lightyellow", ec="gray"))

# our heel on a right-hand axis (no CREATIVE heel exists in AIS)
axh = ax.twinx()
axh.axhspan(18, 22, color="green", alpha=.08)
axh.plot(ot, oheel, color="seagreen", lw=1.4, alpha=.85,
         label=f"our heel (°)  mean {np.nanmean(oheel):.0f}°")
axh.set_ylabel("our heel magnitude (°)", color="seagreen")
axh.tick_params(axis="y", labelcolor="seagreen"); axh.set_ylim(0, 30)
axh.text(TACK-START-1, 21, "18–22° target", ha="right", va="center", color="green", fontsize=7.5)

l1, b1 = ax.get_legend_handles_labels(); l2, b2 = axh.get_legend_handles_labels()
ax.legend(l1+l2, b1+b2, loc="lower center", fontsize=8.5, ncol=3); ax.grid(alpha=.3)
fig.tight_layout()
out = os.path.join(FIGURES, "fig_sog_compare.png"); fig.savefig(out, dpi=120); plt.close(fig)

print(f"window {TACK-START:.0f}s | our SOG mean {np.nanmean(osog):.2f} med {np.nanmedian(osog):.2f}")
print(f"CREATIVE SOG mean {np.nanmean(css):.2f} med {np.nanmedian(css):.2f}  ({len(css)} AIS pts)")
print(f"CREATIVE lead {lead0:+.2f} -> {lead1:+.2f} BL (net {lead1-lead0:+.2f})")
print("wrote", out)
