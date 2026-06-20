"""B4 (tester reliability) — a step-back/deload week stays EASY and SHORT.

Two invariants:
  * no HIT session (VO2max / threshold / over-under / sweet-spot / sprint) — this
    was already true via the picker, asserted here as a guard;
  * the long ride is capped at STEPBACK_LONG_RIDE_CAP_MIN (2.5h). The
    sampler/match could set a weekend endurance slot to the matched file's full
    length (the prescription↔file decoupling), so a deload picked up a 205-min
    "long ride"; the authoritative per-day pass now clamps it.

Event goals (long-ride progression active), a spread of weekend caps. Restores
the tracked library index so the run is hermetic.
"""
import unittest
from datetime import date, timedelta
from pathlib import Path

import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "workouts" / ".library_index.json"
_HIT = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}


def _egoal(weeks, wknd, hpw):
    return tp.Goal(
        goal_type="event", target_date=date.today() + timedelta(weeks=weeks),
        target_ctl=90, hours_per_week=hpw,
        max_weekday_hours=2.0, max_weekend_hours=wknd,
        available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[0],
        daily_max_hours={}, plan_weeks=weeks,
        event_km=160, event_climb_m=2000, event_type="gran_fondo",
    )


class TestDeloadLongRideCap(unittest.TestCase):
    def setUp(self):
        self._backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None

    def tearDown(self):
        if self._backup is not None:
            _LIB_INDEX.write_bytes(self._backup)

    def test_deload_stays_easy_and_short(self):
        # Big weekend caps are where the long-ride progression used to overgrow
        # the deload ride (W12, the pre-taper block).
        for wknd in (4.0, 4.5, 5.0, 6.0):
            for hpw in (10.0, 12.0, 14.0):
                with self.subTest(wknd=wknd, hpw=hpw):
                    _ph, weeks = tp.generate_plan(
                        _egoal(16, wknd, hpw), recent_weekly_tss=600)
                    for w in weeks:
                        if not getattr(w, "is_stepback", False):
                            continue
                        longest = max(
                            ((s.duration_min or 0) for s in w.sessions
                             if s and s.session_type != "rest"), default=0)
                        self.assertLessEqual(
                            longest, tp.STEPBACK_LONG_RIDE_CAP_MIN,
                            f"deload W{w.week_num} long ride {longest}min "
                            f"> {tp.STEPBACK_LONG_RIDE_CAP_MIN}min cap")
                        self.assertFalse(
                            any(s.session_type in _HIT for s in w.sessions),
                            f"deload W{w.week_num} contains a HIT session")


if __name__ == "__main__":
    unittest.main()
