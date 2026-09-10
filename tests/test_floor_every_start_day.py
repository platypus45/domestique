"""A continuous plan keeps its anaerobic and neuromuscular floor on every start day.

test_354 caught it on a Friday. The floor put its anaerobic session on the
Saturday after Friday's sprint, the 48 h spacing pass eased it, and the plan
had none; on a Saturday start the neuromuscular session went the same way.
Spacing outranks a variety floor (notes/overhaul-plan.md D1), so the floor
now takes a day the 48 h rule allows, anywhere in the phase, before it takes
a clashing one.
"""
import collections
from datetime import date, timedelta

import pytest

import plan_invariants as pi
import training_planner as tp

WEEK = [date(2026, 9, 7) + timedelta(days=k) for k in range(7)]


@pytest.mark.parametrize("start", WEEK, ids=lambda d: d.strftime("%a"))
def test_the_floor_holds_on_every_start_day(start, monkeypatch):
    class _Today(date):
        @classmethod
        def today(cls):
            return cls(start.year, start.month, start.day)
    monkeypatch.setattr(tp, "date", _Today)
    goal = tp.Goal(goal_type="continuous", target_date=None, hours_per_week=8.0, focus="both")
    _p, weeks = tp.generate_plan(goal, current_ctl=50.0, recent_weekly_tss=500.0)
    classes = collections.Counter(tp._content_class_for_zwo(s.zwo_file or "")
                                  for w in weeks for s in w.sessions
                                  if s.session_type != "rest")
    assert classes["anaerobic"] >= 1 and classes["neuromuscular"] >= 1, dict(classes)
    spacing = [str(v) for v in pi.audit(weeks, goal, today=start) if v.rule == "spacing"]
    assert not spacing, spacing
