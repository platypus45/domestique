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
    """v1.9.2 — availability applies the user's hours LITERALLY (v1.7.3,
    bidirectional), but a 6h/session sanity cap (MAX_AVAIL_SESSION_MIN=360)
    prevents a typo/extreme value from spawning an absurd session. The old 2×
    clamp was intentionally removed; this asserts the cap, not the clamp."""

    def test_scale_capped_at_six_hours(self):
        mon = _next_monday()
        # Current planned 60 min; user asks for 10h that day. Literal would be
        # 600 min — capped at 360 (6h), never higher.
        s = _mk_session(mon, session_type="z2", duration_min=60, tss=45.0)
        weeks = [_mk_week(mon, [s])]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        overrides = {mon.isoformat(): 10.0}
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}
        _, _ = tp.reforecast(
            goal, weeks, tsb_series=tsb_series,
            availability_overrides=overrides,
        )
        self.assertEqual(weeks[0].sessions[0].duration_min, tp.MAX_AVAIL_SESSION_MIN)
        self.assertLessEqual(weeks[0].sessions[0].duration_min, 360)

    def test_realistic_hours_applied_literally(self):
        # A realistic 2.5h on a 60-min session → 150 min (under the cap, literal).
        mon = _next_monday()
        s = _mk_session(mon, session_type="z2", duration_min=60, tss=45.0)
        weeks = [_mk_week(mon, [s])]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}
        tp.reforecast(goal, weeks, tsb_series=tsb_series,
                      availability_overrides={mon.isoformat(): 2.5})
        self.assertEqual(weeks[0].sessions[0].duration_min, 150)
        # tss rescaled with duration: round(150/60 * 45) = 112
        self.assertEqual(weeks[0].sessions[0].tss_estimate, 112)


class TestAvailabilitySprintTypeCeiling(unittest.TestCase):
    """v2.0.6 — the availability reflow must apply the per-type duration ceiling
    (TYPE_CEILING['sprint']=45), so raising a sprint day to 1.5h can't render a
    90-min ~140-TSS session. Previously only the generate_plan sampler clamped;
    the reforecast availability path sized to the full day (6h sanity cap only)."""

    def test_sprint_day_clamped_to_type_ceiling(self):
        mon = _next_monday()
        # Start at 30 min so the override genuinely changes duration (exercises
        # the clamp + the >=15% re-match). Without the fix → 90; with it → 45.
        s = _mk_session(mon, session_type="sprint", duration_min=30, tss=28.0)
        weeks = [_mk_week(mon, [s])]
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
        overrides = {mon.isoformat(): 1.5}  # 90 min available
        tsb_series = {mon + timedelta(days=i): 0.0 for i in range(7)}
        tp.reforecast(goal, weeks, tsb_series=tsb_series,
                      availability_overrides=overrides)
        out = weeks[0].sessions[0]
        self.assertLessEqual(out.duration_min, tp.TYPE_CEILING["sprint"])
        self.assertLess(out.duration_min, 90)  # did NOT take the full availability

    def test_sprint_tss_per_hour_is_neuromuscular_not_threshold(self):
        # v2.0.6 — the sprint day-TARGET must reflect FULL-recovery neuromuscular
        # load (IF ~0.75), not threshold. Below what the IF<=0.82 match guard can
        # even deliver (0.82**2*100 ≈ 67) and well under the threshold target.
        self.assertLessEqual(tp.TSS_PER_HOUR["sprint"], 67)
        self.assertLess(tp.TSS_PER_HOUR["sprint"], tp.TSS_PER_HOUR["threshold"])


if __name__ == "__main__":
    unittest.main()
