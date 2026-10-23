#!/usr/bin/env python
"""CREATIVE lee-bow diagnostic — does the one boat close enough to matter actually
backwind us, and does our own air show it?  Single figure, CREATIVE only.

HONEST ABOUT DATA DENSITY: CREATIVE is AIS only — ~4 independent moments (8 fixes
in 4 near-duplicate pairs, ~30 s apart) across the whole 112 s tack. It is drawn as
DISCRETE DOTS, never a continuous line. Our own boat is 10 Hz, drawn as solid lines.
The visual contrast (a few red dots vs smooth blue/green) is the point: the lee-bow
*position* is robust (4 checks agree), but speed trends from CREATIVE are not readable.

Left  : CREATIVE's position relative to us in BOATLENGTHS at each AIS fix (course
        frame, bow up, leeward left).
Right : our SPD/AWS (continuous, real) — no velocity collapse — with CREATIVE's
        separation and SOG as discrete dots.

True start anchored on the .log row_times (02:57:02.9 UTC). Read-only except the PNG.
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
from Analyze import polar

MAIN_REPO = "/Users/viola/Resilio Sync/Dropbox/Sailboat/Code/RegattaView"
EVENT_ID = "2026-06-04T02_56"
START = 1780541822.897          # true line-cross, from .log row_times @ video t=1214.18
TACK  = 1780541935.105          # our tack away, video t=1326.44
LOA   = 10.5                    # J/105 length (m) -> boatlength unit
FIGURES = os.path.join(NOTE_DIR, "figures")
os.makedirs(FIGURES, exist_ok=True)

df = load_calibrated_log("2026-06-03T17_14.parquet", calibration="display")
ut = df["unixtime"].to_numpy(float)

slc = json.load(open(os.path.join(MAIN_REPO, "Analyze", "AIS", f"{EVENT_ID}.json")))
day = json.load(open(os.path.join(MAIN_REPO, "Analyze", "AISDays", "2026-06-03.json")))
cre = np.array(next(b for b in slc["boats"] if b["name"] == "CREATIVE")["track"], float)  # lat,lon,ts
cre_sog = None
for b in day.get("boats", []):
    if b["name"] == "CREATIVE":
        cre_sog = np.array([[p[2], (p[3] if len(p) > 3 and p[3] is not None else np.nan)]
                            for p in b["track"]], float)

def our(t):
    r = nearest_row(df, ut, t)
    return dict(lat=r.lat, lon=r.lon, hdg=r.hdg_true, spd=r.spd_kt, sog=r.sog_kt,
                awa=r.awa, aws=r.aws_kt, twd=r.twd_true, tws=r.tws_kt)
COURSE = float(np.median([our(START + s)["hdg"] for s in range(20, 110, 5)]))

# --- OUR boat: continuous, real 10 Hz -----------------------------------------
gt = np.arange(0.0, TACK - START + 0.5, 1.0)
o_spd = np.array([our(START + t)["spd"] for t in gt])
o_aws = np.array([our(START + t)["aws"] for t in gt])
tgt = np.array([polar.speed_kt(abs(((o["twd"]-o["hdg"]+180) % 360)-180), o["tws"])
                for o in (our(START + t) for t in gt)])

# --- CREATIVE: ONLY the real AIS fixes (sparse!) ------------------------------
fixsel = (cre[:, 2] >= START - 1) & (cre[:, 2] <= TACK + 1)
cfix = cre[fixsel]                      # lat,lon,ts
crel, cahead, clat, crng, csog = [], [], [], [], []
for la, lo, ts in cfix:
    o = our(ts)
    e, n = offset_en(la, lo, o["lat"], o["lon"])
    ahead, stbd = course_frame(e, n, COURSE)
    crel.append(ts - START); cahead.append(ahead/LOA); clat.append(stbd/LOA); crng.append(np.hypot(e, n)/LOA)
    csog.append(np.interp(ts, cre_sog[:, 0], cre_sog[:, 1]) if cre_sog is not None else np.nan)
crel, cahead, clat, crng, csog = map(np.array, (crel, cahead, clat, crng, csog))
NMOM = len(np.unique(np.round(crel / 5)))     # ~4 distinct moments

fig = plt.figure(figsize=(14, 6.2))

# ---- left: course-frame map in boatlengths, CREATIVE = discrete fixes --------
ax = fig.add_subplot(1, 2, 1)
ax.set_aspect("equal")
ax.scatter([0], [0], marker="^", s=320, color="black", zorder=6)
ax.annotate("US", (0, 0), textcoords="offset points", xytext=(7, 2), fontweight="bold", fontsize=11)
wfrom = float(our(START+60)["twd"]) - COURSE
wx, wy = np.sin(np.radians(wfrom)), np.cos(np.radians(wfrom))
ax.annotate("", xy=(3.0-wx*1.4, 3.0-wy*1.4), xytext=(3.0, 3.0),
            arrowprops=dict(arrowstyle="-|>", color="steelblue", lw=2))
ax.annotate(f"wind\n({our(START+60)['twd']:.0f}°)", (3.0, 3.2), color="steelblue", fontsize=9, ha="center")
for r in (1, 2, 3):
    ax.add_patch(plt.Circle((0, 0), r, fill=False, ls=":", color="gray", alpha=.5))
sc = ax.scatter(clat, cahead, c=crel, cmap="viridis", s=160, zorder=5,
                edgecolor="crimson", linewidth=1.6)
for x, y, rs in zip(clat, cahead, crel):
    ax.annotate(f"+{rs:.0f}s", (x, y), textcoords="offset points", xytext=(7, 4), fontsize=8, color="crimson")
ax.annotate(f"CREATIVE — {len(cfix)} AIS fixes\n(~{NMOM} moments, ~30 s apart)\nevery one: ~2 BL ahead & to leeward",
            (np.median(clat), np.median(cahead)), textcoords="offset points",
            xytext=(40, -78), fontsize=9, color="crimson", ha="left",
            arrowprops=dict(arrowstyle="->", color="crimson"))
ax.axhline(0, color="k", lw=.6); ax.axvline(0, color="k", lw=.6)
ax.set_xlim(-4, 4); ax.set_ylim(-4, 4)
ax.set_xlabel("← leeward      lateral (boatlengths)      windward →")
ax.set_ylabel("← astern      along our course (boatlengths)      ahead →")
ax.set_title("Where CREATIVE was, at each AIS fix (boatlengths; us at origin)")
cb = fig.colorbar(sc, ax=ax, fraction=.046, pad=.04); cb.set_label("seconds from start")

# ---- right: our continuous air/speed vs CREATIVE's sparse dots ----------------
ax2 = fig.add_subplot(1, 2, 2)
ax2.plot(crel, cahead, color="crimson", lw=.8, ls=":", alpha=.5)          # faint guide only
ax2.scatter(crel, cahead, color="crimson", s=55, label="CREATIVE ahead of us (BL)", zorder=5)
ax2.plot(crel, -clat, color="darkorange", lw=.8, ls=":", alpha=.5)
ax2.scatter(crel, -clat, color="darkorange", s=55, marker="s", label="CREATIVE to leeward (BL)", zorder=5)
ax2.axhline(0, color="k", lw=.5)
ax2.set_ylabel("CREATIVE separation (boatlengths) — AIS dots only"); ax2.set_ylim(0, 5)
ax2.set_xlabel("seconds from start")
ax2.axvline(0, color="k", lw=1); ax2.axvline(TACK-START, color="purple", lw=1.2, ls="--")
ax2.annotate("our tack", (TACK-START, 4.6), color="purple", fontsize=8, ha="right")
ax3 = ax2.twinx()
ax3.plot(gt, o_spd, color="navy", lw=2, label="our SPD (kt, 10 Hz)")
ax3.plot(gt, tgt, color="navy", lw=1, ls=":", label="J/105 target SPD")
ax3.plot(gt, o_aws, color="seagreen", lw=1.6, label="our AWS (kt, 10 Hz)")
ax3.scatter(crel, csog, color="crimson", marker="x", s=55, label="CREATIVE SOG (kt, AIS)", zorder=5)
ax3.set_ylabel("our speed / apparent wind (kt) — continuous"); ax3.set_ylim(0, 22)
l1, b1 = ax2.get_legend_handles_labels(); l2, b2 = ax3.get_legend_handles_labels()
ax2.legend(l1+l2, b1+b2, fontsize=7.5, loc="upper center", ncol=2)
ax2.set_title("Our air (lines) steady = no gassing · CREATIVE (dots) too sparse for trends")
ax2.grid(alpha=.25)

fig.suptitle("CREATIVE lee-bow check — 2026-06-04 start tack (true start 02:57:02.9Z) · CREATIVE = AIS dots only",
             fontsize=12)
fig.tight_layout()
out = os.path.join(FIGURES, "fig_creative_leebow.png")
fig.savefig(out, dpi=120); plt.close(fig)

print(f"COURSE axis {COURSE:.0f}°  |  CREATIVE AIS fixes in tack: {len(cfix)} ({NMOM} moments)")
print("  rel(s):", np.round(crel, 0).tolist())
print(f"  ahead BL: {np.round(cahead,1).tolist()}  leeward BL: {np.round(-clat,1).tolist()}")
print(f"  CREATIVE SOG at fixes: {np.round(csog,1).tolist()}  | our SPD median {np.median(o_spd):.1f} (target {np.median(tgt):.1f}), AWS {np.median(o_aws):.1f}")
print("wrote", out)
