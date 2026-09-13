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


def test_a_holiday_restarts_the_count():
    """Two weeks marked unavailable are written as weeks of rest days. They
    unload the rider (D6), so the first week back is a load week, not the
    deload the calendar count would make it (the Step 5 review, L4). Part 3
    had a rest-only week count as a load week, since it prescribed nothing
    (the part 3 review, H1), and until it was past: the plan built with the
    holiday ahead still made the first week back a deload (the second part 3
    review, M-5)."""
    hol = (MONDAY + timedelta(weeks=5), MONDAY + timedelta(weeks=7, days=-1))
    _p, weeks = tp.generate_plan(_goal(), seed_salt=1, current_ctl=43.0,
                                 recent_weekly_tss=300.0, athlete=ATHLETE,
                                 unavailable_periods=[hol])
    _TODAY[0] = MONDAY + timedelta(weeks=7)
    past = [w for w in weeks if w.end < _TODAY[0]]
    away = [w for w in past if w.start >= hol[0]]
    assert away and all(tp._went_unridden(w) for w in away)
    assert not tp.stepback_due(past, "build1")
    back = next(w for w in weeks if w.start > hol[1])
    assert not back.is_stepback, f"week {back.week_num}, the first back, is a stepback"


def test_the_weeks_back_from_a_holiday_rise():
    """Two weeks off lower the load the rider carries, and the load weeks
    back rise from it. On a 4-week rolling mean the holiday's zeros held the
    weeks back down and then dropped out of the window: 248, 236, 157, then
    an unload at 113 (the second part 3 review, M-5). The load carried is a
    28-day exponentially weighted mean now (Murray, Gabbett et al. 2017)."""
    hol = (MONDAY + timedelta(weeks=5), MONDAY + timedelta(weeks=7, days=-1))
    _p, weeks = tp.generate_plan(_goal(), seed_salt=1, current_ctl=43.0,
                                 recent_weekly_tss=300.0, athlete=ATHLETE,
                                 unavailable_periods=[hol])
    back = []
    for w in (w for w in weeks if w.start > hol[1]):
        if w.is_stepback:
            break
        back.append(w.tss_target)
    assert len(back) >= 2 and back == sorted(back), back


def _unload(w):
    return w.is_stepback or w.phase in ("taper", "recon", "recovery_ramp")


def _longest_load_run(weeks):
    """Load weeks in a row, in calendar weeks: a row that ends mid-week
    holding fewer than 4 days -- the sliver before a taper -- is part of the
    taper's week, as stepback_due counts it."""
    run = best = 0
    for w in sorted(weeks, key=lambda w: w.start):
        if w.end.weekday() + 1 < 4 and (w.end - w.start).days + 1 < 4:
            continue
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


def test_a_sliver_before_the_taper_is_not_a_stepback():
    """The taper is laid back from race day, off the Monday grid on purpose,
    so the week before it ends in a sliver: a lone Monday before a Sunday
    event. The rhythm counted rows, took that day for a week and made it a
    47-TSS "stepback" in 8 of 98 event plans. The rhythm counts calendar
    weeks, and a week belongs to the phase that holds most of its days: a
    sliver's week is the taper's."""
    target = MONDAY + timedelta(weeks=12, days=6)                 # a Sunday
    goal = tp.Goal(goal_type="event", event_type="granfondo", event_km=160,
                   event_climb_m=2000, target_date=target, hours_per_week=10.0,
                   max_weekday_hours=2.0, max_weekend_hours=4.0,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=0)
    weeks = tp.generate_plan(goal, seed_salt=1, current_ctl=50.0,
                             recent_weekly_tss=350.0, athlete=ATHLETE)[1]
    slivers = [w for w in weeks[1:]
               if w.phase != "taper" and (w.end - w.start).days + 1 < 4]
    assert slivers, "no sliver before the taper, so this proves nothing"
    assert not any(w.is_stepback for w in slivers)


def _ridden_week(i, status, target=300):
    start = MONDAY + timedelta(weeks=i)
    return tp.PlannedWeek(
        week_num=i + 1, start=start, end=start + timedelta(days=6), phase="build1",
        tss_target=target, is_stepback=False,
        sessions=[tp.PlannedSession(day=start + timedelta(days=d), day_name="",
                                    session_type="z2", duration_min=60,
                                    tss_estimate=50.0, description="", status=status)
                  for d in range(1, 7)])


def test_weeks_the_rider_missed_restart_the_count():
    """D6: a week the rider did not ride unloaded them, whatever its label.
    After one ridden load week and two fully missed ones, the rhythm still
    asked for a stepback: a deload for someone coming back from two weeks off
    (the Step 5 review, L4)."""
    _TODAY[0] = MONDAY + timedelta(weeks=4)
    back = [_ridden_week(1, "done"), _ridden_week(2, "missed"), _ridden_week(3, "missed")]
    assert not tp.stepback_due(back, "build1")
    assert tp.stepback_due([_ridden_week(i, "done") for i in (1, 2, 3)], "build1")


def test_a_week_away_unloads_the_rider_not_any_week_of_rest_days():
    """A week the rider is away for unloads them, ahead of today or behind
    it. Not every week of rest days, though: a plan made on a Monday after a
    420 TSS ride prescribes nothing for the days left, and that week loaded
    them (the third part 3 review, M1). Nor a stub: a plan made on a
    Thursday opens with Thursday to Sunday at rest, and read as an unload it
    moved every owner-mode continuous plan's stepback."""
    def rest_row(start, days, away=True):
        return tp.PlannedWeek(
            week_num=1, start=start, end=start + timedelta(days=days - 1), phase="base",
            tss_target=300, is_stepback=False,
            sessions=[tp.PlannedSession(day=start + timedelta(days=d), day_name="",
                                        session_type="rest", duration_min=0, tss_estimate=0.0,
                                        description=tp.REST_UNAVAILABLE if away else "Rest day")
                      for d in range(days)])
    _TODAY[0] = MONDAY
    ahead, behind = MONDAY + timedelta(weeks=1), MONDAY - timedelta(weeks=2)
    assert tp._went_unridden(rest_row(ahead, 7)) and tp._went_unridden(rest_row(behind, 7))
    assert not tp._went_unridden(rest_row(ahead, 7, away=False))
    assert not tp._went_unridden(rest_row(behind, 7, away=False))
    assert not tp._went_unridden(rest_row(ahead + timedelta(days=3), 4))
    assert not tp._went_unridden(rest_row(behind + timedelta(days=3), 4))


def test_a_week_ridden_as_prescribed_loaded_the_rider_whatever_its_budget():
    """D6 reads what a week prescribed, not its budget. A builder that fell
    short of a 500 budget with 300 TSS, all of it ridden, loaded the rider;
    read against the budget the week looked unridden, and three of them in a
    row never came round to a stepback. The first test's weeks prescribed
    exactly their budget, so they could not tell (the second part 3 review,
    M-3)."""
    _TODAY[0] = MONDAY + timedelta(weeks=4)
    weeks = [_ridden_week(i, "done", target=500) for i in (1, 2, 3)]
    assert not any(tp._went_unridden(w) for w in weeks)
    assert tp.stepback_due(weeks, "build1")


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


def test_after_the_plan_the_home_card_keeps_its_rhythm(tmp_path, monkeypatch):
    """Past the plan's last week the card switched to ISO week numbers, which
    put up to 8 weeks between two unloads (the fix review, F3). It carries the
    plan's 3:1 on from the plan's last unload week."""
    weeks = _generated()
    (tmp_path / "current_plan.json").write_text(
        json.dumps({"weeks": [tp.week_to_dict(w) for w in weeks]}))
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    anchor = max(w.end for w in weeks if _unload(w))       # the last week it unloads
    anchor -= timedelta(days=anchor.weekday())
    first = weeks[-1].end + timedelta(days=7 - weeks[-1].end.weekday())
    card, want = [], []
    for i in range(9):
        monday = first + timedelta(weeks=i)
        _TODAY[0] = monday
        card.append(tp.generate_weekly_plan(_goal(), current_ctl=50).is_stepback)
        want.append((monday - anchor).days // 7 % tp.STEP_BACK_EVERY == 0)
    assert any(want)
    assert card == want
