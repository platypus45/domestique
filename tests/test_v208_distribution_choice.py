"""v2.1.0 — J1: the intensity-distribution model was FORCED polarized. It is now
a user choice (Goal.distribution; polarized default, plus pyramidal / threshold)
that selects the per-phase IntensityBudget table via BUDGETS_BY_MODEL. These
guard that (a) the default path is byte-for-byte unchanged so the model is never
hard-forced and existing plans are untouched, (b) the alternatives change only
the KIND of intensity (z3 vs z4/z5 split) not the dose (total hard minutes, TSS,
HIT count, easy volume all preserved), and (c) generate_plan honors goal.distribution.
"""
from datetime import date, timedelta
import unittest

import training_planner as tp


def _hard(b):
    return (b.z3_minutes_per_week + b.z4_minutes_per_week
            + b.z5plus_minutes_per_week)


class TestBudgetModels(unittest.TestCase):
    def tearDown(self):
        tp.set_active_distribution("polarized")  # never leak state across tests

    def test_default_is_polarized_and_reuses_the_same_objects(self):
        # The default path must be unchanged — get_budget_for_phase returns the
        # SAME IntensityBudget instances the legacy code used.
        self.assertEqual(tp.get_active_distribution(), "polarized")
        for ph in tp.BUDGETS:
            self.assertIs(tp.get_budget_for_phase(ph), tp.BUDGETS[ph])

    def test_unknown_or_none_falls_back_to_polarized(self):
        self.assertEqual(tp.set_active_distribution("bogus"), "polarized")
        self.assertEqual(tp.set_active_distribution(None), "polarized")

    def test_alternatives_change_kind_not_dose(self):
        pol = {ph: tp.BUDGETS[ph] for ph in ("build1", "build2", "peak")}
        for model in ("pyramidal", "threshold"):
            tp.set_active_distribution(model)
            for ph, p in pol.items():
                m = tp.get_budget_for_phase(ph)
                self.assertEqual(_hard(m), _hard(p), f"{model}/{ph} total hard")
                self.assertEqual(m.tss_per_week, p.tss_per_week)
                self.assertEqual(m.hit_count_max, p.hit_count_max)
                self.assertEqual(m.hit_count_min, p.hit_count_min)
                self.assertEqual(m.z1z2_minutes_per_week, p.z1z2_minutes_per_week)
                self.assertEqual(m.rest_days_per_week, p.rest_days_per_week)

    def test_threshold_share_rises_polarized_to_pyramidal_to_threshold(self):
        def z3(model, ph):
            tp.set_active_distribution(model)
            return tp.get_budget_for_phase(ph).z3_minutes_per_week
        for ph in ("build1", "build2", "peak"):
            self.assertLess(z3("polarized", ph), z3("pyramidal", ph), ph)
            self.assertLessEqual(z3("pyramidal", ph), z3("threshold", ph), ph)

    def test_recovery_and_taper_phases_are_model_agnostic(self):
        for model in ("pyramidal", "threshold"):
            tp.set_active_distribution(model)
            for ph in ("base", "taper", "consolidation", "history"):
                self.assertIs(tp.get_budget_for_phase(ph), tp.BUDGETS[ph],
                              f"{model}/{ph} must stay polarized")


class TestGeneratePlanHonorsModel(unittest.TestCase):
    def tearDown(self):
        tp.set_active_distribution("polarized")

    def _goal(self, model=None):
        kw = dict(goal_type="event", plan_weeks=12,
                  target_date=date.today() + timedelta(weeks=12),
                  event_km=120, event_climb_m=1500, event_type="gran_fondo",
                  hours_per_week=12.0, max_weekday_hours=2.0,
                  max_weekend_hours=4.0,
                  available_days=[1, 2, 3, 4, 5, 6], rest_days=[0])
        if model is not None:
            kw["distribution"] = model
        return tp.Goal(**kw)

    def test_goal_defaults_to_polarized(self):
        self.assertEqual(self._goal().distribution, "polarized")

    def test_generate_plan_activates_the_goals_model(self):
        ath = {"ftp": 250, "weight_kg": 70}
        for model in ("polarized", "pyramidal", "threshold"):
            _ph, weeks = tp.generate_plan(self._goal(model), athlete=ath,
                                          recent_weekly_tss=500)
            self.assertTrue(weeks, f"{model} plan must be non-empty")
            self.assertEqual(tp.get_active_distribution(), model)


if __name__ == "__main__":
    unittest.main()
