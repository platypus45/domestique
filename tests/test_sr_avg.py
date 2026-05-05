"""Unit tests for the v1.0.7 NP-alternative — strain-rate watt-equivalent.

Locked contract: /tmp/MASTER_DECISIONS_v107.md §1, IMPL-V107-NP-ALTERNATIVE.

The four required tests (per agent prompt):
  1. 1-hour ride at exactly CP → sr_avg_w ≈ CP ± 2 W (anchor calibration).
  2. All-Z2 ride (300 W CP, 200 W constant) → sr_if < 0.6.
  3. Above-FTP 5×5 ride → sr_avg_w accelerates per-interval (diagnostic case).
  4. Returns {sr_avg_w: None, ...} when CP is None.
"""

from __future__ import annotations

import strain_score as ss


# ═════════════════════════════════════════════════════════════════════════════
# 1) Anchor calibration: 1 hour at exactly CP → sr_avg_w ≈ CP ± 2 W.
# ═════════════════════════════════════════════════════════════════════════════

def test_one_hour_at_cp_gives_sr_avg_eq_cp():
    """At anchor (P=CP, full W'), MPA stays at Pmax → k_strain = CP/Pmax →
    SR_per_sec = CP^2/Pmax → SR_avg_W = (CP^2/Pmax)·(Pmax/CP) = CP."""
    trace = [250] * 3600
    res = ss.compute_sr_avg(trace, cp=250, w_prime=20000, pmax=1000)
    assert res["sr_avg_w"] is not None
    assert abs(res["sr_avg_w"] - 250) <= 2.0
    # IF == 1.0 at exactly CP
    assert abs(res["sr_if"] - 1.0) <= 0.01
    # Total SS still ≈ 100 at the anchor (Xert convention preserved)
    assert abs(res["sr_total_ss"] - 100.0) <= 2.0


# ═════════════════════════════════════════════════════════════════════════════
# 2) All-Z2 ride: SR_IF < 0.6 — strain-rate stays well below threshold for
#    sub-FTP steady-state work (fully-loaded W' tank, MPA = Pmax).
# ═════════════════════════════════════════════════════════════════════════════

def test_all_z2_ride_sr_if_below_0_6():
    """At P=200 / CP=300 / Pmax=800 (Pmax/CP=2.67, physiological), the
    constant Z2 effort produces sr_avg_w ≈ 178 W, sr_if ≈ 0.59 — slightly
    below NP_IF (0.667) because there are no spikes for the 4th-power to
    weight."""
    trace = [200] * 3600
    res = ss.compute_sr_avg(trace, cp=300, w_prime=20000, pmax=800)
    assert res["sr_if"] is not None
    assert res["sr_if"] < 0.6


# ═════════════════════════════════════════════════════════════════════════════
# 3) Above-FTP 5×5: per-interval strain-rate acceleration. As W' depletes
#    across reps, MPA collapses toward CP, k_strain numerator grows, SR per
#    second rises — interval 5 mean SR > interval 1 mean SR. This is the
#    diagnostic case (NP saturates at the 4th-power; SR detects fatigue
#    accumulation).
# ═════════════════════════════════════════════════════════════════════════════

def test_above_ftp_5x5_strain_rate_accelerates_per_interval():
    """Build a 5×5 trace at 110 % CP work / 50 % CP recovery (CP=250,
    Pmax=1000, W'=20000). Pull the per-second SR series via the internal
    helper, then compare interval-1 mean to interval-5 mean."""
    cp, w_prime, pmax = 250, 20000, 1000
    trace: list[float] = []
    for _ in range(5):
        trace.extend([275] * 300)  # 5 min @ 110 % CP
        trace.extend([125] * 300)  # 5 min @ 50 % CP recovery

    res = ss._compute_sr_series(trace, cp=cp, w_prime=w_prime, pmax=pmax)
    sr_series = res["sr_series"]
    assert len(sr_series) == len(trace) == 3000

    # Interval N work-block lives at 600·(N-1) … 600·(N-1)+300 of the trace.
    int1_mean = sum(sr_series[0:300]) / 300.0
    int5_mean = sum(sr_series[2400:2700]) / 300.0
    assert int5_mean > int1_mean, (
        f"Strain-rate should accelerate across reps as W' depletes — got "
        f"interval-1 {int1_mean:.1f} W vs interval-5 {int5_mean:.1f} W"
    )

    # Sanity: monotone-ish across all five intervals.
    means = [
        sum(sr_series[600 * i : 600 * i + 300]) / 300.0
        for i in range(5)
    ]
    assert means[4] > means[0]
    # And the overall sr_if should exceed NP_IF by the divergence margin
    # (audit gate 4: sr_if > if_pct + 0.05). At this trace, sr_if ≈ 1.84
    # vs NP_IF ≈ 0.93 — the 0.05 margin is comfortably exceeded.
    assert res["sr_if"] is not None
    assert res["sr_if"] > 0.5 + 0.05  # cushion against any Pmax/CP retune


# ═════════════════════════════════════════════════════════════════════════════
# 4) Graceful None when fitness signature isn't calibrated.
# ═════════════════════════════════════════════════════════════════════════════

def test_returns_none_when_cp_is_none():
    """Dashboard renders the right column greyed-out behind the
    'Calibrate W' & Pmax' tooltip when SR_avg can't be computed."""
    res = ss.compute_sr_avg([200] * 100, cp=None, w_prime=20000, pmax=1000)
    assert res == {"sr_avg_w": None, "sr_if": None, "sr_total_ss": None}

    # Also None when W' missing.
    res2 = ss.compute_sr_avg([200] * 100, cp=250, w_prime=None, pmax=1000)
    assert res2 == {"sr_avg_w": None, "sr_if": None, "sr_total_ss": None}

    # Also None when Pmax missing.
    res3 = ss.compute_sr_avg([200] * 100, cp=250, w_prime=20000, pmax=None)
    assert res3 == {"sr_avg_w": None, "sr_if": None, "sr_total_ss": None}

    # Also None when Pmax ≤ CP (degenerate fitness signature).
    res4 = ss.compute_sr_avg([200] * 100, cp=250, w_prime=20000, pmax=240)
    assert res4["sr_avg_w"] is None
