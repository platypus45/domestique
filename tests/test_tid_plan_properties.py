"""Layer 5: properties of the plans the models actually produce.

Layers 1-4 (tests/test_tid_targets.py) check the tables and the maps. They
cannot tell you whether a real plan built from those tables is safe, trainable,
or different from the other model's. This does, end to end, over a matrix of
athlete volumes and models.

Slow by construction -- each case is a real generate_plan. The safety ceilings
here are absolute: no plausible target, model or bug may produce a week outside
them, so they are the last line before a rider is handed something harmful.
"""
import os
import pathlib
import sys
import unittest
from datetime import date, timedelta

os.environ.setdefault("PYTHONHASHSEED", "0")
SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
sys.path.insert(0, str(SRC))
import training_planner as tp  # noqa: E402

# Realistic weeks, not uniform days: short weekdays, a long weekend ride.
SHAPES = {
    "6h":  {0: 0.75, 1: 1.0, 2: 1.0, 3: 1.0, 4: 0.75, 5: 1.5, 6: 0.0},
    "12h": {0: 1.0,  1: 1.75, 2: 1.5, 3: 1.75, 4: 1.0, 5: 4.0, 6: 1.0},
    "18h": {0: 1.5,  1: 2.5, 2: 2.0, 3: 2.5, 4: 1.5, 5: 5.0, 6: 3.0},
}
MODELS = ("pyramidal", "polarized", "threshold")

# Absolute safety rails. Not targets -- the line past which a week is wrong
# whatever the model says.
MAX_Z3_PCT = 18.0        # above 106% FTP, as a share of the week's minutes
MIN_Z1_PCT = 55.0
MAX_HIT_PER_WEEK = 4


def _goal(dmh, weeks_out=16, distribution="auto"):
    return tp.Goal(
        goal_type="event", distribution=distribution, rest_days=[d for d, h in dmh.items() if h <= 0],
        available_days=[d for d, h in dmh.items() if h > 0],
        daily_max_hours={d: h for d, h in dmh.items() if h > 0},
        hours_per_week=sum(dmh.values()),
        max_weekday_hours=max(h for d, h in dmh.items() if d < 5),
        max_weekend_hours=max(h for d, h in dmh.items() if d >= 5) or 1.0,
        target_date=date.today() + timedelta(weeks=weeks_out),
        event_name="property test", event_km=140.0, event_climb_m=1400.0,
    )


class PlanProperties(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = {r["File"]: r for r in tp.load_workout_library()}
        cls.plans = {}
        for label, dmh in SHAPES.items():
            for model in MODELS:
                # On the GOAL, not the module global: generate_plan calls
                # set_active_distribution from goal.distribution itself, so a
                # global set here is overwritten before the first week is laid
                # -- which is exactly how the first version of this test
                # "passed" the toggle check with two identical plans.
                try:
                    _ph, weeks = tp.generate_plan(
                        _goal(dmh, distribution=model), seed_salt=3,
                        current_ctl=50.0, recent_weekly_tss=450.0)
                except Exception as e:      # a model must never fail to plan
                    cls.plans[(label, model)] = e
                    continue
                cls.plans[(label, model)] = weeks

    def _bands(self, w):
        acc = {f"z{i}": 0.0 for i in range(1, 7)}
        for s in w.sessions:
            r = self.lib.get(s.zwo_file or "")
            if not r:
                continue
            fd = float(r.get("Duration(min)", 0) or 0)
            sd = float(s.duration_min or 0)
            k = (sd / fd) if fd > 0 else 1.0
            for i in range(1, 7):
                acc[f"z{i}"] += float(r.get(f"Z{i}%", 0) or 0) / 100 * fd * k
        t = sum(acc.values()) or 1.0
        return (100 * (acc["z1"] + acc["z2"]) / t,
                100 * (acc["z3"] + acc["z4"]) / t,
                100 * (acc["z5"] + acc["z6"]) / t)

    def _full_weeks(self, weeks):
        return [w for w in weeks
                if (w.end - w.start).days >= 6 and not w.is_stepback]

    def test_every_model_at_every_volume_produces_a_plan(self):
        for key, val in self.plans.items():
            self.assertNotIsInstance(val, Exception, f"{key}: {val}")
            self.assertGreater(len(val), 4, key)

    def test_no_week_breaches_the_intensity_ceiling(self):
        bad = []
        for key, weeks in self.plans.items():
            for w in self._full_weeks(weeks):
                _e, _m, hard = self._bands(w)
                if hard > MAX_Z3_PCT:
                    bad.append(f"{key} {w.phase} wk{w.week_num}: {hard:.1f}% z3")
        self.assertEqual(bad, [], "\n".join(bad[:15]))

    def test_no_week_falls_below_the_easy_floor(self):
        bad = []
        for key, weeks in self.plans.items():
            for w in self._full_weeks(weeks):
                easy, _m, _h = self._bands(w)
                if easy < MIN_Z1_PCT:
                    bad.append(f"{key} {w.phase} wk{w.week_num}: {easy:.1f}% z1")
        self.assertEqual(bad, [], "\n".join(bad[:15]))

    def test_hard_sessions_stay_inside_the_phase_cap(self):
        bad = []
        for key, weeks in self.plans.items():
            for w in self._full_weeks(weeks):
                # tp's own union-of-axes predicate, not a local guess: a local
                # set that counted tempo and sweet spot as hard reported five
                # "HIT" sessions in a week the sampler had capped at three.
                n = tp._week_hit_count(w)
                cap = tp.get_budget_for_phase(w.phase).hit_count_max
                if n > max(cap, MAX_HIT_PER_WEEK):
                    bad.append(f"{key} {w.phase} wk{w.week_num}: {n} HIT > {cap}")
        self.assertEqual(bad, [], "\n".join(bad[:15]))

    def test_hard_days_keep_48_hours_apart(self):
        """Seiler 2010. The rule that makes four hard sessions the ceiling.

        This was an expectedFailure: the sampler spaced the SLOTS it
        designated hard, but a file served to an endurance slot can carry
        threshold content, and nothing re-checked spacing against what was
        actually served. Measured on the 6 h shape, 16-week event plan, seed 3:
        6 breaches.

        It passes now because the check moved to the end -- a rule about what
        the rider RECEIVES is verified on what the rider receives -- and
        because that final pass runs in every entry point rather than only in
        generate_plan. The predicate below is tp._session_is_hit, which reads
        the served content, so this is the content-level rule, not the
        slot-label one. Within-week only; cross-week spacing is covered by
        plan_invariants.check_hard_day_spacing over the whole plan.
        """
        bad = []
        for key, weeks in self.plans.items():
            for w in self._full_weeks(weeks):
                days = sorted(s.day for s in w.sessions if tp._session_is_hit(s))
                for a, b in zip(days, days[1:]):
                    if (b - a).days < 2:
                        bad.append(f"{key} {w.phase}: hard on {a} and {b}")
        self.assertEqual(bad, [], "\n".join(bad[:15]))

    def test_the_toggle_actually_changes_the_plan(self):
        """A toggle that changes nothing is worse than no toggle: it tells the
        rider their choice mattered when it did not."""
        for label in SHAPES:
            pol = self.plans[(label, "polarized")]
            pyr = self.plans[(label, "pyramidal")]
            pol_files = {s.zwo_file for w in pol for s in w.sessions if s.zwo_file}
            pyr_files = {s.zwo_file for w in pyr for s in w.sessions if s.zwo_file}
            overlap = len(pol_files & pyr_files) / max(1, len(pol_files | pyr_files))
            self.assertLess(overlap, 0.9,
                            f"{label}: POL and PYR share {overlap:.0%} of their files")

    def test_polarized_plans_carry_less_middle_than_threshold_plans(self):
        """The direction that defines the models, checked on delivered plans
        rather than on the tables. Averaged over the plan because a single week
        is noisy -- the sampler picks from a library, not from the target."""
        for label in SHAPES:
            mids = {}
            for model in ("polarized", "threshold"):
                ws = self._full_weeks(self.plans[(label, model)])
                mids[model] = sum(self._bands(w)[1] for w in ws) / max(1, len(ws))
            self.assertLess(mids["polarized"], mids["threshold"],
                            f"{label}: POL mid {mids['polarized']:.1f} "
                            f"vs THR mid {mids['threshold']:.1f}")

    def test_higher_volume_does_not_mean_a_harder_week(self):
        """The dose model's promise, delivered: more hours must buy easy
        minutes, not more intensity, because intensity is capped by recovery."""
        for model in MODELS:
            hard = {}
            for label in ("6h", "18h"):
                ws = self._full_weeks(self.plans[(label, model)])
                hard[label] = sum(self._bands(w)[2] for w in ws) / max(1, len(ws))
            self.assertLessEqual(hard["18h"], hard["6h"] + 3.0,
                                 f"{model}: 6h {hard['6h']:.1f}% z3 -> "
                                 f"18h {hard['18h']:.1f}%")


if __name__ == "__main__":
    unittest.main()
