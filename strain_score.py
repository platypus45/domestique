"""Three-dimensional strain-score model (v1.0.6, additive to TSS).

Implements the per-system power decomposition and strain accounting from
Kontro, Mastracci, Cheung, MacInnis 2026 — *The three-dimensional
impulse-response model* (PLOS ONE 10.1371/journal.pone.0341721).

This module ships as a SECONDARY lens; v1.0.4/v1.0.5 TSS-driven planning
remains primary. The 3D decomposition produces:

  * SS_CP    — aerobic strain (CP-bounded share of power output)
  * SS_W'    — glycolytic strain (W' tank above CP)
  * SS_Pmax  — phosphocreatine strain (PCr-mediated short bursts)

Calibrated per Kontro Eq. 13 so that 1 hour at exactly CP ≈ 100 SS, matching
Xert's XSS convention (https://www.baronbiosys.com/glossary/xss/).

Core formulas (LOCKED per /tmp/MASTER_DECISIONS_v106.md §1):

    Per-second attribution (Kontro Eq. 8-10):
        P_CP   = min(P, CP)
        P_Pmax = (P - CP)^2 / (Pmax - CP)        if P > CP else 0
        P_W'   = (P - CP) - P_Pmax               if P > CP else 0

    Maximum Power Available (Kontro Eq. 4):
        MPA = Pmax - (Pmax - CP) * (W'_exp / W')
        W'_exp = W' - W'bal

    Strain rate / strain score (Kontro Eq. 11-13):
        k_strain = (Pmax - MPA + CP) / (Pmax - P + CP)
        SR       = k_strain * P
        SS_dt    = SR * (Pmax / CP^2 * 100 / 3600) * dt

    W'bal recovery (Skiba 2012/2015 differential, mirroring
    `training_live.py:500-545`):
        when P > CP:  dW'bal/dt = -(P - CP)
        when P <= CP: tau = 546 * exp(-0.01 * DCP) + 316
                      W'bal += (W' - W'bal) * (1 - exp(-dt / tau))

    PCr depletion / recovery (literature-anchored, PMC2636983):
        when P > CP:  Pmax_bal -= (P_Pmax / Pmax_floor_window) * dt   # drain
        when P <= CP: Pmax_bal += (Pmax - Pmax_bal) *
                                  (1 - exp(-dt / tau_pcr))             # refill
        tau_pcr default = 30 s (PMC2636983 healthy-subject mean).

The Kontro paper does NOT specify a PCr depletion model in detail; the
Pmax_bal drain term above uses Eq. 9's PCr-share (P_Pmax) divided by a
window equal to tau_pcr so a sustained Pmax-equivalent burst empties the
pool in ~tau_pcr seconds, with exponential refill. This choice is
documented here per §9.6 of the master decisions.

Per-component Banister impulse-response (Eq. 5 of Kontro, generalised):

    fitness[t] = sum_{i<=t} SS[i] * exp(-(t - i) / tau_fit)
    fatigue[t] = sum_{i<=t} SS[i] * exp(-(t - i) / tau_fat)
    form[t]    = k_fit * fitness[t] - k_fat * fatigue[t]

Default tau pairs (Kontro Fig. S2 single-athlete illustrative example;
NOT population-validated — see `training.py` for the locked constants):

    CP    52 / 10  d
    W'     5 / 5   d
    Pmax  10 / 4   d

Public API (used by IMPL-3D-INGEST):

    compute_xss_components(power_trace, cp, w_prime, pmax,
                           tau_pcr=30.0) -> dict
"""

from __future__ import annotations

import math
from typing import Iterable, Sequence


# ═════════════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═════════════════════════════════════════════════════════════════════════════

# Kontro Fig. S2 illustrative default. The PCr recovery time constant in
# seconds (literature: PMC2636983, healthy-subject mean ~30 s).
DEFAULT_TAU_PCR_S: float = 30.0

# Skiba 2012 W'bal-recovery model parameters (matches training_live.py).
# tau = SKIBA_TAU_A * exp(-SKIBA_TAU_K * DCP) + SKIBA_TAU_B (seconds).
SKIBA_TAU_A: float = 546.0
SKIBA_TAU_K: float = 0.01
SKIBA_TAU_B: float = 316.0

# SS_dt calibration constant. Choosing 100 SS per hour-at-CP (Xert XSS
# convention). Encoded inline as `Pmax/CP^2 * 100 / 3600`.


# ═════════════════════════════════════════════════════════════════════════════
# PER-SECOND PRIMITIVES
# ═════════════════════════════════════════════════════════════════════════════

def attribute_strain_per_second(
    P: float, CP: float, Pmax: float
) -> tuple[float, float, float]:
    """Decompose 1 s power into (P_CP, P_Pmax, P_W') per Kontro Eq. 8-10.

    Returns (P_CP, P_Pmax, P_W'). Sub-CP power is fully aerobic; supra-CP
    splits into a quadratic PCr share and a residual glycolytic share.
    Sum equals P (within fp tolerance).
    """
    if Pmax <= CP:
        # Degenerate: caller handed a Pmax that doesn't exceed CP. Fall
        # back to all-aerobic to avoid div-by-zero.
        return (float(P), 0.0, 0.0)

    P_CP = min(P, CP)
    if P > CP:
        excess = P - CP
        P_Pmax = (excess * excess) / (Pmax - CP)
        # Clamp: when P approaches Pmax, P_Pmax can numerically equal
        # excess; W' share floors at 0.
        if P_Pmax > excess:
            P_Pmax = excess
        P_W_prime = excess - P_Pmax
    else:
        P_Pmax = 0.0
        P_W_prime = 0.0
    return (P_CP, P_Pmax, P_W_prime)


def MPA(W_prime_bal: float, W_prime: float, CP: float, Pmax: float) -> float:
    """Maximum Power Available (Kontro Eq. 4).

    With a full W' tank, MPA equals Pmax. As W' depletes (W'bal -> 0),
    MPA collapses to CP. W'_exp = W' - W'bal is the cumulative spend.
    """
    if W_prime <= 0:
        return float(CP)
    W_exp = max(0.0, W_prime - W_prime_bal)
    return Pmax - (Pmax - CP) * (W_exp / W_prime)


def strain_rate(P: float, MPA_t: float, CP: float, Pmax: float) -> float:
    """k_strain * P  (Kontro Eq. 11-12).

    k_strain rises sharply as P approaches MPA. The (Pmax - P + CP)
    denominator is positive when P < Pmax + CP, which holds for
    physiological P values.
    """
    denom = Pmax - P + CP
    if denom <= 0:
        # P very close to / above Pmax+CP — saturate strain rate.
        denom = 1e-6
    k_strain = (Pmax - MPA_t + CP) / denom
    return k_strain * P


def ss_per_second(
    P: float, MPA_t: float, CP: float, Pmax: float, dt: float = 1.0
) -> float:
    """One second of strain score (Kontro Eq. 13).

    Calibrated so 1 hour at exactly CP yields 100 SS:

        SS = SR * (Pmax / CP^2 * 100 / 3600) * dt

    Verified by test #6 in tests/test_strain_score.py.
    """
    if CP <= 0:
        return 0.0
    SR = strain_rate(P, MPA_t, CP, Pmax)
    calib = (Pmax / (CP * CP)) * (100.0 / 3600.0)
    return SR * calib * dt


# ═════════════════════════════════════════════════════════════════════════════
# RIDE-LEVEL DECOMPOSITION
# ═════════════════════════════════════════════════════════════════════════════

def compute_xss_components(
    power_trace: Sequence[float],
    cp: int | float,
    w_prime: int | float,
    pmax: int | float,
    tau_pcr: float = DEFAULT_TAU_PCR_S,
) -> dict:
    """Per-ride 3D strain-score decomposition.

    Walks the 1 Hz power trace, integrating:

      * W'bal (Skiba 2012 differential — drain when P > CP, exponential
        recovery τ = 546·exp(-0.01·DCP)+316 when P ≤ CP).
      * Pmax_bal (PCr depletion-recovery — drain proportional to P_Pmax
        share when P > CP, exponential τ_pcr recovery when P ≤ CP).
      * Per-second strain rate using current MPA(W'bal).
      * Per-system attribution weighted by P_CP/P, P_Pmax/P, P_W'/P.

    Returns:
        {
          "xss_total":        float,
          "xss_cp":           float,
          "xss_w_prime":      float,
          "xss_pmax":         float,
          "w_prime_bal_min":  float,  # joules — lowest W'bal seen
          "pmax_bal_min":     float,  # watts — lowest Pmax_bal seen
        }

    Public API. IMPL-3D-INGEST calls this for per-ride attribution.
    """
    cp_f = float(cp)
    wp_f = float(w_prime)
    pmax_f = float(pmax)

    if cp_f <= 0 or wp_f <= 0 or pmax_f <= cp_f:
        # Inputs not physiologically valid — return zeros.
        return {
            "xss_total": 0.0,
            "xss_cp": 0.0,
            "xss_w_prime": 0.0,
            "xss_pmax": 0.0,
            "w_prime_bal_min": wp_f,
            "pmax_bal_min": pmax_f,
        }

    w_bal = wp_f
    pmax_bal = pmax_f
    w_bal_min = wp_f
    pmax_bal_min = pmax_f

    xss_total = 0.0
    xss_cp = 0.0
    xss_w = 0.0
    xss_pm = 0.0

    dt = 1.0  # 1 Hz
    pmax_drain_window = max(1.0, tau_pcr)  # see module docstring

    for raw in power_trace:
        try:
            P = float(raw)
        except (TypeError, ValueError):
            continue
        if P < 0 or not math.isfinite(P):
            P = 0.0

        # Per-system attribution at current second.
        P_CP, P_Pmax_share, P_W_share = attribute_strain_per_second(
            P, cp_f, pmax_f
        )

        # Strain rate uses the CURRENT MPA before W'bal updates this tick.
        mpa_t = MPA(w_bal, wp_f, cp_f, pmax_f)
        ss = ss_per_second(P, mpa_t, cp_f, pmax_f, dt=dt)

        if P > 0 and ss > 0:
            xss_total += ss
            # Weight components by physiological share. When P==0 ss==0
            # so the per-component weighting never diverges.
            xss_cp += ss * (P_CP / P)
            xss_pm += ss * (P_Pmax_share / P)
            xss_w  += ss * (P_W_share / P)

        # ─── W'bal update (Skiba 2012, mirror training_live.py:514-523) ──
        if P > cp_f:
            w_bal -= (P - cp_f) * dt
        else:
            dcp = max(0.0, cp_f - P)
            tau_w = SKIBA_TAU_A * math.exp(-SKIBA_TAU_K * dcp) + SKIBA_TAU_B
            w_bal += (wp_f - w_bal) * (1.0 - math.exp(-dt / tau_w))
        w_bal = max(0.0, min(wp_f, w_bal))
        if w_bal < w_bal_min:
            w_bal_min = w_bal

        # ─── Pmax_bal update (PCr depletion/recovery) ────────────────────
        if P > cp_f:
            pmax_bal -= (P_Pmax_share / pmax_drain_window) * dt
        else:
            pmax_bal += (pmax_f - pmax_bal) * (
                1.0 - math.exp(-dt / max(1e-6, tau_pcr))
            )
        pmax_bal = max(cp_f, min(pmax_f, pmax_bal))
        if pmax_bal < pmax_bal_min:
            pmax_bal_min = pmax_bal

    return {
        "xss_total": xss_total,
        "xss_cp": xss_cp,
        "xss_w_prime": xss_w,
        "xss_pmax": xss_pm,
        "w_prime_bal_min": w_bal_min,
        "pmax_bal_min": pmax_bal_min,
    }


# ═════════════════════════════════════════════════════════════════════════════
# PER-COMPONENT BANISTER IMPULSE-RESPONSE
# ═════════════════════════════════════════════════════════════════════════════

def banister(
    ss_history: Sequence[float],
    tau_fit: float,
    tau_fat: float,
    k_fit: float = 1.0,
    k_fat: float = 2.0,
) -> tuple[float, float, float]:
    """Per-component Banister fitness/fatigue/form (Kontro Eq. 5).

    `ss_history` is a daily-resolution list of per-day SS_x values
    (oldest first; the last entry is "today"). Returns the (fitness,
    fatigue, form) tuple as of the day represented by the last index.

    Equilibrium for a constant daily input `S`:
        fitness_eq ≈ S * tau_fit
    After one tau, fitness reaches (1 - 1/e) ≈ 63 % of equilibrium —
    verified by test #11 in tests/test_strain_score.py.
    """
    if not ss_history:
        return (0.0, 0.0, 0.0)

    fitness = 0.0
    fatigue = 0.0
    today_idx = len(ss_history) - 1
    for i, val in enumerate(ss_history):
        v = float(val)
        if v == 0.0:
            continue
        delta = today_idx - i
        fitness += v * math.exp(-delta / max(1e-6, tau_fit))
        fatigue += v * math.exp(-delta / max(1e-6, tau_fat))
    form = k_fit * fitness - k_fat * fatigue
    return (fitness, fatigue, form)
