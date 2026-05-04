"""v4.6.6 IMPL-B INJURY-GATES — six guardrails inside adjust_today_session.

One test per gate; each synthesizes the exact trigger condition and verifies
the action. The guardrails close the long-standing bug audited in
/tmp/audit_intensity_feedback.md and /tmp/audit_soreness.md: the planner
DETECTS overload signals (TSS, intensity, soreness) but, pre-v4.6.6, none of
them mutated the persisted plan.

Citations (every gate cites one row of /tmp/MASTER_DECISIONS_v466.md §1):
  G1 — Foster 1998   *Med Sci Sports Exerc* 30:1164-1168
  G2 — Hulin 2014    *Br J Sports Med* 48:708-712
  G3 — Seiler 2010 / Stöggl 2014 / Treff 2019 (polarized 80/20 model)
  G5 — Hooper & Mackinnon 1995 + Cheung 2003 *Sports Med* 33:145-164
  G6 — Hooper & Mackinnon 1995 *J Sci Med Sport*
  G7 — Foster 1998 (session-RPE)

The "fresh HRV / fresh readiness" baseline used by every test below ensures
the new gates fire INDEPENDENTLY of the existing HRV/readiness path — the
v4.6.6 bug was precisely that fresh HRV masked peripheral fatigue.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

import training_planner as tp


# ── shared fixture ───────────────────────────────────────────────────────────

def _fresh_readiness() -> dict:
    """Composite readiness that would NOT trigger any pre-v4.6.6 downshift.

    Score 90 (>= 80 = green) and no DFA cap, so any forced downgrade in the
    test must come from the new G1/G2/G3/G5/G6/G7 gates.
    """
    return {"score": 90, "dfa_cap": {}}


def _planned(session_type: str, day: date | None = None, duration_min: int = 60) -> tp.PlannedSession:
    d = day or date.today()
    return tp.PlannedSession(
        day=d,
        day_name=d.strftime("%A").lower(),
        session_type=session_type,
        duration_min=duration_min,
        tss_estimate=round(duration_min / 60 * tp.TSS_PER_HOUR.get(session_type, 45)),
        description=f"Planned {session_type} {duration_min}min",
    )


# ── G1 — yesterday was hard, even without a planned session ──────────────────

def test_g1_unplanned_hard_yesterday_forces_z2():
    """The user's exact bug: yesterday planned=0, actual TSS=100 → today must drop to Z2.

    Pre-v4.6.6 the ratio was `actual / planned` so planned=0 short-circuited to
    1.0 and the gate never fired. v4.6.6 G1 takes ratio computed by the caller
    (app.py uses `max(yesterday_planned, phase_daily_avg)` as denom). We
    simulate the post-fix value here: 100 / (400/7) ≈ 1.75 > 1.5.
    """
    planned = _planned("vo2max", duration_min=60)
    # Unplanned hard ride yesterday: 100 TSS vs base-phase daily-avg 57 TSS
    # → ratio ≈ 1.75 > 1.5 — gate fires.
    ratio = 100.0 / (tp.PHASE_TARGETS["base"]["tss_per_week"] / 7.0)
    assert ratio > 1.5, "fixture invariant: ratio must exceed 1.5"

    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        yesterday_tss_ratio=ratio,
        rides_recent=[],
        daily_log_today={},
    )
    assert adj.session_type == "z2", f"expected G1 to drop to z2, got {adj.session_type!r}"
    assert adj.adapted is True
    assert "G1" in reason or "yesterday" in reason.lower()
    assert "Foster 1998" in adj.description, (
        f"description must cite Foster 1998 — got: {adj.description!r}"
    )


# ── G2 — 48h Z5+ ceiling, cycling included ──────────────────────────────────

def test_g2_48h_z5plus_ceiling():
    """Synth rides with Z5+ time = 30min in last 48h → today forced Z2 even with fresh HRV.

    Pre-v4.6.6 the equivalent yesterday-only HR-zone check at app.py:4839 was
    GATED OUT for cycling sports — so a Z5 cycling ride yesterday would not
    block a planned VO2max today. v4.6.6 G2 includes cycling and uses a
    rolling 48h window (Hulin 2014).
    """
    planned = _planned("vo2max", duration_min=60)
    # Two rides yesterday and today-early, both cycling — sums to 30min Z5+
    now_iso = datetime.now().isoformat()
    yesterday_iso = (datetime.now() - timedelta(hours=18)).isoformat()
    rides = [
        {
            "date": date.today().isoformat(),
            "start_date_local": now_iso,
            "sport": "Ride",
            "time_in_zone": {"z1": 0, "z2": 0, "z3": 0, "z4": 0, "z5": 600, "z6": 300, "z7": 0},
        },
        {
            "date": (date.today() - timedelta(days=1)).isoformat(),
            "start_date_local": yesterday_iso,
            "sport": "VirtualRide",
            "time_in_zone": {"z5": 600, "z6": 0, "z7": 300},
        },
    ]
    # 600+300+600+0+300 = 1800s = 30min Z5+
    assert tp._last_48h_z5plus_min(rides) == pytest.approx(30.0)

    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=rides,
        daily_log_today={},
    )
    assert adj.session_type == "z2"
    assert adj.adapted is True
    assert "G2" in reason
    assert "Hulin" in adj.description, f"got: {adj.description!r}"


# ── G3 — polarization breach drops next 1-2 hard sessions ───────────────────

def test_g3_polarization_breach_drops_next_hard():
    """Synth weekly actual.z4plus_pct = 30 vs target 8 → reforecast drops next hard.

    Reforecast is the home of G3 (per /tmp/MASTER_DECISIONS_v466.md §3).
    """
    today = date.today()
    # Build a 2-week plan with vo2max sessions in the next week.
    next_monday = today + timedelta(days=(7 - today.weekday()))
    next_sunday = next_monday + timedelta(days=6)
    sessions = []
    for offset in range(7):
        d = next_monday + timedelta(days=offset)
        st = "vo2max" if offset in (1, 3) else "z2"
        sessions.append(tp.PlannedSession(
            day=d, day_name=d.strftime("%A").lower(),
            session_type=st, duration_min=60,
            tss_estimate=round(60 / 60 * tp.TSS_PER_HOUR.get(st, 45)),
            description=f"planned {st}",
        ))
    pw = tp.PlannedWeek(
        week_num=next_monday.isocalendar()[1],
        start=next_monday, end=next_sunday,
        phase="base",
        tss_target=400,
        is_stepback=False,
        sessions=sessions,
    )

    # Actual breach: 30% Z4+ vs target 8% (HIT ceiling busted)
    actual_pol = {"z1z2_pct": 50, "z3_pct": 20, "z4plus_pct": 30}
    target_pol = tp.PHASE_POLARIZED_TARGETS["base"]
    assert tp._polarization_breach(actual_pol, target_pol) is True

    goal = tp.Goal(goal_type="general", target_date=today + timedelta(weeks=12))
    # tsb_series=empty so the TSB downshift loop doesn't double-touch sessions.
    _, info = tp.reforecast(
        goal, [pw],
        tsb_series={d: 0.0 for d in (next_monday + timedelta(days=i) for i in range(7))},
        recent_activities=[],
        actual_polarization=actual_pol,
        target_polarization=target_pol,
    )
    assert info.get("polarization_breach") is True
    assert len(info.get("g3_dropped_days", [])) >= 1, (
        f"G3 must drop at least one hard session — got info={info!r}"
    )
    # Verify the dropped session moved one tier down (vo2max → threshold).
    dropped = [s for s in pw.sessions if s.session_type == "threshold" and s.adapted]
    assert dropped, "expected a vo2max → threshold downshift in next week's sessions"
    assert "Seiler" in dropped[0].description or "Stöggl" in dropped[0].description \
        or "Treff" in dropped[0].description, (
        f"description must cite the polarization paper trio — got: {dropped[0].description!r}"
    )


# ── G5 — soreness 6/7 forces recovery regardless of readiness ───────────────

def test_g5_soreness_6_forces_recovery():
    """daily_log.soreness=6, all other readiness perfect → today forced recovery.

    The bug: pre-v4.6.6, soreness only contributed 20% to the readiness
    composite; a 6/7 reading drops `subjective` from 100 to ~25, the composite
    score still lands ≥60, and the planner runs the planned interval. G5
    bypasses central-HRV-driven readiness (Cheung 2003 — peripheral ≠ central).
    """
    planned = _planned("vo2max", duration_min=60)
    daily_log = {
        "soreness": 6,
        # Everything else benign — proves the gate fires on soreness alone.
        "fatigue": 1, "stress": 1, "sleep_quality": 1,
    }
    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=[],
        daily_log_today=daily_log,
    )
    assert adj.session_type == "recovery"
    assert adj.adapted is True
    assert "G5" in reason
    assert "Hooper 1995" in adj.description
    assert "Cheung 2003" in adj.description, (
        f"G5 description must cite Cheung 2003 (peripheral fatigue) — got: {adj.description!r}"
    )


# ── G6 — Hooper composite ≥18 forces Z2 ─────────────────────────────────────

def test_g6_hooper_18_forces_z2():
    """Hooper sum = 18 (e.g. fatigue 5 + stress 5 + soreness 4 + 8-sleep_quality 4) → Z2 cap.

    Each component below the G5 individual threshold (no field hits 6) but the
    composite still triggers G6 (Hooper & Mackinnon 1995).
    """
    planned = _planned("vo2max", duration_min=60)
    daily_log = {
        "sleep_quality": 4,  # 8 - 4 = 4
        "fatigue": 5,
        "stress": 5,
        "soreness": 4,        # < 6 so G5 won't fire
    }
    # Verify fixture maths
    h = (
        daily_log["fatigue"] + daily_log["soreness"] + daily_log["stress"]
        + (8 - daily_log["sleep_quality"])
    )
    assert h == 18, f"fixture invariant: hooper must sum to 18 (got {h})"

    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=[],
        daily_log_today=daily_log,
    )
    assert adj.session_type == "z2"
    assert adj.adapted is True
    assert "G6" in reason
    assert "Hooper" in adj.description, f"got: {adj.description!r}"


# ── G7 — 3-day mean RPE ≥7 drops planned HIT one tier ───────────────────────

def test_g7_high_3day_rpe_drops_hit():
    """Mean ride.feel = 4.0 over last 3d (= 8/10 on Borg-CR-10 axis) AND planned vo2max → threshold.

    G7 reads `feel` (1-5) and `perceived_exertion` (1-10); the helper rescales
    feel via `feel * 2`. With 3 rides at feel=4 the rolling mean = 8/10 ≥ 7,
    so the gate downshifts vo2max → threshold (Foster 1998 session-RPE).
    """
    planned = _planned("vo2max", duration_min=60)
    today = date.today()
    rides = [
        {
            "date": (today - timedelta(days=offset)).isoformat(),
            "feel": 4,             # ICU 1-5, 4 = "hard"
            "perceived_exertion": None,
        }
        for offset in (0, 1, 2)
    ]
    mean_rpe = tp._last_3d_mean_feel(rides)
    assert mean_rpe is not None and mean_rpe >= 7.0, (
        f"fixture invariant: mean must be ≥7 on the 1-10 axis (got {mean_rpe!r})"
    )

    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        rides_recent=rides,
        daily_log_today={},
    )
    assert adj.session_type == "threshold", (
        f"expected vo2max → threshold via _drop_intensity, got {adj.session_type!r}"
    )
    assert adj.adapted is True
    assert "G7" in reason
    assert "Foster 1998" in adj.description


# ── Negative tests — gates don't misfire on absent data ─────────────────────

def test_no_gates_fire_on_clean_inputs():
    """Empty rides + empty daily_log + fresh readiness → planned session passes through.

    Sanity check: the new gates must NEVER misfire on absent telemetry. Each
    helper must return a safe default (0/None/False) per
    /tmp/MASTER_DECISIONS_v466.md §4.
    """
    planned = _planned("vo2max", duration_min=60)
    adj, reason = tp.adjust_today_session(
        planned, _fresh_readiness(),
        yesterday_tss_ratio=1.0,
        rides_recent=[],
        daily_log_today={},
    )
    assert adj.session_type == "vo2max", (
        f"clean inputs must not adapt; got {adj.session_type} (reason={reason!r})"
    )
    assert reason == ""


# ── Helper unit tests ───────────────────────────────────────────────────────

def test_polarization_breach_helper_z4_ceiling():
    actual = {"z1z2_pct": 70, "z3_pct": 12, "z4plus_pct": 18}
    target = tp.PHASE_POLARIZED_TARGETS["base"]  # z4plus_pct = 5
    assert tp._polarization_breach(actual, target) is True, (
        "18% Z4+ vs target 5% (delta 13 > 8) must trip breach"
    )


def test_polarization_breach_helper_z12_floor():
    actual = {"z1z2_pct": 60, "z3_pct": 30, "z4plus_pct": 10}
    target = tp.PHASE_POLARIZED_TARGETS["base"]  # z1z2_pct = 80
    # 60 < 80-10 = 70 → breach
    assert tp._polarization_breach(actual, target) is True


def test_polarization_breach_helper_no_breach():
    actual = {"z1z2_pct": 78, "z3_pct": 17, "z4plus_pct": 5}
    target = tp.PHASE_POLARIZED_TARGETS["base"]
    assert tp._polarization_breach(actual, target) is False


def test_polarization_breach_helper_safe_defaults():
    """Helpers must never crash on None/empty inputs."""
    assert tp._polarization_breach(None, None) is False
    assert tp._polarization_breach({}, {}) is False
    assert tp._polarization_breach({"z4plus_pct": 0}, {"z4plus_pct": 0}) is False
    assert tp._last_48h_z5plus_min([]) == 0.0
    assert tp._last_3d_mean_feel([]) is None
