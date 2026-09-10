"""The single owner of week state, and the auditor that grades it.

Two things are pinned here. First, that plan_invariants actually fails when
the fault it names is present -- an auditor nobody has tried to fool is not
evidence, and the first version of check_slot_file_coherence iterated the
training sessions only, which made it permanently blind to the rest-slot
fault it exists to catch. Second, that TrainingWeek refuses to hand out a week
whose intensity exceeds what the athlete's ceiling can carry, which is the
decision the old build-then-repair chain could not make: the volume pass may
shrink easy rides and never hard ones, so a week that arrived with 175 TSS of
intensity against a 112 TSS ceiling stayed 56% over however hard it worked.
"""
import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import training_planner as tp        # noqa: E402
import week_plan as wp               # noqa: E402
import plan_invariants as pi         # noqa: E402

MON = dt.date(2026, 9, 7)


def sess(day, t="vo2max", dur=60, tss=80, f=""):
    return tp.PlannedSession(day=day, day_name=day.strftime("%a"), session_type=t,
                             duration_min=dur, tss_estimate=tss,
                             description="x", zwo_file=f)


def week(sessions, target=300, n=1):
    return tp.PlannedWeek(week_num=n, start=MON, end=MON + dt.timedelta(days=6),
                          phase="base1", tss_target=target, is_stepback=False,
                          sessions=sessions)


def goal():
    return tp.Goal(goal_type="continuous", rest_days=[6],
                   available_days=[0, 1, 2, 3, 4, 5],
                   daily_max_hours={0: 1.0, 1: 2.0, 2: 2.0, 3: 2.0, 4: 2.0, 5: 2.0},
                   hours_per_week=10, max_weekday_hours=2.0,
                   max_weekend_hours=2.0, plan_weeks=4)


class TheAuditorFailsWhenItShould(unittest.TestCase):
    """Each check, fed a plan carrying exactly the fault it names."""

    def _fires(self, rule, wk):
        hits = {v.rule for v in pi.audit([wk], goal())}
        self.assertIn(rule, hits, f"{rule} did not fire; saw {sorted(hits)}")

    def test_back_to_back_hard_days(self):
        self._fires("hard_day_spacing",
                    week([sess(MON), sess(MON + dt.timedelta(days=1))]))

    def test_week_over_its_own_target(self):
        self._fires("weekly_volume", week([sess(MON, tss=500)], target=200))

    def test_session_on_a_day_the_athlete_cannot_ride(self):
        self._fires("rest_days", week([sess(MON + dt.timedelta(days=6))]))

    def test_session_longer_than_the_day_allows(self):
        self._fires("daily_duration_cap", week([sess(MON, dur=180)]))

    def test_rest_slot_still_carrying_a_hard_workout(self):
        self._fires("slot_file_coherence", week([
            sess(MON, dur=60, tss=80),
            sess(MON + dt.timedelta(days=3), t="rest", dur=0, tss=0, f="ghost.zwo"),
        ]))

    def test_week_with_a_target_and_nothing_in_it(self):
        self._fires("empty_week", week([], target=300))

    def test_a_legal_week_is_reported_clean(self):
        wk = week([sess(MON, dur=60, tss=80),
                   sess(MON + dt.timedelta(days=3), dur=60, tss=80)], target=300)
        self.assertEqual(pi.audit([wk], goal()), [])


class TheOwnerDecidesRatherThanRepairs(unittest.TestCase):
    def _owner(self, ceiling, ridden=None):
        ctx = wp.WeekContext(week_num=1, start=MON, phase=None, goal=goal(),
                             tss_ceiling=ceiling, ridden=ridden or [])
        return wp.TrainingWeek(ctx)

    def test_hard_work_is_capped_at_its_share_of_the_ceiling(self):
        """The constraint the repair pass could not enforce."""
        tw = self._owner(200)
        for d in range(0, 6, 2):                       # spaced, so only the cap bites
            tw._commit(sess(MON + dt.timedelta(days=d), t="vo2max", dur=60, tss=90))
        self.assertLessEqual(tw._hard_tss, 200 * wp.HARD_CEILING_SHARE + 0.5,
                             f"hard load {tw._hard_tss} exceeds its share of 200")

    def test_the_taper_keeps_its_intensity(self):
        """Mujika & Padilla 2003: a taper cuts volume and HOLDS intensity, so the
        hard-share cap must not bite in it. Invisible at plan level -- every
        event plan built for the gates carried 0-50% hard in its taper -- so it
        is pinned here, where the exemption actually binds (gates.md W03)."""
        taper = tp.Phase(name="taper", start=MON, end=MON + dt.timedelta(days=6), weeks=1,
                         focus="", weekly_tss_target=200, z2_pct=70, hit_per_week=3,
                         session_types=[])
        ctx = wp.WeekContext(week_num=1, start=MON, phase=taper, goal=goal(), tss_ceiling=200)
        tw = wp.TrainingWeek(ctx)
        for d in range(0, 6, 2):
            tw._commit(sess(MON + dt.timedelta(days=d), t="vo2max", dur=60, tss=80))
        self.assertGreater(tw._hard_tss, 200 * wp.HARD_CEILING_SHARE + 1,
                           "the taper's intensity was cut by the hard-share cap")

    def test_the_week_total_stays_under_the_ceiling(self):
        tw = self._owner(150)
        for d in range(6):
            tw._commit(sess(MON + dt.timedelta(days=d), t="z2", dur=90, tss=70))
        self.assertLessEqual(tw._tss, 150 + 0.5)

    def test_a_hard_session_too_close_to_another_is_eased_to_z2(self):
        """Eased for recovery means eased to somewhere recoverable.

        Dropping one rung lands on tempo, which is the moderate-intensity
        black hole: most of the glycolytic cost of the session it replaced,
        none of the polarisation that makes a three-zone week work.
        """
        tw = self._owner(400)
        tw._commit(sess(MON, t="vo2max", dur=60, tss=80))
        second = tw._commit(sess(MON + dt.timedelta(days=1), t="vo2max", dur=60, tss=80))
        self.assertEqual(second.session_type, "z2")
        self.assertNotEqual(second.session_type, "tempo")

    def test_the_easing_drops_the_workout_file_it_no_longer_matches(self):
        tw = self._owner(400)
        tw._commit(sess(MON, t="vo2max", dur=60, tss=80))
        second = tw._commit(sess(MON + dt.timedelta(days=1), t="vo2max",
                                 dur=60, tss=80, f="vo2_5x3min.zwo"))
        self.assertEqual(second.zwo_file, "")

    def test_work_already_ridden_comes_off_the_ceiling(self):
        """The Thursday-regenerate bug: a week the athlete is partway through
        is not a blank slate."""
        ridden = [{"date": (MON + dt.timedelta(days=1)).isoformat(), "tss": 120.0}]
        self.assertEqual(self._owner(300).ceiling, 300)
        self.assertAlmostEqual(self._owner(300, ridden).ceiling, 180)

    def test_a_week_already_fully_ridden_prescribes_nothing_more(self):
        ridden = [{"date": MON.isoformat(), "tss": 400.0}]
        tw = self._owner(300, ridden)
        self.assertEqual(tw.ceiling, 0)
        s = tw._commit(sess(MON + dt.timedelta(days=3), t="z2", dur=90, tss=70))
        self.assertEqual(s.session_type, "rest")


class TheSealCatchesLaterWriters(unittest.TestCase):
    def setUp(self):
        wp._install_seal(tp.PlannedSession)
        self._strict = wp.STRICT_SEAL
        wp.STRICT_SEAL = True

    def tearDown(self):
        wp.STRICT_SEAL = self._strict

    def _sealed(self):
        ctx = wp.WeekContext(week_num=1, start=MON, phase=None, goal=goal(),
                             tss_ceiling=300)
        tw = wp.TrainingWeek(ctx)
        tw._commit(sess(MON, t="z2", dur=60, tss=50))
        tw.week = week(tw._committed)
        tw.seal()
        return tw._committed[0]

    def test_editing_a_committed_session_raises(self):
        s = self._sealed()
        with self.assertRaises(wp.SealedSessionError):
            s.session_type = "vo2max"

    def test_the_error_names_the_pass_that_did_it(self):
        s = self._sealed()
        with self.assertRaises(wp.SealedSessionError) as cm:
            s.duration_min = 999
        self.assertIn("test_the_error_names_the_pass_that_did_it", str(cm.exception))

    def test_amend_is_the_supported_way_for_the_athlete_to_edit(self):
        s = self._sealed()
        with wp.TrainingWeek.amend(s):
            s.session_type = "vo2max"
        self.assertEqual(s.session_type, "vo2max")
        with self.assertRaises(wp.SealedSessionError):
            s.session_type = "z2"          # re-sealed on the way out


if __name__ == "__main__":
    unittest.main()
