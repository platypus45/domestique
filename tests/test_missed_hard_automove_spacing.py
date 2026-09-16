"""C2 (v3.12.0) — the one re-owe rule after the merge: a missed hard session
is auto-moved once, onto a day that keeps 48 h from every hard session that
was ridden or is still planned; a missed one blocks nothing (D6).

v3.11.5's own guard (48 h from a DONE hard day) lived in refit_remaining_week
and was deleted with the recycle. This pins that the auto-move honours it.
"""
from datetime import date
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app  # noqa: E402


def _sess(day, st, dur, status="pending", **kw):
    d = {"day": day, "day_name": date.fromisoformat(day).strftime("%a"),
         "session_type": st, "duration_min": dur, "tss_estimate": dur,
         "description": st, "zwo_file": "", "zwo_name": "", "status": status,
         "user_moved": False, "moved_from": ""}
    d.update(kw)
    return d


def _plan(sessions):
    return {"goal": {"rest_days": [6], "available_days": [0, 1, 2, 3, 4, 5], "distribution": "polarized"},
            "availability": {},
            "weeks": [{"week_num": 1, "start": "2026-06-22", "end": "2026-06-28", "phase": "build1",
                       "tss_target": 400, "sessions": sessions}]}


def test_a_done_hard_day_blocks_the_next_48h_for_the_moved_session():
    """Mon vo2max missed; Thu threshold RIDDEN; Fri and Sat free. The re-owed
    vo2max may not land on Fri (24 h after a done hard day); Sat is the first
    day that keeps 48 h."""
    plan = _plan([
        _sess("2026-06-22", "vo2max", 60, status="missed"),
        _sess("2026-06-23", "z2", 60, status="done"),
        _sess("2026-06-24", "z2", 60, status="done"),
        _sess("2026-06-25", "threshold", 60, status="done"),
        _sess("2026-06-26", "rest", 0),
        _sess("2026-06-27", "rest", 0),
        _sess("2026-06-28", "rest", 0),
    ])
    applied = app._auto_apply_missed_moves(plan, date(2026, 6, 26))
    assert applied, "the missed hard session is re-owed once"
    assert applied[0]["from"] == "2026-06-22"
    assert applied[0]["to"] != "2026-06-26", "Fri is 24 h after Thursday's ridden threshold"
    assert applied[0]["to"] == "2026-06-27"


def test_a_missed_hard_day_blocks_nothing():
    """D6: Tue vo2max MISSED, Wed threshold missed too, Thu free. A miss is
    no spacing obstacle for the re-owe, and whatever lands stays 48 h apart
    from every hard session that was ridden or is still planned."""
    plan = _plan([
        _sess("2026-06-22", "z2", 60, status="done"),
        _sess("2026-06-23", "vo2max", 60, status="missed"),
        _sess("2026-06-24", "threshold", 60, status="missed"),
        _sess("2026-06-25", "rest", 0),
        _sess("2026-06-26", "z2", 60),
        _sess("2026-06-27", "rest", 0),
        _sess("2026-06-28", "rest", 0),
    ])
    applied = app._auto_apply_missed_moves(plan, date(2026, 6, 25))
    assert "2026-06-25" in [a["to"] for a in applied], applied
    by_day = {s["day"]: s for s in plan["weeks"][0]["sessions"]}
    hard_days = sorted(d for d, s in by_day.items()
                       if s["session_type"] in ("vo2max", "threshold") and s["status"] != "missed")
    for a, b in zip(hard_days, hard_days[1:]):
        assert (date.fromisoformat(b) - date.fromisoformat(a)).days >= 2, hard_days
