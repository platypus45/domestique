"""The auditor fails when it should, and only then.

Each rule is fed a plan carrying exactly the fault it names -- an auditor
nobody has tried to fool is not evidence -- and the legal version of the same
week alongside it, so the rule is shown not to fire on noise. The first
auditor's controls live in test_week_plan_owner.py and still apply.
"""
import datetime as dt
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import training_planner as tp        # noqa: E402
import plan_invariants as pi         # noqa: E402

MON = dt.date(2026, 9, 7)
HARD_FILE = "anaerobic_3x10x15s-45s_121pct_64min.zwo"   # classified anaerobic
EASY_FILE = "z2_gate_probe_60min.zwo"                    # unclassified -> prefix -> endurance


def day(i):
    return MON + dt.timedelta(days=i)


def sess(i, t="z2", dur=60, tss=45, f="", status="pending", **kw):
    s = tp.PlannedSession(day=day(i), day_name=day(i).strftime("%a"), session_type=t,
                          duration_min=dur, tss_estimate=tss, description="x",
                          zwo_file=f, status=status)
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def week(sessions, target=300, n=1, first=0, last=6, phase="build1", stepback=False):
    return tp.PlannedWeek(week_num=n, start=day(first), end=day(last), phase=phase,
                          tss_target=target, is_stepback=stepback, sessions=sessions)


def goal(**kw):
    base = dict(goal_type="continuous", rest_days=[6], available_days=[0, 1, 2, 3, 4, 5],
                daily_max_hours={d: 2.0 for d in range(6)}, hours_per_week=10,
                max_weekday_hours=2.0, max_weekend_hours=2.0, plan_weeks=4)
    base.update(kw)
    return tp.Goal(**base)


def rules(weeks, g=None, rides=None, today=None):
    return {v.rule for v in pi.audit(weeks, g or goal(), rides=rides, today=today)}


def rides(first, last, tss):
    return [{"date": day(i).isoformat(), "tss": tss} for i in range(first, last + 1)]


class TheServedFileIsWhatCounts(unittest.TestCase):
    def test_a_classified_file_resolves_to_its_content(self):
        self.assertEqual(pi.content_class(HARD_FILE), "anaerobic")

    def test_an_unclassified_file_falls_back_to_a_clear_prefix(self):
        self.assertEqual(pi.content_class(EASY_FILE), "endurance")

    def test_an_unknown_file_is_unknown_not_guessed(self):
        self.assertEqual(pi.content_class("mystery.zwo"), "")


class Spacing(unittest.TestCase):
    def test_an_easy_label_serving_hard_content_is_a_hard_day(self):
        """The fault the label-only auditor could not see."""
        wk = week([sess(0, t="z2", f=HARD_FILE), sess(1, t="vo2max", tss=80)])
        self.assertIn("hard_day_spacing", rules([wk]))

    def test_the_same_week_with_easy_content_is_clean(self):
        wk = week([sess(0, t="z2", f=EASY_FILE), sess(1, t="vo2max", tss=80)])
        self.assertNotIn("hard_day_spacing", rules([wk]))

    def test_a_missed_session_does_not_block_the_next_day(self):
        wk = week([sess(0, t="vo2max", tss=80, status="missed"), sess(1, t="vo2max", tss=80)])
        self.assertNotIn("hard_day_spacing", rules([wk]))

    def test_a_dismissed_session_does_not_block_the_next_day(self):
        wk = week([sess(0, t="vo2max", tss=80, status="dismissed"), sess(1, t="vo2max", tss=80)])
        self.assertNotIn("hard_day_spacing", rules([wk]))

    def test_the_opener_before_a_race_is_by_design(self):
        wk = week([sess(4, t="vo2max", tss=40, is_opener=True),
                   sess(5, t="race", tss=250, is_race=True)])
        self.assertNotIn("hard_day_spacing", rules([wk]))


class TheBudgetIsDerivedNotRead(unittest.TestCase):
    """A Thursday stub (Thu-Sun), target prorated 4/7 of a 350-TSS week."""

    def stub(self, *sessions):
        return week(list(sessions), target=200, first=3, last=6)

    def test_rides_come_off_the_calendar_week(self):
        ridden = rides(0, 2, 100)                       # 300 of 350 already done
        over = self.stub(sess(3, tss=75), sess(4, tss=75))
        fair = self.stub(sess(3, tss=50))
        self.assertIn("weekly_volume", rules([over], rides=ridden, today=day(3)))
        self.assertNotIn("weekly_volume", rules([fair], rides=ridden, today=day(3)))

    def test_a_double_subtraction_is_caught_as_under_delivery(self):
        """OWN-1: rides subtracted AND the week prorated -> 19 TSS for Thu-Sun."""
        ridden = rides(0, 2, 34)                        # ~100 of 350 done
        starved = self.stub(sess(3, tss=19))
        fair = self.stub(sess(3, tss=60), sess(4, tss=60), sess(5, tss=60))
        self.assertIn("under_delivery", rules([starved], rides=ridden, today=day(3)))
        self.assertNotIn("under_delivery", rules([fair], rides=ridden, today=day(3)))

    def test_the_stamped_net_target_is_not_trusted(self):
        """Grading against the planner's own net_tss_target hid OWN-1."""
        starved = self.stub(sess(3, tss=19))
        starved.net_tss_target = 19
        self.assertIn("under_delivery", rules([starved], rides=rides(0, 2, 34), today=day(3)))

    def test_a_race_week_is_exempt_from_under_delivery(self):
        wk = week([sess(5, t="race", tss=250, is_race=True)], target=200)
        self.assertNotIn("under_delivery", rules([wk]))

    def test_history_is_not_graded_as_a_prescription(self):
        """Past weeks of a regenerated plan keep pending sessions; not ours to grade."""
        past = week([sess(0, tss=400)], target=100)
        self.assertIn("weekly_volume", rules([past]))
        self.assertNotIn("weekly_volume", rules([past], today=day(8)))


class CapsAndContent(unittest.TestCase):
    def test_the_default_day_caps_are_enforced(self):
        """DUP-10: only a per-day dict was read, so default caps were invisible."""
        g = goal(daily_max_hours={}, max_weekday_hours=1.0)
        self.assertIn("daily_duration_cap", rules([week([sess(1, dur=150)])], g))
        self.assertNotIn("daily_duration_cap", rules([week([sess(1, dur=55)])], g))

    def test_an_easy_slot_serving_hard_content(self):
        self.assertIn("easy_slot_content", rules([week([sess(1, f=HARD_FILE)])]))
        self.assertNotIn("easy_slot_content", rules([week([sess(1, f=EASY_FILE)])]))

    def test_hard_work_beyond_its_share_of_the_week(self):
        over = week([sess(0, t="vo2max", tss=70), sess(2, t="vo2max", tss=70)], target=200)
        fair = week([sess(0, t="vo2max", tss=55), sess(2, t="vo2max", tss=55),
                     sess(3, tss=45)], target=200)
        self.assertIn("hard_share", rules([over]))
        self.assertNotIn("hard_share", rules([fair]))

    def test_the_taper_may_keep_its_intensity(self):
        taper = week([sess(0, t="vo2max", tss=70), sess(2, t="vo2max", tss=70)],
                     target=200, phase="taper")
        self.assertNotIn("hard_share", rules([taper]))

    def test_an_unload_week_heavier_than_its_block(self):
        def blk(unload):
            load = [week([sess(0, tss=300)], n=i + 1) for i in range(3)]
            return load + [week([sess(0, tss=unload)], n=4, stepback=True)]
        self.assertIn("stepback_lightest", rules(blk(350)))
        self.assertNotIn("stepback_lightest", rules(blk(200)))

    def test_a_ridden_session_on_a_rest_day_is_not_a_planner_fault(self):
        wk = week([sess(6, status="done")])
        self.assertNotIn("rest_days", rules([wk]))


if __name__ == "__main__":
    unittest.main()
