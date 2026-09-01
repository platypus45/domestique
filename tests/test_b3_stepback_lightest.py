"""B3 (tester reliability) — a step-back/deload week must be the LIGHTEST in
its block.

plan_week gives step-back weeks fixed easy durations while build weeks scale
with the load-based ceiling, so a light build week (typically a pre-taper peak
week, trimmed by the authoritative per-day clamp) could end up BELOW the deload
— inverting the 3-up-1-down rhythm (tester saw W4>W3, W8>W6). The final
_enforce_stepback_is_lightest pass shrinks the deload's easy volume below the
lightest build week in its block. Invariant-based, a handful of configs/seeds,
restores the tracked library index so the run is hermetic.
"""
import shutil
import unittest
from datetime import date, timedelta
from pathlib import Path

import training_planner as tp

_LIB_INDEX = Path(__file__).resolve().parent.parent / "src" / "workouts" / ".library_index.json"


def _goal(weeks, hpw, rest_days, wkd, wknd):
    return tp.Goal(
        goal_type="event", target_date=date.today() + timedelta(weeks=weeks),
        target_ctl=90, hours_per_week=hpw,
        max_weekday_hours=wkd, max_weekend_hours=wknd,
        available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=rest_days,
        daily_max_hours={}, plan_weeks=weeks,
    )


def _wk_tss(w):
    return sum((s.tss_estimate or 0) for s in w.sessions
               if s and s.session_type != "rest")


class TestStepbackIsLightest(unittest.TestCase):
    def setUp(self):
        # The planner rewrites the tracked .library_index.json; snapshot + restore.
        self._backup = _LIB_INDEX.read_bytes() if _LIB_INDEX.exists() else None

    def tearDown(self):
        if self._backup is not None:
            _LIB_INDEX.write_bytes(self._backup)

    def test_stepback_lighter_than_every_build_in_block(self):
        # A spread of configs incl. the pre-taper-block case that used to invert
        # (hpw=8, modest recent load). Block = consecutive preceding non-stepback,
        # non-taper weeks back to the previous step-back / plan start.
        configs = [
            (16, 8, [0], 2.5, 4.5, None),
            (16, 8, [0], 2.5, 4.5, 400),
            (16, 10, [0, 3], 2.5, 4.5, 550),
            (16, 12, [0], 2.0, 4.0, 700),
            (12, 10, [0], 2.0, 4.0, 500),
        ]
        for weeks_n, hpw, rest, wkd, wknd, rwt in configs:
            with self.subTest(weeks=weeks_n, hpw=hpw, rest=rest, rwt=rwt):
                _ph, weeks = tp.generate_plan(
                    _goal(weeks_n, hpw, rest, wkd, wknd), recent_weekly_tss=rwt)
                for i, wk in enumerate(weeks):
                    if not getattr(wk, "is_stepback", False) or wk.phase == "taper":
                        continue
                    builds, j = [], i - 1
                    while j >= 0 and not getattr(weeks[j], "is_stepback", False):
                        if weeks[j].phase != "taper":
                            builds.append(weeks[j])
                        j -= 1
                    if not builds:
                        continue
                    sb = _wk_tss(wk)
                    lightest = min(_wk_tss(b) for b in builds)
                    self.assertLess(
                        sb, lightest,
                        f"W{wk.week_num} deload {sb:.0f} >= lightest build "
                        f"{lightest:.0f} in its block")

    def test_stepback_only_shrinks_never_adds_easy_load(self):
        # The guard must never turn a deload into a HARD week: a step-back week
        # carries no HIT session regardless.
        hit = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}
        _ph, weeks = tp.generate_plan(
            _goal(16, 10, [0], 2.5, 4.5), recent_weekly_tss=550)
        for wk in weeks:
            if getattr(wk, "is_stepback", False):
                self.assertFalse(
                    any(s.session_type in hit for s in wk.sessions),
                    f"deload W{wk.week_num} contains a HIT session")


if __name__ == "__main__":
    unittest.main()
