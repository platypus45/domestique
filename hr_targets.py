"""Power-target → heart-rate-target conversion for HR-only riders (no power meter).

Single source of truth for the hr target_mode: the workout-detail API, the FIT
builders and the dashboard all convert through :func:`power_target_to_hr` so a
segment can never show one number on screen and another on the head unit.

Physiology contract (see IP_HR_ONLY / verified research):
  * Power↔HR is a usable proxy ONLY for steady aerobic work — Coggan L1–L4
    (≤105% FTP) held long enough for HR to stabilise (HR onset τ ≈ 36 s, so
    ~2–3 min). Those segments get a bpm RANGE from Coggan's own %FTP↔%LTHR
    dual table, anchored on LTHR (threshold-to-threshold, like FTP).
  * Anything Z5+ (≥106% FTP) or shorter than HR_MIN_SEG_S gets an RPE cue,
    NEVER a bpm target: HR lags ~30 s, saturates at HRmax, and is
    non-steady-state there (Coggan marks L6/L7 heart-rate "N/A").
  * Long steady holds (≥ HR_DRIFT_S) carry a cardiac-drift note: expect
    +10–20 bpm at constant output (Coyle & González-Alonso 2001) — hold the
    lower half of the range early.

NOTE (two HR reference frames, deliberate): these targets are %LTHR-anchored
(Coggan dual table). The planner's Norwegian `hr_ceiling_pct` is %HRmax and is
description-text only — see training_planner.py:1336. zones.py's Friel 5-zone
HR model (81/89/93/102% LTHR) is the *analysis* bucketing and is intentionally
NOT the prescription table used here.
"""
from __future__ import annotations

# HR can't stabilise on shorter segments (τ≈36 s ⇒ ~3 min to steady state).
HR_MIN_SEG_S = 150
# Steady holds at least this long get the cardiac-drift note.
HR_DRIFT_S = 1200

# Coggan dual-table %FTP → %LTHR zone rows (L1–L4 = the HR-guidable band).
# (zone, ftp_pct_hi, lthr_frac_lo, lthr_frac_hi). L1's 0.50 floor is pragmatic
# (FIT/UI need a number; Coggan only bounds L1 as "<68%").
_HR_ROWS = [
    (1, 55.0, 0.50, 0.68),
    (2, 75.0, 0.69, 0.83),
    (3, 90.0, 0.84, 0.94),
    (4, 105.0, 0.95, 1.05),
]

# Piecewise-linear %FTP → %LTHR map for ramp endpoints, through the Coggan
# zone-boundary pairs. Input clamped to [55, 105]: below 55% FTP maps to the
# 68% LTHR "easy" ceiling; 106–120% caps at 105% LTHR (HR won't meaningfully
# exceed threshold mid-ramp); >120% is not mappable at all (RPE).
_RAMP_ANCHORS = [(55.0, 0.68), (75.0, 0.83), (90.0, 0.94), (105.0, 1.05)]

# Zone → RPE cue (CR10). Z5+ and any too-short segment degrade to these.
_RPE_ROWS = {
    1: (1, 2, "very easy spin"),
    2: (2, 3, "easy — conversational"),
    3: (4, 5, "moderate — tempo"),
    4: (6, 7, "hard — steady threshold effort"),
    5: (8, 9, "very hard — VO2 effort"),
    6: (9, 10, "extremely hard — anaerobic effort"),
    7: (10, 10, "all-out sprint"),
}

_ZONE_NAMES = {1: "Z1 Recovery", 2: "Z2 Endurance", 3: "Z3 Tempo",
               4: "Z4 Threshold", 5: "Z5 VO2max", 6: "Z6 Anaerobic",
               7: "Z7 Sprint"}

DRIFT_NOTE = ("long steady effort: expect HR to drift up 10-20 bpm — "
              "hold the lower half of the range early")


def zone_of_pct(pct: float) -> int:
    """Coggan zone (1-7) for a %FTP value. Mirrors zones._POWER_FRACS bounds.

    Rounded to 6 decimals first: callers pass raw ``fraction * 100`` and IEEE
    gives float("0.55")*100 == 55.000000000000001, which without rounding
    misclassified the ubiquitous 55%-FTP recovery block as Z2 — wrong bpm
    floor on 2,557 segments across 1,457 library files (red-team D1).
    """
    pct = round(pct, 6)
    if pct <= 55:
        return 1
    if pct <= 75:
        return 2
    if pct <= 90:
        return 3
    if pct <= 105:
        return 4
    if pct <= 120:
        return 5
    if pct <= 150:
        return 6
    return 7


def _lthr_frac_at(pct: float) -> float:
    """Continuous %FTP → LTHR-fraction map (ramp endpoints). Clamped [55,105]."""
    pct = max(55.0, min(105.0, pct))
    for (x1, y1), (x2, y2) in zip(_RAMP_ANCHORS, _RAMP_ANCHORS[1:]):
        if pct <= x2:
            return y1 + (y2 - y1) * (pct - x1) / (x2 - x1)
    return _RAMP_ANCHORS[-1][1]


def _rpe(zone: int, reason: str) -> dict:
    lo, hi, label = _RPE_ROWS[zone]
    return {"kind": "rpe", "rpe_low": lo, "rpe_high": hi, "label": label,
            "zone": zone, "zone_name": _ZONE_NAMES[zone], "reason": reason}


def _resolve_rows_bpm(lthr: int, max_hr: int,
                      hr_rows_override: dict | None) -> dict:
    """Absolute bpm bounds per prescription row (zones 1-4).

    Default = the Coggan %LTHR table. ``hr_rows_override`` (v2.5.0 W1 — the
    Settings "HR workout targets" editor) supplies athlete-tuned ABSOLUTE bpm:
    {"z1_high": int, "z2": [lo,hi], "z3": [lo,hi], "z4": [lo,hi]}. Validation
    (monotone, sane range) happens at the settings write; here we just clamp
    to max_hr so a later max_hr edit can't leave targets above the ceiling.
    """
    if hr_rows_override:
        try:
            z1h = min(int(hr_rows_override["z1_high"]), max_hr)
            rows = {1: (min(round(0.50 * lthr), z1h), z1h)}
            for z, key in ((2, "z2"), (3, "z3"), (4, "z4")):
                lo, hi = hr_rows_override[key]
                hi = min(int(hi), max_hr)
                rows[z] = (min(int(lo), hi), hi)
            return rows
        except (KeyError, TypeError, ValueError, IndexError):
            pass  # malformed override → Coggan defaults (never crash a chart)
    out = {}
    for z, _top, lo, hi in _HR_ROWS:
        bpm_hi = min(round(hi * lthr), max_hr)
        out[z] = (min(round(lo * lthr), bpm_hi), bpm_hi)
    return out


def power_target_to_hr(pct_start: float, pct_end: float, duration_s: float,
                       lthr: int, max_hr: int,
                       hr_rows_override: dict | None = None) -> dict:
    """Convert one workout segment's power target to HR/RPE guidance.

    pct_start/pct_end are %FTP at the segment's start/end IN TIME ORDER
    (equal for a steady block; differing for a Warmup/Cooldown/Ramp — the
    caller resolves ramp direction exactly like the detail chart does).
    ``hr_rows_override`` = athlete-tuned absolute bpm rows (see
    :func:`_resolve_rows_bpm`); None → Coggan defaults.

    Returns one of:
      {"kind":"hr", "bpm_low","bpm_high","zone","zone_name","drift",("note")}
      {"kind":"hr_ramp", "bpm_start","bpm_end","zone","zone_name","capped"}
      {"kind":"rpe", "rpe_low","rpe_high","label","zone","zone_name","reason"}

    Pure + deterministic (contract C1); clamps preserve low<=high (C16).
    """
    hard_pct = max(pct_start, pct_end)
    zone = zone_of_pct(hard_pct)

    # Supra-threshold (>120% FTP anywhere) — HR cannot express it. RPE.
    if zone >= 6:
        return _rpe(zone, "supra_threshold")
    # Too short for HR to stabilise — RPE even in Z2 (contract C4).
    if duration_s < HR_MIN_SEG_S:
        return _rpe(zone, "short")

    rows = _resolve_rows_bpm(lthr, max_hr, hr_rows_override)

    if abs(pct_start - pct_end) > 1e-9:
        # Ramp: piecewise-linear through the zone-top bpm at the Coggan %FTP
        # boundaries (55/75/90/105) — with an override those tops ARE the
        # athlete's own numbers, so ramps agree with steady rows by
        # construction. 106-120% ends cap at the Z4 top.
        anchors = [(55.0, rows[1][1]), (75.0, rows[2][1]),
                   (90.0, rows[3][1]), (105.0, rows[4][1])]

        def _bpm_at(pct: float) -> int:
            pct = max(55.0, min(105.0, pct))
            for (x1, y1), (x2, y2) in zip(anchors, anchors[1:]):
                if pct <= x2:
                    return min(round(y1 + (y2 - y1) * (pct - x1) / (x2 - x1)), max_hr)
            return min(anchors[-1][1], max_hr)

        return {"kind": "hr_ramp", "bpm_start": _bpm_at(pct_start),
                "bpm_end": _bpm_at(pct_end),
                "zone": zone, "zone_name": _ZONE_NAMES[zone],
                "capped": hard_pct > 105}

    # Steady Z5 gets RPE, not a ">=106% to max, may not reach" pseudo-range
    # (locked decision D-b: barely actionable + HRmax-clamp inversion risk).
    if zone == 5:
        return _rpe(5, "supra_threshold")

    bpm_low, bpm_high = rows[zone]
    out = {"kind": "hr", "bpm_low": bpm_low, "bpm_high": bpm_high,
           "zone": zone, "zone_name": _ZONE_NAMES[zone],
           "drift": duration_s >= HR_DRIFT_S}
    if out["drift"]:
        out["note"] = DRIFT_NOTE
    return out


__all__ = ["power_target_to_hr", "zone_of_pct",
           "HR_MIN_SEG_S", "HR_DRIFT_S", "DRIFT_NOTE"]
