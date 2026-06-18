"""v2.1.0 — fixes surfaced by the multi-wave grill of the deferred IPs.

B1: _goal_from_plan_dict used to DROP target_date + every event_* field, so any
    recalc/refit/reforecast lost the event (and starved the F4 race-eve guard,
    which keys on goal.target_date). It now restores the persisted event.
B2: _enforce_event_taper_eve (F4) ran only in generate_plan, not in the
    reforecast / regenerate / refit adaptation paths — so a hard session could
    resurface within EVENT_EVE_EASY_DAYS of the event after an auto-adjust.
    The recalc paths now re-assert it.
(B3 lives in test_v208_volume_ceiling — the no-history CTL×7 fallback.)
"""
from datetime import date, timedelta
import unittest
from unittest import mock

import app
import training_planner as tp


def _event_goal(weeks=10):
    return tp.Goal(
        goal_type="event", plan_weeks=weeks,
        target_date=date.today() + timedelta(weeks=weeks),
        event_km=160, event_climb_m=2000, event_type="gran_fondo",
        hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=3.5,
        available_days=[1, 2, 3, 4, 5, 6], rest_days=[0],
    )


class TestB1GoalRoundTrip(unittest.TestCase):
    def test_goal_from_plan_dict_restores_event_fields(self):
        td = date.today() + timedelta(weeks=8)
        g = {
            "type": "event",
            "event_date": td.isoformat(),
            "event_name": "Granfondo Test",
            "event_km": 140, "event_climb": 2200, "event_type": "gran_fondo",
            "hours_per_week": 10.0, "rest_days": [0],
            "available_days": [1, 2, 3, 4, 5, 6], "plan_weeks": 8,
            "distribution": "pyramidal",
        }
        goal = app._goal_from_plan_dict(g)
        self.assertEqual(goal.target_date, td, "target_date must survive recalc")
        self.assertEqual(goal.event_km, 140)
        self.assertEqual(goal.event_climb_m, 2200)  # persisted as event_climb
        self.assertEqual(goal.event_type, "gran_fondo")
        self.assertEqual(goal.event_name, "Granfondo Test")
        self.assertEqual(goal.distribution, "pyramidal")  # J1 still wired

    def test_no_event_date_yields_none_not_crash(self):
        goal = app._goal_from_plan_dict({"type": "ctl", "event_date": None})
        self.assertIsNone(goal.target_date)


class TestB2RecalcReassertsTaperEve(unittest.TestCase):
    def test_reforecast_and_regenerate_invoke_the_eve_guard(self):
        goal = _event_goal()
        _ph, weeks = tp.generate_plan(
            goal, athlete={"ftp": 250, "weight_kg": 70}, recent_weekly_tss=400)
        # Spy AFTER first build (generate_plan's own call already ran). The
        # guard's demotion behavior is covered by test_v208_taper_eve; here we
        # only prove the recalc paths now call it with the event date.
        seen = []
        with mock.patch.object(tp, "_enforce_event_taper_eve",
                               side_effect=lambda wk, td, *a, **k: seen.append(td)):
            tp.reforecast(goal, weeks)
            tp.regenerate_from_today(goal, weeks, 50.0)
        self.assertEqual(
            seen, [goal.target_date, goal.target_date],
            f"reforecast + regenerate must each re-assert the eve guard; got {seen}")


if __name__ == "__main__":
    unittest.main()
