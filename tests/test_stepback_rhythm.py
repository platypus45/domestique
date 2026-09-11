"""The 3:1 loading rhythm is the plan's, not the button's.

Three load weeks, then an unloading week (Rønnestad; Issurin: a 20-30% cut).
Generate counted weeks across the plan, regenerate and recalculate restarted
the count at every phase, and extend used the week number. So the recovery
rhythm depended on which button was pressed: after a regenerate, up to six
load weeks in a row (notes/review/dupes.md DUP-3). One predicate now counts the
load weeks since the last unload, and a rebuild continues the count.
"""
import json
from datetime import date, timedelta

import pytest

import training_planner as tp

MONDAY = date(2026, 9, 14)
ATHLETE = {"ftp": 240, "weight_kg": 72}
_TODAY = [MONDAY]


class _Today(date):
    @classmethod
    def today(cls):
        d = _TODAY[0]
        return cls(d.year, d.month, d.day)


@pytest.fixture(autouse=True)
def _frozen_today(monkeypatch):
    _TODAY[0] = MONDAY
    monkeypatch.setattr(tp, "date", _Today)


def _goal():
    return tp.Goal(goal_type="event", event_type="granfondo", event_km=180,
                   event_climb_m=2500, target_date=MONDAY + timedelta(weeks=24, days=6),
                   hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=4.5,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=25)


def _unload(w):
    return w.is_stepback or w.phase in ("taper", "recon", "recovery_ramp")


def _longest_load_run(weeks):
    run = best = 0
    for w in sorted(weeks, key=lambda w: w.start):
        run = 0 if _unload(w) else run + 1
        best = max(best, run)
    return best


def _rides(first, last, tss=80.0):
    out, d = [], first
    while d <= last:
        out.append({"date": d.isoformat(), "tss": tss})
        d += timedelta(days=1)
    return out


def _generated():
    return tp.generate_plan(_goal(), seed_salt=3, current_ctl=50.0,
                            recent_weekly_tss=380.0, athlete=ATHLETE)[1]


def test_generate_unloads_every_fourth_week():
    """The rhythm a plan built in one go has always had."""
    weeks = _generated()
    assert [w.week_num for w in weeks if w.is_stepback][:3] == [4, 8, 12]
    assert _longest_load_run(weeks) <= 3


# Rebuild points where the old counters broke the rhythm, measured on this
# plan: regenerate ran 4 and 5 load weeks in a row at weeks 1 and 2 (week 9 is
# a control), and recalculate ran 6, 5 and 6 at weeks 2, 5 and 14.
@pytest.mark.parametrize("weeks_in", [1, 2, 9])
def test_a_regenerate_keeps_the_rhythm(weeks_in):
    base = _generated()
    today = MONDAY + timedelta(weeks=weeks_in)
    _TODAY[0] = today
    rebuilt = tp.regenerate_from_today(
        _goal(), base, 50.0, activities=_rides(today - timedelta(days=12), today),
        seed_salt=3, athlete=ATHLETE)[1]
    assert _longest_load_run(rebuilt) <= 3


def test_a_regenerate_after_an_absence_counts_the_recovery_ramp():
    base = _generated()
    today = MONDAY + timedelta(weeks=9)
    _TODAY[0] = today
    rebuilt = tp.regenerate_from_today(
        _goal(), base, 42.0,
        activities=_rides(today - timedelta(days=45), today - timedelta(days=21)),
        seed_salt=3, athlete=ATHLETE)[1]
    assert any(w.phase in ("recon", "recovery_ramp") for w in rebuilt), \
        "no recovery ramp was built, so this case proves nothing"
    assert _longest_load_run(rebuilt) <= 3


@pytest.mark.parametrize("weeks_in", [2, 5, 14])
def test_a_recalculation_keeps_the_rhythm(weeks_in):
    base = _generated()
    today = MONDAY + timedelta(weeks=weeks_in)
    _TODAY[0] = today
    _p, rebuilt, info = tp.recalculate_plan(
        _goal(), base, 50.0, recent_activities=_rides(today - timedelta(days=14), today),
        athlete=ATHLETE)
    assert info.get("action") != "no_change", f"nothing was rebuilt: {info.get('reason')}"
    assert _longest_load_run(rebuilt) <= 3


def _week(i, stepback=False):
    return tp.PlannedWeek(week_num=i, start=MONDAY + timedelta(weeks=i),
                          end=MONDAY + timedelta(weeks=i, days=6), phase="continuous",
                          tss_target=400, is_stepback=stepback, sessions=[])


def test_a_deload_the_app_advanced_restarts_the_count():
    """A deload advanced into a week (monotony or ACWR tripped) is an unload
    week; the scheduled stepback must not land two weeks after it."""
    weeks = [_week(1), _week(2, stepback=True), _week(3), _week(4)]
    assert not tp.stepback_due(weeks, "continuous")
    assert tp.stepback_due(weeks + [_week(5)], "continuous")


def test_the_home_cards_week_unloads_when_the_plan_does(tmp_path, monkeypatch):
    """The home page's week counted weeks from the plan's first Monday and
    unloaded one late: weeks 5, 9 and 13 against the plan's 4, 8 and 12 (the
    Step 5 review, L3). It reads the plan's own week."""
    weeks = _generated()
    (tmp_path / "current_plan.json").write_text(
        json.dumps({"weeks": [tp.week_to_dict(w) for w in weeks]}))
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    card, plan = [], []
    for w in weeks[:12]:
        _TODAY[0] = w.start
        card.append(tp.generate_weekly_plan(_goal(), current_ctl=50).is_stepback)
        plan.append(w.is_stepback)
    assert any(plan), "no stepback in the first twelve weeks, so this proves nothing"
    assert card == plan
