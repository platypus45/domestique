"""Continuous-mode daily policy (3.4.0 W2 — IP_CONTINUOUS_MODE amendments C+D).

Pure decision functions for the open-ended ("continuous") goal type:

* ``suggest_today_family`` — HRV-gated rotation policy: maps
  (focus_pref, zone-rail deficits, readiness, days-since-last-anaerobic)
  to today's suggested stimulus family (low_aerobic | high_aerobic |
  anaerobic) plus a one-sentence reason. Thresholds per the grill P3
  verdict (GRILL_CONTINUOUS.md):
    - Hard days only when the 7-day LnRMSSD sits INSIDE the SWC band
      (baseline mean ± 0.5 SD) — Javaloyes 2019 IJSPP 14(1):23-32 and
      Javaloyes 2020 (both RCT, cyclists); band adopted TWO-SIDED per the
      Vesterinen/Altini refinement (a spike above the band is also a
      stress signal). No fixed weekly HIT cap — hard-day frequency emerges
      from the daily gate, exactly as in the HRV-guided RCT arms.
    - TSB deep-fatigue floor mirrors the reforecast downshift line
      (training_planner: ``tsb < -25``).
    - 48h hard-day spacing + easy day-after a glycolytic session are kept
      (Hulin 2014 rolling 48h Z5+ ceiling; the R5 glyco day-after rule).

* ``deload_trigger`` / ``foster_monotony`` — amendment C values (grill P4):
  advance the scheduled deload when Foster monotony >= 2.0 (Foster 1998
  Med Sci Sports Exerc 30:1164) or weekly ACWR > 1.5 (Gabbett 2016 Br J
  Sports Med 50:273-280, the existing injury-risk line in
  training_planner's G4 gate).

Hermetic by design: stdlib only, no I/O, no repo imports — the app layer
(app.py) feeds it already-computed series and persists any consequence.
"""
from __future__ import annotations

# ── Families ────────────────────────────────────────────────────────────────
FAMILY_LOW = "low_aerobic"          # Z1-Z2 (recovery / z2 / long_z2)
FAMILY_HIGH = "high_aerobic"        # Z3-Z5 (tempo / SS / threshold / VO2max)
FAMILY_ANAEROBIC = "anaerobic"      # Z6+ (sprint / anaerobic / neuromuscular)
FAMILIES = (FAMILY_LOW, FAMILY_HIGH, FAMILY_ANAEROBIC)

# ── Amendment C thresholds (grill P4) ───────────────────────────────────────
MONOTONY_DELOAD_MIN = 2.0   # Foster 1998: monotony >= 2.0 = elevated risk
ACWR_DELOAD_MIN = 1.5       # Gabbett 2016: > 1.5 doubles injury risk
# JSON-safe stand-in for an infinite monotony (7 identical daily loads ⇒
# SD = 0). Foster monotony is realistically < 7; 99 is unambiguous.
MONOTONY_CAP = 99.0

# ── Amendment D thresholds (grill P3) ───────────────────────────────────────
TSB_LOW_FLOOR = -25         # mirrors training_planner's reforecast downshift
ANAEROBIC_SPACING_DAYS = 2  # 48h hard-day spacing + glyco day-after (R5)
ANAEROBIC_OVERDUE_DAYS = 7  # weekly anaerobic dose bar (grill P6 rotation)
# A ride counts as anaerobic exposure at >= 60s of Z6+Z7 — the sprint
# contract's t150 >= 60s admissibility bar (grill P6).
ANAEROBIC_Z67_MIN_S = 60
# Minimum rail gap that counts as "owed" — sub-floor deficits are zone-edge
# noise, not a prescription. Family-scaled: Z6+ minutes are inherently few
# (a full sprint session banks only ~3-8min of Z6+), Z3-Z5 minutes are bulk.
HIGH_OWED_MIN_MINUTES = 10
ANAEROBIC_OWED_MIN_MINUTES = 2


def foster_monotony(daily_tss) -> "float | None":
    """Raw Foster (1998) monotony over a daily-load window: mean / SD.

    ``daily_tss`` is a sequence of per-day loads (typically 7 entries, one
    per calendar day, zeros for rest days). Returns None when the window
    carries no load at all; an all-equal loaded week (SD = 0, the textbook
    worst monotony) returns MONOTONY_CAP so callers/JSON never see inf.
    Population SD (÷ n) — same math as app._monotony_score's banding.
    """
    vals = [float(v or 0) for v in (daily_tss or [])]
    if not vals or sum(vals) <= 0:
        return None
    mean = sum(vals) / len(vals)
    var = sum((v - mean) ** 2 for v in vals) / len(vals)
    sd = var ** 0.5
    if sd <= 0:
        return MONOTONY_CAP
    return min(mean / sd, MONOTONY_CAP)


def deload_trigger(monotony: "float | None",
                   acwr: "float | None") -> "dict | None":
    """Amendment C trip-wire: should the deload be pulled into THIS week?

    Returns None when neither series trips, else a JSON-safe record
    ``{trigger, value, threshold, reason}``. Monotony is evaluated first
    (grill P4 order: "monotony >= 2.0 OR ACWR > 1.5").
    """
    if monotony is not None and monotony >= MONOTONY_DELOAD_MIN:
        # The SD=0 cap value is an internal stand-in — phrase it honestly
        # instead of leaking "monotony 99.00" into the chip.
        mono_txt = (f"training monotony {monotony:.2f} ≥ "
                    f"{MONOTONY_DELOAD_MIN:.1f} over the last 7 days"
                    if monotony < MONOTONY_CAP else
                    "the last 7 days carried near-identical daily load "
                    "(maximal monotony)")
        return {
            "trigger": "monotony",
            "value": round(float(monotony), 2),
            "threshold": MONOTONY_DELOAD_MIN,
            "reason": (
                f"Deload pulled forward — {mono_txt} (Foster 1998: "
                f"monotonous loading raises overtraining risk)."
            ),
        }
    if acwr is not None and acwr > ACWR_DELOAD_MIN:
        return {
            "trigger": "acwr",
            "value": round(float(acwr), 2),
            "threshold": ACWR_DELOAD_MIN,
            "reason": (
                f"Deload pulled forward — last week you absorbed "
                f"{acwr:.2f}× the planned load (ACWR > "
                f"{ACWR_DELOAD_MIN:.1f} doubles injury risk, Gabbett 2016)."
            ),
        }
    return None


def hrv_band(ln_rmssd_7d: "float | None", swc_lower: "float | None",
             swc_upper: "float | None") -> str:
    """Where the 7-day LnRMSSD sits vs the SWC band (mean ± 0.5 SD).

    Returns "in_band" | "below" | "above" | "unknown". Two-sided on
    purpose (Vesterinen/Altini): an HRV spike ABOVE the band is treated as
    a stress signal too, unlike the one-sided 2019 rule.
    """
    if ln_rmssd_7d is None or swc_lower is None or swc_upper is None:
        return "unknown"
    if ln_rmssd_7d < swc_lower:
        return "below"
    if ln_rmssd_7d > swc_upper:
        return "above"
    return "in_band"


def suggest_today_family(
    focus_pref: str,
    deficits: "dict | None",
    readiness: "dict | None",
    days_since_last_anaerobic: "int | None",
    deload_week: bool = False,
) -> dict:
    """Amendment D rotation policy — today's suggested stimulus family.

    Args:
        focus_pref: continuous-goal focus pref ("ftp" | "vo2" | "both").
        deficits: this week's planned-minus-actual zone-rail minutes per
            family, e.g. ``{"low_aerobic": 120, "high_aerobic": 40,
            "anaerobic": 8}``. Positive = still owed this week. Missing
            keys / None → 0.
        readiness: ``{ln_rmssd_7d, swc_lower, swc_upper, tsb}`` (any may
            be None — the gate degrades, it never crashes).
        days_since_last_anaerobic: calendar days since the last ride with
            >= ANAEROBIC_Z67_MIN_S of Z6+Z7 time; None = none on record.
        deload_week: True when the current plan week is a deload (scheduled
            or advanced) — a deload outranks every hard rung, including the
            weekly-anaerobic-dose one.

    Returns ``{"family": <one of FAMILIES>, "reason": <one sentence>}``.
    First-match-wins ladder; every branch cites its rule.
    """
    if deload_week:
        return {"family": FAMILY_LOW, "reason": (
            "Low-aerobic today — deload week: recovery IS the training, "
            "keep every ride easy (Issurin 2010)."
        )}
    r = readiness or {}
    d = deficits or {}
    high_min = float(d.get(FAMILY_HIGH) or 0)
    ana_min = float(d.get(FAMILY_ANAEROBIC) or 0)
    # Sub-floor rail gaps are noise, not debts (family-scaled floors).
    high_owed = high_min if high_min >= HIGH_OWED_MIN_MINUTES else 0.0
    ana_owed = ana_min if ana_min >= ANAEROBIC_OWED_MIN_MINUTES else 0.0
    dsa = days_since_last_anaerobic
    ln7 = r.get("ln_rmssd_7d")
    lo, hi = r.get("swc_lower"), r.get("swc_upper")
    tsb = r.get("tsb")
    band = hrv_band(ln7, lo, hi)

    # 1-2. HRV gate (Javaloyes 2019/2020, two-sided per Vesterinen/Altini):
    # hard only when the 7-day LnRMSSD is IN the SWC band.
    if band == "below":
        return {"family": FAMILY_LOW, "reason": (
            f"Low-aerobic today — 7-day LnRMSSD {ln7:.2f} is below your "
            f"normal band ({lo:.2f}–{hi:.2f}); hard days only when HRV is "
            f"in band (Javaloyes 2019)."
        )}
    if band == "above":
        return {"family": FAMILY_LOW, "reason": (
            f"Low-aerobic today — 7-day LnRMSSD {ln7:.2f} is above your "
            f"normal band ({lo:.2f}–{hi:.2f}), which also signals stress "
            f"(two-sided HRV rule, Vesterinen/Altini)."
        )}

    # 3. TSB deep-fatigue floor (same line the reforecast downshift uses).
    if tsb is not None and float(tsb) < TSB_LOW_FLOOR:
        return {"family": FAMILY_LOW, "reason": (
            f"Low-aerobic today — TSB {float(tsb):.0f} is under "
            f"{TSB_LOW_FLOOR} (deep fatigue); recover before the next "
            f"hard day."
        )}

    # 4. 48h spacing + glyco day-after (Hulin 2014 / R5): a recent
    # anaerobic day blocks ALL hard work today.
    if dsa is not None and dsa < ANAEROBIC_SPACING_DAYS:
        return {"family": FAMILY_LOW, "reason": (
            f"Low-aerobic today — anaerobic work {dsa}d ago; keep 48h "
            f"between hard days and ride easy the day after a glycolytic "
            f"session (Hulin 2014)."
        )}

    # Hard is allowed from here. When there is no HRV baseline the gate is
    # TSB-only — say so instead of pretending (Kiviniemi: no data, no gate).
    gate_txt = ("HRV in band" if band == "in_band"
                else "no HRV baseline yet (TSB-gated)")

    # 5. Weekly anaerobic dose overdue (grill P6: >= 1 anaerobic/wk bar).
    if dsa is not None and dsa >= ANAEROBIC_OVERDUE_DAYS:
        return {"family": FAMILY_ANAEROBIC, "reason": (
            f"Anaerobic today — {gate_txt}, last anaerobic stimulus "
            f"{dsa}d ago (weekly sprint/neuromuscular dose due)."
        )}

    # 6. Both hard families still owed → focus pref breaks the tie:
    # "vo2" reaches for the glycolytic stimulus first, "ftp"/"both" build
    # the threshold engine first.
    if high_owed > 0 and ana_owed > 0:
        if str(focus_pref or "both") == "vo2":
            return {"family": FAMILY_ANAEROBIC, "reason": (
                f"Anaerobic today — {gate_txt}, Z6+ still owed "
                f"{ana_owed:.0f}min this week and your focus is VO2max."
            )}
        return {"family": FAMILY_HIGH, "reason": (
            f"High-aerobic today — {gate_txt}, Z3–Z5 still owed "
            f"{high_owed:.0f}min this week."
        )}

    # 7. Single-family debts.
    if high_owed > 0:
        return {"family": FAMILY_HIGH, "reason": (
            f"High-aerobic today — {gate_txt}, Z3–Z5 still owed "
            f"{high_owed:.0f}min this week."
        )}
    if ana_owed > 0:
        last = f"last anaerobic {dsa}d ago" if dsa is not None else \
            "none on record"
        return {"family": FAMILY_ANAEROBIC, "reason": (
            f"Anaerobic today — {gate_txt}, Z6+ still owed "
            f"{ana_owed:.0f}min this week ({last})."
        )}

    # 8. Nothing hard owed → base. Green HRV is not a licence to add
    # intensity (Seiler 80/20 discipline).
    return {"family": FAMILY_LOW, "reason": (
        "Low-aerobic today — this week's hard-intensity budget is "
        "already served; extend the aerobic base (Seiler 80/20)."
    )}
