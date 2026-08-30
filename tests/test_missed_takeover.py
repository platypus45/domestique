"""A missed HARD session in a full week takes over an easy day — once.

Before this, the auto-reschedule could only move a miss into an existing REST
slot. A rider whose week had none lost the session silently, and the week
rode out on its easy back half — "missing multiple planned days and then just
serving some weak z2". The redistribution evidence (docs/SCIENCE.md) supports
exactly one narrower move: ONE missed key-intensity session may be relocated
within the week, as a move and never an addition, at least 48h from any other
hard day. The displaced easy volume is written off — missed z2 is not worth
compensating, and intensity is what preserves adaptation (Hickson 1985).
"""
from __future__ import annotations

import datetime

import app as app_mod


def _plan(sessions):
    monday = datetime.date(2026, 8, 17)
    week = {"week_num": 1, "start": monday.isoformat(),
            "end": (monday + datetime.timedelta(days=6)).isoformat(),
            "phase": "base", "tss_target": 300, "sessions": []}
    for i, (stype, status) in enumerate(sessions):
        day = monday + datetime.timedelta(days=i)
        week["sessions"].append({
            "day": day.isoformat(), "day_name": day.strftime("%A"),
            "session_type": stype, "duration_min": 0 if stype == "rest" else 60,
            "tss_estimate": 0 if stype == "rest" else 55,
            "status": status, "zwo_file": "", "description": stype})
    return {"goal": {"rest_days": []}, "weeks": [week]}


_TUE = datetime.date(2026, 8, 18)


def test_missed_hard_takes_over_an_easy_day():
    plan = _plan([("threshold", "missed"), ("z2", "pending"),
                  ("recovery", "pending"), ("vo2max", "pending"),
                  ("z2", "pending"), ("z2", "pending"), ("z2", "pending")])
    sugg = app_mod._compute_missed_suggestions(plan, _TUE)
    assert len(sugg) == 1
    s = sugg[0]
    assert s["reason"] == "easy_day_takeover"
    assert s["missed_session_type"] == "threshold"
    # Tuesday itself qualifies: its neighbours are the missed Monday (does not
    # count — it will not be ridden) and an easy Wednesday.
    assert s["suggested_date"] == _TUE.isoformat()


def test_spacing_blocks_days_next_to_pending_hard():
    """Wed and Fri sit next to Thursday's pending VO2max — both blocked."""
    plan = _plan([("threshold", "missed"), ("rest", "moved_from:x"),
                  ("z2", "pending"), ("vo2max", "pending"),
                  ("z2", "pending"), ("z2", "pending"), ("z2", "pending")])
    # Tuesday is a moved-stub rest (not a candidate); Wed/Fri touch Thursday.
    sugg = app_mod._compute_missed_suggestions(plan, _TUE)
    assert len(sugg) == 1
    assert sugg[0]["suggested_date"] == datetime.date(2026, 8, 22).isoformat(), (
        "the takeover landed within 48h of the week's other hard day")


def test_only_one_takeover_per_week():
    plan = _plan([("threshold", "missed"), ("vo2max", "missed"),
                  ("z2", "pending"), ("z2", "pending"),
                  ("z2", "pending"), ("z2", "pending"), ("z2", "pending")])
    sugg = app_mod._compute_missed_suggestions(
        plan, datetime.date(2026, 8, 19))
    takeovers = [s for s in sugg if s["reason"] == "easy_day_takeover"]
    assert len(takeovers) == 1, (
        f"{len(takeovers)} takeovers — a badly-missed week must never convert "
        f"its whole back half to intensity")


def test_missed_easy_is_still_written_off():
    plan = _plan([("z2", "missed"), ("z2", "pending"),
                  ("recovery", "pending"), ("threshold", "pending"),
                  ("z2", "pending"), ("z2", "pending"), ("z2", "pending")])
    sugg = app_mod._compute_missed_suggestions(plan, _TUE)
    assert sugg == [], (
        "a missed easy session took over another day — missed easy volume is "
        "deliberately written off (no evidence compensating it helps)")


def test_free_rest_slot_still_preferred():
    plan = _plan([("threshold", "missed"), ("z2", "pending"),
                  ("rest", "pending"), ("z2", "pending"),
                  ("z2", "pending"), ("z2", "pending"), ("rest", "pending")])
    sugg = app_mod._compute_missed_suggestions(plan, _TUE)
    assert len(sugg) == 1
    assert sugg[0]["reason"] == "rest_slot", (
        "a free rest slot exists — the takeover must stay the second choice")
