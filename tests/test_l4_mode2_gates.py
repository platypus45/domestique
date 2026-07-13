"""3.3.3 (L4) — Mode-2 week-arithmetic gates (DIAG_L4 fixes G1 + G2).

G1  recognize_entry reserves a trainable remainder for NON-EVENT goals:
    c ≤ runway − MIN_REMAINING_WEEKS (floor 0), so "place me from my rides"
    can never drop the rider at the plan's final weeks (11 empty elapsed rows
    + one Z2 consolidation week — DIAG L4 scenario D2). Payload carries
    weeks_remaining. Event goals keep the legacy runway−1 cap: their
    remaining runway is calendar-anchored at the target (H1 recompute).

G2  generate_plan's PART-B input gate refuses a backdated NON-EVENT plan with
    <1 schedulable (non-elapsed) week — a backdate ≥ span used to persist a
    plan 100% in the past (zero sessions, DIAG L4 scenario E). Short 1..3-week
    remainders stay allowed (the UI warns instead of the engine blocking).

Pinned env (W8 pattern): frozen today = 2026-01-05 (Monday), current_ctl=50,
recent_weekly_tss=650. Hermetic — synthetic rides, no HOME writes, no network.
"""
from __future__ import annotations

from datetime import timedelta

import pytest

import training_planner as tp
from conftest import PLANNER_PIN_ANCHOR as ANCHOR, PLANNER_PIN_ARGS


@pytest.fixture(scope="module", autouse=True)
def _env(planner_pinned_env):
    yield


# ── helpers (mirror test_entry_recognizer.py) ────────────────────────────────

def _rides(weekly: dict[int, float]) -> list[dict]:
    """Synthetic archive: window w (whole 7-day windows counted back from the
    frozen today) carries ``weekly[w]`` TSS split over two rides."""
    out = []
    for w, load in weekly.items():
        for days_back in (7 * w, 7 * w - 3):
            out.append({
                "started_at": (ANCHOR - timedelta(days=days_back)).isoformat()
                              + "T09:00:00",
                "tss": load / 2.0,
            })
    return out


def _goal(**kw) -> "tp.Goal":
    base = dict(goal_type="general", plan_weeks=12, hours_per_week=8.0)
    base.update(kw)
    return tp.Goal(**base)


def _scan(goal, rides):
    return tp.recognize_entry(goal, rides, current_ctl=50.0)


def _gen(goal):
    return tp.generate_plan(goal, seed_salt=0, **PLANNER_PIN_ARGS)


# ── G1 — scan credit cap: reserve a trainable remainder ─────────────────────

def test_fully_consistent_rider_capped_at_runway_minus_reserve():
    """Archive (14w) longer than the runway (12w), every week compliant —
    the scan proposes exactly runway − MIN_REMAINING_WEEKS, never runway−1."""
    res = _scan(_goal(), _rides({w: 500 for w in range(1, 15)}))
    cap = 12 - tp.MIN_REMAINING_WEEKS
    assert res["proposal_weeks"] == cap  # == 8, was 11 pre-G1
    assert res["equivalent_start_date"] == \
        (ANCHOR - timedelta(days=7 * cap)).isoformat()
    assert all(r["qualifies"] for r in res["weeks"])


@pytest.mark.parametrize("runway", [4, 5, 6, 8, 12, 16])
def test_scan_never_proposes_beyond_runway_minus_reserve(runway):
    """The reservation invariant holds at every plan length (archive always
    longer than the runway, fully consistent)."""
    res = _scan(_goal(plan_weeks=runway),
                _rides({w: 500 for w in range(1, runway + 3)}))
    assert res["proposal_weeks"] <= max(0, runway - tp.MIN_REMAINING_WEEKS)


def test_minimum_plan_scan_floors_to_zero():
    """runway 4 (the wizard minimum): cap floors at 0 — start fresh, no
    credit, no equivalent start date."""
    res = _scan(_goal(plan_weeks=4), _rides({w: 500 for w in range(1, 9)}))
    assert res["proposal_weeks"] == 0
    assert res["equivalent_start_date"] is None
    assert res["weeks"] == []
    assert res["weeks_remaining"] == 4  # fresh start ⇒ full runway


# ── G1 — proposal payload carries weeks_remaining ────────────────────────────

def test_payload_carries_weeks_remaining_on_proposal():
    # Archive 6 < cap 8: full credit 6, remainder = 12 − 6.
    res = _scan(_goal(), _rides({w: 500 for w in range(1, 7)}))
    assert res["proposal_weeks"] == 6
    assert res["weeks_remaining"] == 6


def test_payload_carries_weeks_remaining_on_zero_proposal():
    res = _scan(_goal(), [])
    assert res["proposal_weeks"] == 0
    assert res["weeks_remaining"] == 12  # nothing credited ⇒ full runway


def test_capped_proposal_reserves_min_remaining():
    res = _scan(_goal(), _rides({w: 500 for w in range(1, 15)}))
    assert res["weeks_remaining"] == tp.MIN_REMAINING_WEEKS


# ── G2 — generate gate: backdate ≥ span refuses with the counts ─────────────

def test_generate_backdate_beyond_span_raises_with_counts():
    """DIAG L4 scenario E: ftp 12-week budget, 15-week backdate → the old
    engine persisted a plan 100% in the past (0 sessions). Now: ValueError
    naming weeks_elapsed / weeks_total, raised before any plan is built."""
    g = _goal(goal_type="ftp", start_date=ANCHOR - timedelta(weeks=15),
              entry_mode="declared")
    with pytest.raises(ValueError) as ei:
        _gen(g)
    msg = str(ei.value)
    assert "15 weeks back" in msg
    assert "only 12 weeks" in msg


def test_generate_backdate_exactly_span_raises():
    """Boundary: backdate == plan span leaves zero non-elapsed weeks."""
    g = _goal(start_date=ANCHOR - timedelta(weeks=12), entry_mode="declared")
    with pytest.raises(ValueError) as ei:
        _gen(g)
    msg = str(ei.value)
    assert "12 weeks back" in msg
    assert "only 12 weeks" in msg


def test_generate_backdate_leaving_two_weeks_fine():
    """1..3-week remainders are NOT blocked (the UI warns instead): 10-week
    backdate on a 12-week plan generates, with the 2 remaining weeks
    schedulable and no session in the past."""
    start = ANCHOR - timedelta(weeks=10)
    g = _goal(start_date=start, entry_mode="declared")
    phases, weeks = _gen(g)
    assert len(weeks) == 12
    assert weeks[0].start == start
    assert weeks[-1].end == start + timedelta(days=12 * 7 - 1)
    for w in weeks:
        if w.end < ANCHOR:              # elapsed row: kept, but sessionless
            assert w.sessions == []
    future = [s for w in weeks for s in w.sessions]
    assert future, "the remaining weeks must be schedulable"
    assert all(s.day >= ANCHOR for s in future)


# ── event goals unaffected (existing behavior pinned) ───────────────────────

def test_event_scan_keeps_legacy_cap():
    """G1's reservation does NOT apply to event goals: with a 12-week archive
    and an 8-week runway the scan still credits runway−1 = 7 (the remaining
    runway is calendar-anchored at the target, recomputed by H1 — credit
    never eats future weeks)."""
    goal = tp.Goal(goal_type="event", target_date=ANCHOR + timedelta(days=56),
                   event_name="TestFondo", event_km=150.0,
                   event_climb_m=1500.0, plan_weeks=8, hours_per_week=8.0)
    res = _scan(goal, _rides({w: 500 for w in range(1, 13)}))
    assert res["proposal_weeks"] == 7
    assert res["equivalent_start_date"] == \
        (ANCHOR - timedelta(days=49)).isoformat()
    # Remainder = the H1 hypothesis's emitted week rows (16 — the per-phase
    # 7-day emitter walk can exceed the ceil-span 15) − credit (7): the
    # calendar runway to the target, NOT the G1 reserve.
    assert res["weeks_remaining"] == 9
    assert res["weeks_remaining"] > tp.MIN_REMAINING_WEEKS


def test_event_generate_deep_backdate_unaffected():
    """G2's gate is non-event only: a backdated event plan (H1-consistent
    plan_weeks = elapsed + to-target) generates the full span with the
    remaining runway scheduled and the plan ending at the target."""
    start = ANCHOR - timedelta(days=70)           # 10 weeks in
    target = ANCHOR + timedelta(days=14)          # 2 weeks to race day
    goal = tp.Goal(goal_type="event", target_date=target,
                   event_name="TestFondo", event_km=150.0,
                   event_climb_m=1500.0, start_date=start,
                   entry_mode="recognized", plan_weeks=12,
                   hours_per_week=8.0)
    phases, weeks = _gen(goal)
    assert weeks[0].start == start
    assert weeks[-1].end == target
    future = [s for w in weeks for s in w.sessions]
    assert future, "event plan must schedule the remaining runway"
    assert all(s.day >= ANCHOR for s in future)
    assert max(s.day for s in future) <= target
