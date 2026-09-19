"""v2.1.0 — J1: the intensity-distribution model was FORCED polarized. It is now
a user choice (Goal.distribution; "auto" default, plus polarized / pyramidal /
threshold / custom) that selects the per-phase IntensityBudget table via
BUDGETS_BY_MODEL. These guard that (a) the default path reuses the shipped table,
(b) the alternatives change only the KIND of intensity (z3 vs z4/z5 split) not
the dose (total hard minutes, TSS, HIT count, easy volume all preserved), and
(c) generate_plan budgets every week with goal.distribution.

The model is read from the goal at every lookup. It used to be a module global
set per request, and requests run on a thread pool, so one request's model
leaked into another's plan (notes/review/state.md STA-1).
"""
from datetime import date, timedelta
import unittest
from unittest.mock import patch

import training_planner as tp


def _hard(b):
    return (b.z3_minutes_per_week + b.z4_minutes_per_week
            + b.z5plus_minutes_per_week)


def _g(model=None):
    return tp.Goal(goal_type="event", **({"distribution": model} if model else {}))


class TestBudgetModels(unittest.TestCase):
    def test_default_is_auto_and_reuses_the_same_objects(self):
        """The default is no longer a fixed model. "auto" follows
        DEFAULT_TID_SEQUENCE -- pyramidal through base and build, polarized into
        peak and taper -- which is the order Filipas 2022 found beat every
        other, and which Rosenblat 2025 supports by finding the models
        otherwise equivalent at the group level."""
        self.assertEqual(_g().distribution, "auto")
        for ph in tp.BUDGETS:
            self.assertIs(tp.get_budget_for_phase(ph), tp.BUDGETS[ph])
            self.assertIs(tp.get_budget_for_phase(ph, _g()), tp.BUDGETS[ph])

    def test_unknown_or_none_falls_back_to_auto(self):
        self.assertIs(tp.budget_table(_g("bogus")), tp.BUDGETS)
        self.assertIs(tp.budget_table(None), tp.BUDGETS)
        self.assertEqual(tp.active_model_for_phase("peak", _g("bogus")),
                         tp.DEFAULT_TID_SEQUENCE["peak"])

    def test_a_goal_never_sees_another_goals_model(self):
        """No process state: looking up one model cannot change another goal's
        answer, whatever ran in between."""
        before = tp.get_budget_for_phase("build1", _g("pyramidal")).polarized_target
        tp.get_budget_for_phase("build1", _g("threshold"))
        self.assertEqual(tp.get_budget_for_phase("build1", _g("pyramidal")).polarized_target,
                         before)
        for gone in ("set_active_distribution", "_ACTIVE_DISTRIBUTION", "_VO2_MICRO_ONLY"):
            self.assertFalse(hasattr(tp, gone), gone)

    def test_alternatives_change_kind_not_dose(self):
        pol = {ph: tp.BUDGETS[ph] for ph in ("build1", "build2", "peak")}
        for model in ("pyramidal", "threshold"):
            for ph, p in pol.items():
                m = tp.get_budget_for_phase(ph, _g(model))
                self.assertEqual(_hard(m), _hard(p), f"{model}/{ph} total hard")
                self.assertEqual(m.tss_per_week, p.tss_per_week)
                self.assertEqual(m.hit_count_max, p.hit_count_max)
                self.assertEqual(m.hit_count_min, p.hit_count_min)
                self.assertEqual(m.z1z2_minutes_per_week, p.z1z2_minutes_per_week)
                self.assertEqual(m.rest_days_per_week, p.rest_days_per_week)

    def test_middle_zone_share_rises_polarized_to_pyramidal_to_threshold(self):
        """Measured on the three-zone TARGET, not on z3_minutes_per_week.
        That field is now one of four internal buckets the sampler scores
        against, derived per week by scale_budget_to_week; the model's identity
        lives in how much of the week sits at 76-105% FTP."""
        def mid(model, ph):
            return tp.get_budget_for_phase(ph, _g(model)).polarized_target["z2_pct"]
        for ph in ("build1", "build2", "peak"):
            self.assertLess(mid("polarized", ph), mid("pyramidal", ph), ph)
            self.assertLess(mid("pyramidal", ph), mid("threshold", ph), ph)

    def test_recovery_phases_are_easy_in_every_model(self):
        """Recovery is no longer "the polarized object reused" -- every model
        now carries its own row for every phase. What has to hold is the
        substance: a consolidation week is easy whichever model is chosen."""
        for model in ("polarized", "pyramidal", "threshold"):
            cons = tp.get_budget_for_phase("consolidation", _g(model)).polarized_target
            self.assertGreaterEqual(cons["z1_pct"], 88, f"{model}/consolidation")
            self.assertLessEqual(cons["z3_pct"], 2, f"{model}/consolidation")


class TestGeneratePlanHonorsModel(unittest.TestCase):
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

    def test_goal_defaults_to_auto(self):
        """"auto" = follow DEFAULT_TID_SEQUENCE per phase, not one model for
        the whole plan. A rider who names a model still gets it everywhere."""
        self.assertEqual(self._goal().distribution, "auto")

    def test_generate_plan_budgets_every_week_with_the_goals_model(self):
        ath = {"ftp": 250, "weight_kg": 70}
        for model in ("polarized", "pyramidal", "threshold"):
            seen, real = [], tp.scale_budget_to_week

            def spy(*a, **k):
                seen.append(k.get("model"))
                return real(*a, **k)
            with patch.object(tp, "scale_budget_to_week", side_effect=spy):
                _ph, weeks = tp.generate_plan(self._goal(model), athlete=ath,
                                              recent_weekly_tss=500)
            self.assertTrue(weeks, f"{model} plan must be non-empty")
            self.assertTrue(seen, f"{model}: no week was budgeted")
            self.assertEqual(set(seen), {model}, f"{model}: weeks budgeted under {set(seen)}")


if __name__ == "__main__":
    unittest.main()
