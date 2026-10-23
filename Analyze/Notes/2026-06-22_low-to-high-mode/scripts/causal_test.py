"""Causal test: was the height gain at tau=0 caused by the trim change
(traveler down + sheet on, jib inhaul off -> less twist / more leech / more
load), or by an external factor (lift, pressure, current, or a pure luff)?

Strategy: each hypothesis predicts a DIFFERENT joint signature across channels
that come from independent sensors. Control for pressure by comparing within a
matched TWS band, and lean on the robust channels (HDG=compass, COG=GPS,
heel=IMU, STW=paddle) over the soft masthead ones (TWD/AWA).

  hypothesis            TWS   HDG    TWA/AWA   heel   STW    COG
  ------------------    ----  -----  --------  -----  -----  -----
  lift (windshift)      flat  up     ~const    ~const ~const up
  more pressure         UP    up     aft       UP     UP     up
  current line          flat  ~const ~const    ~const ~const up   (track only)
  pure luff/pinch       flat  up     fwd       DOWN   DOWN   up
  TRIM ON / less twist  flat  up     fwd       UP     ~const up   <- the claim
"""
import os, sys, json, math
from os.path import join, abspath, dirname
import numpy as np

HERE = dirname(abspath(__file__))
REPO_ROOT = abspath(join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, REPO_ROOT)
from Libraries.sailview_log_v2 import read_slog

VIDDIR = "/Users/viola/Data/Insta360/VID"
S8 = join(VIDDIR, "VID_20260622_192548_00_008.slog")
S9 = join(VIDDIR, "VID_20260622_195503_00_009.slog")
TRANS_VIDEO_T = 94.486806


def load(path):
    s = read_slog(path); d = s["data"]; n = s["num_rows"]; mps = s["msec_per_sample"]
    u0 = s["start_unix_ms"] / 1000.0
    return dict(unix=u0 + np.arange(n) * mps / 1000.0, stw=d["spd"] * 1.22, sog=d["sog"],
                awa=d["awa"] + 0.5, aws=d["aws"], heel=d["zg100_roll"] + 4.0, tws=d["tws"],
                hdg=(d["hdg"] - 2.3) % 360, twd=(d["twd"] - 2.3) % 360, cog=d["cog"],
                rud=d["rudder"], u0=u0)

a = load(S8); b = load(S9)
TRANS_UNIX = b["u0"] + TRANS_VIDEO_T
keys = ["unix", "stw", "sog", "awa", "aws", "heel", "tws", "hdg", "twd", "cog", "rud"]
C = {k: np.concatenate([a[k], b[k]]) for k in keys}
o = np.argsort(C["unix"]); C = {k: C[k][o] for k in keys}
tau = C["unix"] - TRANS_UNIX

sang = lambda x: (x + 180) % 360 - 180
def cm(x):
    r = np.radians(x); return math.degrees(math.atan2(np.nanmean(np.sin(r)), np.nanmean(np.cos(r))))

C["twa"] = sang(C["twd"] - C["hdg"])

# ---- fine table right around the transition (within _009, single clip) ----
print("FINE TABLE around tau=0  (all in _009, post speed-build)")
print(" tau   STW  AWA   AWS  heel  TWS   HDG  COG  TWA  rud")
for lo in range(-60, 145, 5):
    m = (tau >= lo) & (tau < lo + 5)
    if m.sum() == 0: continue
    mk = " <=tau0" if lo == 0 else ""
    print(f"{lo:4d}  {C['stw'][m].mean():4.1f} {cm(C['awa'][m]):5.1f} {C['aws'][m].mean():4.1f} "
          f"{C['heel'][m].mean():4.1f} {C['tws'][m].mean():4.1f} {cm(C['hdg'][m]):4.0f} "
          f"{cm(C['cog'][m]):4.0f} {cm(C['twa'][m]):5.1f} {C['rud'][m].mean():4.1f}{mk}")

# ---- matched-window before/after (tight, symmetric, avoid footing burst & late TWS rise) ----
BEF = (tau >= -55) & (tau < -5)      # settled footing plateau, pre-change
AFT = (tau >= 35) & (tau < 95)       # settled high mode, post-change (before late TWS build)
def stat(m):
    return dict(stw=round(float(C['stw'][m].mean()),2), awa=round(cm(C['awa'][m]),1),
                aws=round(float(C['aws'][m].mean()),1), heel=round(float(C['heel'][m].mean()),1),
                tws=round(float(C['tws'][m].mean()),2), hdg=round(cm(C['hdg'][m]),1),
                cog=round(cm(C['cog'][m]),1), twa=round(cm(C['twa'][m]),1),
                twd=round(cm(C['twd'][m]),1), rud_sd=round(float(C['rud'][m].std()),1),
                rud=round(float(C['rud'][m].mean()),1), n=int(m.sum()))
sb, sa = stat(BEF), stat(AFT)
print("\nMATCHED BEFORE  tau[-55,-5):", json.dumps(sb))
print("MATCHED AFTER   tau[35,95):  ", json.dumps(sa))
print("\nDELTA (after-before):")
for k in ['tws','hdg','cog','twa','awa','heel','stw','aws','twd']:
    print(f"   {k:5s}: {sa[k]-sb[k]:+.1f}")

# ---- TWS-controlled: heel & HDG at MATCHED wind speed ----
band = (C['tws'] >= 8.9) & (C['tws'] <= 9.7)
preB = band & (tau >= -90) & (tau < 0)
postB = band & (tau >= 0) & (tau < 110)
print("\nTWS-CONTROLLED (only samples with TWS in [8.9,9.7]):")
print(f"  BEFORE n={preB.sum():4d}  TWS={C['tws'][preB].mean():.2f}  heel={C['heel'][preB].mean():.1f}  "
      f"HDG={cm(C['hdg'][preB]):.1f}  STW={C['stw'][preB].mean():.2f}  COG={cm(C['cog'][preB]):.1f}")
print(f"  AFTER  n={postB.sum():4d}  TWS={C['tws'][postB].mean():.2f}  heel={C['heel'][postB].mean():.1f}  "
      f"HDG={cm(C['hdg'][postB]):.1f}  STW={C['stw'][postB].mean():.2f}  COG={cm(C['cog'][postB]):.1f}")
print(f"  => at ~equal wind: d_heel={C['heel'][postB].mean()-C['heel'][preB].mean():+.1f}  "
      f"d_HDG={cm(C['hdg'][postB])-cm(C['hdg'][preB]):+.1f}  d_STW={C['stw'][postB].mean()-C['stw'][preB].mean():+.2f}")

# ---- lift check: is TWD persistently shifted across tau=0 ? ----
twd_pre = cm(C['twd'][(tau>=-90)&(tau<0)]); twd_post = cm(C['twd'][(tau>=0)&(tau<110)])
print(f"\nLIFT CHECK: TWD before={twd_pre:.1f}  after={twd_post:.1f}  shift={sang(twd_post-twd_pre):+.1f} deg "
      f"(a lift big enough to explain dHDG would need ~ -9 deg here)")

# ---- discrimination figure: higher AND faster at matched wind ----
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
FIGURES = join(HERE, "..", "figures")
band = (C['tws'] >= 8.9) & (C['tws'] <= 9.7)
preB = band & (tau >= -90) & (tau < 0)
postB = band & (tau >= 0) & (tau < 110)
fig, ax = plt.subplots(figsize=(8.4, 6.2))
# y = "height" = -COG so up = pointing higher
ax.scatter(C['stw'][preB], -C['cog'][preB], s=10, c="#9aa0a6", alpha=0.45, label="before (footing)")
ax.scatter(C['stw'][postB], -C['cog'][postB], s=10, c="#1f8a3b", alpha=0.5, label="after (de-twist)")
bx, by = C['stw'][preB].mean(), -cm(C['cog'][preB])
ax_, ay = C['stw'][postB].mean(), -cm(C['cog'][postB])
ax.scatter([bx], [by], s=260, c="#5f6368", edgecolor="k", zorder=5, marker="o")
ax.scatter([ax_], [ay], s=260, c="#137333", edgecolor="k", zorder=5, marker="o")
ax.annotate("", xy=(ax_, ay), xytext=(bx, by),
            arrowprops=dict(arrowstyle="-|>", lw=2.6, color="#cc0000"))
ax.text((bx+ax_)/2+0.02, (by+ay)/2-1.2, "higher  +  faster", color="#cc0000",
        fontsize=11, weight="bold")
ax.set_xlabel("STW  (kt, →  faster)")
ax.set_ylabel("pointing  =  −COG  (deg, ↑  higher / closer to wind)")
ax.set_title("Same true wind (9.3–9.4 kt) and same heel (~13°): after the de-twist the\n"
             "operating point moved UP and RIGHT — we pointed higher AND went faster.",
             fontsize=11.5)
ax.text(0.025, 0.97,
        "What each cause would do here:\n"
        "  • pinch / over-trim  → up-LEFT (higher but slower)\n"
        "  • windshift / lift   → straight UP (same speed)\n"
        "  • more pressure      → needs TWS to rise (it didn't)\n"
        "  • current line       → moves COG only, not STW/heel\n"
        "Observed: UP + RIGHT  ⇒  the sails got more efficient.",
        transform=ax.transAxes, va="top", ha="left", fontsize=8.6, family="monospace",
        bbox=dict(boxstyle="round", fc="#fffbe6", ec="#caa700", alpha=0.95))
ax.grid(True, alpha=0.25)
ax.legend(loc="lower right", fontsize=9)
fig.tight_layout()
fig.savefig(join(FIGURES, "why_trim.png"), dpi=120)
print("wrote why_trim.png")
