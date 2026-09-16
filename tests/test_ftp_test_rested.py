"""An FTP test is taken rested.

A test measures fitness only on fresh legs, and its number sets every zone of
the block after it (Allen & Coggan). The plan put a mid-cycle test at the
start of build2 or peak, wherever the 3:1 rhythm had that week, and barred it
from stepback weeks; a block starting two load weeks after an unload tested
tired. The owner's decision: test when rested. A test ends the unload week
before the block it calibrates, on a day after two easy days and before no
hard one (48 h).
"""
from datetime import date, timedelta

import pytest

import training_planner as tp

MONDAY = date(2026, 9, 14)
_TODAY = [MONDAY]
EASY = ("rest", "z2", "long_z2", "recovery")


class _Today(date):
    @classmethod
    def today(cls):
        d = _TODAY[0]
        return cls(d.year, d.month, d.day)


@pytest.fixture(autouse=True)
def _frozen(monkeypatch):
    _TODAY[0] = MONDAY
    monkeypatch.setattr(tp, "date", _Today)


def _assert_rested(weeks, after=None):
    by_day = {s.day: s for w in weeks for s in w.sessions}
    week_of = {s.day: w for w in weeks for s in w.sessions}
    tests = sorted(d for d, s in by_day.items()
                   if s.session_type == "ftp_test" and (after is None or d > after))
    assert tests, "no FTP test, so this proves nothing"
    for d in tests:
        w = week_of[d]
        assert by_day[d].zwo_file, f"{d}: a test with no protocol file to ride"
        assert tp._is_unload_week(w), f"{d} tests in a load week ({w.phase} week {w.week_num})"
        for k in (1, 2):
            p = by_day.get(d - timedelta(days=k))
            assert p is None or p.session_type in EASY, \
                f"{d}: {p.session_type} {k} day(s) before the test"
        p = by_day.get(d - timedelta(days=1))
        assert p is None or p.session_type != "long_z2", f"{d}: the long ride the day before"
        nxt = by_day.get(d + timedelta(days=1))
        assert nxt is None or not tp._session_is_hit(nxt), f"{d}: hard the day after the test"


@pytest.mark.parametrize("owner", [False, True])
@pytest.mark.parametrize("n,event_day", [(10, 5), (16, 6), (20, 5)])
def test_a_mid_cycle_test_ends_an_unload_week(monkeypatch, owner, n, event_day):
    monkeypatch.setattr(tp, "_USE_TRAINING_WEEK", owner)
    goal = tp.Goal(goal_type="event", event_type="granfondo", event_km=160,
                   event_climb_m=2000, target_date=MONDAY + timedelta(weeks=n, days=event_day),
                   hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=4.0,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=0)
    weeks = tp.generate_plan(goal, seed_salt=2, current_ctl=50.0, recent_weekly_tss=350.0,
                             athlete={"ftp": 240, "weight_kg": 72})[1]
    _assert_rested(weeks)


def test_the_long_ride_never_lands_the_day_before_a_test():
    """The event long-ride pass runs after the test is placed. It grew the
    Saturday before a Sunday test into a 130-minute ride."""
    sat = MONDAY + timedelta(days=5)
    week = [tp.PlannedSession(day=sat, day_name="Sat", session_type="z2",
                              duration_min=60, tss_estimate=45.0, description=""),
            tp.PlannedSession(day=sat + timedelta(days=1), day_name="Sun",
                              session_type="ftp_test", duration_min=60,
                              tss_estimate=70.0, description="")]
    tp._apply_long_ride_target(week, target_min=150, max_weekend_min=240, is_stepback=False)
    assert week[0].duration_min == 60


def test_a_recalculated_plan_tests_in_an_unload_week():
    """Recalculate schedules a test six weeks after the last; a due test used
    to skip the unload week for the next load one."""
    goal = tp.Goal(goal_type="event", event_type="granfondo", event_km=160,
                   event_climb_m=2000, target_date=MONDAY + timedelta(weeks=20, days=5),
                   hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=4.0,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=0)
    athlete = {"ftp": 240, "weight_kg": 72}
    base = tp.generate_plan(goal, seed_salt=2, current_ctl=50.0, recent_weekly_tss=350.0,
                            athlete=athlete)[1]
    today = MONDAY + timedelta(weeks=5)
    _TODAY[0] = today
    rides = [{"date": (today - timedelta(days=i)).isoformat(), "tss": 80.0}
             for i in range(1, 15)]
    _p, rebuilt, info = tp.recalculate_plan(goal, base, 50.0, recent_activities=rides,
                                            athlete=athlete)
    assert info.get("action") != "no_change", info.get("reason")
    _assert_rested(rebuilt, after=today)
