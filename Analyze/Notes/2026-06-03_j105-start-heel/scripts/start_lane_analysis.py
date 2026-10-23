#!/usr/bin/env python
"""Starting-tack performance + lane analysis for a single detected start.

Post-mortem of the *starting tack* (gun -> first tack): boatspeed/heel/pointing
vs the J/105 polar, leeway, current, the wind shift, and — using AIS — where the
one-design fleet was relative to us and whether we held our lane.

Reuses calibration-correct tooling only (no new physics):
  - Analyze.current_analysis.load_calibrated_log / estimate_current / leeway_angle
  - Analyze.polar (J/105 target boatspeed / VMG)
  - Analyze/leeway_model.json (global classic leeway, K fixed at the 1.22 STW cal)

Outputs (into this note's scripts/_out/, gitignored): three diagnostic PNG figures
+ stats.json. These are exploratory — the published note shows fig_sog_compare.png
and fig_creative_leebow.png (from the sibling scripts), not these. stats.json holds
the exact per-phase numbers the note quotes, kept separate so they stay exact.

Read-only w.r.t. source data; writes only under this note's scripts/_out/.
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
from Analyze.current_analysis import load_calibrated_log, estimate_current, current_speed_dir, leeway_angle
from Analyze.race_geometry import offset_en, course_frame, angdiff
from Analyze import polar

# Generated data (AIS slices, day file) lives only in the MAIN checkout, not the worktree.
MAIN_REPO = "/Users/viola/Resilio Sync/Dropbox/Sailboat/Code/RegattaView"

# ---- event parameters -------------------------------------------------------
EVENT_ID   = "2026-06-04T02_56"
PARQUET    = "2026-06-03T17_14.parquet"
# True start (line cross) from the .log row_times at video time=1214.178882.
# NB: events_index.json t_key_unix=1780541780.22 is the AUTO-DETECTED start and is
# 42.7 s EARLY; anchoring on it shifts every window into the pre-start. Use the real one.
GUN_UNIX   = 1780541822.897             # true start, 02:57:02.9 UTC
FIRST_TACK = 1780541918.7175274         # first tack after start (events_index)
WIN_PRE    = 30.0                       # seconds before gun
WIN_POST   = 130.0                      # seconds after gun (first tack ~+96)
K_LEEWAY   = json.load(open(os.path.join(REPO_ROOT, "Analyze", "leeway_model.json")))["leeway"]["K"]
OUTDIR     = os.path.join(HERE, "_out")   # diagnostics only; not in the note, not deployed
os.makedirs(OUTDIR, exist_ok=True)

# ---- load our boat ----------------------------------------------------------
df = load_calibrated_log(PARQUET, calibration="display")
ut = df["unixtime"].to_numpy(float)
m = (ut >= GUN_UNIX - WIN_PRE) & (ut <= GUN_UNIX + WIN_POST)
sub = df[m].copy().reset_index(drop=True)
t = sub["unixtime"].to_numpy(float) - GUN_UNIX     # seconds rel. gun

hdg = sub["hdg_true"].to_numpy(float)
cog = sub["cog_true"].to_numpy(float)
spd = sub["spd_kt"].to_numpy(float)
sog = sub["sog_kt"].to_numpy(float)
heel = sub["heel"].to_numpy(float)
twd = sub["twd_true"].to_numpy(float)
tws = sub["tws_kt"].to_numpy(float)
awa = sub["awa"].to_numpy(float)
aws = sub["aws_kt"].to_numpy(float)
lat = sub["lat"].to_numpy(float)
lon = sub["lon"].to_numpy(float)

twa = angdiff(twd, hdg)                              # signed TWA
abs_twa = np.abs(twa)
# upwind VMG toward the wind (ground frame)
rel_cog_twd = angdiff(cog, twd)
vmg = sog * np.cos(np.radians(rel_cog_twd))
# model leeway and measured (hdg vs cog, includes current)
lam = leeway_angle("classic", (K_LEEWAY,), spd, heel, aws)
meas_drift = angdiff(hdg, cog)                       # +ve = track left of heading
# J/105 polar targets
tgt_spd = np.array([polar.speed_kt(a, w) for a, w in zip(abs_twa, tws)])
tgt_vmg = np.array([polar.upwind_vmgmax_kt(w) for w in tws])

# ---- phase masks (relative to gun) -----------------------------------------
def win(a, b): return (t >= a) & (t < b)
phase = {
    "prestart":  win(-WIN_PRE, 0),
    "early_0_40": win(0, 40),       # slow/pinch phase off the line
    "recovery_40_55": win(40, 55),  # bear-away to rebuild speed
    "settled_55_150": win(55, 150), # powered up, before the tack
    "tack_150_170": win(150, 170),
}
def stats(mask, arr):
    v = arr[mask]
    v = v[np.isfinite(v)]
    if v.size == 0: return dict(n=0)
    return dict(n=int(v.size), min=float(np.min(v)), med=float(np.median(v)),
                mean=float(np.mean(v)), max=float(np.max(v)))

ourstats = {}
for pname, pm in phase.items():
    ourstats[pname] = {c: stats(pm, arr) for c, arr in
                       dict(spd=spd, sog=sog, heel=heel, hdg=hdg, twd=twd, tws=tws,
                            twa=twa, abs_twa=abs_twa, awa=awa, aws=aws, vmg=vmg,
                            tgt_spd=tgt_spd, tgt_vmg=tgt_vmg, lam=lam,
                            meas_drift=meas_drift).items()}

# speed/vmg deficit vs target, per phase
deficit = {}
for pname, pm in phase.items():
    sd = (spd - tgt_spd)[pm]; vd = (vmg - tgt_vmg)[pm]
    sd = sd[np.isfinite(sd)]; vd = vd[np.isfinite(vd)]
    deficit[pname] = dict(spd_def_med=float(np.median(sd)) if sd.size else None,
                          vmg_def_med=float(np.median(vd)) if vd.size else None,
                          spd_pct=float(100*np.median(spd[pm]/tgt_spd[pm])) if pm.any() else None,
                          vmg_pct=float(100*np.median(np.where(tgt_vmg[pm]>0, vmg[pm]/tgt_vmg[pm], np.nan))) if pm.any() else None)

# ---- current on a steady, powered, spd>2.5 sub-window -----------------------
cur_mask = win(55, 150) & (spd > 2.5)
cE, cN, lam_used = estimate_current(sub[cur_mask], model="classic", params=(K_LEEWAY,))
cur_spd, cur_set = current_speed_dir(np.nanmedian(cE), np.nanmedian(cN))
current = dict(speed_kt=float(cur_spd), set_deg_true=float(cur_set),
               note="steady 55-150s window, spd>2.5; scatter-dominated (~0.6 kt RMS vs SSCOFS)")

# ---- AIS fleet --------------------------------------------------------------
ais = json.load(open(os.path.join(MAIN_REPO, "Analyze", "AIS", f"{EVENT_ID}.json")))
day = json.load(open(os.path.join(MAIN_REPO, "Analyze", "AISDays", "2026-06-03.json")))
# day file: per-boat track tuples [lat,lon,ts,sog_kt?]; index sog by mmsi
day_sog = {}
for b in day.get("boats", []):
    tr = b.get("track", [])
    arr = np.array([[p[2], (p[3] if len(p) > 3 and p[3] is not None else np.nan)] for p in tr], float)
    if arr.size: day_sog[str(b.get("mmsi"))] = arr

def our_at(tq):
    i = int(np.argmin(np.abs(ut - tq)))
    return df["lat"].iloc[i], df["lon"].iloc[i], df["hdg_true"].iloc[i], df["twd_true"].iloc[i]

# competitor position in OUR heading-relative frame (fwd toward bow, right to starboard)
SNAP_T = [-20, 0, 30, 60, 90, 115]   # pre-start through the first tack (~+96 s)
fleet = []
for b in ais["boats"]:
    tr = np.array(b["track"], float)   # [lat,lon,ts]
    ts = tr[:, 2]
    rows = []
    for ds in SNAP_T:
        tq = GUN_UNIX + ds
        j = int(np.argmin(np.abs(ts - tq)))
        if abs(ts[j] - tq) > 12:
            rows.append(None); continue
        olat, olon, ohdg, otwd = our_at(tq)
        e, n = offset_en(tr[j, 0], tr[j, 1], olat, olon)
        fwd, right = course_frame(e, n, ohdg)
        rng = float(np.hypot(e, n))
        rows.append(dict(ds=ds, fwd=float(fwd), right=float(right), rng=rng,
                         side=("windward" if right > 0 else "leeward")))
    fleet.append(dict(name=b["name"], mmsi=str(b["mmsi"]),
                      min_dist_m=b.get("min_dist_m"), frame=rows))

# ---- FIGURE 1: timeseries ---------------------------------------------------
fig, ax = plt.subplots(4, 1, figsize=(11, 12), sharex=True)
def marks(a):
    a.axvline(0, color="k", lw=1.2, ls="-", alpha=.7)
    a.axvline(FIRST_TACK - GUN_UNIX, color="purple", lw=1.2, ls="--", alpha=.7)
    a.axvspan(0, 40, color="orange", alpha=.08)
ax[0].plot(t, twd, color="teal"); ax[0].set_ylabel("TWD (°true)")
ax[0].set_title(f"Start {EVENT_ID}  gun=02:56:20Z  (orange=slow phase, dashed=first tack)")
ax[1].axhspan(18, 22, color="green", alpha=.12, label="J/105 target 18–22°")
ax[1].plot(t, np.abs(heel), color="firebrick"); ax[1].set_ylabel("|heel| (°)"); ax[1].legend(loc="upper left", fontsize=8)
ax[2].plot(t, spd, label="SPD (water)", color="navy")
ax[2].plot(t, sog, label="SOG", color="dodgerblue", alpha=.7)
ax[2].plot(t, tgt_spd, label="J/105 target", color="gray", ls=":"); ax[2].set_ylabel("speed (kt)"); ax[2].legend(loc="upper left", fontsize=8)
ax[3].plot(t, vmg, label="upwind VMG", color="darkgreen")
ax[3].plot(t, tgt_vmg, label="target VMG", color="gray", ls=":"); ax[3].set_ylabel("VMG (kt)")
ax[3].set_xlabel("seconds relative to gun"); ax[3].legend(loc="upper left", fontsize=8)
for a in ax: marks(a); a.grid(alpha=.25)
fig.tight_layout(); fig.savefig(os.path.join(OUTDIR, "fig1_timeseries.png"), dpi=110); plt.close(fig)

# ---- FIGURE 2: fleet in our wind/heading frame ------------------------------
fig, axs = plt.subplots(2, 3, figsize=(15, 9), sharex=True, sharey=True)
for k, ds in enumerate(SNAP_T):
    a = axs[k // 3][k % 3]
    a.axhline(0, color="k", lw=.6); a.axvline(0, color="k", lw=.6)
    a.scatter([0], [0], marker="^", s=120, color="black", zorder=5)
    a.annotate("US", (0, 0), textcoords="offset points", xytext=(4, 4), fontweight="bold")
    for b in fleet:
        row = b["frame"][k]
        if row is None: continue
        c = "crimson" if b["name"] == "CREATIVE" else ("navy" if row["right"] > 0 else "darkorange")
        a.scatter([row["right"]], [row["fwd"]], s=45, color=c)
        if row["rng"] < 320:
            a.annotate(b["name"][:6], (row["right"], row["fwd"]),
                       textcoords="offset points", xytext=(3, 3), fontsize=7)
    a.set_title(f"t+{ds}s"); a.grid(alpha=.25)
    if k % 3 == 0: a.set_ylabel("ahead / behind us (m)")
    if k // 3 == 1: a.set_xlabel("← leeward   to-starboard (m)   windward →")
axs[0][0].set_xlim(-260, 260); axs[0][0].set_ylim(-260, 260)
fig.suptitle("Fleet relative to us (heading-relative frame; starboard tack → windward=right)", fontsize=12)
fig.tight_layout(); fig.savefig(os.path.join(OUTDIR, "fig2_fleet_windaxis.png"), dpi=110); plt.close(fig)

# ---- FIGURE 3: our SOG vs nearby fleet SOG ----------------------------------
fig, ax = plt.subplots(figsize=(11, 6))
ax.plot(t, sog, color="black", lw=2.2, label="US (SOG)", zorder=5)
ax.axvline(0, color="k", lw=1); ax.axvspan(0, 40, color="orange", alpha=.08)
near = sorted(fleet, key=lambda b: (b["min_dist_m"] or 9e9))[:6]
for b in near:
    arr = day_sog.get(b["mmsi"])
    if arr is None: continue
    tt = arr[:, 0] - GUN_UNIX; ss = arr[:, 1]
    sel = (tt >= -WIN_PRE) & (tt <= WIN_POST) & np.isfinite(ss)
    if sel.sum() >= 2:
        ax.plot(tt[sel], ss[sel], alpha=.8, label=f"{b['name']} ({b['min_dist_m']:.0f}m)")
ax.set_xlabel("seconds relative to gun"); ax.set_ylabel("SOG (kt)")
ax.set_title("Our SOG vs nearby J/105s off the line"); ax.legend(fontsize=8); ax.grid(alpha=.25)
fig.tight_layout(); fig.savefig(os.path.join(OUTDIR, "fig3_speed_vs_fleet.png"), dpi=110); plt.close(fig)

# ---- shift summary ----------------------------------------------------------
def at(tq, arr):
    i = int(np.argmin(np.abs(t - tq))); return float(arr[i])
shift = dict(twd_gun=at(0, twd), twd_p30=at(30, twd), twd_p60=at(60, twd),
             twd_p150=at(150, twd),
             veer_0_150=float(at(150, twd) - at(0, twd)),
             hdg_gun=at(0, hdg), hdg_p150=at(150, hdg),
             interp="starboard tack; rising TWD = veer = LIFT (heading rises toward mark ~210°)")

# ---- dump stats -------------------------------------------------------------
out = dict(event=EVENT_ID, gun_unix=GUN_UNIX, K_leeway=K_LEEWAY,
           window=[-WIN_PRE, WIN_POST], n_samples=int(m.sum()),
           our_phase_stats=ourstats, deficit=deficit, current=current,
           shift=shift,
           fleet=[dict(name=b["name"], mmsi=b["mmsi"], min_dist_m=b["min_dist_m"],
                       frame=[r for r in b["frame"]]) for b in fleet])
with open(os.path.join(OUTDIR, "stats.json"), "w") as f:
    json.dump(out, f, indent=2)

# ---- console summary --------------------------------------------------------
print(f"=== {EVENT_ID}  start-tack analysis ===")
print(f"window [-{WIN_PRE:.0f},+{WIN_POST:.0f}]s  n={int(m.sum())}  K_leeway={K_LEEWAY:.3f}")
for p in ["prestart", "early_0_40", "recovery_40_55", "settled_55_150"]:
    s = ourstats[p]
    print(f"\n[{p}]")
    print(f"  SPD  med {s['spd']['med']:.1f}  (target {s['tgt_spd']['med']:.1f})  "
          f"def {deficit[p]['spd_def_med']:+.1f} kt ({deficit[p]['spd_pct']:.0f}% of target)")
    print(f"  |heel| med {abs(s['heel']['med']):.1f}   VMG med {s['vmg']['med']:.2f} (target {s['tgt_vmg']['med']:.2f}, {deficit[p]['vmg_pct']:.0f}%)")
    print(f"  |TWA| med {s['abs_twa']['med']:.0f}   AWA med {s['awa']['med']:.0f}   leeway(model) med {abs(s['lam']['med']):.1f}")
print(f"\nSHIFT: TWD {shift['twd_gun']:.0f}->{shift['twd_p150']:.0f} ({shift['veer_0_150']:+.0f}°) "
      f"hdg {shift['hdg_gun']:.0f}->{shift['hdg_p150']:.0f}  => starboard LIFT")
print(f"CURRENT (55-150s): {current['speed_kt']:.2f} kt setting {current['set_deg_true']:.0f}°T")
print("\nFLEET (heading-relative; +right=windward/starboard):")
for b in sorted(fleet, key=lambda x: (x['min_dist_m'] or 9e9)):
    f0 = b["frame"][0]; f5 = b["frame"][-1]
    if f0 and f5:
        print(f"  {b['name']:16s} min {b['min_dist_m']:5.0f}m | t0 fwd{f0['fwd']:+5.0f} rt{f0['right']:+5.0f} ({f0['side']}) "
              f"-> t150 fwd{f5['fwd']:+5.0f} rt{f5['right']:+5.0f} ({f5['side']})")
print(f"\nwrote: {OUTDIR}/fig1_timeseries.png, fig2_fleet_windaxis.png, fig3_speed_vs_fleet.png, stats.json")
