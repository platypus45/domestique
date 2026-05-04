"""Composite readiness score (0-100) based on HRV, TSB, sleep, RHR."""

import logging as _logging

_log = _logging.getLogger("domestique.readiness")


def _normalize(value: float, low: float, high: float) -> float:
    """Map value onto 0-100, clipped at bounds."""
    if high == low:
        return 50.0
    return max(0.0, min(100.0, (value - low) / (high - low) * 100.0))


def check_dfa_stress_cap(recent_dfa_alpha1: list[float] | None) -> dict:
    """F1 (v4.1.0) — DFA α1 aerobic-stress gate (Rogers 2021).

    If the mean of the last 3 rides' α1 values < 0.5, the athlete is sustained
    in high autonomic stress. Caller (the planner / today-session endpoint)
    should downgrade any threshold/VO2 session for the next day to Z2. We
    don't mutate the plan here — we return a structured dict so the caller
    can log and apply.

    Args:
        recent_dfa_alpha1: list of dfa_alpha1_avg values from the 3 most
            recent rides, newest first. Pass None or [] to skip the check.

    Returns:
        {"cap_applied": bool, "mean_alpha1": float|None, "reason": str}
    """
    if not recent_dfa_alpha1:
        return {"cap_applied": False, "mean_alpha1": None, "reason": ""}
    vals = [v for v in recent_dfa_alpha1[:3] if isinstance(v, (int, float))]
    if len(vals) < 3:
        return {"cap_applied": False, "mean_alpha1": None,
                "reason": "insufficient_dfa_rides"}
    mean = sum(vals) / len(vals)
    if mean < 0.5:
        _log.info(
            f"EVENT=dfa_cap_applied mean_alpha1={mean:.3f} rides={len(vals)}"
        )
        return {
            "cap_applied": True,
            "mean_alpha1": round(mean, 3),
            "reason": "DFA α1 < 0.5 (mean of last 3 rides) — high aerobic stress",
        }
    return {"cap_applied": False, "mean_alpha1": round(mean, 3), "reason": ""}


def check_aerobic_decoupling(last_decoupling_pct: float | None) -> dict:
    """F2 (v4.1.0) — aerobic-decoupling advisory.

    If the most recent ride's Pa:Hr decoupling > 5%, flag tomorrow as
    "Z2-only recommended". We advise rather than auto-swap — decoupling is
    less reliable than DFA α1 as a day-to-day stress signal.
    """
    if last_decoupling_pct is None:
        return {"advisory": False, "decoupling_pct": None, "reason": ""}
    try:
        pct = float(last_decoupling_pct)
    except (TypeError, ValueError):
        return {"advisory": False, "decoupling_pct": None, "reason": ""}
    if pct > 5.0:
        return {
            "advisory": True,
            "decoupling_pct": round(pct, 1),
            "reason": f"Last ride Pa:Hr decoupling {pct:.1f}% > 5% — Z2-only recommended",
        }
    return {"advisory": False, "decoupling_pct": round(pct, 1), "reason": ""}


def compute_readiness(
    ln_rmssd_7d: float | None = None,
    swc_lower: float | None = None,
    swc_upper: float | None = None,
    tsb: float | None = None,
    sleep_h: float | None = None,
    rhr_delta: float | None = None,
    subjective: float | None = None,   # 1-10 score
    recent_dfa_alpha1: list[float] | None = None,
    last_decoupling_pct: float | None = None,
) -> dict:
    """
    Returns:
      score        0-100
      status       UITSTEKEND / GOED / MATIG / SLECHT
      advice       training recommendation
      components   dict with per-component scores
      missing      list of components that were None (lower confidence)
    """
    components = {}
    weights = {}
    missing = []

    # HRV component (30%)
    if ln_rmssd_7d is not None and swc_lower is not None and swc_upper is not None:
        # Use fixed population bounds rather than athlete-adaptive, for comparability
        # across very-stable vs high-variability athletes. 2.5–4.0 spans the typical
        # log-ms RMSSD range for trained adults (Plews 2013).
        hrv_score = _normalize(ln_rmssd_7d, 2.5, 4.0)
        components["hrv"] = round(hrv_score, 1)
        weights["hrv"] = 0.30
    else:
        missing.append("hrv")

    # TSB component (20%) — bell curve peaking at +5 to +15 (Coggan: peak form range)
    # Linear ramp from -30 (score 0) to +10 (score 100), then penalty for detraining >+15
    if tsb is not None:
        if tsb <= 10:
            # Fatigue range: linear from -30→0 to +10→100
            tsb_score = _normalize(tsb, -30, 10)
        elif tsb <= 15:
            # Still good but declining: 100 → 80
            tsb_score = 100 - (tsb - 10) * 4  # 5 points → 20 drop
        else:
            # Detraining: 80 at +15, drops to 40 at +30
            tsb_score = max(20, 80 - (tsb - 15) * (40 / 15))
        components["tsb"] = round(max(0, min(100, tsb_score)), 1)
        weights["tsb"] = 0.20
    else:
        missing.append("tsb")

    # Subjective component (20%)
    if subjective is not None:
        subj_score = _normalize(subjective, 1, 10)
        components["subjective"] = round(subj_score, 1)
        weights["subjective"] = 0.20
    else:
        missing.append("subjective")

    # Sleep component (15%)
    if sleep_h is not None:
        sleep_score = _normalize(sleep_h, 5.0, 9.0)
        components["sleep"] = round(sleep_score, 1)
        weights["sleep"] = 0.15
    else:
        missing.append("sleep")

    # RHR component (15%)
    if rhr_delta is not None:
        rhr_score = _normalize(-rhr_delta, -10, 5)
        components["rhr"] = round(rhr_score, 1)
        weights["rhr"] = 0.15
    else:
        missing.append("rhr")

    if not components:
        return {
            "score": None,
            "status": "INSUFFICIENT_DATA",
            "advice": "Ensure Garmin is synced with Intervals.icu",
            "components": {},
            "missing": missing,
        }

    # Require at least 3 components — a score from 1-2 components is
    # not a "readiness score", it's a single metric.
    if len(components) < 3:
        for k in ("hrv", "tsb", "subjective", "sleep", "rhr"):
            if k not in components and k not in missing:
                missing.append(k)
        return {
            "score": None,
            "status": "INSUFFICIENT_DATA",
            "advice": "Not enough components to compute readiness (need ≥3).",
            "components": components,
            "missing": missing,
        }

    # re-normalise weights for available components
    total_w = sum(weights.values())
    score = sum(components[k] * weights[k] / total_w for k in components)
    score = round(score, 1)

    # English status tokens (EXCELLENT/GOOD/MODERATE/POOR) are matched in
    # dashboard.html statusClass() alongside the legacy Dutch labels.
    if score >= 80:
        status = "EXCELLENT"
        advice = "Intervals, key workout or long ride — fully green"
    elif score >= 60:
        status = "GOOD"
        advice = "Z2 / planned moderate session — avoid all-out efforts"
    elif score >= 40:
        status = "MODERATE"
        advice = "Active recovery or short Z1 session — do not increase volume"
    else:
        status = "POOR"
        advice = "Rest. Do not train. Recovery takes priority."

    # F1/F2 (v4.1.0): attach DFA + decoupling signals. These don't modify the
    # composite score (so historical test fixtures keep working) but live on
    # the readiness payload for the planner / today-session endpoint to act on.
    dfa_info = check_dfa_stress_cap(recent_dfa_alpha1)
    dec_info = check_aerobic_decoupling(last_decoupling_pct)

    return {
        "score": score,
        "status": status,
        "advice": advice,
        "components": components,
        "missing": missing,
        "dfa_cap": dfa_info,
        "decoupling_advisory": dec_info,
    }
