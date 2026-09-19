"""The phase budget is the athlete's, and a hard slot must be hard.

BUDGETS (training_planner.py) is authored in absolute minutes for a ~10 h/week
rider and used to be applied verbatim to everyone. Two consequences, both
measured with tests/probe_budget_fidelity.py:

  * For anyone under about ten hours there was no intensity ceiling at all --
    build1's 225 hard minutes are 38% of a 10 h week but 94% of a 4 h one, and
    peak's are 100%.
  * The sampler verified its week against `budget.tss_per_week` (425/600/650),
    not the athlete's own `phase.weekly_tss_target`, so for a rider on a 287
    TSS week the check was unreachable.

And a HIT slot could be filled by a workout that was merely LABELLED hard: on a
peak week for a 2 h/day rider the three HIT slots delivered 41 minutes above Z2
where build1's three delivered 72 -- the peaking phase easier than the build
phase it sharpens.
"""
import os
import pathlib
import sys
import unittest

os.environ.setdefault("PYTHONHASHSEED", "0")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import training_planner as tp  # noqa: E402


def _shares(b):
    """The budget's four internal buckets folded back into the three-zone
    model. z4 is the MIDDLE -- 91-105% FTP is work at threshold, i.e. at LT2 --
    not the hard pole. Getting that backwards here is the same mistake the
    planner itself made, so it is worth stating rather than inlining."""
    tot = (b.z1z2_minutes_per_week + b.z3_minutes_per_week
           + b.z4_minutes_per_week + b.z5plus_minutes_per_week) or 1
    return (100 * b.z1z2_minutes_per_week / tot,
            100 * (b.z3_minutes_per_week + b.z4_minutes_per_week) / tot,
            100 * b.z5plus_minutes_per_week / tot)


class ScaleBudgetToWeek(unittest.TestCase):
    def test_the_ratio_survives_every_volume(self):
        """Given a target ratio, scaling to a week preserves it exactly. What
        that ratio SHOULD be at a given volume is tid_target_pct's job, tested
        in test_tid_targets.py; this is the arithmetic underneath."""
        for phase in ("base", "build1", "build2", "peak", "taper"):
            pt = tp.BUDGETS[phase].polarized_target
            for tss in (120, 250, 400, 700):
                s = tp.scale_budget_to_week(tp.BUDGETS[phase], tss)
                e, z2, hard = _shares(s)
                self.assertAlmostEqual(e, pt["z1_pct"], delta=1.5,
                                       msg=f"{phase} @{tss}: easy {e:.1f}")
                self.assertAlmostEqual(hard, pt["z3_pct"], delta=1.5,
                                       msg=f"{phase} @{tss}: hard {hard:.1f}")

    def test_the_target_is_recomputed_for_the_weeks_own_volume(self):
        """The dose model's whole point: the same phase is a different
        percentage at 6 h and at 15 h, because the hard sessions do not
        multiply with the hours."""
        small = tp.scale_budget_to_week(
            tp.BUDGETS["peak"], 300, available_minutes=6 * 60,
            model="polarized", phase_name="peak")
        big = tp.scale_budget_to_week(
            tp.BUDGETS["peak"], 900, available_minutes=18 * 60,
            model="polarized", phase_name="peak")
        self.assertGreater(_shares(small)[2], _shares(big)[2],
                           "hard share must fall as the week grows")
        self.assertLess(_shares(small)[0], _shares(big)[0],
                        "easy share must rise as the week grows")

    def test_minutes_track_the_target(self):
        a = tp.scale_budget_to_week(tp.BUDGETS["build1"], 200)
        b = tp.scale_budget_to_week(tp.BUDGETS["build1"], 400)
        ma = a.z1z2_minutes_per_week + a.z3_minutes_per_week + a.z4_minutes_per_week + a.z5plus_minutes_per_week
        mb = b.z1z2_minutes_per_week + b.z3_minutes_per_week + b.z4_minutes_per_week + b.z5plus_minutes_per_week
        self.assertAlmostEqual(mb / ma, 2.0, delta=0.05)

    def test_tss_per_week_becomes_the_athletes_own_target(self):
        """The check the sampler makes has to be against a number that can
        actually bind. 600 never did for a rider on 287."""
        s = tp.scale_budget_to_week(tp.BUDGETS["build1"], 287)
        self.assertEqual(s.tss_per_week, 287)
        self.assertNotEqual(s.tss_per_week, tp.BUDGETS["build1"].tss_per_week)

    def test_available_time_clamps_it_and_the_ratio_still_holds(self):
        """When the target needs more hours than the rider has, the RATIO is
        what survives and the load lands short. Buying the missing load with
        intensity instead is the failure this exists to stop."""
        s = tp.scale_budget_to_week(tp.BUDGETS["peak"], 600, available_minutes=240,
                                    model="polarized", phase_name="peak")
        total = (s.z1z2_minutes_per_week + s.z3_minutes_per_week
                 + s.z4_minutes_per_week + s.z5plus_minutes_per_week)
        self.assertLessEqual(total, 241)
        want = tp.tid_target_pct(
            "polarized", "peak", 240,
            tp.hit_slots_for_volume(240, tp.BUDGETS["peak"].hit_count_max))
        e, _z2, hard = _shares(s)
        self.assertAlmostEqual(e, want["z1_pct"], delta=1.5)
        self.assertAlmostEqual(hard, want["z3_pct"], delta=1.5)

    def test_the_hourly_rate_comes_from_tss_per_hour_not_a_constant(self):
        """_BAND_TSS_PER_HOUR must stay derived from TSS_PER_HOUR, or the two
        tables drift and the minutes stop matching the load they imply."""
        self.assertEqual(tp._BAND_TSS_PER_HOUR["z1z2"], float(tp.TSS_PER_HOUR["z2"]))
        self.assertEqual(tp._BAND_TSS_PER_HOUR["z3"],
                         (tp.TSS_PER_HOUR["tempo"] + tp.TSS_PER_HOUR["sweetspot"]) / 2)
        self.assertEqual(tp._BAND_TSS_PER_HOUR["z4"],
                         (tp.TSS_PER_HOUR["threshold"] + tp.TSS_PER_HOUR["overunder"]) / 2)

    def test_the_implied_load_is_close_to_the_target(self):
        """Round-trip: turn the target into minutes, price the minutes back."""
        for phase in ("base", "build1", "peak"):
            s = tp.scale_budget_to_week(tp.BUDGETS[phase], 300)
            tss = (s.z1z2_minutes_per_week / 60 * tp._BAND_TSS_PER_HOUR["z1z2"]
                   + s.z3_minutes_per_week / 60 * tp._BAND_TSS_PER_HOUR["z3"]
                   + s.z4_minutes_per_week / 60 * tp._BAND_TSS_PER_HOUR["z4"]
                   + s.z5plus_minutes_per_week / 60 * tp._BAND_TSS_PER_HOUR["z5plus"])
            self.assertAlmostEqual(tss, 300, delta=4, msg=phase)

    def test_a_scaled_budget_is_flagged_so_the_sampler_does_not_discount_twice(self):
        """PlannedWeek.tss_target already carries the Issurin stepback. The
        sampler used to apply 0.72 itself; doing both unloads to 52%."""
        self.assertFalse(tp.BUDGETS["build1"].week_scaled)
        self.assertTrue(tp.scale_budget_to_week(tp.BUDGETS["build1"], 200).week_scaled)
        src = tp.inspect.getsource(tp.sample_week_workouts) if hasattr(tp, "inspect") else None
        if src is None:
            import inspect
            src = inspect.getsource(tp.sample_week_workouts)
        self.assertIn("not budget.week_scaled", src)
        self.assertIn("1.0 if budget.week_scaled", src)

    def test_hit_counts_and_rest_days_are_left_alone(self):
        """Those are the phase table's business and stay its business."""
        for phase in ("base", "build1", "peak"):
            b = tp.BUDGETS[phase]
            s = tp.scale_budget_to_week(b, 250)
            self.assertEqual((s.hit_count_min, s.hit_count_max, s.rest_days_per_week),
                             (b.hit_count_min, b.hit_count_max, b.rest_days_per_week))

    def test_zero_target_does_not_explode(self):
        s = tp.scale_budget_to_week(tp.BUDGETS["base"], 0)
        self.assertEqual(s.tss_per_week, 0)
        self.assertEqual(s.z1z2_minutes_per_week, 0)


class HitSlotContract(unittest.TestCase):
    def test_the_floor_is_a_share_of_what_the_slot_owes(self):
        remaining = {"z1z2": 300.0, "z3": 20.0, "z4": 50.0, "z5plus": 40.0}
        self.assertAlmostEqual(tp._hit_slot_hard_floor(remaining, 3),
                               110 / 3 * tp._HIT_SLOT_HARD_MIN_SHARE, places=6)
        # One slot left owes the whole remaining budget, so its floor is higher.
        self.assertGreater(tp._hit_slot_hard_floor(remaining, 1),
                           tp._hit_slot_hard_floor(remaining, 3))

    def test_no_slots_left_means_no_floor(self):
        self.assertEqual(tp._hit_slot_hard_floor({"z4": 60.0}, 0), 0.0)

    def test_hard_minutes_counts_everything_above_z2(self):
        z = {"z1z2": 40.0, "z3": 5.0, "z4": 8.0, "z5plus": 3.0}
        self.assertEqual(tp._hard_minutes(z), 16.0)
        self.assertEqual(tp._hard_minutes({"z1z2": 60.0}), 0.0)

    def test_the_session_that_prompted_this_would_be_rejected(self):
        """The real file from the measured peak week: 41 minutes, 1.7 of them
        above Z2, admitted to one of peak's three HIT slots."""
        remaining = {"z1z2": 280.0, "z3": 15.0, "z4": 45.0, "z5plus": 46.0}
        floor = tp._hit_slot_hard_floor(remaining, 3)
        weak = {"z1z2": 39.3, "z3": 0.0, "z4": 1.7, "z5plus": 0.0}
        real = {"z1z2": 37.5, "z3": 0.0, "z4": 0.0, "z5plus": 19.5}   # 3x13x30/15
        self.assertLess(tp._hard_minutes(weak), floor)
        self.assertGreaterEqual(tp._hard_minutes(real), floor)

    def test_the_contract_never_makes_a_week_unplannable(self):
        """If nothing in the library clears the floor the sampler must fall
        back, not emit an empty slot."""
        import inspect
        src = inspect.getsource(tp.sample_week_workouts)
        self.assertIn("if _gated:", src)
        self.assertIn("feasible = _gated", src)
        self.assertIn("falling back to the ungated pool", src)


if __name__ == "__main__":
    unittest.main()
