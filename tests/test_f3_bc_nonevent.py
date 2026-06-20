"""F3 (owner feature) — B/C intermediate races allowed on NON-event plans.

Previously the B/C mini-tapers (_apply_secondary_event_tapers) only ran for
event/ctl goals, so an FTP / VO2max / general block couldn't target an
intermediate race. The gate is widened to run for ANY goal that carries B/C
events. Two checks: the mechanism demotes HIT for an ftp goal (deterministic),
and the gate actually fires inside generate_plan (no HIT in the B-race taper
window). Restores the tracked library index so the run is hermetic.
"""
import unittest
from datetime import date, timedelta
from pathlib import Path

import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "workouts" / ".library_index.json"
_HIT = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}


class TestBcOnNonEvent(unittest.TestCase):
    def setUp(self):
        self._backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None

    def tearDown(self):
        if self._backup is not None:
            _LIB_INDEX.write_bytes(self._backup)

    def test_secondary_taper_demotes_hit_for_ftp_goal(self):
        # Mechanism: a B race on an ftp goal demotes the HIT session on the race
        # day to an easy opener. Controlled weeks → deterministic.
        d0 = date.today() + timedelta(days=14)
        race = d0 + timedelta(days=2)
        sessions = [
            tp.PlannedSession(day=d0, day_name="Mon", session_type="z2",
                              duration_min=60, tss_estimate=45, description="Z2"),
            tp.PlannedSession(day=d0 + timedelta(days=1), day_name="Tue",
                              session_type="vo2max", duration_min=60,
                              tss_estimate=90, description="VO2"),
            tp.PlannedSession(day=race, day_name="Wed", session_type="threshold",
                              duration_min=60, tss_estimate=90, description="THR"),
        ]
        wk = tp.PlannedWeek(week_num=2, start=d0, end=d0 + timedelta(days=6),
                            phase="build1", tss_target=300, is_stepback=False,
                            sessions=sessions)
        goal = tp.Goal(
            goal_type="ftp", hours_per_week=8.0,
            events=[tp.TargetEvent(date=race, priority="B", name="B race")])
        tp._apply_secondary_event_tapers([wk], goal)
        # B priority = 2-day window before the race (inclusive): both the Tue
        # vo2max and the Wed threshold fall in it and must be demoted off HIT.
        self.assertNotIn(sessions[1].session_type, _HIT,
                         "vo2max in the B window should be demoted")
        self.assertNotIn(sessions[2].session_type, _HIT,
                         "threshold on the B race day should be demoted")

    def test_gate_fires_in_generate_plan_for_ftp(self):
        # Integration: generating an FTP plan WITH a B race leaves no HIT session
        # in the race's 2-day taper window — proving the gate now runs for ftp.
        race = date.today() + timedelta(weeks=6)  # mid-plan, clear of the edges
        goal = tp.Goal(
            goal_type="ftp", target_date=None, hours_per_week=10.0,
            max_weekday_hours=2.0, max_weekend_hours=4.0,
            available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[0],
            daily_max_hours={}, plan_weeks=12,
            events=[tp.TargetEvent(date=race, priority="B", name="B race")])
        for salt in range(3):
            with self.subTest(seed=salt):
                _ph, weeks = tp.generate_plan(goal, seed_salt=salt,
                                              recent_weekly_tss=500)
                for w in weeks:
                    for s in w.sessions:
                        d = getattr(s, "day", None)
                        if d is None:
                            continue
                        if 0 <= (race - d).days <= 2:
                            self.assertNotIn(
                                s.session_type, _HIT,
                                f"HIT {s.session_type} on {d} inside the B-race "
                                f"taper window (gate didn't fire for ftp)")


if __name__ == "__main__":
    unittest.main()
