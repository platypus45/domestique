"""v2.0.8 — F4: "it gives me VO2max intervals just before the race, which is
stupid." A taper keeps some intensity earlier, but the final days before the A
event must be easy openers. Fix: _enforce_event_taper_eve demotes any HIT within
EVENT_EVE_EASY_DAYS of target_date to a short easy Z2 opener.
"""
from datetime import date, timedelta
import unittest

import training_planner as tp


class TestTaperEve(unittest.TestCase):
    def test_no_hard_session_in_final_days_before_event(self):
        td = date.today() + timedelta(weeks=10)
        goal = tp.Goal(
            goal_type="event", plan_weeks=10, target_date=td,
            event_km=160, event_climb_m=2000, event_type="gran_fondo",
            hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=3.5,
            available_days=[1, 2, 3, 4, 5, 6], rest_days=[0],
        )
        _phases, weeks = tp.generate_plan(goal, athlete={"ftp": 250, "weight_kg": 70})
        bad = []
        for w in weeks:
            for s in w.sessions:
                if s.session_type == "rest":
                    continue
                delta = (td - s.day).days
                if 0 <= delta <= tp.EVENT_EVE_EASY_DAYS and tp._session_is_hit(s):
                    bad.append((s.day.isoformat(), s.session_type, f"{delta}d before"))
        self.assertEqual(
            bad, [],
            f"hard session(s) within {tp.EVENT_EVE_EASY_DAYS}d of the event: {bad}")


if __name__ == "__main__":
    unittest.main()
