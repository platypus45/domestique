"""v2.0.3 F7 (T-REGEN-RAMP) — a mid-plan regen CONTINUES the long-ride ramp.

generate_plan enumerates the event long-ride ramp index ``_wi`` over the FULL
plan; regenerate_from_today (and recalculate_plan) enumerate future-only weeks.
Before F7, ``_wi`` restarted at 0 on a mid-plan regen → the weekend long ride
reset to ``long_start_h`` (the floor) instead of continuing from where the
athlete already was. F7 offsets ``_wi`` by the count of already-elapsed weeks
(past + recovery) so the ramp continues.

To exercise a mid-plan regen deterministically we generate a full event plan,
then shift every week's dates back N weeks so the plan looks like it started N
weeks ago (weeks 0..N-1 become "past"). A regen from today then rebuilds the
future; the first ramp-eligible future week's long ride must be WELL above the
floor (i.e. the ramp picked up mid-stream, not from week 0).
"""
from datetime import date, timedelta

import pytest

import training_planner as tp


_ATHLETE = {"ftp": 250, "weight_kg": 70}


def _event_goal(weeks_out: int) -> tp.Goal:
    return tp.Goal(
        goal_type="event",
        plan_weeks=weeks_out,
        target_date=date.today() + timedelta(weeks=weeks_out),
        event_km=160,
        event_climb_m=2000,
        event_type="gran_fondo",
        hours_per_week=10.0,
        max_weekday_hours=2.0,
        max_weekend_hours=6.0,  # generous so the weekend cap doesn't mask the ramp
        available_days=[1, 2, 3, 4, 5, 6],
        rest_days=[0],
    )


def _long_ride_min(week) -> int:
    """Longest weekend (Sat/Sun) z2/long_z2 session duration in the week."""
    best = 0
    for s in week.sessions:
        d = getattr(s, "day", None)
        wd = d.weekday() if hasattr(d, "weekday") else None
        if wd in (5, 6) and s.session_type in ("z2", "long_z2"):
            best = max(best, int(s.duration_min or 0))
    return best


def _shift_back(weeks, n_weeks: int):
    delta = timedelta(weeks=n_weeks)
    for w in weeks:
        w.start = w.start - delta
        w.end = w.end - delta
        for s in w.sessions:
            if hasattr(s.day, "__sub__"):
                s.day = s.day - delta


def test_mid_plan_regen_continues_long_ride_ramp():
    """First ramp-eligible regenerated week's long ride is far above the floor."""
    today = date.today()
    goal = _event_goal(weeks_out=14)
    _phases, weeks = tp.generate_plan(goal, athlete=_ATHLETE)

    targets = tp._event_demand_targets(goal, _ATHLETE, {"current_ctl": 45.0})
    floor_min = int(round(targets["long_start_h"] * 60))
    step_min = tp.LONG_RIDE_STEP_MIN

    # Make the plan look like it started 6 weeks ago.
    elapsed = 6
    _shift_back(weeks, elapsed)

    _np, all_weeks, _info = tp.regenerate_from_today(
        goal, weeks, current_ctl=45.0, athlete=_ATHLETE,
    )

    # Ramp-eligible future weeks: strictly after today, in a build/base phase
    # (recovery_ramp / recon / taper / stepback are NOT part of the ramp), and
    # NOT clamped by the ≥3-weeks-out taper gate.
    eligible = [
        w for w in all_weeks
        if w.start > today
        and w.phase in ("base", "build1", "build2", "peak")
        and not w.is_stepback
        and (not goal.target_date or (goal.target_date - w.start).days > 21)
        and _long_ride_min(w) > 0
    ]
    assert eligible, "no ramp-eligible future weeks after regen"

    first_long = _long_ride_min(eligible[0])
    # If the ramp RESET, the first eligible week would sit at ~floor (one step
    # at most). Continuing the ramp from ~6 elapsed weeks puts it well past
    # floor + a couple of steps. Require a clear margin so this can't pass by a
    # one-step fluke.
    reset_ceiling = floor_min + 2 * step_min
    assert first_long > reset_ceiling, (
        f"long ride reset to ~floor on mid-plan regen (F7 regression): "
        f"first eligible long={first_long}min, floor={floor_min}min, "
        f"reset_ceiling={reset_ceiling}min"
    )


def test_more_elapsed_weeks_ramps_higher():
    """Monotonicity: regenerating later in the plan (more elapsed weeks) yields
    a higher (or capped-equal) first long ride than regenerating earlier — the
    offset tracks elapsed time."""
    today = date.today()

    def _first_eligible_long(elapsed: int) -> int:
        goal = _event_goal(weeks_out=16)
        _p, weeks = tp.generate_plan(goal, athlete=_ATHLETE)
        _shift_back(weeks, elapsed)
        _np, all_weeks, _info = tp.regenerate_from_today(
            goal, weeks, current_ctl=45.0, athlete=_ATHLETE,
        )
        elig = [
            w for w in all_weeks
            if w.start > today
            and w.phase in ("base", "build1", "build2", "peak")
            and not w.is_stepback
            and (not goal.target_date or (goal.target_date - w.start).days > 21)
            and _long_ride_min(w) > 0
        ]
        return _long_ride_min(elig[0]) if elig else 0

    early = _first_eligible_long(3)
    late = _first_eligible_long(7)
    assert early > 0 and late > 0, f"missing eligible weeks (early={early}, late={late})"
    assert late >= early, (
        f"regenerating later did not ramp the long ride higher "
        f"(elapsed=3 → {early}min, elapsed=7 → {late}min) — offset not applied"
    )
