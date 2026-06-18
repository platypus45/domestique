"""v2.2 — F1 block periodization (OPT-IN, default OFF).

DEFAULT-OFF PARITY is the safety contract for the whole F1 build: with
block_periodization unset (the default), the block layer must be completely
dormant — no week carries a block_focus, so the picker plug-ins (B3-B5) never
fire and the plan is today's behaviour. Each later F1 item (B2-B7) extends this
file with its block-ON behaviour test + keeps this parity assertion green.
"""
from datetime import date, timedelta
import unittest

import training_planner as tp


def _goal(block=False):
    return tp.Goal(
        goal_type="event", plan_weeks=12,
        target_date=date.today() + timedelta(weeks=12),
        event_km=140, event_climb_m=1800, event_type="gran_fondo",
        hours_per_week=10.0, max_weekday_hours=2.0, max_weekend_hours=3.5,
        available_days=[1, 2, 3, 4, 5, 6], rest_days=[0],
        block_periodization=block,
    )


class TestBlockPeriodizationOptIn(unittest.TestCase):
    def test_defaults_off(self):
        self.assertFalse(tp.Goal(goal_type="event").block_periodization)

    def test_default_off_leaves_block_layer_dormant(self):
        # PARITY: block off → no week has a block_focus set, so the block
        # plug-ins are inert and the plan is the legacy weekly-mixed output.
        _ph, weeks = tp.generate_plan(
            _goal(block=False), athlete={"ftp": 250, "weight_kg": 70},
            recent_weekly_tss=500)
        self.assertTrue(weeks)
        for w in weeks:
            self.assertIsNone(getattr(w, "block_focus", None),
                              f"block off but week {w.week_num} has block_focus")


if __name__ == "__main__":
    unittest.main()
