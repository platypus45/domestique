"""v1.0.3 IMPL-AVAILABILITY — per-day availability override scaling in reforecast().

Four tests cover the new ``availability_overrides`` kwarg on ``tp.reforecast()``:
  1. test_half_hours_halves_duration   — hours=0.5 vs current 60 min → 30 min
  2. test_zero_hours_becomes_rest      — hours=0 → session_type=rest, duration=0, tss=0
  3. test_day_not_in_overrides_kept    — absent day keeps its current duration
  4. test_scale_clamped_at_two         — hours=10 vs current 60 min does NOT exceed 2×

Algorithm under test (training_planner.py reforecast() §IMPL-AVAILABILITY block):
- Per-week scale = clamp(available_mins / current_mins, 0.4, 2.0)
- Days in overrides scaled by that scale; absent days untouched.
- hours <= 0 → session_type=rest, duration_min=0, tss_estimate=0.
- Touched dates merged into info["touched_days"] for app.py write-back.
"""
from __future__ import annotations

import unittest
from datetime import date, timedelta

import training_planner as tp


def _next_monday() -> date:
    """Pick the next future Monday so all sessions are after today."""
    today = date.today()
    days_until_mon = (7 - today.weekday()) % 7
    if days_until_mon == 0:
        days_until_mon = 7  # always strictly future
    return today + timedelta(days=days_until_mon)


def _mk_session(day: date, *, session_type: str = "z2",
                duration_min: int = 60, tss: float = 45.0) -> tp.PlannedSession:
    return tp.PlannedSession(
        day=day,
        day_name=day.strftime("%A"),
        session_type=session_type,
        duration_min=duration_min,
        tss_estimate=tss,
        description="",
    )


def _mk_week(start: date, sessions: list[tp.PlannedSession]) -> tp.PlannedWeek:
    return tp.PlannedWeek(
        week_num=1,
        start=start,
        end=start + timedelta(days=6),
        phase="build1",
        tss_target=400.0,
        is_stepback=False,
        sessions=sessions,
        hit_per_week=2,
    )


class TestAvailabilityHalfHoursHalvesDuration(unittest.TestCase):
    """hours=0.5 against a 60-min Z2 ride → 30 min after scaling."""

    def test_half_hours_halves_duration(self):
        mon = _next_monday()
        # Single-day week with one 60-min Z2 session.
        s = _mk_session(mon, session_type="z2", duration_min=60, tss=45.0)
        weeks = [_mk_week(mon, [s])]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        overrides = {mon.isoformat(): 0.5}  # available_mins = 30 → scale = 30/60 = 0.5
        # TSB-neutral series so the TSB downshift loop is a no-op.
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}
        _, info = tp.reforecast(
            goal, weeks, tsb_series=tsb_series,
            availability_overrides=overrides,
        )
        self.assertEqual(weeks[0].sessions[0].duration_min, 30)
        # tss_estimate = round(30/60 * 45) = round(22.5) → banker's rounding → 22
        # (TSS_PER_HOUR["z2"] = 45). Either 22 or 23 is acceptable depending on
        # Python's round-half-to-even; assert the float value is correct.
        self.assertEqual(weeks[0].sessions[0].tss_estimate, round(30 / 60 * 45))
        # Day must appear in touched_days so app.py persists it.
        self.assertIn(mon.isoformat(), info["touched_days"])
        self.assertEqual(info["action"], "reforecasted")


class TestAvailabilityZeroHoursBecomesRest(unittest.TestCase):
    """hours=0 → session_type='rest', duration_min=0, tss_estimate=0."""

    def test_zero_hours_becomes_rest(self):
        mon = _next_monday()
        s = _mk_session(mon, session_type="sweetspot", duration_min=75, tss=100.0)
        weeks = [_mk_week(mon, [s])]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        overrides = {mon.isoformat(): 0.0}
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}
        _, info = tp.reforecast(
            goal, weeks, tsb_series=tsb_series,
            availability_overrides=overrides,
        )
        self.assertEqual(weeks[0].sessions[0].session_type, "rest")
        self.assertEqual(weeks[0].sessions[0].duration_min, 0)
        self.assertEqual(weeks[0].sessions[0].tss_estimate, 0)
        self.assertIn(mon.isoformat(), info["touched_days"])


class TestAvailabilityDayNotInOverridesKeptUntouched(unittest.TestCase):
    """A day NOT in availability_overrides keeps its current planned duration."""

    def test_day_not_in_overrides_kept(self):
        mon = _next_monday()
        tue = mon + timedelta(days=1)
        # Two-session week: Mon overridden, Tue absent from overrides.
        s_mon = _mk_session(mon, session_type="z2", duration_min=60, tss=45.0)
        s_tue = _mk_session(tue, session_type="threshold", duration_min=90, tss=135.0)
        weeks = [_mk_week(mon, [s_mon, s_tue])]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        # Override only Monday — Tuesday must stay untouched at 90 min.
        overrides = {mon.isoformat(): 1.0}  # 60 mins → keep at 60 (scale=1.0)
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}
        _, _ = tp.reforecast(
            goal, weeks, tsb_series=tsb_series,
            availability_overrides=overrides,
        )
        # Tue session pristine.
        self.assertEqual(weeks[0].sessions[1].duration_min, 90)
        self.assertEqual(weeks[0].sessions[1].session_type, "threshold")
        self.assertEqual(weeks[0].sessions[1].tss_estimate, 135.0)
        # Mon scale 1.0 → 60 unchanged.
        self.assertEqual(weeks[0].sessions[0].duration_min, 60)


class TestAvailabilityScaleClampedAtTwo(unittest.TestCase):
    """hours=10 against current 60 min must NOT inflate beyond 2× (clamp 2.0)."""

    def test_scale_clamped_at_two(self):
        mon = _next_monday()
        # Current planned 60 min; user asks for 10h that day → raw_scale = 600/60 = 10.0.
        # Clamp at 2.0 → max new duration = 120 min, NOT 600.
        s = _mk_session(mon, session_type="z2", duration_min=60, tss=45.0)
        weeks = [_mk_week(mon, [s])]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        overrides = {mon.isoformat(): 10.0}
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}
        _, _ = tp.reforecast(
            goal, weeks, tsb_series=tsb_series,
            availability_overrides=overrides,
        )
        # Hard cap: 60 * 2.0 = 120, never higher.
        self.assertEqual(weeks[0].sessions[0].duration_min, 120)
        self.assertLessEqual(weeks[0].sessions[0].duration_min, 60 * 2)
        # tss = round(120/60 * 45) = 90
        self.assertEqual(weeks[0].sessions[0].tss_estimate, 90)


if __name__ == "__main__":
    unittest.main()
