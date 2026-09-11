"""A week's budget is one number: the load that lifts fitness at the phase's
rate, within the guards (Step 5 part 3).

The owner's decision: load ramps by Couzens, and the build carries the most it
safely can. The planner had a flat target per phase, capped at 1.3 x the load
the rider started from, which the volume pass then filled to that same cap. A
16-week plan for a rider at 300 TSS a week took CTL from 43 to 49 by the taper,
with 8 weeks prescribed over their own budget.
"""
from datetime import date, timedelta
from itertools import takewhile

import pytest

import plan_invariants as pi
import training_planner as tp

MONDAY = date(2026, 9, 14)


class _Today(date):
    @classmethod
    def today(cls):
        return cls(MONDAY.year, MONDAY.month, MONDAY.day)


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    monkeypatch.setattr(tp, "date", _Today)


def _plan(ctl, chronic, hours=12.0):
    goal = tp.Goal(goal_type="event", event_type="granfondo", event_km=160,
                   event_climb_m=2000, target_date=MONDAY + timedelta(weeks=16, days=5),
                   hours_per_week=hours, max_weekday_hours=2.0, max_weekend_hours=4.5,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=0)
    return tp.generate_plan(goal, seed_salt=1, current_ctl=float(ctl),
                            recent_weekly_tss=float(chronic),
                            athlete={"ftp": 240, "weight_kg": 72})[1]


def _load(w):
    return sum((s.tss_estimate or 0) for s in w.sessions if s.session_type != "rest")


def test_the_plan_builds_fitness():
    """The flat plan took the 300-TSS rider from CTL 43 to 49 by the taper."""
    pre_taper = list(takewhile(lambda w: w.phase != "taper", _plan(43, 300)))
    assert pi.projected_ctl(pre_taper, 43.0)[-1][1] >= 58


def test_no_week_is_prescribed_over_its_budget():
    """The base fill prescribed 8 of that rider's weeks over their own label."""
    for ctl, chronic in ((36, 250), (43, 300), (55, 385)):
        over = [w.week_num for w in _plan(ctl, chronic)
                if w.phase != "taper" and _load(w) > 1.05 * w.tss_target]
        assert not over, (ctl, over)


def test_load_weeks_rise_from_the_riders_own_load():
    """The flat labels put a 300-TSS rider's base at 273: a detraining base."""
    weeks = [w for w in _plan(43, 300)
             if not w.is_stepback and w.phase in ("base", "build1", "build2")]
    assert weeks[0].tss_target >= 300
    bases = [w.tss_target for w in weeks if w.phase == "base"]
    builds = [w.tss_target for w in weeks if w.phase.startswith("build")]
    assert min(builds) > max(bases)


def test_available_hours_are_a_ceiling():
    """D5: six hours a week carry at most 6 x 65 TSS, whatever the rider's load."""
    assert max(w.tss_target for w in _plan(55, 500, hours=6.0)) <= 6 * 65


def test_the_acwr_holds_for_a_trained_rider():
    """A guard on the ramp rather than a regression test (the flat plan never
    ramped): plan_invariants.check_acwr, from the rider's own load. A 150-TSS
    rider still crosses 1.5 in two or three build weeks: passes that run after
    a week is built shave the weeks before it, which the ramp cannot see
    (Step 6)."""
    for ctl, chronic in ((43, 300), (55, 385), (70, 490)):
        bad = pi.check_acwr(_plan(ctl, chronic), chronic)
        assert not bad, (ctl, [str(v) for v in bad])


def test_a_backdated_plans_elapsed_weeks_build_nothing():
    """A plan declared eight weeks in ramps from the rider's fitness today:
    their CTL already holds what they rode. Fed the elapsed weeks as if ridden
    to plan, the ramp asked 468 TSS of the week they enter, from a rider
    carrying 245 (1.91x)."""
    goal = tp.Goal(goal_type="event", event_type="granfondo", event_km=160,
                   event_climb_m=2000, target_date=MONDAY + timedelta(weeks=8, days=6),
                   hours_per_week=12.0, max_weekday_hours=2.0, max_weekend_hours=4.5,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=0,
                   start_date=MONDAY - timedelta(weeks=8), entry_mode="declared")
    weeks = tp.generate_plan(goal, seed_salt=1, current_ctl=35.0, recent_weekly_tss=35.0 * 7,
                             athlete={"ftp": 240, "weight_kg": 72})[1]
    bad = pi.check_acwr(weeks, 35.0 * 7, MONDAY)
    assert not bad, [str(v) for v in bad]


def _week(i, loads, stepback=False, target=0.0):
    start = MONDAY + timedelta(weeks=i)
    return tp.PlannedWeek(
        week_num=i + 1, start=start, end=start + timedelta(days=6), phase="base",
        tss_target=target, is_stepback=stepback,
        sessions=[tp.PlannedSession(day=start + timedelta(days=d), day_name="",
                                    session_type="rest" if load == 0 else "z2",
                                    duration_min=0 if load == 0 else 60,
                                    tss_estimate=float(load), description="")
                  for d, load in enumerate(loads)])


def test_rest_days_do_not_cut_an_unload_week_past_issurin():
    """The rule that gives an unload week more rest days than its load weeks
    took a quarter of the week per rest day from a rider on four days: 88 TSS
    against a 179 budget. It stops where the week would fall under 60% of the
    load week before it (Issurin: a 20-30% cut; past ~40% the rider detrains),
    which is 0.60 / 0.72 of a stepback's budget."""
    block = [_week(i, [0, 0, 60, 0, 60, 70, 70]) for i in range(3)]
    unload = _week(3, [0, 0, 45, 0, 45, 50, 50], stepback=True, target=190.0)
    tp._enforce_stepback_is_lightest([*block, unload])
    assert _load(unload) >= 190 * 0.60 / 0.72 - 1
