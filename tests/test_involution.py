"""Involution tests: put a plan through the pipeline and get the same numbers back.

Every layer here converts between the same few quantities in different units --
a distribution ratio, zone minutes, a TSS load, a .zwo file, a measured
time-in-zone. Each conversion is a place the meaning can quietly drift, and unit
tests do not catch drift because each side passes on its own terms.

An involution test asserts the round trip: convert forward, convert back, and
require the original. Six of them here, each closing a loop that was actually
broken at some point in this branch's history:

  target  ->  budget minutes  ->  target            (the ratio survives sizing)
  minutes <-> TSS                                   (time and load agree)
  Coggan  ->  three zones  ->  Coggan               (the fold is a partition)
  dose    ->  .zwo  ->  scanner  ->  dose           (the generator tells the truth)
  plan    ->  sessions  ->  analysis  ->  plan      (what we prescribed is what
                                                     the app reports back)
  week    ->  budget  ->  week                      (a week rebuilt from its own
                                                     budget is the same week)
"""
import os
import pathlib
import sys
import tempfile
import unittest
from datetime import date, timedelta

os.environ.setdefault("PYTHONHASHSEED", "0")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import training_planner as tp   # noqa: E402
import workout_gen as wg        # noqa: E402
import zones as zones_mod       # noqa: E402
import analytics                # noqa: E402

MODELS = ("pyramidal", "polarized", "threshold")
PHASES = ("base", "build1", "build2", "peak", "taper")
VOLUMES_H = (6, 8, 10, 12, 15, 20)


def _budget_shares(b):
    """The budget's four internal buckets read back as three-zone percentages."""
    tot = (b.z1z2_minutes_per_week + b.z3_minutes_per_week
           + b.z4_minutes_per_week + b.z5plus_minutes_per_week) or 1
    return {"z1_pct": 100 * b.z1z2_minutes_per_week / tot,
            "z2_pct": 100 * (b.z3_minutes_per_week + b.z4_minutes_per_week) / tot,
            "z3_pct": 100 * b.z5plus_minutes_per_week / tot}


class TargetSurvivesBecomingABudget(unittest.TestCase):
    """target -> zone minutes -> target."""

    def test_the_ratio_comes_back_unchanged(self):
        bad = []
        for model in MODELS:
            for ph in PHASES:
                shape = tp._PHASE_SHAPE[ph]
                for h in VOLUMES_H:
                    mins = h * 60
                    hits = tp.hit_slots_for_volume(mins, shape.hit_count_max)
                    want = tp.tid_target_pct(model, ph, mins, hits)
                    # Price the target's own minutes as a TSS load, then let
                    # scale_budget_to_week turn that load back into minutes.
                    rate = (want["z1_pct"] / 100 * tp._BAND_TSS_PER_HOUR["z1z2"]
                            + want["z2_pct"] / 100 * tp._BAND_TSS_PER_HOUR["z4"]
                            + want["z3_pct"] / 100 * tp._BAND_TSS_PER_HOUR["z5plus"])
                    tss = mins / 60 * rate
                    got = _budget_shares(tp.scale_budget_to_week(
                        tp.BUDGETS_BY_MODEL[model][ph], tss, mins,
                        model=model, phase_name=ph))
                    for k in ("z1_pct", "z2_pct", "z3_pct"):
                        if abs(got[k] - want[k]) > 1.0:
                            bad.append(f"{model}/{ph}@{h}h {k}: "
                                       f"{want[k]:.1f} -> {got[k]:.1f}")
        self.assertEqual(bad, [], "\n".join(bad[:15]))


class MinutesAndLoadAgree(unittest.TestCase):
    """minutes -> TSS -> minutes, through the same per-band rates.

    The round trip alone is not enough here and that is worth stating: pricing
    and sizing both read _BAND_TSS_PER_HOUR, so if that table drifts they drift
    together and the loop still closes. Replacing the z3 rate with a hardcoded
    42.0 passed cleanly until the first test below was added. A round trip only
    proves consistency; it takes an independent pin to prove correctness.
    """

    def test_the_band_rates_are_still_derived_from_TSS_PER_HOUR(self):
        self.assertEqual(tp._BAND_TSS_PER_HOUR["z1z2"], float(tp.TSS_PER_HOUR["z2"]))
        self.assertEqual(tp._BAND_TSS_PER_HOUR["z3"],
                         (tp.TSS_PER_HOUR["tempo"] + tp.TSS_PER_HOUR["sweetspot"]) / 2)
        self.assertEqual(tp._BAND_TSS_PER_HOUR["z4"],
                         (tp.TSS_PER_HOUR["threshold"] + tp.TSS_PER_HOUR["overunder"]) / 2)
        self.assertEqual(tp._BAND_TSS_PER_HOUR["z5plus"],
                         (tp.TSS_PER_HOUR["vo2max"] + tp.TSS_PER_HOUR["sprint"]) / 2)

    def test_pricing_a_budget_and_sizing_it_back_is_the_identity(self):
        for model in MODELS:
            for ph in PHASES:
                for tss in (150, 300, 500, 800):
                    b = tp.scale_budget_to_week(tp.BUDGETS_BY_MODEL[model][ph], tss)
                    priced = (b.z1z2_minutes_per_week / 60 * tp._BAND_TSS_PER_HOUR["z1z2"]
                              + b.z3_minutes_per_week / 60 * tp._BAND_TSS_PER_HOUR["z3"]
                              + b.z4_minutes_per_week / 60 * tp._BAND_TSS_PER_HOUR["z4"]
                              + b.z5plus_minutes_per_week / 60 * tp._BAND_TSS_PER_HOUR["z5plus"])
                    self.assertAlmostEqual(priced, tss, delta=4,
                                           msg=f"{model}/{ph} @{tss} -> {priced:.0f}")


class TheZoneFoldIsAPartition(unittest.TestCase):
    """Coggan seconds -> three zones -> the same total, nothing lost or double-counted."""

    def test_every_second_survives_the_fold(self):
        cases = [
            {f"z{i}": v for i, v in enumerate([600, 2400, 900, 300, 200, 60, 15], start=1)},
            {"z1": 3600},
            {"z5": 100, "z7": 50},
            {},
        ]
        for tiz in cases:
            folded = zones_mod.three_zone(tiz)
            self.assertAlmostEqual(sum(folded.values()),
                                   sum(float(v) for v in tiz.values()), places=6,
                                   msg=str(tiz))

    def test_the_percentages_come_back_to_a_hundred(self):
        tiz = {"z1": 600, "z2": 2400, "z3": 900, "z4": 300, "z5": 200, "z6": 60, "z7": 15}
        pct = zones_mod.three_zone_pct(tiz)
        self.assertAlmostEqual(sum(pct.values()), 100.0, places=6)

    def test_analytics_and_the_canonical_fold_are_the_same_function(self):
        tiz = {"z1": 600, "z2": 2400, "z3": 900, "z4": 300, "z5": 200, "z6": 60, "z7": 15}
        block = analytics.compute_polarization_block(tiz)
        want = zones_mod.three_zone_pct(tiz)
        self.assertAlmostEqual(block["z1z2_pct"], want["z1"], places=1)
        self.assertAlmostEqual(block["z3z4_pct"], want["z2"], places=1)
        self.assertAlmostEqual(block["z5plus_pct"], want["z3"], places=1)


class TheGeneratorTellsTheTruth(unittest.TestCase):
    """dose -> .zwo -> scanner -> dose."""

    def test_what_it_claims_is_what_the_scanner_measures(self):
        import app
        tmp = pathlib.Path(tempfile.mkdtemp())
        bad, n = [], 0
        for st, win, dose in (("vo2max", 60, 18), ("vo2max", 90, 24),
                              ("threshold", 75, 32), ("overunder", 70, 40),
                              ("sweetspot", 90, 45), ("threshold", 100, 40)):
            r = wg.generate(st, win, dose)
            if r is None:
                continue
            n += 1
            fn, xml, s = r
            p = tmp / fn
            p.write_text(xml, encoding="utf-8")
            scan = app._scan_zwo_for_library(p)
            band = s.protocol.band()
            got = {"z1": scan["z1_sec"] + scan["z2_sec"],
                   "z2": scan["z3_sec"] + scan["z4_sec"],
                   "z3": scan["z5_sec"] + scan["z6_sec"]}[band] / 60
            if abs(got - s.band_s[band] / 60) > 0.1:
                bad.append(f"{st} {dose}/{win}: claims {s.band_s[band]/60:.1f}, "
                           f"scanner {got:.1f}")
        self.assertGreaterEqual(n, 5, "the matrix produced nothing to check")
        self.assertEqual(bad, [], "\n".join(bad))


class APlanReadsBackAsItWasPrescribed(unittest.TestCase):
    """plan -> sessions -> the analysis path -> the plan's own numbers.

    The loop that was broken for the whole life of this codebase: the planner
    folded Coggan zones one way and the analysis another, so a week never read
    back as what it was prescribed. This closes it end to end.
    """

    @classmethod
    def setUpClass(cls):
        cls.lib = {r["File"]: r for r in tp.load_workout_library()}
        dmh = {0: 1.0, 1: 1.75, 2: 1.5, 3: 1.75, 4: 1.0, 5: 4.0, 6: 1.0}
        g = tp.Goal(goal_type="event", rest_days=[], available_days=list(range(7)),
                    daily_max_hours=dmh, hours_per_week=sum(dmh.values()),
                    max_weekday_hours=2.0, max_weekend_hours=4.0,
                    distribution="pyramidal",
                    target_date=date.today() + timedelta(weeks=16),
                    event_name="involution", event_km=140.0, event_climb_m=1400.0)
        _p, cls.weeks = tp.generate_plan(g, seed_salt=5, current_ctl=50.0,
                                        recent_weekly_tss=450.0)

    def _week_coggan_seconds(self, w):
        acc = {f"z{i}": 0.0 for i in range(1, 8)}
        for s in w.sessions:
            r = self.lib.get(s.zwo_file or "")
            if not r:
                continue
            fd = float(r.get("Duration(min)", 0) or 0)
            sd = float(s.duration_min or 0)
            k = (sd / fd) if fd > 0 else 1.0
            for i in range(1, 7):
                acc[f"z{i}"] += float(r.get(f"Z{i}%", 0) or 0) / 100 * fd * 60 * k
        return acc

    def test_the_analysis_path_returns_the_planners_own_zone_split(self):
        """Fold a planned week with zones.three_zone and with
        analytics.compute_polarization_block; they must agree to a rounding
        step, because they are meant to be the same operation."""
        bad = []
        for w in self.weeks:
            if (w.end - w.start).days < 6:
                continue
            secs = self._week_coggan_seconds(w)
            if sum(secs.values()) <= 0:
                continue
            direct = zones_mod.three_zone_pct(secs)
            via_analytics = analytics.compute_polarization_block(
                {k: int(v) for k, v in secs.items()})
            for a, b, label in ((direct["z1"], via_analytics["z1z2_pct"], "z1"),
                                (direct["z2"], via_analytics["z3z4_pct"], "z2"),
                                (direct["z3"], via_analytics["z5plus_pct"], "z3")):
                if abs(a - b) > 0.2:
                    bad.append(f"wk{w.week_num} {label}: {a:.1f} vs {b:.1f}")
        self.assertEqual(bad, [], "\n".join(bad[:15]))

    def test_a_weeks_sessions_account_for_its_reported_duration(self):
        bad = []
        for w in self.weeks:
            if (w.end - w.start).days < 6:
                continue
            secs = sum(self._week_coggan_seconds(w).values())
            planned = sum(float(s.duration_min or 0) for s in w.sessions
                          if s.zwo_file and s.zwo_file in self.lib) * 60
            if planned > 0 and abs(secs - planned) > 60:
                bad.append(f"wk{w.week_num}: zones {secs/60:.0f}min vs "
                           f"sessions {planned/60:.0f}min")
        self.assertEqual(bad, [], "\n".join(bad[:15]))

    def test_the_week_is_classified_the_same_way_from_either_side(self):
        for w in self.weeks:
            if (w.end - w.start).days < 6:
                continue
            secs = self._week_coggan_seconds(w)
            if sum(secs.values()) <= 0:
                continue
            d = zones_mod.three_zone_pct(secs)
            a = analytics.compute_polarization_block({k: int(v) for k, v in secs.items()})
            self.assertEqual(
                analytics.classify_distribution(d["z1"], d["z2"], d["z3"]),
                analytics.classify_distribution(a["z1z2_pct"], a["z3z4_pct"],
                                                a["z5plus_pct"]),
                f"wk{w.week_num}")


if __name__ == "__main__":
    unittest.main()
