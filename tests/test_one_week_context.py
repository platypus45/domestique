"""Every entry point builds a week from the same context.

The week builder's inputs were reassembled at six call sites that disagreed on
17 of 24 arguments. Refit sampled a fixed_core plan with the random sampler --
one missed hard day cost both long rides and added two hard sessions -- always
as week 0 of its phase, and with no emphasis. The owner's regenerate dropped
the event's climbing emphasis (notes/review/dupes.md DUP-4, owner.md OWN-9).

The tests watch which builder each entry point calls, and with what context.
"""
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
def _frozen(monkeypatch):
    _TODAY[0] = MONDAY
    monkeypatch.setattr(tp, "date", _Today)


@pytest.fixture
def calls(monkeypatch):
    """(builder, phase, week_num, week_in_phase, emphasis) for every week built."""
    seen = []
    for name in ("sample_week_workouts", "expand_blueprint_week"):
        orig = getattr(tp, name)

        def spy(ctx, *a, _orig=orig, _name=name, **k):
            seen.append((_name, ctx.phase.name, ctx.week_num, ctx.week_in_phase,
                         ctx.emphasis_profile))
            return _orig(ctx, *a, **k)
        monkeypatch.setattr(tp, name, spy)
    return seen


def _goal(mode="auto"):
    # 200 km with 3500 m of climbing: 17.5 m/km, a climbing route (> 12 m/km)
    return tp.Goal(goal_type="event", event_type="granfondo", event_km=200,
                   event_climb_m=3500, target_date=MONDAY + timedelta(weeks=14),
                   hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=5.0,
                   available_days=list(range(7)), rest_days=[0], daily_max_hours={},
                   plan_weeks=14, plan_mode=mode)


def _plan(goal):
    return tp.generate_plan(goal, seed_salt=2, current_ctl=50.0, recent_weekly_tss=380.0,
                            athlete=ATHLETE)[1]


def _a_refit_case(weeks):
    """A week that is not the first of its phase, holding a hard session with
    trainable days after it. That session is missed; refit runs the next day."""
    for i, w in enumerate(weeks):
        if i == 0 or weeks[i - 1].phase != w.phase or w.phase == "taper":
            continue
        for s in w.sessions:
            later = [x for x in w.sessions if x.day > s.day and x.session_type != "rest"]
            if tp._session_is_hit(s) and len(later) >= 2:
                s.status = "missed"
                return w, s.day + timedelta(days=1)
    raise AssertionError("no week to refit in this plan")


def _index_in_phase(weeks, week):
    """How many weeks of the same phase come straight before ``week``."""
    i = next(k for k, w in enumerate(weeks) if w is week)
    n = 0
    while i - n - 1 >= 0 and weeks[i - n - 1].phase == week.phase:
        n += 1
    return n


@pytest.mark.parametrize("mode, builder", [("fixed_core", "expand_blueprint_week"),
                                           ("auto", "sample_week_workouts")])
def test_refit_builds_the_week_the_way_the_plan_was_built(calls, mode, builder):
    goal = _goal(mode)
    weeks = _plan(goal)
    week, today = _a_refit_case(weeks)
    _TODAY[0] = today
    calls.clear()
    tp.refit_remaining_week(goal, weeks, today, seed_salt=2, athlete=ATHLETE)
    assert calls, "refit built nothing"
    name, _phase, _num, in_phase, _emph = calls[-1]
    assert name == builder, f"a {mode} plan was refitted by {name}"
    want = _index_in_phase(weeks, week)
    assert in_phase == want, \
        f"refit built week {week.week_num} as week {in_phase} of its phase, not {want}"


def test_the_owners_regenerate_keeps_the_climbing_emphasis(calls, monkeypatch):
    monkeypatch.setattr(tp, "_USE_TRAINING_WEEK", True)
    goal = _goal()
    weeks = _plan(goal)
    today = MONDAY + timedelta(days=17)
    _TODAY[0] = today
    calls.clear()
    tp.regenerate_from_today(goal, weeks, 50.0, seed_salt=2, athlete=ATHLETE,
                             activities=[{"date": (today - timedelta(days=i)).isoformat(),
                                          "tss": 70.0} for i in range(1, 11)])
    climbing = [c for c in calls if c[1] in ("build2", "peak")]
    assert climbing, "no build2 or peak week was rebuilt, so this proves nothing"
    assert all(c[4] == "event_climb" for c in climbing), sorted({c[4] for c in climbing}, key=str)
