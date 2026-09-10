"""Planned weeks start on Mondays.

Every rollup in the app aggregates Monday-Sunday -- the week tile, the
"N of M planned sessions done" counter, the weekly TSS budget, the ramp-rate
check. The planner used to lay weeks from whatever day it ran on: a Thursday
regenerate produced Thursday-Wednesday weeks, and an event on a Saturday
produced a Saturday-anchored taper. tests/probe_week_anchors.py measured 87 of
91 emitted week starts landing on something other than a Monday.

The plan still STARTS the day it is asked for. Making it open on the following
Monday instead would have been a two-line change, but it leaves a rider who
generates on a Thursday with nothing to ride until Monday -- so the first week
is short (today..Sunday) and every week after it is a full Monday-Sunday one.
That is the invariant these tests pin.

The tests go through the public builders rather than the helpers: the property
that matters is what a rider's plan looks like, not that a helper returns a
Monday.
"""
import os
import pathlib
import sys
import unittest
from datetime import date, timedelta

os.environ.setdefault("PYTHONHASHSEED", "0")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import training_planner as tp  # noqa: E402


def _goal(target_date, goal_type="event", rest=(5, 6), hours=2.0):
    avail = [d for d in range(7) if d not in rest]
    return tp.Goal(goal_type=goal_type, rest_days=list(rest), available_days=avail,
                   daily_max_hours={d: hours for d in avail},
                   target_date=target_date, event_name="anchor test")


def _on_weekday(base: date, wd: int) -> date:
    return base + timedelta(days=(wd - base.weekday()) % 7)


class WeekAnchorHelpers(unittest.TestCase):
    def test_monday_on_or_after_never_moves_backward(self):
        for i in range(14):
            d = date(2026, 9, 7) + timedelta(days=i)
            got = tp._monday_on_or_after(d)
            self.assertEqual(got.weekday(), 0)
            self.assertGreaterEqual(got, d)
            self.assertLess((got - d).days, 7)

    def test_monday_on_or_before_never_moves_forward(self):
        for i in range(14):
            d = date(2026, 9, 7) + timedelta(days=i)
            got = tp._monday_on_or_before(d)
            self.assertEqual(got.weekday(), 0)
            self.assertLessEqual(got, d)

    def test_a_monday_is_its_own_anchor_in_both_directions(self):
        mon = date(2026, 9, 14)
        self.assertEqual(tp._monday_on_or_after(mon), mon)
        self.assertEqual(tp._monday_on_or_before(mon), mon)

    def test_taper_anchor_keeps_the_span_inside_mujikas_band(self):
        """Rounding to the nearest Monday puts an event on a Monday at a
        15-day taper, over Mujika's 14-day ceiling -- this test caught that.
        Only one Monday can sit in the 8-14 day window, so there is no
        rounding decision, and every weekday must land inside the band."""
        for i in range(7):
            target = _on_weekday(date(2026, 11, 2), i)
            start = tp._taper_anchor(target)
            span = (target - start).days + 1
            self.assertEqual(start.weekday(), 0)
            self.assertTrue(8 <= span <= 14,
                            f"event on weekday {i}: taper span {span}d is outside 8-14")


class GeneratedPlansStartOnMondays(unittest.TestCase):
    def test_every_week_after_the_first_starts_on_a_monday(self):
        for wd in range(7):
            target = _on_weekday(date.today() + timedelta(weeks=10), wd)
            _, weeks = tp.generate_plan(_goal(target), seed_salt=1)
            offenders = [w.start.isoformat() for w in weeks[1:] if w.start.weekday() != 0]
            self.assertEqual(offenders, [], f"event on weekday {wd}: {offenders}")

    def test_the_opening_week_ends_on_a_sunday(self):
        """What makes the short first week harmless: it still closes on the
        Sunday every rollup closes on, so the week after it opens on Monday
        rather than inheriting today's weekday."""
        for wd in range(7):
            target = _on_weekday(date.today() + timedelta(weeks=10), wd)
            _, weeks = tp.generate_plan(_goal(target), seed_salt=1)
            self.assertEqual(weeks[0].end.weekday(), 6,
                             f"event on weekday {wd}: opening week ends {weeks[0].end}")

    def test_every_phase_after_the_first_starts_on_a_monday(self):
        for wd in range(7):
            target = _on_weekday(date.today() + timedelta(weeks=10), wd)
            phases, _ = tp.generate_plan(_goal(target), seed_salt=1)
            offenders = [f"{p.name}@{p.start}" for p in phases[1:] if p.start.weekday() != 0]
            self.assertEqual(offenders, [], f"event on weekday {wd}: {offenders}")

    def test_a_plan_starts_today_and_not_next_monday(self):
        """The whole reason the opening week is short rather than skipped.
        A rider generating on a Thursday must get Thursday onward, not four
        blank days followed by a tidy Monday."""
        for wd in range(7):
            target = _on_weekday(date.today() + timedelta(weeks=10), wd)
            phases, weeks = tp.generate_plan(_goal(target), seed_salt=1)
            self.assertEqual(phases[0].start, date.today(), f"weekday {wd}")
            self.assertEqual(weeks[0].start, date.today(), f"weekday {wd}")

    def test_regenerating_produces_monday_weeks_whatever_day_it_runs(self):
        """The reported case: a Wednesday regenerate used to produce
        Wednesday-Tuesday weeks while every rollup aggregated Monday-Sunday."""
        target = _on_weekday(date.today() + timedelta(weeks=12), 5)  # Saturday event
        goal = _goal(target)
        _, base = tp.generate_plan(goal, seed_salt=1)
        _, weeks, _ = tp.regenerate_from_today(goal, base, current_ctl=45.0, seed_salt=1)
        offenders = [w.start.isoformat() for w in weeks[1:] if w.start.weekday() != 0]
        self.assertEqual(offenders, [], str(offenders))

    def test_weeks_do_not_overlap_and_leave_no_gap(self):
        """Monday anchoring is only worth having if the seams stay clean: two
        rows sharing a start, or a day owned by nobody, would break the same
        rollups this is for."""
        target = _on_weekday(date.today() + timedelta(weeks=10), 5)
        _, weeks = tp.generate_plan(_goal(target), seed_salt=1)
        starts = [w.start for w in weeks]
        self.assertEqual(len(starts), len(set(starts)), "duplicate week starts")
        for a, b in zip(weeks, weeks[1:]):
            self.assertEqual(b.start, a.end + timedelta(days=1),
                             f"{a.start}..{a.end} then {b.start}: gap or overlap")

    def test_the_taper_still_ends_on_race_day(self):
        """Anchoring moved the taper's start, never its end: race day belongs
        to the plan (FC1-CLIP D3)."""
        for wd in range(7):
            target = _on_weekday(date.today() + timedelta(weeks=10), wd)
            phases, _ = tp.generate_plan(_goal(target), seed_salt=1)
            tapers = [p for p in phases if p.name == "taper"]
            if tapers:
                self.assertEqual(tapers[-1].end, target, f"weekday {wd}")


if __name__ == "__main__":
    unittest.main()
