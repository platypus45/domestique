"""Issue #7 — a race must show on the calendar, not a training session.

_mark_race_days replaces the slot on every race date (A target event + every B/C
event in goal.events) with the race itself (is_race + race meta, no zwo, a 🏁
description), so the calendar / This Week / today surface the race instead of the
stray tapered session a rider saw (a 6h Z2 on Marmotte day).
"""
import unittest
from datetime import date, timedelta

import training_planner as tp


def _event_goal(weeks=4, km=175, climb=5000, name="Marmotte", events=None):
    return tp.Goal(
        goal_type="event", target_date=date.today() + timedelta(weeks=weeks),
        target_ctl=90, hours_per_week=10, max_weekday_hours=2.0, max_weekend_hours=5.0,
        available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[0], daily_max_hours={},
        plan_weeks=weeks, event_km=km, event_climb_m=climb, event_type="granfondo",
        event_name=name, events=events or [],
    )


def _race_session(weeks, d):
    for w in weeks:
        for s in w.sessions:
            if s.day == d:
                return s
    return None


class TestRaceCalendar(unittest.TestCase):
    def test_a_event_day_becomes_a_race(self):
        goal = _event_goal()
        _ph, weeks = tp.generate_plan(goal, recent_weekly_tss=500)
        s = _race_session(weeks, goal.target_date)
        self.assertIsNotNone(s, "A event day must exist in the plan")
        self.assertTrue(s.is_race)
        self.assertEqual((s.race or {}).get("priority"), "A")
        self.assertEqual((s.race or {}).get("km"), 175.0)
        self.assertEqual(s.zwo_file, "", "race day must carry no workout file")
        self.assertIn("RACE", s.description)
        self.assertTrue((s.race or {}).get("est_duration_min", 0) > 300,
                        "175km gran fondo race should estimate a long ride")

    def test_bc_race_day_becomes_a_race(self):
        bc = tp.TargetEvent(date=date.today() + timedelta(weeks=2), priority="B",
                            name="Local crit", event_km=40, event_climb_m=200,
                            event_type="crit")
        goal = _event_goal(events=[bc])
        _ph, weeks = tp.generate_plan(goal, recent_weekly_tss=500)
        s = _race_session(weeks, bc.date)
        self.assertIsNotNone(s)
        self.assertTrue(s.is_race)
        self.assertEqual((s.race or {}).get("priority"), "B")
        self.assertEqual((s.race or {}).get("name"), "Local crit")

    def test_no_race_marking_without_events(self):
        # A pure FTP plan with no target event + no B/C races → no race days.
        goal = tp.Goal(goal_type="ftp", target_date=None, target_ctl=80,
                       hours_per_week=8, max_weekday_hours=2.0, max_weekend_hours=4.0,
                       available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[0],
                       daily_max_hours={}, plan_weeks=4)
        _ph, weeks = tp.generate_plan(goal, recent_weekly_tss=400)
        races = [s for w in weeks for s in w.sessions if s.is_race]
        self.assertEqual(races, [], "no events → no race days")


if __name__ == "__main__":
    unittest.main()
