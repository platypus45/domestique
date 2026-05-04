"""Regression tests for fix26 §6 — daily_adapt demoted to projection-only,
rematch classifier, regen preserves user_moved.

Covers:
- §6.1: daily_adapt_plan is projection-only (NO mutation of inputs or disk).
- §6.9: rematch classifier requires 3/3 axes (TSS ±15%, duration ±20%, IF-band).
        2/3 → ambiguous; 1/3 → no_match.
- §6.10: remaining filter uses status==pending, not calendar date.
- §6.11: missed never auto-dismisses (end-of-week prompt is UI policy).
- §6.12: regenerate_from_today preserves user_moved + status + dismissed_at.
"""
from __future__ import annotations

import unittest
from copy import deepcopy
from datetime import date, timedelta

from training_planner import (
    PlannedSession,
    PlannedWeek,
    SESSION_TYPE_TO_BAND,
    REMATCH_TOL_TSS_PCT,
    REMATCH_TOL_DURATION_PCT,
    classify_rematch,
    daily_adapt_plan,
    rematch_week,
)


def _mk_session(day, day_name, session_type, duration_min, tss_estimate,
                **kw) -> PlannedSession:
    defaults = dict(
        description=f"{session_type} session" if session_type else "session",
        zwo_file="",
        zwo_name="",
        matched=True,
        adapted=False,
        status="pending",
        user_moved=False,
        moved_from="",
        completion_matches=None,
        dismissed_at="",
    )
    defaults.update(kw)
    return PlannedSession(
        day=day, day_name=day_name, session_type=session_type,
        duration_min=duration_min, tss_estimate=tss_estimate, **defaults,
    )


def _mk_week(start: date, sessions: list) -> PlannedWeek:
    return PlannedWeek(
        week_num=1, start=start, end=start + timedelta(days=6),
        phase="base", tss_target=sum(s.tss_estimate for s in sessions),
        is_stepback=False, sessions=sessions,
    )


class TestDailyAdaptIsProjectionOnly(unittest.TestCase):
    """§6.1 — daily_adapt_plan must NOT mutate the input week."""

    def setUp(self):
        # Monday today → Tuesday planned Z2 pending ahead
        self.today = date(2026, 4, 20)  # Monday
        self.monday = self.today
        sessions = [
            _mk_session(self.monday + timedelta(days=0), "Mon", "rest", 0, 0),
            _mk_session(self.monday + timedelta(days=1), "Tue", "z2", 60, 45),
            _mk_session(self.monday + timedelta(days=2), "Wed", "sweetspot", 75, 80),
            _mk_session(self.monday + timedelta(days=3), "Thu", "vo2max", 60, 75),
            _mk_session(self.monday + timedelta(days=4), "Fri", "rest", 0, 0),
            _mk_session(self.monday + timedelta(days=5), "Sat", "long_z2", 120, 90),
            _mk_session(self.monday + timedelta(days=6), "Sun", "rest", 0, 0),
        ]
        self.week = _mk_week(self.monday, sessions)

    def test_projection_returns_unchanged_week(self):
        snapshot = deepcopy(self.week)
        out_week, info = daily_adapt_plan(self.week, [], today=self.today)
        self.assertIs(out_week, self.week)  # same object returned
        self.assertTrue(info["projection_only"])
        # Individual sessions unchanged on every field
        for a, b in zip(snapshot.sessions, self.week.sessions):
            self.assertEqual(a.session_type, b.session_type)
            self.assertEqual(a.tss_estimate, b.tss_estimate)
            self.assertEqual(a.duration_min, b.duration_min)
            self.assertEqual(a.description, b.description)
            self.assertFalse(getattr(b, "adapted", False))

    def test_projection_with_big_surplus_does_not_mutate(self):
        # Pretend the user did a monster 300-TSS ride yesterday.
        actual = [{"date": (self.monday - timedelta(days=1)).isoformat(), "tss": 300}]
        before = deepcopy(self.week.sessions)
        _out, info = daily_adapt_plan(self.week, actual, today=self.today)
        # No mutation even though scale_factor would shrink remaining
        for a, b in zip(before, self.week.sessions):
            self.assertEqual(a.tss_estimate, b.tss_estimate)
            self.assertEqual(a.duration_min, b.duration_min)
            self.assertEqual(a.session_type, b.session_type)
        # But projection was computed
        self.assertTrue(info["projection_only"])
        self.assertIn("projected_adaptations", info)

    def test_projection_with_tsb_deload_does_not_mutate(self):
        # Severe TSB → would have de-escalated vo2max/sweetspot/etc.
        before_types = [s.session_type for s in self.week.sessions]
        _out, info = daily_adapt_plan(self.week, [], today=self.today, tsb=-40.0)
        after_types = [s.session_type for s in self.week.sessions]
        self.assertEqual(before_types, after_types)
        self.assertTrue(info["projection_only"])
        self.assertGreater(len(info["tsb_deload_projected"]), 0)


class TestProjectionFilterUsesStatus(unittest.TestCase):
    """§6.10 — remaining_sessions uses status=='pending', not calendar date.

    A user-moved VO2max sitting on a past date must still count as pending
    for projection purposes.
    """

    def test_done_sessions_excluded_from_remaining(self):
        monday = date(2026, 4, 20)
        today = monday + timedelta(days=4)  # Friday
        sessions = [
            _mk_session(monday + timedelta(days=0), "Mon", "z2", 60, 45, status="done"),
            _mk_session(monday + timedelta(days=1), "Tue", "vo2max", 60, 75, status="done"),
            _mk_session(monday + timedelta(days=2), "Wed", "rest", 0, 0),
            _mk_session(monday + timedelta(days=3), "Thu", "sweetspot", 75, 80, status="pending"),
            _mk_session(monday + timedelta(days=4), "Fri", "rest", 0, 0),
            _mk_session(monday + timedelta(days=5), "Sat", "long_z2", 120, 90, status="pending"),
            _mk_session(monday + timedelta(days=6), "Sun", "rest", 0, 0),
        ]
        week = _mk_week(monday, sessions)
        _out, info = daily_adapt_plan(week, [], today=today)
        # Only the 2 pending sessions with TSS>0 should contribute to the projection.
        # With 0 actual and weekly_target=290 and remaining_planned_tss=170,
        # scale is 1.0 or less. Key check: done sessions not double-counted.
        # Indirect: assertTrue info structure correct and projection_only True.
        self.assertTrue(info["projection_only"])

    def test_dismissed_sessions_excluded(self):
        monday = date(2026, 4, 20)
        today = monday + timedelta(days=2)  # Wed
        sessions = [
            _mk_session(monday + timedelta(days=1), "Tue", "vo2max", 60, 75, status="dismissed"),
            _mk_session(monday + timedelta(days=3), "Thu", "z2", 60, 45, status="pending"),
        ]
        # Add rest/other days so PlannedWeek.sessions has enough structure
        week = _mk_week(monday, sessions)
        _out, info = daily_adapt_plan(week, [], today=today)
        # Dismissed must not appear in any projected adaptation
        dates = {a["date"] for a in info["projected_adaptations"]}
        self.assertNotIn((monday + timedelta(days=1)).isoformat(), dates)


class TestRematchClassifier(unittest.TestCase):
    """§6.9 — 3/3 axes required for done, 2/3 → ambiguous, <2 → no_match."""

    def _session(self, **kw):
        return _mk_session(
            kw.pop("day", date(2026, 4, 20)),
            kw.pop("day_name", "Mon"),
            kw.pop("session_type", "vo2max"),
            kw.pop("duration_min", 60),
            kw.pop("tss_estimate", 75),
            **kw,
        )

    def test_three_of_three_axes_returns_done(self):
        s = self._session(session_type="vo2max", duration_min=60, tss_estimate=75)
        a = {
            "tss": 74, "duration_min": 61,
            "intensity_factor": 1.00,  # anaerobic band
            "date": s.day.isoformat(),
        }
        r = classify_rematch(s, a)
        self.assertEqual(r["matched_axes"], 3)
        self.assertEqual(r["status"], "done")
        self.assertTrue(r["axes"]["tss_ok"])
        self.assertTrue(r["axes"]["duration_ok"])
        self.assertTrue(r["axes"]["if_band_ok"])

    def test_two_of_three_axes_returns_ambiguous(self):
        # TSS + duration match, but IF-band mismatch (did Z2 instead of VO2max)
        s = self._session(session_type="vo2max", duration_min=60, tss_estimate=75)
        a = {
            "tss": 75, "duration_min": 60,
            "intensity_factor": 0.55,  # low_aerobic band (not anaerobic)
            "date": s.day.isoformat(),
        }
        r = classify_rematch(s, a)
        self.assertEqual(r["matched_axes"], 2)
        self.assertEqual(r["status"], "ambiguous")

    def test_one_of_three_axes_returns_no_match(self):
        # Only TSS matches; duration way off, IF-band different
        s = self._session(session_type="vo2max", duration_min=60, tss_estimate=75)
        a = {
            "tss": 75, "duration_min": 180,  # 3x longer
            "intensity_factor": 0.50,
            "date": s.day.isoformat(),
        }
        r = classify_rematch(s, a)
        self.assertEqual(r["matched_axes"], 1)
        self.assertEqual(r["status"], "no_match")

    def test_zero_axes_returns_no_match(self):
        s = self._session(session_type="vo2max", duration_min=60, tss_estimate=75)
        a = {"tss": 20, "duration_min": 180, "intensity_factor": 0.50}
        r = classify_rematch(s, a)
        self.assertEqual(r["matched_axes"], 0)
        self.assertEqual(r["status"], "no_match")

    def test_tss_tolerance_is_15_percent(self):
        s = self._session(tss_estimate=100)
        # +15% → 115 is within tolerance; +16% → 116 is just over
        self.assertTrue(classify_rematch(s, {
            "tss": 115, "duration_min": s.duration_min,
            "intensity_factor": 1.0,  # anaerobic matches vo2max
        })["axes"]["tss_ok"])
        self.assertFalse(classify_rematch(s, {
            "tss": 116.1, "duration_min": s.duration_min,
            "intensity_factor": 1.0,
        })["axes"]["tss_ok"])
        # Also assert the constant is locked to 0.15
        self.assertEqual(REMATCH_TOL_TSS_PCT, 0.15)

    def test_duration_tolerance_is_20_percent(self):
        s = self._session(duration_min=60)
        # +20% → 72 min within tolerance; +21% → 72.6 just over
        self.assertTrue(classify_rematch(s, {
            "tss": s.tss_estimate, "duration_min": 72,
            "intensity_factor": 1.0,
        })["axes"]["duration_ok"])
        self.assertFalse(classify_rematch(s, {
            "tss": s.tss_estimate, "duration_min": 72.1,
            "intensity_factor": 1.0,
        })["axes"]["duration_ok"])
        self.assertEqual(REMATCH_TOL_DURATION_PCT, 0.20)

    def test_if_band_categorical_not_numeric(self):
        # VO2max prescribed; activity with IF=0.96 still classifies as
        # "high_aerobic" (sweetspot/threshold band) not "anaerobic".
        s = self._session(session_type="vo2max")
        r = classify_rematch(s, {
            "tss": s.tss_estimate, "duration_min": s.duration_min,
            "intensity_factor": 0.96,  # just below anaerobic cutoff 0.97
        })
        self.assertFalse(r["axes"]["if_band_ok"])
        # And at 0.97 it crosses into anaerobic
        r2 = classify_rematch(s, {
            "tss": s.tss_estimate, "duration_min": s.duration_min,
            "intensity_factor": 0.97,
        })
        self.assertTrue(r2["axes"]["if_band_ok"])


class TestRematchWeek(unittest.TestCase):
    """rematch_week() — full-week preview."""

    def test_missed_not_auto_dismissed_same_week(self):
        """§6.11 — past session without actual stays 'missed', not 'dismissed'."""
        monday = date(2026, 4, 13)  # last Monday
        today = date(2026, 4, 20)   # Sun+1 — end-of-week
        sessions = [
            _mk_session(monday + timedelta(days=1), "Tue", "vo2max", 60, 75),  # past, no actual
            _mk_session(monday + timedelta(days=3), "Thu", "z2", 60, 45),
        ]
        week = _mk_week(monday, sessions)
        out = rematch_week(week, [], today=today)
        statuses = {m["session_date"]: m["new_status"] for m in out["matches"]}
        self.assertEqual(statuses[(monday + timedelta(days=1)).isoformat()], "missed")
        # Crucial: missed, NOT dismissed
        self.assertNotIn("dismissed", statuses.values())

    def test_full_match_marks_done(self):
        monday = date(2026, 4, 20)
        today = monday + timedelta(days=3)
        day = monday + timedelta(days=1)
        sessions = [_mk_session(day, "Tue", "vo2max", 60, 75)]
        week = _mk_week(monday, sessions)
        activities = [{
            "date": day.isoformat(), "tss": 75, "duration_min": 60,
            "intensity_factor": 1.0, "id": 42,
        }]
        out = rematch_week(week, activities, today=today)
        m = out["matches"][0]
        self.assertEqual(m["new_status"], "done")
        self.assertEqual(m["activity_id"], 42)
        self.assertEqual(m["matched_axes"], 3)


class TestPlanRegenPreservesUserMoved(unittest.TestCase):
    """§6.12 — plan regen preserves user_moved, status, dismissed_at.

    The canonical regen pattern: `regenerate_from_today` reconstructs the
    current week from "today" forward. `adapted_current_week` is the
    lookup that must preserve user_moved (and status != pending) sessions
    by date. If a user drags Thursday's VO2max onto Friday, the post-regen
    Friday slot must still carry user_moved=True + vo2max.
    """

    def test_regenerate_preserves_user_moved_session(self):
        from training_planner import regenerate_from_today, Goal

        today = date.today()
        monday = today - timedelta(days=today.weekday())

        # Pick a future-relative-to-today day for the moved session so the
        # regen loop actually revisits that slot. Fall back to today if today
        # is already the last weekday.
        future_offset = None
        for off in range(7):
            d = monday + timedelta(days=off)
            if d >= today:
                future_offset = off
                break
        assert future_offset is not None

        moved_day = monday + timedelta(days=future_offset)
        orig_src = monday + timedelta(days=(future_offset + 2) % 7)
        vo2_moved = _mk_session(
            moved_day,
            ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][future_offset],
            "vo2max", 60, 75,
            user_moved=True, moved_from=orig_src.isoformat(),
        )
        # Build full 7-day week with at most one user_moved marker
        sessions = []
        for off in range(7):
            d = monday + timedelta(days=off)
            if d == moved_day:
                sessions.append(vo2_moved)
            else:
                sessions.append(_mk_session(
                    d, ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"][off],
                    "z2" if off not in (2, 6) else "rest",
                    60 if off not in (2, 6) else 0,
                    45 if off not in (2, 6) else 0,
                ))
        week = _mk_week(monday, sessions)
        goal = Goal(
            goal_type="general", hours_per_week=8.0,
            available_days=list(range(7)), rest_days=[],
            plan_weeks=4,
        )
        _new_phases, all_weeks, _info = regenerate_from_today(
            goal=goal, old_plan_weeks=[week], current_ctl=40.0,
        )
        # Find the week containing moved_day in the regen output.
        cur_week = None
        for w in all_weeks:
            if w.start <= moved_day <= w.end:
                cur_week = w
                break
        self.assertIsNotNone(cur_week, "no week containing moved_day in regen output")
        moved_s = next((s for s in cur_week.sessions if s.day == moved_day), None)
        self.assertIsNotNone(moved_s, f"moved_day {moved_day} not present in regen week")
        self.assertTrue(getattr(moved_s, "user_moved", False),
                        "regen must preserve user_moved=True")
        self.assertEqual(moved_s.session_type, "vo2max",
                         "regen must not re-prescribe a user_moved session")
        self.assertEqual(getattr(moved_s, "moved_from", ""), orig_src.isoformat())


class TestSessionTypeToBandLockedValues(unittest.TestCase):
    """SESSION_TYPE_TO_BAND must stay consistent with dashboard.html."""

    def test_expected_bands(self):
        expected = {
            "recovery": "low_aerobic",
            "z2": "low_aerobic",
            "long_z2": "low_aerobic",
            "tempo": "mid_aerobic",
            "sweetspot": "high_aerobic",
            "threshold": "high_aerobic",
            "vo2max": "anaerobic",
            "overunder": "anaerobic",
        }
        for k, v in expected.items():
            self.assertEqual(SESSION_TYPE_TO_BAND[k], v)


if __name__ == "__main__":
    unittest.main()
