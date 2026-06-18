"""v2.0.8 — F2: user reported "no rest weeks (only 1 rest day)". Investigation:
the planner DOES insert step-back/unload weeks every STEP_BACK_EVERY (=4) weeks
(Issurin 3:1; Z2/recovery only; ~28-60% TSS cut) — training_planner.py:4839
sets is_stepback = global_week % STEP_BACK_EVERY == 0. The user's "no rest weeks"
was the 24.5h overscheduled / broken-profile state (the per-day-availability-fills
bug, resolved by E1). Rest DAYS now also appear via E1's
_enforce_weekly_volume_ceiling (converts excess easy days to rest). This guards
that unload weeks keep firing and are genuinely lighter.
"""
from datetime import date, timedelta
import unittest

import training_planner as tp


def _twelve_week_goal():
    return tp.Goal(
        goal_type="event", plan_weeks=12,
        target_date=date.today() + timedelta(weeks=12),
        event_km=120, event_climb_m=1500, event_type="gran_fondo",
        hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=3.5,
        available_days=[1, 2, 3, 4, 5, 6], rest_days=[0],
    )


class TestRestWeeks(unittest.TestCase):
    def test_stepback_unload_weeks_fire_and_are_lighter(self):
        _phases, weeks = tp.generate_plan(_twelve_week_goal(),
                                          athlete={"ftp": 250, "weight_kg": 70})
        sb = [w for w in weeks if getattr(w, "is_stepback", False)]
        self.assertGreaterEqual(
            len(sb), 1, "a 12-week plan must contain >=1 unload/step-back week")
        normal = [w for w in weeks
                  if not getattr(w, "is_stepback", False)
                  and w.phase not in ("taper", "consolidation")]
        self.assertTrue(normal, "expected some normal (non-unload, non-taper) weeks")
        avg_norm = sum(w.tss_target for w in normal) / len(normal)
        for w in sb:
            self.assertLess(
                w.tss_target, avg_norm,
                f"step-back week {w.week_num} (tss {w.tss_target}) not lighter "
                f"than the ~{avg_norm:.0f} normal-week average")


if __name__ == "__main__":
    unittest.main()
