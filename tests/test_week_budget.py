"""A week's budget is one number: the load that lifts fitness at the phase's
rate, within the guards (Step 5 part 3).

The owner's decision: load ramps by Couzens, and the build carries the most it
safely can. The planner had a flat target per phase, capped at 1.3 x the load
the rider started from, which the volume pass then filled to that same cap. A
16-week plan for a rider at 300 TSS a week took CTL from 43 to 49 by the taper,
with 8 weeks prescribed over their own budget.
"""
import functools
from datetime import date, timedelta
from itertools import takewhile

import pytest

import plan_invariants as pi
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


@functools.lru_cache(maxsize=None)
def _plan(ctl, chronic, hours=12.0):
    goal = tp.Goal(goal_type="event", event_type="granfondo", event_km=160,
                   event_climb_m=2000, target_date=MONDAY + timedelta(weeks=16, days=5),
                   hours_per_week=hours, max_weekday_hours=2.0, max_weekend_hours=4.5,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=0)
    return tuple(tp.generate_plan(goal, seed_salt=1, current_ctl=float(ctl),
                                  recent_weekly_tss=float(chronic), athlete=ATHLETE)[1])


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


def _block(phase, ctl=100.0):
    """The ramp fed its own budgets for three four-week blocks of one phase
    (three load weeks and an unload), from a rider in steady state at
    ``ctl``, through the performance model day by day over evenly spread
    days: the CTL each block gains, and TSB's lowest."""
    ramp = tp.LoadRamp(ctl, 7 * ctl, None)
    fit = tired = ctl
    gains, low = [], 0.0
    for _block_no in range(3):
        start = fit
        for wk in range(4):
            load = ramp.budget(phase, wk == 3)
            for _day in range(7):
                fit += (load / 7 - fit) / 42
                tired += (load / 7 - tired) / 7
                low = min(low, fit - tired)
            ramp.advance(load, 7, wk != 3)
        gains.append(fit - start)
    return gains, low


def test_each_phase_loads_as_couzens_rule_says():
    """k decides a fit rider's load weeks (CTL 100), where the sweet spot
    does not bind. Base takes Couzens' "1,2,3": about 10 CTL a four-week
    block, TSB bottoming near -20. The build takes the most that stays in
    his 10-20 a block and above -30, the floor of Friel's productive band.
    As this ramp lays a block out (CTL re-read weekly, the unload at 0.72),
    k = 45 bottomed at -34 (the second part 3 review, M-2). Nothing noticed
    k before: 30, 45 or 60 all passed."""
    gains, low = _block("base")
    assert all(9 <= g <= 13 for g in gains) and -25 <= low <= -20, (gains, low)
    gains, low = _block("build1")
    assert all(g <= 20 for g in gains) and -31 <= low <= -27, (gains, low)


def test_the_goals_target_stops_the_ramp():
    """No week takes the ramp's CTL past the goal's target: a rider at CTL 80
    aiming for 85 levels off there (the second part 3 review, L-8: nothing
    caught the cap's removal)."""
    ramp = tp.LoadRamp(80.0, 560.0, 85.0)
    for wk in range(12):
        ramp.advance(ramp.budget("build1", wk % 4 == 3), 7, wk % 4 != 3)
        assert ramp.ctl <= 85.5, (wk, ramp.ctl)


def test_the_sweet_spot_guards_the_cuts_too():
    """A cut is taken from the last full load week, which a holiday can leave
    far behind: the unload after two weeks off was budgeted at 343 against a
    cap of 248 (the second part 3 review, L-1)."""
    ramp = tp.LoadRamp(45.0, 315.0, None)
    for load in (330, 350, 0, 0):
        ramp.advance(load, 7, load > 0)
    cap = tp.ACWR_CEILING * ramp.chronic
    assert ramp.budget("build1", is_stepback=True) <= cap + 1, cap


_STEP6 = "passes move weeks after the ramp has counted them (Step 6)"


@pytest.mark.parametrize("ctl, chronic", [
    (36, 250),
    pytest.param(43, 300, marks=pytest.mark.xfail(strict=True, reason=_STEP6)),
    (55, 385),
    pytest.param(70, 490, marks=pytest.mark.xfail(strict=True, reason=_STEP6)),
    # the owner, carrying more than CTL x 7: week 6 reads 1.45x (Step 6).
    pytest.param(33, 256, marks=pytest.mark.xfail(strict=True, reason=_STEP6)),
])
def test_the_acwr_holds(ctl, chronic):
    """No week over 1.3x the load the rider carries into it (a 28-day EWMA),
    the top of Gabbett's (2016) sweet spot, in any phase, from riders at 250
    TSS a week up. The ramp budgets every week within it; passes then cut
    weeks it has counted (the per-day clamp after the week-total pass), so
    the 300- and 490-TSS riders' plans read 1.39x and 1.37x, and the 150-TSS
    rider's crosses too. The EWMA weighs the latest week 0.39 where the
    rolling mean weighed it 0.25, so a cut shows more. They stay under the
    danger line (below)."""
    bad = pi.check_acwr(_plan(ctl, chronic), chronic)
    assert not bad, (ctl, [str(v) for v in bad])


@pytest.mark.parametrize("ctl, chronic", [
    pytest.param(28, 150, marks=pytest.mark.xfail(strict=True, reason=_STEP6)),
    (36, 250), (43, 300), (55, 385), (70, 490), (33, 256)])
def test_no_rider_crosses_the_danger_line(ctl, chronic):
    """Gabbett's danger zone starts at 1.5x. The build was budgeted at it, and
    the 150- and 250-TSS riders crossed it (the part 3 review, H2). What the
    rider gets, with no tolerance: the 150-TSS rider's week 10 reads 1.55x,
    364 TSS against 235 carried, where the ramp budgeted 1.3x the 280 it had
    counted before the passes cut those weeks. The auditor's 1.05 tolerance
    had hidden it (the third review, H2)."""
    bad = pi.check_acwr(_plan(ctl, chronic), chronic, tolerance=1.0, limit=pi.ACWR_DANGER)
    assert not bad, (ctl, [str(v) for v in bad])


def test_the_ramp_starts_from_the_load_the_rider_carries():
    """A rider training above CTL x 7 is budgeted from what they carry.

    The start was floored at CTL x 7 against a recent mean that could not
    decay (L1); ride_storage.chronic_weekly_tss decays on its own, so the
    floor only cost fit riders load. The owner carries 256 TSS a week at CTL
    32.6, and the floor budgeted their first build week at 297 where 333 is
    within the sweet spot. Restoring the floor puts the first line back to
    297, and the last line fails if the ceiling is simply gone."""
    assert tp.LoadRamp(32.6, 256.0).budget("build1") == 333
    assert tp.LoadRamp(32.6, None).budget("build1") == 297     # nothing known
    assert tp.LoadRamp(32.6, 180.0).budget("build1") == 234    # and it still cuts


def test_a_rider_with_no_history_starts_somewhere():
    """CTL 0 and no rides: the ACWR had nothing to multiply, so every budget was
    0 (the part 3 review, M4). Couzens' loading rule at CTL 0 is 30 TSS a day."""
    assert tp.LoadRamp(0.0).budget("base") == 7 * tp.LOAD_K_DEFAULT
    assert min(w.tss_target for w in _plan(0, 0) if w.phase != "taper") > 0


def test_the_cuts_read_full_load_weeks():
    """A short row at a phase seam, scaled to a week, is a few days' noise: the
    cuts took the last row, and one taper's budget came out at 151 against a
    pass-trimmed 334."""
    ramp = tp.LoadRamp(50.0, 350.0)
    for weekly, days in ((500.0, 7), (420.0, 7), (350.0, 5), (140.0, 2)):
        ramp.advance(weekly, days, True)
    assert ramp.budget("taper", taper_frac=0.6) == 300        # the most of the full weeks
    assert ramp.budget("base", is_stepback=True) == 302       # 0.72 x the last full one


def test_an_unload_week_stays_under_its_blocks_lightest():
    """B3: 0.72 of the last load week, and at most 0.9 of the block's lightest."""
    ramp = tp.LoadRamp(50.0, 350.0)
    for weekly in (300.0, 500.0):
        ramp.advance(weekly, 7, True)
    assert ramp.budget("base", is_stepback=True) == 270


def _backdated_goal():
    return tp.Goal(goal_type="event", event_type="granfondo", event_km=160,
                   event_climb_m=2000, target_date=MONDAY + timedelta(weeks=8, days=6),
                   hours_per_week=12.0, max_weekday_hours=2.0, max_weekend_hours=4.5,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=0,
                   start_date=MONDAY - timedelta(weeks=8), entry_mode="declared")


def test_a_backdated_plans_elapsed_weeks_build_nothing():
    """A plan declared eight weeks in ramps from the rider's fitness today:
    their CTL already holds what they rode. Fed the elapsed weeks as if ridden
    to plan, the ramp asked 508 TSS of their second week, 1.88x the four
    before it. The dry run behind the labels had the same fault, unseen: the
    phases already behind today carry what today's fitness allows. Checked
    at the danger line, which the fault crossed: passes move the later weeks
    after the ramp has counted them, and week 14 reads 1.45x the load carried
    (Step 6)."""
    weeks = tp.generate_plan(_backdated_goal(), seed_salt=1, current_ctl=35.0,
                             recent_weekly_tss=35.0 * 7, athlete=ATHLETE)[1]
    bad = pi.check_acwr(weeks, 35.0 * 7, MONDAY, limit=pi.ACWR_DANGER)
    assert not bad, [str(v) for v in bad]
    elapsed = [p for p in tp.generate_phases(_backdated_goal(), 35.0, recent_weekly_tss=35.0 * 7)
               if p.end < MONDAY]
    assert elapsed and all(p.weekly_tss_target <= tp.ACWR_CEILING * 35 * 7 + 1 for p in elapsed), (
        [(p.name, p.weekly_tss_target) for p in elapsed])


def test_extend_budgets_the_week_it_appends():
    """A continuous plan's rolling horizon: the week extend appends is budgeted
    by the ramp that followed the weeks it kept, not at the rolling phase's
    label, where plan_week falls back when it is handed no budget (the part 3
    review found no test catching that)."""
    goal = tp.Goal(goal_type="continuous", hours_per_week=10.0, max_weekday_hours=2.0,
                   max_weekend_hours=3.0, available_days=[1, 2, 3, 4, 5, 6], rest_days=[0],
                   plan_weeks=4)
    _p, weeks = tp.generate_plan(goal, seed_salt=1, current_ctl=40.0,
                                 recent_weekly_tss=280.0, athlete=ATHLETE)
    _TODAY[0] = MONDAY + timedelta(weeks=1)
    phases, all_weeks, info = tp.extend_continuous_plan(goal, weeks, 40.0,
                                                        recent_weekly_tss=280.0, athlete=ATHLETE)
    assert info["action"] == "extended"
    appended = all_weeks[-1]
    label = phases[0].weekly_tss_target
    fallback = round(label * tp.STEPBACK_LOAD_FACTOR) if appended.is_stepback else label
    assert appended.tss_target != fallback, (appended.tss_target, label)


def test_the_home_card_sizes_the_plans_own_week(monkeypatch, tmp_path):
    """The home card (/api/weekly-plan) sized its week from the phase's label,
    a projection that runs above the built weeks: 694 TSS for a build week the
    plan budgets at 540 (the part 3 review, M2). It reads the plan's own rows."""
    import json
    goal = tp.Goal(goal_type="event", event_type="granfondo", event_km=160,
                   event_climb_m=2000, target_date=MONDAY + timedelta(weeks=16, days=5),
                   hours_per_week=12.0, max_weekday_hours=2.0, max_weekend_hours=4.5,
                   available_days=[1, 2, 3, 4, 5, 6], rest_days=[0], plan_weeks=0)
    phases, weeks = tp.generate_plan(goal, seed_salt=1, current_ctl=43.0,
                                     recent_weekly_tss=300.0, athlete=ATHLETE)
    monkeypatch.setattr(tp, "PLAN_DIR", tmp_path)
    (tmp_path / "current_plan.json").write_text(json.dumps(
        {"goal": tp.goal_to_dict(goal), "weeks": [tp.week_to_dict(w) for w in weeks]}))
    week = next(w for w in weeks if w.phase == "build2" and not w.is_stepback)
    phase = next(p for p in phases if p.name == "build2")
    assert phase.weekly_tss_target > 1.15 * week.tss_target      # the label runs above
    _TODAY[0] = week.start
    card = tp.generate_weekly_plan(goal=goal, current_phase=phase, current_ctl=43.0)
    assert _load(card) <= 1.10 * week.tss_target, (_load(card), week.tss_target)


def _week(i, loads, stepback=False, target=0.0, away=False):
    start = MONDAY + timedelta(weeks=i)
    return tp.PlannedWeek(
        week_num=i + 1, start=start, end=start + timedelta(days=6), phase="base",
        tss_target=target, is_stepback=stepback,
        sessions=[tp.PlannedSession(day=start + timedelta(days=d), day_name="",
                                    session_type="rest" if load == 0 else "z2",
                                    duration_min=0 if load == 0 else 60,
                                    tss_estimate=float(load),
                                    description=tp.REST_UNAVAILABLE if away else "")
                  for d, load in enumerate(loads)])


def test_a_week_away_resets_the_block_the_cuts_read():
    """The ramp reads load or unload from the row it follows. Counted from
    the stepback flag alone, a week the rider was away for stayed in the
    block, and the next unload was cut against a load week from before it
    (the third part 3 review, M2: nothing failed when the flag was put
    back)."""
    ramp = tp.LoadRamp(45.0, 315.0)
    for w in (_week(0, [0, 0, 40, 0, 40, 60, 60]),          # 200
              _week(1, [0] * 7, away=True),                  # a week away
              _week(2, [0, 0, 80, 0, 80, 120, 120]),         # 400
              _week(3, [0, 0, 80, 0, 80, 130, 130])):        # 420
        ramp.follow(w, 600.0)
    assert ramp.budget("base", is_stepback=True) == round(420 * tp.STEPBACK_LOAD_FACTOR)


def test_a_week_away_leaves_the_blocks_lightest_alone():
    """B3 cuts an unload clearly under its block's lightest load week. Its
    block stopped at stepback flags alone, so a week away was the lightest
    and the unload after one was cut to 21 TSS (the third review, M2)."""
    block = [_week(0, [0, 0, 60, 0, 60, 70, 70]), _week(1, [0] * 7, away=True),
             _week(2, [0, 0, 60, 0, 60, 70, 70])]
    unload = _week(3, [0, 0, 45, 0, 45, 50, 50], stepback=True, target=190.0)
    tp._enforce_stepback_is_lightest([*block, unload])
    assert _load(unload) >= 190 * tp.STEPBACK_DEEPEST / tp.STEPBACK_LOAD_FACTOR - 1


def test_a_short_row_moves_the_load_carried_by_its_days():
    """A three-day row is three days of load, not a week of it."""
    ramp, week = tp.LoadRamp(50.0, 350.0), tp.LoadRamp(50.0, 350.0)
    ramp.advance(700.0, 3, True)
    week.advance(700.0, 7, True)
    assert 350.0 < ramp.chronic < week.chronic


def test_rest_days_leave_an_unload_week_near_its_budget():
    """The rule that gives an unload week more rest days than its load weeks
    took a quarter of the week per rest day from a rider on four days: 88 TSS
    against a 179 budget. It stops a sixth under the week's budget
    (STEPBACK_DEEPEST / STEPBACK_LOAD_FACTOR): the ramp has counted the week at
    its budget, and the weeks after it are budgeted from that."""
    block = [_week(i, [0, 0, 60, 0, 60, 70, 70]) for i in range(3)]
    unload = _week(3, [0, 0, 45, 0, 45, 50, 50], stepback=True, target=190.0)
    tp._enforce_stepback_is_lightest([*block, unload])
    assert _load(unload) >= 190 * tp.STEPBACK_DEEPEST / tp.STEPBACK_LOAD_FACTOR - 1
