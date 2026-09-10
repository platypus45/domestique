"""Every entry point plans from the same athlete.

generate_phases derives each phase's load from two facts about the rider:
their chronic weekly load, which the ACWR ceiling multiplies (Gabbett 2016:
acute load past ~1.3x chronic is where injury risk climbs), and the CTL the
goal's rule aims for, capped by a safe ramp (Couzens). Generate supplied the
first; regenerate, recalculate, the phase preview and the entry scan did not,
and fell back to hours_per_week x 65. Regenerate also replaced the goal's
target rule with the ramp ceiling itself (notes/review/dupes.md DUP-1, DUP-14).

The rider's load comes from the ride archive here, as it does in production,
where the app hands recalculate nothing: so these tests run unchanged against
the previous code, and fail there.
"""
from datetime import date, timedelta

import pytest

import training_planner as tp

MONDAY = date(2026, 9, 14)
RECENT = 250.0                                   # the rider's chronic weekly load
CEILING = RECENT * tp.ACWR_CEILING
_TODAY = [MONDAY]


class _Today(date):
    @classmethod
    def today(cls):
        d = _TODAY[0]
        return cls(d.year, d.month, d.day)


@pytest.fixture(autouse=True)
def _rider(monkeypatch):
    import ride_storage
    _TODAY[0] = MONDAY
    monkeypatch.setattr(tp, "date", _Today)
    monkeypatch.setattr(ride_storage, "recent_mean_weekly_tss", lambda *a, **k: RECENT)


def _goal(goal_type="event", **kw):
    g = dict(goal_type=goal_type, event_type="granfondo", event_km=160, event_climb_m=2000,
             target_date=MONDAY + timedelta(weeks=20, days=6), hours_per_week=14.0,
             max_weekday_hours=2.5, max_weekend_hours=5.0, available_days=[1, 2, 3, 4, 5, 6],
             rest_days=[0], plan_weeks=0)
    g.update(kw)
    return tp.Goal(**g)


def _peak(phases):
    return max(p.weekly_tss_target for p in phases)


def _rides(first, last, tss=45.0):
    out, d = [], first
    while d <= last:
        out.append({"date": d.isoformat(), "tss": tss})
        d += timedelta(days=1)
    return out


def _base(goal):
    return tp.generate_plan(goal, seed_salt=1, current_ctl=55.0,
                            athlete={"ftp": 240, "weight_kg": 72})[1]


def test_the_preview_uses_the_riders_load_not_their_free_time():
    """The preview calls generate_phases with a goal and a CTL, and nothing
    else. It showed 1.8x the load Generate then built."""
    assert _peak(tp.generate_phases(_goal(), 55.0)) <= CEILING + 1
    assert _peak(tp.generate_phases(_goal(), 55.0, recent_weekly_tss=RECENT)) <= CEILING + 1


@pytest.mark.parametrize("entry", ["regenerate", "recalculate"])
def test_a_rebuild_keeps_the_acwr_ceiling(entry):
    base = _base(_goal())
    today = MONDAY + timedelta(weeks=6)
    _TODAY[0] = today
    rides = _rides(today - timedelta(days=13), today - timedelta(days=1))
    if entry == "regenerate":
        phases, _w, _i = tp.regenerate_from_today(_goal(), base, 55.0, activities=rides,
                                                  seed_salt=1)
    else:
        phases, _w, info = tp.recalculate_plan(_goal(), base, 55.0, recent_activities=rides)
        assert info.get("action") != "no_change", "nothing was rebuilt"
    assert _peak(phases) <= CEILING + 1, (
        f"{entry} planned a {_peak(phases)} TSS week against a chronic load of "
        f"{RECENT:.0f} (ACWR ceiling {CEILING:.0f})")


def test_a_regenerate_aims_for_the_goals_own_target():
    """An FTP goal aims for at most CTL 90. Regenerate aimed for whatever the
    ramp allowed: 134 for the reviewer's rider."""
    goal = _goal("ftp", target_date=MONDAY + timedelta(weeks=16, days=6))
    base = _base(goal)
    today = MONDAY + timedelta(weeks=2)
    _TODAY[0] = today
    _p, _w, info = tp.regenerate_from_today(
        goal, base, 55.0, activities=_rides(today - timedelta(days=13), today - timedelta(days=1)),
        seed_salt=1)
    assert info["adjusted_target_ctl"] <= 90


def test_a_regenerate_respects_an_explicit_target():
    goal = _goal("ctl", target_ctl=60.0)
    base = _base(goal)
    today = MONDAY + timedelta(weeks=2)
    _TODAY[0] = today
    phases, _w, info = tp.regenerate_from_today(
        goal, base, 55.0, activities=_rides(today - timedelta(days=13), today - timedelta(days=1)),
        seed_salt=1)
    assert info["adjusted_target_ctl"] <= 60
    assert _peak(phases) <= 60 * 7 + 1
