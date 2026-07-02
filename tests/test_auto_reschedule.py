"""v2.3.0 — missed sessions are auto-rescheduled (the manual banner was removed).

Covers _auto_apply_missed_moves + the factored _apply_move_session /
_compute_missed_suggestions helpers in app.py.
"""
from datetime import date

import app


def _sess(day, st, dur, status="pending", **kw):
    d = {
        "day": day, "day_name": date.fromisoformat(day).strftime("%a"),
        "session_type": st, "duration_min": dur, "tss_estimate": dur,
        "description": st, "zwo_file": "", "zwo_name": "", "status": status,
        "user_moved": False, "moved_from": "",
    }
    d.update(kw)
    return d


def _plan_with_missed():
    # ISO week Mon 2026-06-22 .. Sun 06-28. Missed vo2max on Mon; Fri/Sat free.
    return {
        "goal": {"rest_days": [6], "available_days": [0, 1, 2, 3, 4, 5],
                 "distribution": "polarized"},
        "availability": {},
        "weeks": [{
            "week_num": 1, "start": "2026-06-22", "end": "2026-06-28",
            "phase": "build1", "tss_target": 400,
            "sessions": [
                _sess("2026-06-22", "vo2max", 60, status="missed"),
                _sess("2026-06-23", "z2", 60),
                _sess("2026-06-24", "threshold", 60, status="done"),
                _sess("2026-06-25", "z2", 45),
                _sess("2026-06-26", "rest", 0),
                _sess("2026-06-27", "rest", 0),
                _sess("2026-06-28", "rest", 0),
            ],
        }],
    }


def test_missed_session_is_auto_relocated():
    plan = _plan_with_missed()
    today = date(2026, 6, 25)  # Thu — Mon is in the past
    applied = app._auto_apply_missed_moves(plan, today)
    assert applied == [{"from": "2026-06-22", "to": "2026-06-26"}]

    by_day = {s["day"]: s for s in plan["weeks"][0]["sessions"]}
    # Origin becomes a rest stub flagged moved.
    assert by_day["2026-06-22"]["session_type"] == "rest"
    assert by_day["2026-06-22"]["status"] == "moved_from:2026-06-26"
    # Target holds the relocated session...
    tgt = by_day["2026-06-26"]
    assert tgt["session_type"] == "vo2max"
    assert tgt["duration_min"] == 60
    # v3.0.0 FC5a: auto-moves set auto_moved, NEVER user_moved — user_moved is
    # reserved for actual user drags (it grants regen immunity; the system's
    # own moves must stay re-decidable). This closes the L3-1 self-immunize chain.
    assert tgt["user_moved"] is False
    assert tgt.get("auto_moved") is True
    # ...and is reset to pending so the missed-hard refit tier won't ALSO
    # redistribute the same stimulus (no double-handling).
    assert tgt["status"] == "pending"


def test_auto_relocate_is_idempotent():
    plan = _plan_with_missed()
    today = date(2026, 6, 25)
    first = app._auto_apply_missed_moves(plan, today)
    assert first  # moved once
    # Re-running finds no missed session left → no-op.
    assert app._auto_apply_missed_moves(plan, today) == []


def test_no_free_slot_leaves_session_missed():
    # Every other day is busy/rest-unavailable → nothing to move onto.
    plan = _plan_with_missed()
    wk = plan["weeks"][0]["sessions"]
    for s in wk:
        if s["session_type"] == "rest":
            s["session_type"] = "z2"  # fill the rest slots
            s["duration_min"] = 60
    today = date(2026, 6, 25)
    assert app._auto_apply_missed_moves(plan, today) == []
    # The miss is untouched (will fall through to the refit tier).
    by_day = {s["day"]: s for s in plan["weeks"][0]["sessions"]}
    assert by_day["2026-06-22"]["status"] == "missed"
