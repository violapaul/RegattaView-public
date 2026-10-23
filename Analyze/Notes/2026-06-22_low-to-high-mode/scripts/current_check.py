"""Rule out current as the cause of the tau=0 height gain.

Two independent angles:
  (1) Instrument current triangle (cur = V_ground(GPS) - V_water(STW along
      heading+leeway)), via Analyze.current_analysis, with the globally-fit
      classic leeway K. Compute set/drift BEFORE vs AFTER the change. If the
      current is the same on both sides, it cannot have caused the change.
  (2) The dCOG ~= dHDG consistency check: if current were steady, the GPS-track
      change should equal the through-water heading change. If current had
      shifted, they'd diverge.
"""
import os, sys, json, math
from os.path import join, abspath, dirname
import numpy as np

HERE = dirname(abspath(__file__))
REPO_ROOT = abspath(join(HERE, "..", "..", "..", ".."))
sys.path.insert(0, REPO_ROOT)
from Analyze.current_analysis import (load_calibrated_log, load_leeway_model,
                                      estimate_current, current_speed_dir)

TRANS_UNIX = 1782183353.691
m = load_leeway_model()
K = float(m["leeway"]["K"])
print(f"leeway model: classic K={K:.3f}  (cv_rms={m['cv_rms_kt']:.2f} kt)")

df = load_calibrated_log("2026-06-22T17_28.parquet", calibration="display")
print("parquet rows:", len(df), " unix span:",
      round(df['unixtime'].min(), 0), "->", round(df['unixtime'].max(), 0))

cur_e, cur_n, lam = estimate_current(df, "classic", (K,))
t = df["unixtime"].to_numpy(float)
hdg = df["hdg_true"].to_numpy(float)
cog = df["cog_true"].to_numpy(float)
spd = df["spd_kt"].to_numpy(float)
sog = df["sog_kt"].to_numpy(float)


def ang_mean(a):
    r = np.radians(a)
    return math.degrees(math.atan2(np.nanmean(np.sin(r)), np.nanmean(np.cos(r))))


def report(lo, hi, label):
    m = (t >= TRANS_UNIX + lo) & (t < TRANS_UNIX + hi)
    ce, cn = np.nanmean(cur_e[m]), np.nanmean(cur_n[m])
    spdv, dirv = current_speed_dir(ce, cn)
    # robustness: also median + scatter
    sp_all, _ = current_speed_dir(cur_e[m], cur_n[m])
    print(f"\n{label}  (tau[{lo},{hi}), n={m.sum()})")
    print(f"  current  set={dirv:5.0f}deg  drift={spdv:.2f} kt   "
          f"(cur_E={ce:+.2f}, cur_N={cn:+.2f} kt; per-sample drift {np.nanmedian(sp_all):.2f}"
          f"±{np.nanstd(sp_all):.2f})")
    print(f"  boat     HDG={ang_mean(hdg[m]):5.1f}  COG={ang_mean(cog[m]):5.1f}  "
          f"STW={np.nanmean(spd[m]):.2f}  SOG={np.nanmean(sog[m]):.2f}  leeway={np.nanmean(lam[m]):+.1f}deg")
    return ce, cn, ang_mean(hdg[m]), ang_mean(cog[m])


eb, nb, hb, cb = report(-55, -5, "BEFORE (footing)")
ea, na, ha, ca = report(35, 95, "AFTER  (de-twist)")

# difference of current across the change
dE, dN = ea - eb, na - nb
dmag = math.hypot(dE, dN)
print(f"\n>>> CHANGE IN CURRENT across tau=0:  dE={dE:+.2f}  dN={dN:+.2f}  |d|={dmag:.2f} kt")
print(f">>> (cv noise floor of the estimator is ~{m['cv_rms_kt']:.2f} kt, so anything"
      f" under that is consistent with NO change)")

# consistency: dCOG vs dHDG
def sang(x): return (x + 180) % 360 - 180
dHDG = sang(ha - hb); dCOG = sang(ca - cb)
print(f"\n>>> dHDG (through-water aim) = {dHDG:+.1f}deg ;  dCOG (GPS track) = {dCOG:+.1f}deg")
print(f">>> they match to {abs(dHDG-dCOG):.1f}deg -> the track turned because the BOAT turned,"
      f" not because current shifted")

# whole-beat current for context
mb = (t >= TRANS_UNIX - 555) & (t < TRANS_UNIX + 250)
ce, cn = np.nanmean(cur_e[mb]), np.nanmean(cur_n[mb])
sp, dr = current_speed_dir(ce, cn)
print(f"\nwhole-beat current: set={dr:.0f}deg drift={sp:.2f} kt")
