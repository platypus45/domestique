"""Regeneration must know what KIND of work was already done, not just how much.

_completed_tss_in has always told the planner how much load sat inside a week.
It could not tell it three hard days from three long easy ones, and those are
not the same week: both spend the load, only one spends the intensity. A rider
who has banked the week's hard minutes should be prescribed easy ones.

_completed_zones_in reads the executed rides' own time-in-zone and folds it
through the same canonical map everything else uses, and scale_budget_to_week
subtracts it band by band.
"""
import json
import os
import pathlib
import sys
import unittest
from datetime import date

os.environ.setdefault("PYTHONHASHSEED", "0")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import training_planner as tp  # noqa: E402

MON, SUN = date(2026, 9, 7), date(2026, 9, 13)
BANDS = ("z1z2", "z3", "z4", "z5plus")


def _mins(b):
    return {"z1z2": b.z1z2_minutes_per_week, "z3": b.z3_minutes_per_week,
            "z4": b.z4_minutes_per_week, "z5plus": b.z5plus_minutes_per_week}


class ReadsWhicheverShapeTheCallerHas(unittest.TestCase):
    def test_the_ride_storage_dict_shape(self):
        rides = [{"date": "2026-09-08",
                  "time_in_zone": {"z1": 600, "z2": 1200, "z3": 300,
                                   "z4": 120, "z5": 60, "z6": 30, "z7": 0}}]
        z = tp._completed_zones_in(rides, MON, SUN)
        self.assertAlmostEqual(z["z1z2"], 30.0)      # (600+1200)/60
        self.assertAlmostEqual(z["z3"], 5.0)
        self.assertAlmostEqual(z["z4"], 2.0)
        self.assertAlmostEqual(z["z5plus"], 1.5)     # (60+30+0)/60

    def test_the_db_row_shape_with_the_icu_envelope_in_raw_json(self):
        raw = {"icu_zone_times": [{"id": "Z1", "secs": 600}, {"id": "Z2", "secs": 1200},
                                  {"id": "Z3", "secs": 300}, {"id": "Z4", "secs": 120},
                                  {"id": "Z5", "secs": 60}, {"id": "Z6", "secs": 30},
                                  {"id": "Z7", "secs": 0}]}
        rows = [{"date": "2026-09-08", "raw_json": json.dumps(raw)}]
        z = tp._completed_zones_in(rows, MON, SUN)
        self.assertAlmostEqual(z["z1z2"], 30.0)
        self.assertAlmostEqual(z["z5plus"], 1.5)

    def test_the_sweet_spot_overlay_is_not_counted_as_an_eighth_zone(self):
        """ICU reports an "SS" bucket alongside the seven zones; its seconds
        OVERLAP Z3/Z4. Counting it would inflate the week's ridden minutes."""
        raw = {"icu_zone_times": [{"id": "Z1", "secs": 600}, {"id": "Z3", "secs": 600},
                                  {"id": "SS", "secs": 400}]}
        z = tp._completed_zones_in([{"date": "2026-09-08", "raw_json": json.dumps(raw)}],
                                   MON, SUN)
        self.assertAlmostEqual(sum(z.values()), 20.0)   # 1200s, not 1600s

    def test_rides_outside_the_window_do_not_count(self):
        rides = [{"date": "2026-09-06", "time_in_zone": {"z1": 3600}},
                 {"date": "2026-09-14", "time_in_zone": {"z1": 3600}}]
        self.assertEqual(sum(tp._completed_zones_in(rides, MON, SUN).values()), 0.0)

    def test_a_ride_with_no_zone_data_contributes_nothing_rather_than_a_guess(self):
        rides = [{"date": "2026-09-08", "tss": 120}]      # HR-only, no power
        self.assertEqual(sum(tp._completed_zones_in(rides, MON, SUN).values()), 0.0)

    def test_junk_input_is_survivable(self):
        for junk in (None, [], [None], ["not a dict"], [{"date": "x"}],
                     [{"date": "2026-09-08", "raw_json": "{not json"}]):
            self.assertEqual(set(tp._completed_zones_in(junk, MON, SUN)), set(BANDS))


class TheBudgetIsWhatIsLeft(unittest.TestCase):
    def test_nothing_ridden_leaves_the_budget_untouched(self):
        """The involution: subtracting zero is the identity."""
        b = tp.get_budget_for_phase("build1")
        plain = tp.scale_budget_to_week(b, 300, 600, model="pyramidal",
                                        phase_name="build1")
        zeroed = tp.scale_budget_to_week(b, 300, 600, model="pyramidal",
                                         phase_name="build1",
                                         spent_zones={k: 0.0 for k in BANDS})
        self.assertEqual(_mins(plain), _mins(zeroed))

    def test_what_was_ridden_comes_off_its_own_band(self):
        b = tp.get_budget_for_phase("build1")
        plain = _mins(tp.scale_budget_to_week(b, 300, 600, model="pyramidal",
                                              phase_name="build1"))
        spent = {"z1z2": 0.0, "z3": 0.0, "z4": 0.0, "z5plus": 5.0}
        left = _mins(tp.scale_budget_to_week(b, 300, 600, model="pyramidal",
                                             phase_name="build1", spent_zones=spent))
        self.assertEqual(left["z5plus"], max(0, plain["z5plus"] - 5))
        for k in ("z1z2", "z3", "z4"):
            self.assertEqual(left[k], plain[k], f"{k} should be untouched")

    def test_a_band_already_spent_goes_to_zero_not_negative(self):
        b = tp.get_budget_for_phase("build1")
        left = _mins(tp.scale_budget_to_week(
            b, 300, 600, model="pyramidal", phase_name="build1",
            spent_zones={k: 10_000.0 for k in BANDS}))
        for k in BANDS:
            self.assertEqual(left[k], 0, k)

    def test_the_case_this_was_built_for(self):
        """Three hard days and three long easy days carry the same load and
        leave very different weeks. The first must have spent its intensity."""
        b = tp.get_budget_for_phase("build1")
        hard = _mins(tp.scale_budget_to_week(
            b, 300, 600, model="pyramidal", phase_name="build1",
            spent_zones={"z1z2": 100.0, "z3": 0.0, "z4": 20.0, "z5plus": 30.0}))
        easy = _mins(tp.scale_budget_to_week(
            b, 300, 600, model="pyramidal", phase_name="build1",
            spent_zones={"z1z2": 150.0, "z3": 0.0, "z4": 0.0, "z5plus": 0.0}))
        self.assertLess(hard["z5plus"], easy["z5plus"],
                        "the hard week must have less intensity left to spend")
        self.assertGreater(hard["z1z2"], easy["z1z2"],
                           "and more easy volume still owed")


if __name__ == "__main__":
    unittest.main()
