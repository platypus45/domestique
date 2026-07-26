"""Composite readiness score (0-100) based on HRV, TSB, sleep, RHR."""

import logging as _logging

_log = _logging.getLogger("domestique.readiness")


def _normalize(value: float, low: float, high: float) -> float:
    """Map value onto 0-100, clipped at bounds."""
    if high == low:
        return 50.0
    return max(0.0, min(100.0, (value - low) / (high - low) * 100.0))


# v1.8.16 — recency windows for the autonomic-stress signals. A fatigue read
# from days ago must not drive TODAY's session (the live bug: a 5-day-old
# decoupling reading downgraded a hard session while TSB was +17 / fresh).
_DFA_CAP_MAX_AGE_DAYS = 2          # newest DFA ride must be ≤ this old to cap
# v3.6.0 — the old `_DECOUPLING_MAX_AGE_DAYS = 2` silenced the signal after a
# SINGLE rest day, which is exactly when a fatigue signal is worth seeing. The
# v1.8.16 bug it was patching was a stale reading advising *as if current* —
# truncation was the wrong fix for that, labelling is the right one. Verified
# before changing it: `decoupling_advisory` never reaches training_planner.py,
# so this gates a message, not a plan change.
_DECOUPLING_FRESH_DAYS = 3         # full-confidence window
_DECOUPLING_MAX_AGE_DAYS = 10      # still shown, labelled "aging", age in copy
# Form-freshness thresholds for the decoupling veto (weak signal only).
_DECOUPLING_VETO_TSB = 5.0         # TSB ≥ this = peaked/fresh


def check_dfa_stress_cap(recent_dfa_alpha1: list[float] | None,
                         newest_age_days: int | None = None) -> dict:
    """F1 (v4.1.0) — DFA α1 aerobic-stress gate (Rogers 2021).

    If the mean of the last 3 rides' α1 values < 0.5, the athlete is sustained
    in high autonomic stress. Caller (the planner / today-session endpoint)
    should downgrade any threshold/VO2 session for the next day to Z2. We
    don't mutate the plan here — we return a structured dict so the caller
    can log and apply.

    v1.8.16 — STRONG signal; NEVER vetoed by TSB/form (acute autonomic stress
    can coexist with a paper-fresh TSB). Only sanity-gate on RECENCY:
    ``newest_age_days`` is the age of the newest DFA-contributing ride. A cap
    derived from week-old data is stale, so suppress when KNOWN > 2 days. Fail
    SAFE: when ``newest_age_days`` is None (unknown), keep the cap — the strong
    safety net must not be silenced by a missing date.

    Args:
        recent_dfa_alpha1: dfa_alpha1_avg values from the 3 most recent DFA
            rides, newest first. None / [] / <3 → no cap.
        newest_age_days: age in days of the newest DFA ride (None = unknown).

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
        # Recency: suppress only when the newest DFA ride is KNOWN-stale.
        if newest_age_days is not None and newest_age_days > _DFA_CAP_MAX_AGE_DAYS:
            return {"cap_applied": False, "mean_alpha1": round(mean, 3),
                    "reason": f"dfa_stale (newest DFA ride {newest_age_days}d old)"}
        _log.info(
            f"EVENT=dfa_cap_applied mean_alpha1={mean:.3f} rides={len(vals)} "
            f"newest_age_days={newest_age_days}"
        )
        return {
            "cap_applied": True,
            "mean_alpha1": round(mean, 3),
            "reason": "DFA α1 < 0.5 (mean of last 3 rides) — high aerobic stress",
        }
    return {"cap_applied": False, "mean_alpha1": round(mean, 3), "reason": ""}


def check_aerobic_decoupling(last_decoupling_pct: float | None,
                             source_age_days: int | None = None,
                             tsb: float | None = None,
                             readiness_status: str | None = None,
                             dfa_present_and_healthy: bool = False) -> dict:
    """F2 (v4.1.0) — aerobic-decoupling advisory. WEAK signal: advise, never
    auto-swap — decoupling is less reliable than DFA α1 and is confounded by
    ride duration, heat and fuelling.

    v1.8.16 gates (the live bug: a 5-day-old 9.9% reading advised Z2 while
    TSB was +17, readiness GOOD, DFA healthy α1=1.126):

      1. RECENCY — only advise if the source ride is ≤ 2 days old. KNOWN-stale
         (>2d) → drop. Unknown age → keep (fail toward warning).
      2. FORM VETO **gated on DFA corroboration** — suppress the advisory when
         form is fresh (``tsb ≥ +5`` OR status ∈ {GOOD, EXCELLENT}) **AND** DFA
         independently confirms freshness (``dfa_present_and_healthy``). When
         DFA is ABSENT/insufficient — the common case, most rides have no RR —
         decoupling is the ONLY acute signal and TSB lags acute fatigue ~7d, so
         a fresh TSB alone is NOT grounds to silence it.
    """
    if last_decoupling_pct is None:
        return {"advisory": False, "decoupling_pct": None, "reason": ""}
    try:
        pct = float(last_decoupling_pct)
    except (TypeError, ValueError):
        return {"advisory": False, "decoupling_pct": None, "reason": ""}
    pct_r = round(pct, 1)
    if pct <= 5.0:
        return {"advisory": False, "decoupling_pct": pct_r, "reason": ""}

    # Gate 1 — recency, now graded instead of binary. Past the outer window the
    # reading is genuinely not about today; inside it, age travels WITH the
    # advisory so a 5-day-old number can never read as this morning's.
    age = source_age_days
    if age is not None and age > _DECOUPLING_MAX_AGE_DAYS:
        return {"advisory": False, "decoupling_pct": pct_r,
                "confidence": "stale",
                "reason": f"decoupling_stale (source ride {age}d old)"}
    if age is None:
        confidence = "unknown"
    elif age <= _DECOUPLING_FRESH_DAYS:
        confidence = "fresh"
    else:
        confidence = "aging"

    # Gate 2 — form veto, ONLY when DFA corroborates freshness, and ONLY for a
    # fresh reading. Vetoing an aging one too would have made widening the
    # window pointless: the cases the wider window exists to surface are
    # exactly the ones a good TSB would have silenced.
    form_fresh = (
        (isinstance(tsb, (int, float)) and tsb >= _DECOUPLING_VETO_TSB)
        or (str(readiness_status or "").upper() in ("GOOD", "EXCELLENT"))
    )
    if form_fresh and dfa_present_and_healthy and confidence == "fresh":
        return {"advisory": False, "decoupling_pct": pct_r,
                "confidence": confidence,
                "reason": ("decoupling_vetoed_by_form "
                           f"(TSB/readiness fresh + DFA healthy; {pct_r}% noted)")}

    when = ("Recent ride" if confidence in ("fresh", "unknown")
            else f"A ride {age}d ago")
    return {
        "advisory": True,
        "decoupling_pct": pct_r,
        "confidence": confidence,
        "reason": f"{when} Pa:Hr decoupling {pct_r}% > 5% — Z2 recommended (advisory)",
    }


def attribute_fatigue_signal(signal_elevated: bool,
                            subjective_score: float | None) -> str:
    """C6 (v3.6.0) — use the rider's own wellness ratings to ATTRIBUTE an
    elevated fatigue signal, not to add a second weighted vote for it.

    Poor sleep and high stress raise perceived effort and cardiac drift at a
    FIXED workload (Temesi 2013 PMID 23760468; Kong 2025 sleep-restriction SMD
    0.39). So an objective fatigue signal and a poor wellness rating are often
    two readings of ONE cause — weighting both independently double-counts it.
    Labelling instead: "explained" means the rider already told us why, so
    there is nothing new to escalate; "unexplained" means the body is drifting
    while the rider feels fine, which is the case actually worth surfacing.

    NOTE: the attribution rule itself is untested inference. No trial has
    validated this split, so it labels a message and never changes a plan.
    The 40 cut is the composite's own existing poor-channel boundary (the
    MODERATE/POOR band edge), not a new tuned number.

    Args:
        signal_elevated: an objective fatigue signal fired (today: decoupling).
        subjective_score: the composite's 0-100 subjective component, or None
            when the rider logged nothing.

    Returns:
        "none" | "explained" | "unexplained" | "unattributed"
    """
    if not signal_elevated:
        return "none"
    if subjective_score is None:
        return "unattributed"
    return "explained" if subjective_score < 40 else "unexplained"


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
    last_decoupling_age_days: int | None = None,   # v1.8.16
    newest_dfa_age_days: int | None = None,        # v1.8.16
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
    # v1.8.16 — recency gates + DFA-corroborated form veto on the weak
    # decoupling signal. DFA cap (strong) is recency-gated but NEVER form-vetoed.
    dfa_info = check_dfa_stress_cap(recent_dfa_alpha1,
                                    newest_age_days=newest_dfa_age_days)
    # DFA "present and healthy" = a real ≥3-ride mean that is NOT in the
    # stress zone (so it independently confirms the athlete is fresh). Used
    # to gate the decoupling veto — without DFA corroboration we keep the
    # advisory even when TSB looks fresh (TSB lags acute fatigue ~7d).
    _dfa_mean = dfa_info.get("mean_alpha1")
    dfa_present_and_healthy = (
        not dfa_info.get("cap_applied")
        and dfa_info.get("reason") != "insufficient_dfa_rides"
        and isinstance(_dfa_mean, (int, float))
        and _dfa_mean >= 0.5
    )
    dec_info = check_aerobic_decoupling(
        last_decoupling_pct,
        source_age_days=last_decoupling_age_days,
        tsb=tsb,
        readiness_status=status,
        dfa_present_and_healthy=dfa_present_and_healthy,
    )

    # C6 — attribute the weak signal against the rider's own wellness rating
    # rather than adding a second weighted vote for the same cause.
    dec_info["attribution"] = attribute_fatigue_signal(
        bool(dec_info.get("advisory")), components.get("subjective"))

    return {
        "score": score,
        "status": status,
        "advice": advice,
        "components": components,
        "missing": missing,
        "dfa_cap": dfa_info,
        "decoupling_advisory": dec_info,
    }
