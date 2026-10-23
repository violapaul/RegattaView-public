"""Figures + video snapshots for the 2026-06-22 low-to-high-mode note.

Thin driver: reads the two per-video v2 .slog files directly (the same logs the
viewer renders), stitches them on wall-clock unix, applies the sailview sensor
calibration, and emits the note's figures + three video stills.

Anchored on the user-confirmed transition: _009 @ video t=94.486806 s.
"""
import os, sys, json, subprocess, math
from os.path import join, abspath, dirname
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Patch

HERE = dirname(abspath(__file__))
REPO_ROOT = abspath(join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, REPO_ROOT)
from Libraries.sailview_log_v2 import read_slog

NOTE_DIR = abspath(join(HERE, ".."))
FIGURES = join(NOTE_DIR, "figures")
OUT = join(HERE, "_out")
os.makedirs(FIGURES, exist_ok=True)
os.makedirs(OUT, exist_ok=True)

VIDDIR = "/Users/viola/Data/Insta360/VID"
V8 = join(VIDDIR, "VID_20260622_192548_00_008.mp4")
V9 = join(VIDDIR, "VID_20260622_195503_00_009.mp4")
S8 = join(VIDDIR, "VID_20260622_192548_00_008.slog")
S9 = join(VIDDIR, "VID_20260622_195503_00_009.slog")

TRANS_VIDEO_T = 94.486806   # _009 in-clip seconds (user-confirmed transition)


# ---- load + calibrate (matches viewer360.js::applySensorCalibration) ----
def load(path):
    s = read_slog(path); d = s["data"]; n = s["num_rows"]; mps = s["msec_per_sample"]
    u0 = s["start_unix_ms"] / 1000.0
    return dict(
        unix=u0 + np.arange(n) * mps / 1000.0,
        stw=d["spd"] * 1.22, sog=d["sog"],
        awa=d["awa"] + 0.5, aws=d["aws"],
        heel=d["zg100_roll"] + 4.0, tws=d["tws"],
        hdg=(d["hdg"] - 2.3) % 360, cog=d["cog"], rud=d["rudder"],
        u0=u0,
    )

a = load(S8); b = load(S9)
TRANS_UNIX = b["u0"] + TRANS_VIDEO_T

keys = ["unix", "stw", "sog", "awa", "aws", "heel", "tws", "hdg", "cog", "rud"]
C = {k: np.concatenate([a[k], b[k]]) for k in keys}
order = np.argsort(C["unix"])
C = {k: C[k][order] for k in keys}
tau = C["unix"] - TRANS_UNIX


def sang(x):
    return (x + 180) % 360 - 180


def cm(x):
    r = np.radians(x)
    return math.degrees(math.atan2(np.nanmean(np.sin(r)), np.nanmean(np.cos(r))))


def win(lo, hi):
    return (tau >= lo) & (tau < hi)


# phase windows (tau seconds)
PHASES = [
    ("Low mode\n(losing height)", -540, -300, "#f4cccc"),
    ("Recovery 1\n(breeze fills)", -300, -110, "#fce5cd"),
    ("Build speed", -110, 0, "#fff2cc"),
    ("High + fast\n(height held)", 0, 240, "#d9ead3"),
]

# ---- phase stats for the note ----
stats = {"transition_unix": TRANS_UNIX}
for label, lo, hi, _ in PHASES:
    m = win(lo, hi)
    stats[label.split("\n")[0]] = dict(
        stw=round(float(np.mean(C["stw"][m])), 2),
        sog=round(float(np.mean(C["sog"][m])), 2),
        awa=round(cm(C["awa"][m]), 1),
        aws=round(float(np.mean(C["aws"][m])), 1),
        heel=round(float(np.mean(C["heel"][m])), 1),
        tws=round(float(np.mean(C["tws"][m])), 1),
        hdg=round(cm(C["hdg"][m]), 0),
        cog=round(cm(C["cog"][m]), 0),
        rud_sd=round(float(np.std(C["rud"][m])), 1),
    )
with open(join(OUT, "stats.json"), "w") as f:
    json.dump(stats, f, indent=2)
print(json.dumps(stats, indent=2))


def shade(ax):
    for label, lo, hi, color in PHASES:
        ax.axvspan(lo, hi, color=color, alpha=0.6, lw=0)
    ax.axvline(0, color="#cc0000", lw=1.6, ls="-")
    ax.set_xlim(-560, 240)
    ax.grid(True, alpha=0.25)


# smooth helper for plotting (rolling mean over ~5 s)
def sm(y, k=51):
    if len(y) < k:
        return y
    ker = np.ones(k) / k
    return np.convolve(y, ker, mode="same")


PLOT = win(-560, 240)
xt = tau[PLOT]

# ============ FIG 1: beat timeline (stacked panels) ============
fig, axs = plt.subplots(4, 1, figsize=(10, 11), sharex=True)
fig.suptitle("One upwind beat, 6/22 evening — low-and-low to high-and-fast\n"
             "(Δt = 0 at the trim change you made; red line)", fontsize=13, y=0.965)

ax = axs[0]
ax.plot(xt, sm(C["hdg"][PLOT]), color="#1f4e79", lw=2, label="HDG (heading)")
ax.plot(xt, sm(C["cog"][PLOT]), color="#7fa8d0", lw=1.6, ls="--", label="COG (track)")
ax.invert_yaxis()  # lower heading = higher pointing -> draw "up = higher"
ax.set_ylabel("deg  (↑ = higher / closer to wind)")
ax.legend(loc="upper right", fontsize=8)
ax.set_title("Height: bow comes up ~20° across the beat", fontsize=10, loc="left")
shade(ax)

ax = axs[1]
ax.plot(xt, sm(C["stw"][PLOT]), color="#990000", lw=2, label="STW (speed thru water)")
ax.plot(xt, sm(C["sog"][PLOT]), color="#e06666", lw=1.4, ls="--", label="SOG")
ax.set_ylabel("kt")
ax.legend(loc="upper left", fontsize=8)
ax.set_title("Speed: holds through every height gain, then builds to 5+", fontsize=10, loc="left")
shade(ax)

ax = axs[2]
ax.plot(xt, sm(C["tws"][PLOT]), color="#38761d", lw=2, label="TWS (true wind)")
ax.set_ylabel("kt")
ax.legend(loc="upper left", fontsize=8)
ax.set_title("Pressure: fills 6→9 kt — note it is FLAT across Δt=0", fontsize=10, loc="left")
shade(ax)

ax = axs[3]
ax.plot(xt, sm(C["heel"][PLOT]), color="#674ea7", lw=2, label="heel")
ax.axhspan(11, 13, color="#674ea7", alpha=0.12, lw=0)
ax.set_ylabel("deg")
ax.set_xlabel("Δt  (seconds relative to the trim change)")
ax.legend(loc="upper left", fontsize=8)
ax.set_title("Heel: 8–9° underpowered → settled 11–13° band", fontsize=10, loc="left")
shade(ax)

# phase legend as a figure-level strip along the bottom (doesn't clobber panel legends)
handles = [Patch(facecolor=c, alpha=0.6, label=l.replace("\n", " ")) for l, _, _, c in PHASES]
fig.legend(handles=handles, loc="lower center", fontsize=8.5, ncol=4, framealpha=0.9,
           bbox_to_anchor=(0.5, 0.005))
fig.tight_layout(rect=[0, 0.045, 1, 0.95])
fig.savefig(join(FIGURES, "beat_timeline.png"), dpi=110)
plt.close(fig)
print("wrote beat_timeline.png")

# ============ FIG 2: money figure — height gained at flat wind ============
M = win(-110, 232)   # trim the tack maneuvers that bracket the beat
xm = tau[M]
fig, ax1 = plt.subplots(figsize=(10, 5.2))
ax1.plot(xm, sm(C["hdg"][M]), color="#1f4e79", lw=2.4, label="HDG (heading)")
ax1.invert_yaxis()
ax1.set_ylabel("HDG  (deg, ↑ = pointing higher)", color="#1f4e79")
ax1.tick_params(axis="y", labelcolor="#1f4e79")
ax1.axvline(0, color="#cc0000", lw=1.6)
ax1.set_xlabel("Δt  (seconds relative to the trim change)")
ax1.grid(True, alpha=0.25)

ax2 = ax1.twinx()
ax2.plot(xm, sm(C["tws"][M]), color="#38761d", lw=2.2, ls="--", label="TWS (true wind)")
ax2.set_ylabel("TWS  (kt)", color="#38761d")
ax2.tick_params(axis="y", labelcolor="#38761d")
ax2.set_ylim(7, 12)

ax1.set_title("The tell: bow comes up ~9° (HDG 43°→34°) while the wind stays flat at ~9.3 kt.\n"
              "Height you make at constant pressure is YOURS, not the breeze's.", fontsize=11)
ax1.annotate("trim change", xy=(0, ax1.get_ylim()[1]), xytext=(8, ax1.get_ylim()[1]),
             color="#cc0000", fontsize=9, va="top")
fig.tight_layout()
fig.savefig(join(FIGURES, "height_at_flat_wind.png"), dpi=110)
plt.close(fig)
print("wrote height_at_flat_wind.png")


# ============ video snapshots (corrected dewarp + horizon level) + HUD ======
# Base frame comes from the shared snapshot tool (Src/scripts/snap.mjs), which
# reuses the production fisheye dewarp + per-frame .poses leveling so the photo
# is readable (level horizon, no barrel distortion) — then we overlay the
# instrument HUD on top of that corrected frame.
SNAP = join(REPO_ROOT, "Src", "scripts", "snap.mjs")
SNAP_FOV = 130   # immersive cockpit-wide; see snap.mjs --fov for the range


def snap_frame(video, t_in_clip, outfile, fov=SNAP_FOV, w=1280, h=720):
    """Render a corrected, horizon-leveled base frame via Src/scripts/snap.mjs."""
    env = {**os.environ, "NODE_NO_WARNINGS": "1"}
    subprocess.run(["node", SNAP, "--video", video, "--time", f"{t_in_clip}",
                    "--out", outfile, "--fov", str(fov),
                    "--width", str(w), "--height", str(h)], check=True, env=env)


def hud_vals(dt):
    """±3 s mean of the calibrated channels at Δt seconds, for the overlay."""
    m = (tau >= dt - 3) & (tau < dt + 3)
    return dict(stw=float(np.mean(C["stw"][m])), awa=cm(C["awa"][m]),
                heel=float(np.mean(C["heel"][m])), tws=float(np.mean(C["tws"][m])),
                cog=cm(C["cog"][m]))


def grab(video, t_in_clip, outfile, dt, phase):
    """Render a corrected, horizon-leveled base frame (snap.mjs) and overlay an
    instrument HUD (DejaVu Sans renders 'Δ' and '°' cleanly — ffmpeg drawtext
    does not)."""
    tmp = join(OUT, "tmp_corr.png")
    snap_frame(video, t_in_clip, tmp)
    img = plt.imread(tmp)
    h, w = img.shape[:2]
    fig = plt.figure(figsize=(w / 150, h / 150), dpi=150)
    ax = fig.add_axes([0, 0, 1, 1]); ax.imshow(img); ax.axis("off")
    sign = "+" if dt >= 0 else "−"
    ax.text(0.018, 0.965, f"Δt = {sign}{abs(dt)} s    {phase}", transform=ax.transAxes,
            va="top", ha="left", fontsize=15, color="white", weight="bold",
            bbox=dict(boxstyle="round,pad=0.4", fc="black", alpha=0.55, ec="none"))
    v = hud_vals(dt)
    rows = (f"STW    {v['stw']:.1f} kt\n"
            f"AWA    {v['awa']:+.0f}°\n"
            f"heel   {v['heel']:.0f}°\n"
            f"TWS    {v['tws']:.1f} kt\n"
            f"track  {v['cog']:.0f}°  (height)")
    ax.text(0.018, 0.03, rows, transform=ax.transAxes, va="bottom", ha="left",
            fontsize=13.5, color="white", family="monospace",
            bbox=dict(boxstyle="round,pad=0.5", fc="black", alpha=0.55, ec="none"))
    fig.savefig(outfile, dpi=150, pil_kwargs={"quality": 90})
    plt.close(fig)
    print("wrote", os.path.basename(outfile), f"(Δt={dt:+d}: STW {v['stw']:.1f}, "
          f"track {v['cog']:.0f}°, heel {v['heel']:.0f}°, TWS {v['tws']:.1f})")


# Δt=-200 -> _008 t = TRANS_UNIX-200 - a.u0 ; "after" at Δt=+100 (not +200): by
# +200 s we'd climbed away and the leeward J/80 is out of frame — +100 keeps the
# duel in the shot while still squarely in the high+fast mode.
t8_m200 = (TRANS_UNIX - 200) - a["u0"]
t9_p100 = TRANS_VIDEO_T + 100
grab(V8, t8_m200, join(FIGURES, "snap_before.jpg"), -200, "breeze filling — still low")
grab(V9, TRANS_VIDEO_T, join(FIGURES, "snap_transition.jpg"), 0, "the de-twist")
grab(V9, t9_p100, join(FIGURES, "snap_after.jpg"), 100, "high + fast")

# Hero: the high+fast moment, corrected + a touch wider/higher-res, no HUD.
snap_frame(V9, t9_p100, join(FIGURES, "hero.jpg"), fov=140, w=1600, h=900)
print("wrote hero.jpg")

print("done. t8_m200=%.1f  t9_p100=%.1f" % (t8_m200, t9_p100))
