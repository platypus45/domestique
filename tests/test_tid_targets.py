"""The intensity-distribution targets, and the layers that stop them going wrong.

A distribution model is easy to break in ways no unit test notices: the numbers
still add to 100, every function still returns, and the plans quietly become
unsafe, untrainable, or all the same. So the checks are layered, cheapest first,
and each layer catches a different class of failure.

  1. TABLE INVARIANTS      — pure arithmetic on the tables. Instant.
  2. ACHIEVABILITY         — can the real library actually deliver the target?
                             This is the layer that stops us solving for
                             constraints nobody can satisfy.
  3. BAND AGREEMENT        — the planner, analytics and the on-track score must
                             fold Coggan zones into the three-zone model the
                             same way. Three groupings once coexisted here.
  4. DIFFERENTIAL          — POL and PYR must produce measurably different
                             plans, differing in the direction that defines
                             them (POL has less middle). A toggle that changes
                             nothing is worse than no toggle.
  5. PLAN PROPERTIES       — generated weeks land near their target and inside
                             hard safety ceilings, across an athlete x phase x
                             model matrix. Slow; the real end-to-end check.

Layer 5 lives in test_tid_plan_properties.py so the fast layers stay fast.
"""
import os
import pathlib
import sys
import unittest

os.environ.setdefault("PYTHONHASHSEED", "0")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import training_planner as tp  # noqa: E402
import zones as zones_mod      # noqa: E402

MODELS = ("pyramidal", "polarized", "threshold")
PHASES = ("base", "build1", "build2", "peak", "taper", "consolidation", "history")
WORK_PHASES = ("build1", "build2", "peak")


VOLUMES_H = (5, 6, 8, 10, 12, 15, 20)


def _target(model, phase, week_h):
    shape = tp._PHASE_SHAPE[phase]
    hits = tp.hit_slots_for_volume(week_h * 60, shape.hit_count_max)
    return tp.tid_target_pct(model, phase, week_h * 60, hits), hits


class TableInvariants(unittest.TestCase):
    """Layer 1. The dose tables and what they derive — arithmetic and shape."""

    def test_every_model_covers_every_phase(self):
        for m in MODELS:
            self.assertEqual(set(tp.PHASE_TID_DOSE[m]), set(PHASES), m)

    def test_every_derived_target_sums_to_100(self):
        for m in MODELS:
            for ph in PHASES:
                for h in VOLUMES_H:
                    row, _ = _target(m, ph, h)
                    self.assertAlmostEqual(sum(row.values()), 100.0, places=6,
                                           msg=f"{m}/{ph}@{h}h")

    def test_nobody_ever_gets_less_than_70_percent_easy(self):
        """A hard floor on Z1 at every volume, not a preference. Every
        observational dataset puts trained endurance athletes at 70%+ easy by
        time-in-zone, in every phase and every model."""
        for m in MODELS:
            for ph in PHASES:
                for h in VOLUMES_H:
                    row, _ = _target(m, ph, h)
                    self.assertGreaterEqual(row["z1_pct"], 70.0,
                                            f"{m}/{ph}@{h}h: {row}")

    def test_intensity_never_exceeds_the_measured_ceiling(self):
        for m in MODELS:
            for ph in PHASES:
                for h in VOLUMES_H:
                    row, _ = _target(m, ph, h)
                    self.assertLessEqual(row["z3_pct"], 12.0, f"{m}/{ph}@{h}h")

    def test_the_hard_share_FALLS_as_volume_rises(self):
        """The property the dose model exists for, and the one a percentage
        target cannot have. Hard work is capped by recovery; easy volume is not.
        It is why elite cyclists at 25 h/week read 90%+ Z1 by time-in-zone."""
        for m in MODELS:
            for ph in WORK_PHASES:
                seq = [_target(m, ph, h)[0]["z3_pct"] for h in (8, 12, 20)]
                self.assertGreaterEqual(seq[0], seq[-1] - 1e-9,
                                        f"{m}/{ph}: z3 {seq} should not rise with volume")
                self.assertGreater(seq[0], seq[-1],
                                   f"{m}/{ph}: z3 {seq} is flat — dose is not binding")

    def test_the_easy_share_RISES_as_volume_rises(self):
        for m in MODELS:
            for ph in WORK_PHASES:
                seq = [_target(m, ph, h)[0]["z1_pct"] for h in (8, 12, 20)]
                self.assertLess(seq[0], seq[-1], f"{m}/{ph}: z1 {seq}")

    def test_polarized_has_less_middle_than_pyramidal(self):
        """This IS the difference between the two models. If it does not hold,
        the toggle is decoration."""
        for ph in WORK_PHASES:
            for h in VOLUMES_H:
                pol, _ = _target("polarized", ph, h)
                pyr, _ = _target("pyramidal", ph, h)
                self.assertLess(pol["z2_pct"], pyr["z2_pct"], f"{ph}@{h}h")
                self.assertGreater(pol["z3_pct"], pyr["z3_pct"], f"{ph}@{h}h")

    def test_threshold_has_the_most_middle(self):
        for ph in WORK_PHASES:
            for h in VOLUMES_H:
                self.assertGreater(_target("threshold", ph, h)[0]["z2_pct"],
                                   _target("pyramidal", ph, h)[0]["z2_pct"],
                                   f"{ph}@{h}h")

    def test_intensity_rises_from_base_to_peak(self):
        for m in MODELS:
            for h in VOLUMES_H:
                seq = [_target(m, ph, h)[0]["z3_pct"]
                       for ph in ("base", "build1", "build2", "peak")]
                self.assertEqual([round(x, 6) for x in seq],
                                 sorted(round(x, 6) for x in seq),
                                 f"{m}@{h}h: {seq} is not progressive")

    def test_recovery_phases_are_the_easiest(self):
        for m in MODELS:
            for h in VOLUMES_H:
                cons, _ = _target(m, "consolidation", h)
                for ph in WORK_PHASES:
                    self.assertGreater(cons["z1_pct"], _target(m, ph, h)[0]["z1_pct"],
                                       f"{m}/{ph}@{h}h")

    def test_hard_sessions_are_capped_by_recovery_not_by_time(self):
        """48 h between hard days fits four in a seven-day week and no more,
        however many hours the rider has."""
        for h in (12, 15, 20, 30, 40):
            self.assertLessEqual(tp.hit_slots_for_volume(h * 60, 99), 4, f"{h}h")
        self.assertEqual(tp.hit_slots_for_volume(3 * 60, 99), 1)
        self.assertLessEqual(tp.hit_slots_for_volume(6 * 60, 99), 2)

    def test_a_phases_own_ceiling_still_wins(self):
        self.assertEqual(tp.hit_slots_for_volume(20 * 60, 1), 1)
        self.assertEqual(tp.hit_slots_for_volume(20 * 60, 0), 0)

    def test_the_default_sequence_is_pyramidal_base_polarized_peak(self):
        """Filipas 2022: the winning order, and the reason a sequence exists at
        all rather than one model for the whole plan."""
        self.assertEqual(tp.DEFAULT_TID_SEQUENCE["base"], "pyramidal")
        self.assertEqual(tp.DEFAULT_TID_SEQUENCE["build1"], "pyramidal")
        self.assertEqual(tp.DEFAULT_TID_SEQUENCE["peak"], "polarized")
        for ph, m in tp.DEFAULT_TID_SEQUENCE.items():
            self.assertIn(m, MODELS, ph)
            self.assertIn(ph, PHASES, ph)

    def test_the_targets_sit_inside_the_observed_literature_range(self):
        """Sanity against real cyclist data rather than against ourselves.
        Lucia 2000 (pro): 88/11/2 active rest, 78/17/5 pre-comp, 77/15/8 comp.
        Zapico 2007 (U23): 78/20/2 winter, 70/22/8 spring.
        Frontiers 2023 (175 TIDs): median 85/7/6.
        A work-phase target outside 70-95 easy or 0-12 hard is off the map."""
        for m in MODELS:
            for ph in WORK_PHASES:
                for h in VOLUMES_H:
                    row, _ = _target(m, ph, h)
                    self.assertTrue(70 <= row["z1_pct"] <= 95, f"{m}/{ph}@{h}h: {row}")
                    self.assertTrue(0 <= row["z3_pct"] <= 12, f"{m}/{ph}@{h}h: {row}")


class AchievableFromTheLibrary(unittest.TestCase):
    """Layer 2. The anti-fiction layer.

    A target is only a target if the workouts to hit it exist. The old table
    asked for 24% of weekly minutes in its hard band; the library's median
    score>=5 file carries 1.7 minutes above 106% FTP. Every week then ran a
    deficit the planner could not close, and the planner's response was to
    over-prescribe.
    """

    @classmethod
    def setUpClass(cls):
        lib = [r for r in tp.load_workout_library()
               if float(r.get("Score", 0) or 0) >= 5]
        z3 = []
        for r in lib:
            z = tp._row_zone_minutes(r)
            d = float(r.get("Duration(min)", 0) or 0)
            if d > 0:
                # Cap at what a slot would actually serve: TYPE_CEILING clamps
                # a long file down, and its zone minutes clamp with it.
                ceil = tp.TYPE_CEILING.get(tp._content_class_for_row(r))
                k = min(1.0, ceil / d) if ceil else 1.0
                z3.append(z["z5plus"] * k)
        z3.sort(reverse=True)
        cls.z3_sorted = z3
        cls.lib_n = len(lib)

    def test_the_library_is_big_enough_to_judge(self):
        self.assertGreater(self.lib_n, 500)

    def test_every_target_is_reachable_by_the_hit_slots_it_gets(self):
        """For each model x phase x volume, the slots the week actually gets,
        each drawing a realistically good (90th percentile) file, must cover the
        target. Not the library's best file -- one an athlete could plausibly be
        served week after week.

        This is the layer that failed first and forced the redesign: with the
        target stated as a flat percentage, three sessions could serve 8% at
        12 h/week and needed five at 15 h. There is no fifth session."""
        p90 = self.z3_sorted[len(self.z3_sorted) // 10]
        failures = []
        for m in MODELS:
            for ph in PHASES:
                shape = tp._PHASE_SHAPE.get(ph)
                if shape is None:
                    continue
                for week_h in VOLUMES_H:
                    hits = tp.hit_slots_for_volume(week_h * 60, shape.hit_count_max)
                    if hits == 0:
                        continue
                    row = tp.tid_target_pct(m, ph, week_h * 60, hits)
                    need = week_h * 60 * row["z3_pct"] / 100
                    have = hits * p90
                    if need > have + 1e-6:
                        failures.append(
                            f"{m}/{ph} @{week_h}h: needs {need:.0f} min z3, "
                            f"{hits} slots x {p90:.0f} min = {have:.0f} min")
        self.assertEqual(failures, [], "\n".join(failures))

    def test_the_ninetieth_percentile_file_is_what_we_think_it_is(self):
        """Guards the test above from drifting silently if the library changes
        shape. If this moves a lot, the targets need re-deriving, not the
        assertion widening."""
        p90 = self.z3_sorted[len(self.z3_sorted) // 10]
        self.assertGreater(p90, 8.0)
        self.assertLess(p90, 30.0)


class BandsAgreeAcrossModules(unittest.TestCase):
    """Layer 3. One Coggan-to-three-zone map, used everywhere.

    Three groupings once coexisted: the planner counted Coggan Z4 as hard,
    analytics counted it as the middle, and the on-track score used a third
    variant. The same ride read differently depending on which screen showed
    it."""

    def test_the_canonical_map_puts_threshold_in_the_middle(self):
        m = zones_mod.THREE_ZONE_FROM_COGGAN
        self.assertEqual(m["z1"], ("z1", "z2"))
        self.assertEqual(m["z2"], ("z3", "z4"))     # FTP itself lives here
        self.assertEqual(m["z3"], ("z5", "z6", "z7"))

    def test_the_map_is_a_partition_of_the_coggan_zones(self):
        seen = [k for keys in zones_mod.THREE_ZONE_FROM_COGGAN.values() for k in keys]
        self.assertEqual(sorted(seen), [f"z{i}" for i in range(1, 8)])

    def test_analytics_folds_the_same_way(self):
        import analytics
        tiz = {"z1": 100, "z2": 200, "z3": 50, "z4": 40, "z5": 20, "z6": 10, "z7": 5}
        block = analytics.compute_polarization_block(tiz)
        want = zones_mod.three_zone_pct(tiz)
        self.assertAlmostEqual(block["z1z2_pct"], want["z1"], places=1)
        self.assertAlmostEqual(block["z3z4_pct"], want["z2"], places=1)
        self.assertAlmostEqual(block["z5plus_pct"], want["z3"], places=1)

    def test_no_module_uses_the_old_distribution_keys(self):
        """The rival grouping travelled as a pair of dict keys: `z1z2_pct` with
        `z4plus_pct`, meaning "Coggan Z4 counts as hard". Both are gone; the
        three-zone keys are z1_pct / z2_pct / z3_pct.

        Narrow on purpose. `z4plus_in_work_pct` in the content classifier is a
        different quantity -- the share of a single workout's WORK portion above
        the Z4 threshold -- and is not a distribution band.
        """
        import re
        # `z4plus_pct` is the diagnostic marker: a band called "Z4 and above"
        # can only come from the grouping that put threshold work in the hard
        # pole. analytics' own z1z2/z3z4/z5plus keys are the SAME three-zone
        # fold under different names, and are left alone.
        rival = re.compile(r'["\']z4plus_pct["\']')
        offenders = []
        for f in sorted(SRC.rglob("*.py")):
            for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
                if rival.search(line) and not line.strip().startswith("#"):
                    offenders.append(f"{f.relative_to(SRC)}:{i}: {line.strip()[:80]}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
