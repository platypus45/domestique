"""API regression tests for fix26 §6 — daily-adapt is projection-only,
move-session / rematch endpoints, dismissed stays visible.

Covers:
- §6.1: /api/plan/daily-adapt never writes current_plan.json.
- §6.2: /api/plan/move-session sets user_moved=True + moved_from.
- §6.3: /api/plan/rematch?apply=0 previews; apply=1 writes.
- §6.8: dismissed sessions stay visible in plan JSON (status field).
- §6.11: /api/plan/rematch does not auto-dismiss missed sessions.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _frozen_date_class(today_value: date):
    """Return a `date` subclass whose .today() returns `today_value`.

    Used to pin the "today" reference in API endpoints so date-windowed
    filters (e.g. _collect_week_activities) behave deterministically
    regardless of when the test runs. Falls through to the real `date`
    for everything else (constructor, fromisoformat, comparisons).
    """
    class _FrozenDate(date):
        @classmethod
        def today(cls):  # pragma: no cover — exercised by patched endpoint
            return today_value
    return _FrozenDate


def _mk_plan_dict(monday: date) -> dict:
    """Build a minimal current_plan.json covering a single week."""
    sessions = [
        {"day": (monday + timedelta(days=0)).isoformat(), "day_name": "Mon",
         "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
         "description": "Rest", "zwo_file": "", "zwo_name": "",
         "status": "pending"},
        {"day": (monday + timedelta(days=1)).isoformat(), "day_name": "Tue",
         "session_type": "z2", "duration_min": 60, "tss_estimate": 45,
         "description": "Z2", "zwo_file": "", "zwo_name": "",
         "status": "pending"},
        {"day": (monday + timedelta(days=2)).isoformat(), "day_name": "Wed",
         "session_type": "sweetspot", "duration_min": 75, "tss_estimate": 80,
         "description": "SS", "zwo_file": "", "zwo_name": "",
         "status": "pending"},
        {"day": (monday + timedelta(days=3)).isoformat(), "day_name": "Thu",
         "session_type": "vo2max", "duration_min": 60, "tss_estimate": 75,
         "description": "VO2", "zwo_file": "", "zwo_name": "",
         "status": "pending"},
        {"day": (monday + timedelta(days=4)).isoformat(), "day_name": "Fri",
         "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
         "description": "Rest", "zwo_file": "", "zwo_name": "",
         "status": "pending"},
        {"day": (monday + timedelta(days=5)).isoformat(), "day_name": "Sat",
         "session_type": "long_z2", "duration_min": 120, "tss_estimate": 90,
         "description": "Long", "zwo_file": "", "zwo_name": "",
         "status": "pending"},
        {"day": (monday + timedelta(days=6)).isoformat(), "day_name": "Sun",
         "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
         "description": "Rest", "zwo_file": "", "zwo_name": "",
         "status": "pending"},
    ]
    return {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0, 4, 6]},
        "phases": [],
        "weeks": [{
            "week_num": 1, "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "base", "tss_target": 290, "is_stepback": False,
            "sessions": sessions,
        }],
        "generated": "2026-04-19T00:00:00",
    }


class PlanApiTestBase(unittest.TestCase):
    """Test base that redirects PLAN_DIR to a temp dir and seeds a plan."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        # Monday of current week (so /api/plan/* endpoints pick this up)
        today = date.today()
        self._monday = today - timedelta(days=today.weekday())
        self._plan = _mk_plan_dict(self._monday)
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))
        # Redirect plan dir
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        # Build client after patching PLAN_DIR (some routes compute paths eagerly)
        self.client = TestClient(app_module.app)

    def tearDown(self):
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _read_plan(self) -> dict:
        return json.loads((self._tmp / "current_plan.json").read_text())


class TestDailyAdaptEndpointIsProjectionOnly(PlanApiTestBase):
    """§6.1 — GET /api/plan/daily-adapt must NOT write current_plan.json."""

    def test_endpoint_does_not_modify_plan_json(self):
        before = (self._tmp / "current_plan.json").read_bytes()
        before_mtime = os.path.getmtime(self._tmp / "current_plan.json")

        r = self.client.get("/api/plan/daily-adapt")
        self.assertEqual(r.status_code, 200)
        data = r.json()

        # Contract: projection_only flag set
        self.assertTrue(data.get("projection_only"))
        # File bytes unchanged, mtime unchanged (no write occurred)
        after = (self._tmp / "current_plan.json").read_bytes()
        self.assertEqual(before, after)
        self.assertEqual(before_mtime, os.path.getmtime(self._tmp / "current_plan.json"))

    def test_endpoint_returns_projected_not_adapted(self):
        r = self.client.get("/api/plan/daily-adapt")
        data = r.json()
        # The legacy "adapted" action no longer fires
        self.assertNotEqual(data.get("action"), "adapted")
        # Only action values the new contract allows
        self.assertIn(data.get("action"),
                      {"projected", "no_change", "no_remaining_sessions",
                       "no_plan", "no_current_week", "error"})


class TestMoveSessionSetsUserMovedFlag(PlanApiTestBase):
    """§6.2 — POST /api/plan/move-session sets user_moved=True."""

    def test_move_sets_user_moved_and_moved_from(self):
        thu = (self._monday + timedelta(days=3)).isoformat()  # VO2max
        mon = self._monday.isoformat()                        # rest
        r = self.client.post("/api/plan/move-session",
                             json={"date": thu, "new_date": mon})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data.get("ok"))

        plan = self._read_plan()
        sessions = plan["weeks"][0]["sessions"]
        # VO2 now at Monday with user_moved=True, moved_from=Thu
        mon_s = next(s for s in sessions if s["day"] == mon)
        self.assertEqual(mon_s["session_type"], "vo2max")
        self.assertTrue(mon_s["user_moved"])
        self.assertEqual(mon_s["moved_from"], thu)
        # Thursday slot is vacated with moved_from marker
        thu_s = next(s for s in sessions if s["day"] == thu)
        self.assertEqual(thu_s["session_type"], "rest")
        self.assertTrue(thu_s["status"].startswith("moved_from:"))

    def test_reject_same_date(self):
        mon = self._monday.isoformat()
        r = self.client.post("/api/plan/move-session",
                             json={"date": mon, "new_date": mon})
        self.assertEqual(r.status_code, 400)

    def test_reject_invalid_date(self):
        r = self.client.post("/api/plan/move-session",
                             json={"date": "not-a-date", "new_date": "also-not"})
        self.assertEqual(r.status_code, 400)


class TestRematchPreviewVsApply(PlanApiTestBase):
    """§6.3 — apply=0 doesn't write, apply=1 does."""

    def test_rematch_apply_0_does_not_write(self):
        before_bytes = (self._tmp / "current_plan.json").read_bytes()
        r = self.client.post("/api/plan/rematch?apply=0")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertFalse(data.get("apply"))
        self.assertIn("matches", data)
        self.assertIn("summary", data)
        # File unchanged
        after_bytes = (self._tmp / "current_plan.json").read_bytes()
        self.assertEqual(before_bytes, after_bytes)

    def test_rematch_apply_1_writes_statuses(self):
        # Seed one actual activity matching Tuesday Z2 exactly (3/3 axes)
        tue = (self._monday + timedelta(days=1)).isoformat()
        matching_activity = {
            "date": tue, "tss": 45, "duration_min": 60,
            "intensity_factor": 0.60, "id": 99,  # low_aerobic
        }
        # v4.4.1 FIX-SERVER: pin "today" to Sat so Tue is unambiguously in
        # the past for the date-window filter in _collect_week_activities
        # (which excludes activities with date >= today+1 when today<Tue).
        # Without this, the test fails when run on Mon (today<Tue → filtered).
        fake_today = self._monday + timedelta(days=5)  # Sat of same week
        FakeDate = _frozen_date_class(fake_today)
        # Stub db.query_activities + ride_storage.list_rides so the endpoint finds our activity
        with patch("db.query_activities", return_value=[matching_activity]), \
             patch("ride_storage.list_rides", return_value=[]), \
             patch("app.date", FakeDate):
            r = self.client.post("/api/plan/rematch?apply=1")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("apply"))
        # Plan JSON now has status=done on the Tuesday session + completion_matches
        plan = self._read_plan()
        tue_s = next(s for s in plan["weeks"][0]["sessions"] if s["day"] == tue)
        self.assertEqual(tue_s["status"], "done")
        self.assertIsInstance(tue_s.get("completion_matches"), list)
        self.assertGreaterEqual(len(tue_s["completion_matches"]), 1)
        self.assertEqual(tue_s["completion_matches"][0]["activity_id"], 99)


class TestRematchClassifier3of3Axes(PlanApiTestBase):
    """§6.9 — endpoint classifier must require 3/3 axes."""

    def test_2_of_3_axes_returns_ambiguous(self):
        # Tuesday is Z2 60min 45TSS. We fabricate an activity that matches
        # TSS + duration but wrong IF-band (very hard effort).
        tue = (self._monday + timedelta(days=1)).isoformat()
        partial = {
            "date": tue, "tss": 45, "duration_min": 60,
            "intensity_factor": 1.00,  # anaerobic ≠ z2's low_aerobic
            "id": 77,
        }
        # v4.4.1 FIX-SERVER: pin "today" to Sat (see test_rematch_apply_1)
        FakeDate = _frozen_date_class(self._monday + timedelta(days=5))
        with patch("db.query_activities", return_value=[partial]), \
             patch("ride_storage.list_rides", return_value=[]), \
             patch("app.date", FakeDate):
            r = self.client.post("/api/plan/rematch?apply=0")
        self.assertEqual(r.status_code, 200)
        matches = r.json()["matches"]
        tue_match = next(m for m in matches if m["session_date"] == tue)
        self.assertEqual(tue_match["matched_axes"], 2)
        self.assertEqual(tue_match["new_status"], "ambiguous")

    def test_3_of_3_axes_returns_done(self):
        tue = (self._monday + timedelta(days=1)).isoformat()
        matching = {
            "date": tue, "tss": 45, "duration_min": 60,
            "intensity_factor": 0.60,
        }
        # v4.4.1 FIX-SERVER: pin "today" to Sat (see test_rematch_apply_1)
        FakeDate = _frozen_date_class(self._monday + timedelta(days=5))
        with patch("db.query_activities", return_value=[matching]), \
             patch("ride_storage.list_rides", return_value=[]), \
             patch("app.date", FakeDate):
            r = self.client.post("/api/plan/rematch?apply=0")
        matches = r.json()["matches"]
        tue_match = next(m for m in matches if m["session_date"] == tue)
        self.assertEqual(tue_match["matched_axes"], 3)
        self.assertEqual(tue_match["new_status"], "done")


class TestDismissedStaysVisible(PlanApiTestBase):
    """§6.8 — dismissed sessions stay visible in plan JSON."""

    def test_dismiss_session_persists_status(self):
        thu = (self._monday + timedelta(days=3)).isoformat()
        r = self.client.post("/api/plan/dismiss-session", json={"date": thu})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertTrue(data.get("dismissed"))
        plan = self._read_plan()
        thu_s = next(s for s in plan["weeks"][0]["sessions"] if s["day"] == thu)
        # Session STAYS in plan (not deleted) and has status=dismissed
        self.assertEqual(thu_s["status"], "dismissed")
        self.assertNotEqual(thu_s.get("dismissed_at", ""), "")
        # Original session_type preserved (so UI can still render it greyed)
        self.assertEqual(thu_s["session_type"], "vo2max")

    def test_undismiss_restores_pending(self):
        thu = (self._monday + timedelta(days=3)).isoformat()
        self.client.post("/api/plan/dismiss-session", json={"date": thu})
        r = self.client.post("/api/plan/dismiss-session",
                             json={"date": thu, "undo": True})
        self.assertEqual(r.status_code, 200)
        plan = self._read_plan()
        thu_s = next(s for s in plan["weeks"][0]["sessions"] if s["day"] == thu)
        self.assertEqual(thu_s["status"], "pending")
        self.assertEqual(thu_s.get("dismissed_at", ""), "")


class TestMoveSessionVisibleAfterRefresh(PlanApiTestBase):
    """fix35b — /api/weekly-plan merge uses ISO week match (was strict date equality).

    Repro: move Mon→Wed via /api/plan/move-session, then call /api/weekly-plan
    (what the dashboard's loadWeeklyCalendar() re-fetches). Assert the Wed
    session reflects user_moved=True and Mon shows status=moved_from:<wed_date>.
    Under the old strict `ws==week.start and we==week.end` guard, the merge
    could silently skip when stored-week dates drifted by a day → the UI card
    snapped back after drop.
    """

    def test_move_session_visible_after_refresh(self):
        # Seed: move Tuesday (z2) → Thursday (was vo2max) so the dst slot has
        # a session both before and after the move.
        tue = (self._monday + timedelta(days=1)).isoformat()
        thu = (self._monday + timedelta(days=3)).isoformat()

        r = self.client.post("/api/plan/move-session",
                             json={"date": tue, "new_date": thu})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ok"))

        # Now hit /api/weekly-plan (exact endpoint loadWeeklyCalendar() calls)
        r = self.client.get("/api/weekly-plan")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        sessions = data.get("sessions", [])
        by_day = {s["day"]: s for s in sessions}

        # Thu session reflects user_moved + moved_from
        self.assertIn(thu, by_day, "Thu session missing from /api/weekly-plan")
        thu_out = by_day[thu]
        self.assertTrue(thu_out.get("user_moved"),
                        f"user_moved flag not visible after refresh: {thu_out}")
        self.assertEqual(thu_out.get("moved_from"), tue)

        # Tue slot shows moved_from:<thu> status
        self.assertIn(tue, by_day, "Tue session missing from /api/weekly-plan")
        tue_out = by_day[tue]
        self.assertTrue(
            str(tue_out.get("status", "")).startswith("moved_from:"),
            f"Tue status should start with moved_from:, got {tue_out.get('status')!r}")

    def test_merge_survives_stored_end_date_drift(self):
        """If the stored week's `end` date drifts (e.g. was Sunday, regenerated
        week ends Sunday + offset), ISO week match on `start` still merges.

        The pre-fix35b strict `ws == week.start and we == week.end` guard would
        silently skip merge if `end` didn't exactly match, making user_moved
        flags invisible after refresh."""
        # Regenerated week.start is today's Monday (self._monday). Keep stored
        # start exactly matching so ISO week matches, but perturb `end` — the
        # old guard would fail on that, the ISO week fix ignores it.
        plan = self._read_plan()
        plan["weeks"][0]["end"] = (
            date.fromisoformat(plan["weeks"][0]["end"]) + timedelta(days=1)
        ).isoformat()
        tue = (self._monday + timedelta(days=1)).isoformat()
        thu = (self._monday + timedelta(days=3)).isoformat()
        for s in plan["weeks"][0]["sessions"]:
            if s["day"] == thu:
                s["user_moved"] = True
                s["moved_from"] = tue
                s["session_type"] = "vo2max"
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))

        r = self.client.get("/api/weekly-plan")
        self.assertEqual(r.status_code, 200)
        sessions = r.json().get("sessions", [])
        by_day = {s["day"]: s for s in sessions}
        self.assertIn(thu, by_day)
        # Merge succeeded despite `end` drift → user_moved surfaces
        self.assertTrue(by_day[thu].get("user_moved"),
                        "ISO week match failed: merge skipped under end-date drift")
        self.assertEqual(by_day[thu].get("moved_from"), tue)


class TestMoveSessionISOWeekBoundary(unittest.TestCase):
    """v4.1.1 FIX-PICKER-MOVE Bug E — move guard uses ISO Mon-Sun.

    Repro: the stored plan may use legacy Fri-Thu week boundaries. A move
    from Tue → Fri lives entirely within ISO week N (Mon-Sun) but crosses
    a stored Fri-Thu boundary — the old ``iterate-stored-weeks`` guard
    rejected it as "move must stay within a single week". The new guard
    validates on ISO week and threads each date into whichever stored
    week actually contains it.
    """

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        # Two adjacent Fri-Thu weeks (legacy convention).
        # Use a Monday in the past so we're inside a single ISO week that
        # also spans the Fri-Thu boundary.
        # Pick a concrete reference anchor: Mon 2026-04-20.
        self._w1_start = date(2026, 4, 17)  # Fri
        self._w1_end = date(2026, 4, 23)    # Thu
        self._w2_start = date(2026, 4, 24)  # Fri
        self._w2_end = date(2026, 4, 30)    # Thu

        def _mkses(d: date, t: str):
            return {
                "day": d.isoformat(), "day_name": d.strftime("%a"),
                "session_type": t, "duration_min": 60 if t != "rest" else 0,
                "tss_estimate": 60 if t != "rest" else 0,
                "description": t, "zwo_file": "", "zwo_name": "",
                "status": "pending",
            }

        # w1 = Fri 04-17 .. Thu 04-23
        w1_sessions = [_mkses(self._w1_start + timedelta(days=i),
                              ["tempo", "long_z2", "long_z2", "rest",
                               "z2", "rest", "z2"][i]) for i in range(7)]
        # w2 = Fri 04-24 .. Thu 04-30
        w2_sessions = [_mkses(self._w2_start + timedelta(days=i),
                              ["tempo", "long_z2", "long_z2", "rest",
                               "tempo", "rest", "tempo"][i]) for i in range(7)]
        plan = {
            "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
            "phases": [],
            "weeks": [
                {"week_num": 1,
                 "start": self._w1_start.isoformat(),
                 "end": self._w1_end.isoformat(),
                 "phase": "base", "tss_target": 300, "is_stepback": False,
                 "sessions": w1_sessions},
                {"week_num": 2,
                 "start": self._w2_start.isoformat(),
                 "end": self._w2_end.isoformat(),
                 "phase": "base", "tss_target": 300, "is_stepback": False,
                 "sessions": w2_sessions},
            ],
            "generated": "2026-04-19T00:00:00",
        }
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        self.client = TestClient(app_module.app)

    def tearDown(self):
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _read_plan(self) -> dict:
        return json.loads((self._tmp / "current_plan.json").read_text())

    def test_move_within_same_stored_week_allowed(self):
        """Case (a): Tue 04-28 → Thu 04-30 — same ISO week 18, same
        stored wk2. Should succeed."""
        r = self.client.post("/api/plan/move-session",
                             json={"date": "2026-04-28",
                                   "new_date": "2026-04-30"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ok"))
        plan = self._read_plan()
        wk2 = plan["weeks"][1]
        thu = next(s for s in wk2["sessions"] if s["day"] == "2026-04-30")
        self.assertTrue(thu["user_moved"])
        self.assertEqual(thu["moved_from"], "2026-04-28")

    def test_move_cross_stored_boundary_same_iso_week_allowed(self):
        """Case (b) — THE BUG: Tue 04-28 → Fri 05-01. Same ISO week 18
        (Mon 04-27..Sun 05-03). Crosses stored Fri-Thu boundary
        (wk2 04-24..04-30 → wk3 05-01..05-07). The old code rejected
        this; v4.1.1 allows it and writes to both stored weeks."""
        # Add wk3 first since we need a destination week containing 05-01.
        plan = self._read_plan()
        wk3 = {
            "week_num": 3, "start": "2026-05-01", "end": "2026-05-07",
            "phase": "base", "tss_target": 300, "is_stepback": False,
            "sessions": [{
                "day": (date(2026, 5, 1) + timedelta(days=i)).isoformat(),
                "day_name": (date(2026, 5, 1) + timedelta(days=i)).strftime("%a"),
                "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
                "description": "Rest", "zwo_file": "", "zwo_name": "",
                "status": "pending",
            } for i in range(7)],
        }
        plan["weeks"].append(wk3)
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))

        r = self.client.post("/api/plan/move-session",
                             json={"date": "2026-04-28",
                                   "new_date": "2026-05-01"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json().get("ok"))

        plan = self._read_plan()
        # Source week (wk2) now has 04-28 as a rest stub with
        # moved_from:2026-05-01 status.
        wk2 = next(w for w in plan["weeks"] if w["start"] == "2026-04-24")
        src = next(s for s in wk2["sessions"] if s["day"] == "2026-04-28")
        self.assertEqual(src["session_type"], "rest")
        self.assertTrue(str(src["status"]).startswith("moved_from:"))
        # Destination week (wk3) now has the moved session at 05-01.
        wk3 = next(w for w in plan["weeks"] if w["start"] == "2026-05-01")
        dst = next(s for s in wk3["sessions"] if s["day"] == "2026-05-01")
        self.assertTrue(dst["user_moved"])
        self.assertEqual(dst["moved_from"], "2026-04-28")
        self.assertEqual(dst["session_type"], "tempo")

    def test_move_cross_iso_week_rejected(self):
        """Case (c): Sun 04-26 → Mon 04-27 — different ISO weeks
        (17 → 18). Should be rejected regardless of stored boundary."""
        r = self.client.post("/api/plan/move-session",
                             json={"date": "2026-04-26",
                                   "new_date": "2026-04-27"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("ISO", r.json().get("error", ""))

    def test_move_next_week_rejected(self):
        """Case (d): Tue 04-28 → Tue 05-05 — next ISO week. Rejected."""
        r = self.client.post("/api/plan/move-session",
                             json={"date": "2026-04-28",
                                   "new_date": "2026-05-05"})
        self.assertEqual(r.status_code, 400)


class TestPickerBugDRegression(unittest.TestCase):
    """v4.1.1 FIX-PICKER-MOVE Bug D — picker returns non-null readiness
    + non-empty workouts list.

    Two root causes fixed:
    1. ``api_workouts`` has ``tags: str = Query(None)`` — when called as
       a plain function the default is a Query() object (truthy), which
       crashed on ``.split(",")`` inside. Picker now passes tags=None.
    2. ``min_score=7`` floor: only 3 of 3000 library workouts score ≥7;
       picker collapsed to empty. Now uses min_score=3 as primary floor
       with widening fallback.
    3. Baseline 70/100 when compute_readiness returns None (fresh
       install, no HRV data).
    """

    def setUp(self):
        self.client = TestClient(app_module.app)

    def test_picker_returns_workouts_and_readiness(self):
        r = self.client.get("/api/picker", params={"subjective": 7, "duration": 75})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # Readiness is a number (may be real if HRV data exists, or 70
        # baseline if not).
        self.assertIsNotNone(data.get("readiness"),
                             "picker readiness must not be None")
        self.assertIsInstance(data.get("readiness"), (int, float))
        # At least 3 workouts (the task contract).
        self.assertGreaterEqual(len(data.get("workouts", [])), 3,
                                f"picker returned <3 workouts: {data}")

    def test_picker_baseline_when_readiness_none(self):
        """Force compute_readiness to return None (no components) and
        confirm the picker still returns 70/100 + workouts."""
        def _empty(*a, **kw):
            return {"score": None, "status": "INSUFFICIENT_DATA",
                    "advice": "Not enough components to compute readiness (need ≥3).",
                    "components": {}, "missing": ["hrv", "tsb", "subjective",
                                                   "sleep", "rhr"]}
        with patch("app.compute_readiness", side_effect=_empty):
            r = self.client.get("/api/picker",
                                params={"subjective": 7, "duration": 75})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data.get("readiness"), 70)
        self.assertTrue(data.get("baseline_used"))
        self.assertGreaterEqual(len(data.get("workouts", [])), 3)


if __name__ == "__main__":
    unittest.main()
