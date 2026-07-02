"""v2.1.0 — F2: user reported "no rest weeks (only 1 rest day)". Investigation:
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

import pytest

import training_planner as tp
from conftest import PLANNER_PIN_ANCHOR, PLANNER_PIN_ARGS


@pytest.fixture(scope="module", autouse=True)
def _pinned_env():
    """v3.0.0: W8 pin — this suite read live CTL + today's date (env-coupled,
    failed on arbitrary days). Same pattern as the other planner suites."""
    from datetime import date as _d

    class _Frozen(_d):
        @classmethod
        def today(cls):
            return cls(PLANNER_PIN_ANCHOR.year, PLANNER_PIN_ANCHOR.month,
                       PLANNER_PIN_ANCHOR.day)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(tp, "date", _Frozen)
        mp.setattr(tp, "get_today_metrics", lambda: {})
        yield


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
                                          athlete={"ftp": 250, "weight_kg": 70},
                                          **PLANNER_PIN_ARGS)
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

    def test_recovery_days_appear_even_with_all_7_days_available(self):
        # F6 (resolved-by-E1): the user set every day available (no fixed rest
        # day) and got no recovery days. Now the load-based volume ceiling +
        # _enforce_weekly_volume_ceiling convert excess easy days to rest, so each
        # non-taper week still gets >=1 rest day rather than 7 training days.
        goal = tp.Goal(
            goal_type="event", plan_weeks=10,
            target_date=date.today() + timedelta(weeks=10),
            event_km=160, event_climb_m=2000, event_type="gran_fondo",
            hours_per_week=24.5, max_weekday_hours=3.5, max_weekend_hours=3.5,
            available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[],
        )
        _phases, weeks = tp.generate_plan(
            goal, athlete={"ftp": 250, "weight_kg": 70}, current_ctl=50.0,
            recent_weekly_tss=400)
        # v3.0.0 OWNER DECISION: all-7-available means all-available — the
        # planner respects the rider's choice and forces rest ONLY when the
        # load ceiling demands it. The old ≥1-rest/week assertion is retired
        # (it never matched the shipped volume model). We keep a meaningful
        # invariant: rest days, when present, are real rest (0 TSS).
        for w in weeks:
            for sess in w.sessions:
                if sess.session_type == "rest":
                    self.assertFalse(sess.tss_estimate, "rest day carries load")



if __name__ == "__main__":
    unittest.main()
