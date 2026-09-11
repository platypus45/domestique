"""Every entry point plans from the same athlete.

generate_phases derives each phase's load from two facts about the rider:
their chronic weekly load, which the ACWR guard multiplies (Gabbett 2016:
acute load past ~1.3x chronic is where injury risk climbs), and the CTL the
goal's rule aims for, reached by a safe ramp (Couzens). Generate supplied the
first; regenerate, recalculate, the phase preview and the entry scan did not,
and fell back to hours_per_week x 65. Regenerate also replaced the goal's
target rule with the ramp ceiling itself (notes/review/dupes.md DUP-1, DUP-14).

The rider's load comes from the ride archive here, as it does in production,
where the app hands recalculate nothing: an entry point that does not read it
plans from some other rider, and fails these.
"""
from datetime import date, timedelta

import pytest

import plan_invariants as pi
import training_planner as tp

MONDAY = date(2026, 9, 14)
RECENT = 250.0                                   # the rider's chronic weekly load
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
    else. It showed 1.8x the load Generate then built. It must lay out the
    phases Generate lays out, from the rider's load: the archive's, and a
    rider carrying more gets more."""
    def labels(**kw):
        return [(p.name, p.start, p.weekly_tss_target)
                for p in tp.generate_phases(_goal(), 55.0, **kw)]
    assert labels() == labels(recent_weekly_tss=RECENT)
    assert labels() != labels(recent_weekly_tss=2 * RECENT)


def _rebuild(entry):
    """The base plan six weeks in, rebuilt for a rider who rode 315 TSS every
    week of it, less than its load weeks asked: no absence, so the rebuild
    ramps from their load rather than a return-from-absence ramp. Returns the
    weeks and the first day the rebuild lays out: regenerate plans from today,
    recalculate from the Monday after the week in progress."""
    base = _base(_goal())
    today = MONDAY + timedelta(weeks=6)
    _TODAY[0] = today
    rides = _rides(MONDAY, today - timedelta(days=1))
    if entry == "regenerate":
        _p, weeks, _i = tp.regenerate_from_today(_goal(), base, 55.0, activities=rides,
                                                 seed_salt=1)
        return weeks, today
    _p, weeks, info = tp.recalculate_plan(_goal(), base, 55.0, recent_activities=rides)
    assert info.get("action") != "no_change", "nothing was rebuilt"
    return weeks, next(w.end for w in weeks if w.start <= today <= w.end) + timedelta(days=1)


@pytest.mark.parametrize("entry", ["regenerate", "recalculate"])
def test_a_rebuild_budgets_from_the_riders_load(entry):
    """Each week a rebuild lays out is budgeted within the ACWR of the rider's
    own load and the weeks budgeted since: 1.3x the load carried into it
    (Gabbett 2016; a 28-day EWMA, pi.chronic_after). An entry point that does
    not read the rider's load starts from CTL x 7 or from their free time, and
    breaks this in its first week."""
    weeks, first = _rebuild(entry)
    carried = float(RECENT)
    for w in weeks:
        days = (w.end - w.start).days + 1
        if w.end < first:
            continue
        budget = w.tss_target * 7 / days
        assert days < 4 or w.phase == "taper" or budget <= tp.ACWR_CEILING * carried + 1, (
            entry, w.week_num, budget, carried)
        carried = pi.chronic_after(carried, budget, days)


@pytest.mark.parametrize("entry", ["regenerate", "recalculate"])
def test_no_rebuilt_week_crosses_the_danger_line(entry):
    """What the rider receives, from the first Monday the rebuild lays out: no
    week past 1.5x the load carried into it, whatever the phase
    (plan_invariants.check_acwr at ACWR_DANGER)."""
    weeks, first = _rebuild(entry)
    bad = pi.check_acwr(weeks, RECENT, first, limit=pi.ACWR_DANGER)
    assert not bad, f"{entry}: " + "; ".join(map(str, bad))


def test_a_regenerated_plan_prescribes_within_the_sweet_spot():
    """1.3x, in every phase. The regenerated unload week is built far over its
    budget and trimmed after the ramp has counted it (Step 6). Against a
    4-week rolling mean, three weeks after it read up to 1.41x, a strict
    xfail; against the load carried, a 28-day EWMA, the rebuilt plan holds."""
    weeks, first = _rebuild("regenerate")
    assert not pi.check_acwr(weeks, RECENT, first)


@pytest.mark.xfail(strict=True, reason="recalculate keeps the week in progress as the old "
                   "plan sized it; kept weeks become derived state in Step 6")
def test_recalculate_rebudgets_the_week_in_progress():
    """A plan's later weeks assume the rider rode the earlier ones. Recalculate
    runs because they did not, and keeps the week in progress verbatim: 443 TSS
    here, for a rider carrying 250 (1.77x). The flat cap hid this before the
    ramp (Step 5 part 3). Only that week is checked, so the xfail turns green
    when kept weeks are re-budgeted, whatever else still drifts."""
    weeks, _first = _rebuild("recalculate")
    current = next(w for w in weeks if w.start <= _TODAY[0] <= w.end)
    assert not [v for v in pi.check_acwr(weeks, RECENT, _TODAY[0])
                if v.week_num == current.week_num]


def test_a_regenerate_ramps_on_from_its_recovery_weeks(monkeypatch):
    """Back from an absence, regenerate lays a recovery ramp, and the plan
    ramps on from it: the ramp follows every row laid out from today, the
    recovery weeks first, so the first load week after them is budgeted from
    what they built (the part 3 review found no test catching the ramp
    skipping them). The first version asked that week for more than 1.3x the
    load the rider carried before they stopped, which held only because the
    recovery ramp overshoots that load (the second part 3 review, L-6)."""
    base = _base(_goal())
    followed, follow = [], tp.LoadRamp.follow
    monkeypatch.setattr(tp.LoadRamp, "follow", lambda self, pw, budget: (
        followed.append((pw.start, pw.end)), follow(self, pw, budget))[1])
    today = MONDAY + timedelta(weeks=6)
    _TODAY[0] = today
    rides = _rides(today - timedelta(days=13), today - timedelta(days=1))   # 4 weeks off, 2 back
    _p, weeks, info = tp.regenerate_from_today(_goal(), base, 55.0, activities=rides, seed_salt=1)
    assert info["recovery_ramp_weeks"] > 0
    ahead = sorted((w.start, w.end) for w in weeks if w.end >= today)
    assert [r for r in followed if r[1] >= today] == ahead


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
    """A CTL target is where the ramp stops: no week of the rebuilt plan takes
    the rider past it."""
    goal = _goal("ctl", target_ctl=60.0)
    base = _base(goal)
    today = MONDAY + timedelta(weeks=2)
    _TODAY[0] = today
    _p, weeks, info = tp.regenerate_from_today(
        goal, base, 55.0, activities=_rides(today - timedelta(days=13), today - timedelta(days=1)),
        seed_salt=1)
    assert info["adjusted_target_ctl"] <= 60
    top = max(c for _n, c in pi.projected_ctl(weeks, 55.0, today))
    assert top <= 60 + 1, f"the plan takes CTL to {top:.1f} against a target of 60"


def test_the_apps_refit_hands_the_planner_the_rider(monkeypatch):
    """Refit's event emphasis needs the rider's FTP and weight. The app's refit
    passed neither, so production never saw the emphasis the tests saw, since
    they pass an athlete (the Step 5 review, L2)."""
    import app as app_module
    import profile_manager

    class _Rider:
        _athlete = {"ftp": 240, "weight_kg": 72}
        ftp, weight_kg = 240, 72

    monkeypatch.setattr(profile_manager.ProfileManager, "get", classmethod(lambda cls: _Rider()))
    seen = {}

    def _refit(goal, weeks, today, **kw):
        seen.update(kw)
        return weeks, {"action": "no_change"}

    monkeypatch.setattr(tp, "refit_remaining_week", _refit)
    goal = _goal()
    plan = {"goal": tp.goal_to_dict(goal), "weeks": [tp.week_to_dict(w) for w in _base(goal)]}
    app_module._apply_refit_to_plan(plan, MONDAY + timedelta(days=2))
    assert seen.get("athlete") == {"ftp": 240, "weight_kg": 72}
