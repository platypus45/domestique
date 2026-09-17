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

_LIB_INDEX = Path(__file__).resolve().parent.parent / "src" / "workouts" / ".library_index.json"
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

    def test_deload_looks_lighter_than_its_build_weeks(self):
        """Issue #4 -- a recovery week must be VISIBLY light (a rider saw a
        'Recovery' week with the same single rest day and more hours than the
        builds).

        Visibly light by plan_invariants.stepback_looks_lighter: more rest days,
        or -- when the rider's available days leave no rest day to spare above
        the load floor, which wins -- a lighter load type. This test used to
        demand more rest days only, anchored to the real date, and failed on 4
        to 14 of 28 start dates depending on availability, every time on a
        recovery week that was already all-Z2 at a lower intensity. The weekday
        dependence came from counting a mid-week start's two-day first week as
        a build.

        So it sweeps four weeks of start dates for two riders: this suite's own
        (every day available, Monday rest) and one with weekends off.
        """
        import datetime as _dt
        import clock
        import plan_invariants as pi

        riders = {
            "all days, Monday rest": dict(
                available=[0, 1, 2, 3, 4, 5, 6], rest=[0], daily={}, wknd=4.5, hpw=12.0,
                wkday=2.0),
            "weekends off, 3h Mon-Fri": dict(
                available=[0, 1, 2, 3, 4], rest=[5, 6], wknd=0.0, hpw=15.0, wkday=3.0,
                daily={d: 3.0 for d in range(5)} | {5: 0.0, 6: 0.0}),
        }
        base = _dt.date(2026, 9, 1)
        failures = []
        try:
            for name, r in riders.items():
                for k in range(28):
                    anchor = base + _dt.timedelta(days=k)
                    clock.freeze(_dt.datetime(anchor.year, anchor.month, anchor.day, 9, 0))
                    goal = tp.Goal(
                        goal_type="event", target_date=anchor + timedelta(weeks=16),
                        target_ctl=90, hours_per_week=r["hpw"],
                        max_weekday_hours=r["wkday"], max_weekend_hours=r["wknd"],
                        available_days=r["available"], rest_days=r["rest"],
                        daily_max_hours=r["daily"], plan_weeks=16,
                        event_km=160, event_climb_m=2000, event_type="gran_fondo")
                    _ph, weeks = tp.generate_plan(goal, recent_weekly_tss=600)
                    for i, w in enumerate(weeks):
                        if not getattr(w, "is_stepback", False):
                            continue
                        builds, j = [], i - 1
                        while j >= 0 and not getattr(weeks[j], "is_stepback", False):
                            builds.append(weeks[j])
                            j -= 1
                        ok, why = pi.stepback_looks_lighter(w, builds)
                        if not ok:
                            failures.append(f"{name}, start {anchor:%a %d}: W{w.week_num} {why}")
        finally:
            clock.unfreeze()
        self.assertEqual(failures, [], "\n".join(failures[:12]))

if __name__ == "__main__":
    unittest.main()
