#!/usr/bin/env python
"""Regression test for the apparent-wind forward-model sign.

Guards `race_logs.estimate_true_wind_helper`'s `f_aw_e = twe + bte`. The test
SEEDS the live jitted solver at the true wind and feeds constant, noise-free
inputs: with the correct sign the forward-model residual is zero at the truth,
so the estimate stays put; with the historical `twe - bte` bug the residual is
nonzero at the truth and the solver drifts away (2-5 deg, worst downwind).

This tests the real shipped code path (the jitted helper), not a copy.

Run:
    conda run -n sailing --no-capture-output python Analyze/test_true_wind_sign.py
"""
from __future__ import annotations
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import numpy as np
from Libraries.global_variables import G
from Libraries import race_logs

G.init_seattle()
VAR = float(G.MAGNETIC_VARIATION)


def synth_inputs(hdg_true, twd, tws, cog, sog, n=2000):
    """Constant synthetic apparent wind consistent with a known true wind.
    Returns the arrays estimate_true_wind_helper consumes (hdg is magnetic)."""
    d2r = np.deg2rad
    tw_vel = -tws * np.array([np.sin(d2r(twd)), np.cos(d2r(twd))])   # (E,N) TO
    boat_vel = sog * np.array([np.sin(d2r(cog)), np.cos(d2r(cog))])
    aw_vel = tw_vel - boat_vel
    aw_from = np.degrees(np.arctan2(-aw_vel[0], -aw_vel[1])) % 360
    aws = float(np.hypot(*aw_vel))
    awa = ((aw_from - hdg_true + 180) % 360) - 180
    rhdg = (hdg_true - VAR) % 360
    o = np.ones(n)
    return aws * o, awa * o, rhdg * o, sog * o, cog * o, sog * o


def check(label, hdg_true, twd, tws, cog, sog, tol=0.5):
    aws, awa, hdg, spd, cog_a, sog_a = synth_inputs(hdg_true, twd, tws, cog, sog)
    # Seed the solver AT the truth. Correct sign -> residual 0 -> stays.
    ptwd, ptws, *_ = race_logs.estimate_true_wind_helper(
        0.0001, aws, awa, hdg, spd, cog_a, sog_a,
        tws_init=tws, twd_init=twd, variation=VAR, tws_mult=16.0, time_delay=12)
    got_twd = float(ptwd[-13]) % 360       # last updated index (loop stops at n-time_delay)
    got_tws = float(ptws[-13])
    err = abs(((got_twd - twd + 180) % 360) - 180)
    ok = err < tol and abs(got_tws - tws) < 0.5
    print(f"  {label:16s} truth twd={twd:5.0f} tws={tws:4.1f} | seeded->"
          f"twd={got_twd:6.1f} tws={got_tws:4.1f} | drift={err:4.2f} deg  "
          f"{'PASS' if ok else 'FAIL'}")
    return ok


def main():
    print("[test] solver stays at the true wind when seeded there (needs +bte):")
    cases = [
        ("upwind stbd",   0,  30, 14,  12, 6),
        ("upwind port",   0, 330, 14, 348, 6),
        ("reach stbd",   90, 120, 14, 100, 7),
        ("downwind stbd",180, 150, 14, 168, 8),
        ("downwind port",180, 210, 14, 192, 8),
    ]
    results = [check(*c) for c in cases]
    print("ALL PASS" if all(results) else "FAILURES PRESENT")
    return 0 if all(results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
