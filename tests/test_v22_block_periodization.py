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


class TestBlockConcentration(unittest.TestCase):
    """B5: block-on build/peak phases concentrate on the focus class (focus is the
    dominant HIT quality) while retaining ≥1 complementary shape. The swap floor is
    best-effort (~55-66% focus, not a hard ≥70% — see IP note), so the invariant is
    'focus dominant + complementary present', asserted over a few seeds."""

    def _block_goal(self):
        from datetime import date, timedelta
        return tp.Goal(
            goal_type="event", plan_weeks=14,
            target_date=date.today() + timedelta(weeks=14),
            event_km=160, event_climb_m=2000, event_type="gran_fondo",
            hours_per_week=12.0, max_weekday_hours=2.5, max_weekend_hours=4.0,
            available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[],
            block_periodization=True)

    def test_focus_dominant_with_complementary(self):
        import collections
        for seed in (0, 7, 42):
            _ph, weeks = tp.generate_plan(
                self._block_goal(), athlete={"ftp": 250, "weight_kg": 70},
                recent_weekly_tss=600, seed_salt=seed)
            for pn, focus in (("build1", "vo2max"), ("build2", "threshold"),
                              ("peak", "threshold")):
                pw = [w for w in weeks if w.phase == pn and not w.is_stepback]
                if not pw:
                    continue
                cc = collections.Counter()
                for w in pw:
                    for s in w.sessions:
                        if tp._session_is_hit(s):
                            cc[tp._content_class_for_zwo(s.zwo_file or "")] += 1
                H = sum(cc.values())
                if H < 2:
                    continue
                self.assertEqual(
                    cc[focus], max(cc.values()),
                    f"seed{seed} {pn}: focus {focus} not the dominant HIT class: {dict(cc)}")
                self.assertGreaterEqual(
                    cc[focus] / H, 0.45, f"seed{seed} {pn}: weak focus share {dict(cc)}")
                self.assertGreater(
                    H - cc[focus], 0, f"seed{seed} {pn}: no complementary shape {dict(cc)}")


class TestBlockSurvivesRecalc(unittest.TestCase):
    """B6: a block plan stays blocked after adaptation — the recalc/regenerate
    sampler paths recompute block_focus from the goal (default-off parity holds)."""

    def _block_goal(self):
        from datetime import date, timedelta
        return tp.Goal(
            goal_type="event", plan_weeks=14,
            target_date=date.today() + timedelta(weeks=14),
            event_km=160, event_climb_m=2000, event_type="gran_fondo",
            hours_per_week=12.0, max_weekday_hours=2.5, max_weekend_hours=4.0,
            available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[],
            block_periodization=True)

    def test_regenerate_keeps_block_focus(self):
        ath = {"ftp": 250, "weight_kg": 70}
        goal = self._block_goal()
        _ph, weeks = tp.generate_plan(goal, athlete=ath, recent_weekly_tss=600)
        _np, new_weeks, _info = tp.regenerate_from_today(goal, weeks, 50.0, athlete=ath)
        focus = [w.block_focus for w in new_weeks
                 if w.phase in ("build1", "build2", "peak")
                 and not getattr(w, "is_stepback", False)]
        self.assertTrue(any(f for f in focus),
                        "recalc dropped block_focus on all build/peak weeks")

    def test_regenerate_default_off_stays_unblocked(self):
        # PARITY: a non-block plan regenerated stays unblocked (no focus leaks in)
        ath = {"ftp": 250, "weight_kg": 70}
        goal = tp.Goal(
            goal_type="event", plan_weeks=14,
            target_date=__import__("datetime").date.today() + __import__("datetime").timedelta(weeks=14),
            event_km=160, event_climb_m=2000, event_type="gran_fondo",
            hours_per_week=12.0, available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[])
        _ph, weeks = tp.generate_plan(goal, athlete=ath, recent_weekly_tss=600)
        _np, new_weeks, _info = tp.regenerate_from_today(goal, weeks, 50.0, athlete=ath)
        self.assertTrue(all(getattr(w, "block_focus", None) is None for w in new_weeks))


class TestBlockPersistence(unittest.TestCase):
    """B7: block_periodization round-trips through the saved plan goal block so a
    block plan stays a block plan when the app reconstructs the goal for recalc."""

    def test_goal_from_plan_dict_restores_block_flag(self):
        import app
        g = {"type": "event", "block_periodization": True, "distribution": "pyramidal"}
        goal = app._goal_from_plan_dict(g)
        self.assertTrue(goal.block_periodization)
        self.assertEqual(goal.distribution, "pyramidal")

    def test_goal_from_plan_dict_defaults_block_off(self):
        import app
        self.assertFalse(app._goal_from_plan_dict({"type": "event"}).block_periodization)


if __name__ == "__main__":
    unittest.main()
