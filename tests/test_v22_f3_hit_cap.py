"""v2.2 — F3: volume-aware weekly HIT cap.

E1 capped total weekly volume but never reduced HIT, so a low-volume week
converged to (fixed phase HIT count) x (tiny easy budget) = an intensity-
dominated / ~100%-HIT week that breaks polarization (an injury risk). F3 scales
the weekly HIT ceiling to the week's load (`_effective_hit_max`): a small week
may carry fewer hard days than the phase minimum, while a healthy-volume week
still reaches `phase.hit_count_max` (no regression).
"""
from datetime import date, timedelta
import unittest

import training_planner as tp


class _StubWeek:
    def __init__(self, phase, tss_target):
        self.phase = phase
        self.tss_target = tss_target


class TestEffectiveHitMaxUnit(unittest.TestCase):
    """Pure-function: deterministic, no planner non-determinism."""

    def tearDown(self):
        tp.set_active_distribution("polarized")

    def test_low_load_drops_below_phase_min(self):
        # peak hit_count_min is 3 today; a 182-TSS "peak" week must drop to 1.
        self.assertEqual(tp._effective_hit_max(_StubWeek("peak", 182)), 1)
        self.assertEqual(tp._effective_hit_max(_StubWeek("build1", 182)), 1)

    def test_healthy_load_reaches_phase_max(self):
        # A real peak week (650+ TSS) keeps the full hit_count_max (=3) — no regression.
        self.assertEqual(tp._effective_hit_max(_StubWeek("peak", 650)),
                         tp.get_budget_for_phase("peak").hit_count_max)
        self.assertEqual(tp._effective_hit_max(_StubWeek("peak", 900)),
                         tp.get_budget_for_phase("peak").hit_count_max)

    def test_never_below_floor_or_above_phase_max(self):
        for phase in ("base", "build1", "build2", "peak", "taper"):
            cap = tp.get_budget_for_phase(phase).hit_count_max
            for tss in (40, 150, 300, 600, 1200):
                e = tp._effective_hit_max(_StubWeek(phase, tss))
                self.assertGreaterEqual(e, 1)
                self.assertLessEqual(e, cap)

    def test_model_agnostic(self):
        # J1 preserves the hard-minute total, so the cap is stable across models.
        base = tp._effective_hit_max(_StubWeek("peak", 400))
        for m in ("pyramidal", "threshold"):
            tp.set_active_distribution(m)
            self.assertEqual(tp._effective_hit_max(_StubWeek("peak", 400)), base)


def _goal(weeks=14):
    return tp.Goal(
        goal_type="event", plan_weeks=weeks,
        target_date=date.today() + timedelta(weeks=weeks),
        event_km=160, event_climb_m=2000, event_type="gran_fondo",
        hours_per_week=12.0, max_weekday_hours=2.5, max_weekend_hours=4.0,
        available_days=[0, 1, 2, 3, 4, 5, 6], rest_days=[],
    )


class TestPlanLevelInvariants(unittest.TestCase):
    """End-to-end: a couple of seeds, invariant-based (planner is non-deterministic)."""

    def _build_weeks(self, recent, seed):
        _ph, weeks = tp.generate_plan(
            _goal(), athlete={"ftp": 250, "weight_kg": 70},
            recent_weekly_tss=recent, seed_salt=seed)
        return weeks

    def tearDown(self):
        import subprocess
        subprocess.run(["git", "checkout", "--", "workouts/.library_index.json"],
                       capture_output=True)

    def test_low_volume_caps_hit_and_bites(self):
        bit = False
        for seed in (0, 7):
            for w in self._build_weeks(200, seed):
                if getattr(w, "is_stepback", False) or w.phase not in ("build1", "build2", "peak"):
                    continue
                eff = tp._effective_hit_max(w)
                self.assertLessEqual(
                    tp._week_hit_count(w), eff,
                    f"seed={seed} {w.phase} wk{w.week_num}: hit "
                    f"{tp._week_hit_count(w)} > effective {eff}")
                if tp._week_hit_count(w) < tp.get_budget_for_phase(w.phase).hit_count_max:
                    bit = True
        self.assertTrue(bit, "low-volume cap never reduced HIT below the phase max")

    def test_healthy_volume_no_regression(self):
        # A healthy-volume plan must still reach the peak phase hit_count_max.
        peak_max = tp.get_budget_for_phase("peak").hit_count_max
        reached = 0
        for w in self._build_weeks(700, 0):
            if w.phase == "peak" and not getattr(w, "is_stepback", False):
                reached = max(reached, tp._week_hit_count(w))
        self.assertEqual(reached, peak_max,
                         f"healthy peak weeks capped below hit_count_max ({reached} < {peak_max})")


if __name__ == "__main__":
    unittest.main()
