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

    def test_block_on_stamps_focus_on_build_peak_only(self):
        # B2: block on → build/peak (non-stepback) weeks carry a focus; VO2 block
        # first (build1) → threshold block (build2/peak). base/taper/stepback None.
        _ph, weeks = tp.generate_plan(
            _goal(block=True), athlete={"ftp": 250, "weight_kg": 70},
            recent_weekly_tss=500)
        seen = {}
        for w in weeks:
            if getattr(w, "is_stepback", False):
                self.assertIsNone(w.block_focus, "stepback weeks take no focus")
                continue
            if w.phase in ("build1", "build2", "peak"):
                seen[w.phase] = w.block_focus
            else:
                self.assertIsNone(w.block_focus,
                                  f"{w.phase} should carry no block_focus")
        self.assertEqual(seen.get("build1"), "vo2max", "build1 = VO2 block")
        for p in ("build2", "peak"):
            if p in seen:
                self.assertEqual(seen[p], "threshold", f"{p} = threshold block")


class TestRotationPenaltyBlockExempt(unittest.TestCase):
    """B3: the rotation penalty exempts the focus class in a block (unit-level,
    deterministic — no planner non-determinism)."""

    def test_focus_class_exempt_when_block_on(self):
        weights = {"vo2max": 1.0, "threshold": 1.0, "sweet_spot": 1.0}
        recent = ["vo2max", "vo2max", "threshold"]  # vo2max + threshold in last_5
        # default (no block): vo2max penalized like always
        self.assertEqual(tp._apply_rotation_penalty(weights, recent)["vo2max"], 0.4)
        # block on, focus vo2max: vo2max EXEMPT; threshold still penalized
        out = tp._apply_rotation_penalty(weights, recent, block_focus="vo2max")
        self.assertEqual(out["vo2max"], 1.0, "focus class must not be penalized")
        self.assertEqual(out["threshold"], 0.4, "non-focus class still rotates")

    def test_parity_when_no_block_focus(self):
        weights = {"vo2max": 1.0, "threshold": 1.0}
        recent = ["vo2max", "threshold"]
        self.assertEqual(
            tp._apply_rotation_penalty(weights, recent),
            tp._apply_rotation_penalty(weights, recent, block_focus=None))


if __name__ == "__main__":
    unittest.main()
