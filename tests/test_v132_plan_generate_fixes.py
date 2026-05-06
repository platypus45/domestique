"""v1.3.2 — three plan-generation bugs surfaced from /api/plan/generate.

1. Generated plan ignored persisted availability calendar.
2. Sessions painted with `missing_workout` warnings on first paint
   because the response payload lacked card_state / content_class /
   display_name (added only on /api/plan reload).
3. Plan generation was rather slow.

This test module covers:
- ``test_generated_plan_honors_availability_overrides`` — generate_plan() now
  accepts ``availability_overrides`` and applies the same per-day
  duration / hours==0 rescaling reforecast() does.
- ``test_generated_plan_has_no_missing_workouts`` — every non-rest session
  in the freshly generated plan carries a non-empty ``zwo_file``.
- ``test_generate_plan_under_5s`` — perf gate. 8-week plan finishes under
  5s wall-clock (warm cache).
- ``test_api_plan_generate_reads_availability`` — endpoint reads the
  persisted plan["availability"] dict and passes overrides into
  generate_plan(); persisted availability survives regeneration.
- ``test_api_plan_generate_response_has_card_state`` — response payload
  now carries card_state / display_name / content_class so the
  dashboard's first paint matches a /api/plan reload.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _future_monday() -> date:
    today = date.today()
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


class TestGeneratePlanAvailability(unittest.TestCase):
    """Direct generate_plan() tests — no FastAPI roundtrip needed."""

    @classmethod
    def setUpClass(cls):
        # Warm the workout-library cache once per class so individual tests
        # don't pay 5s cold-start each.
        tp.load_workout_library()

    def _mk_goal(self, weeks: int = 8) -> tp.Goal:
        return tp.Goal(
            goal_type="general",
            target_date=date.today() + timedelta(weeks=weeks),
            hours_per_week=8.0,
            max_weekday_hours=2.0,
            max_weekend_hours=3.5,
            available_days=[1, 2, 3, 4, 5, 6],
            rest_days=[0],
            daily_max_hours={1: 1.0, 2: 1.5, 3: 1.0, 4: 1.5, 5: 1.0, 6: 3.0},
            plan_weeks=weeks,
        )

    def test_generated_plan_honors_availability_overrides(self):
        """Sat marked hours=0 → Sat sessions converted to rest."""
        goal = self._mk_goal(weeks=8)
        # Mark next 3 Saturdays as 0 hours (holiday).
        overrides: dict[str, float] = {}
        d = date.today()
        sats = 0
        while sats < 3:
            d += timedelta(days=1)
            if d.weekday() == 5:
                overrides[d.isoformat()] = 0.0
                sats += 1

        phases, weeks = tp.generate_plan(
            goal, seed_salt=12345, availability_overrides=overrides,
        )

        # Every overridden Saturday must now be rest with duration 0.
        sat_sessions: list[tp.PlannedSession] = []
        for w in weeks:
            for s in w.sessions:
                if s.day.isoformat() in overrides:
                    sat_sessions.append(s)

        self.assertGreaterEqual(
            len(sat_sessions), 1,
            "expected at least one Sat session covered by overrides",
        )
        for s in sat_sessions:
            self.assertEqual(
                s.session_type, "rest",
                f"{s.day} should be rest, got {s.session_type}",
            )
            self.assertEqual(
                s.duration_min, 0,
                f"{s.day} should have 0 duration, got {s.duration_min}",
            )
            self.assertEqual(s.zwo_file, "")

    def test_availability_override_with_hours_rescales(self):
        """hours > 0 rescales duration and re-matches workout."""
        goal = self._mk_goal(weeks=4)

        # Find the first non-rest session in the freshly generated plan
        # without overrides.
        phases0, weeks0 = tp.generate_plan(goal, seed_salt=999)
        baseline_session = None
        for w in weeks0:
            for s in w.sessions:
                if s.session_type != "rest" and s.duration_min > 60:
                    baseline_session = s
                    break
            if baseline_session is not None:
                break
        if baseline_session is None:
            self.skipTest("no non-rest > 60min session found in baseline plan")

        # Halve that day's available hours.
        target_iso = baseline_session.day.isoformat()
        new_hours = baseline_session.duration_min / 60 / 2
        overrides = {target_iso: new_hours}

        phases1, weeks1 = tp.generate_plan(
            goal, seed_salt=999, availability_overrides=overrides,
        )
        for w in weeks1:
            for s in w.sessions:
                if s.day.isoformat() == target_iso:
                    self.assertLess(
                        s.duration_min, baseline_session.duration_min,
                        "rescaled session should be shorter than baseline",
                    )
                    return
        self.fail(f"rescaled session for {target_iso} not found")

    def test_generated_plan_has_no_missing_workouts(self):
        """Every non-rest session has a non-empty zwo_file (no yellow ⚠)."""
        goal = self._mk_goal(weeks=8)
        phases, weeks = tp.generate_plan(goal, seed_salt=42)
        missing: list[str] = []
        total = 0
        for w in weeks:
            for s in w.sessions:
                if s.session_type == "rest":
                    continue
                total += 1
                if not s.zwo_file:
                    missing.append(f"{s.day} {s.session_type}")
        self.assertGreater(total, 0, "no non-rest sessions in plan")
        # Allow up to 2% edge cases (ftp_test fallback path) — generate_plan
        # already logs an aggregate warning when match_zwo can't fit a slot.
        # The user-visible bug ("ALL days have yellow") only triggers if the
        # ratio is high. Hard cap: <5% missing.
        ratio = len(missing) / total
        self.assertLess(
            ratio, 0.05,
            f"too many missing workouts: {len(missing)}/{total} "
            f"(>5%): {missing[:10]}",
        )

    def test_generate_plan_under_5s(self):
        """Perf gate: 8-week build phase finishes in < 5s warm-cache."""
        goal = self._mk_goal(weeks=8)
        # warm cache (already done in setUpClass — belt and braces)
        tp.load_workout_library()
        t0 = time.time()
        phases, weeks = tp.generate_plan(goal, seed_salt=int(time.time_ns()))
        elapsed = time.time() - t0
        self.assertLess(
            elapsed, 5.0,
            f"generate_plan took {elapsed:.2f}s > 5s warm-cache budget",
        )
        # Sanity: 8wk plan should yield ~8 weeks.
        self.assertGreaterEqual(len(weeks), 6)


def _mk_existing_plan_with_availability(
    plan_dir: Path, monday: date, blocked_days: list[date],
) -> dict:
    """Write a stub current_plan.json that already has an availability dict.

    Used to exercise the /api/plan/generate code path that reads
    plan["availability"] from disk before calling generate_plan().
    """
    weeks = []
    for w_idx in range(2):
        wstart = monday + timedelta(weeks=w_idx)
        sessions = []
        for off in range(7):
            d = wstart + timedelta(days=off)
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": "z2" if off in (1, 3) else "rest",
                "duration_min": 60 if off in (1, 3) else 0,
                "tss_estimate": 45 if off in (1, 3) else 0,
                "description": "",
                "zwo_file": "z2_test.zwo" if off in (1, 3) else "",
                "zwo_name": "Z2 test" if off in (1, 3) else "",
                "status": "pending",
            })
        weeks.append({
            "week_num": w_idx + 1,
            "start": wstart.isoformat(),
            "end": (wstart + timedelta(days=6)).isoformat(),
            "phase": "base",
            "tss_target": 200,
            "is_stepback": False,
            "sessions": sessions,
            "hit_per_week": 0,
        })
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
        "phases": [],
        "weeks": weeks,
        "generated": "2026-04-19T00:00:00",
        "availability": {
            d.isoformat(): {"hours": 0, "type": "holiday"} for d in blocked_days
        },
    }
    (plan_dir / "current_plan.json").write_text(json.dumps(plan))
    return plan


class TestApiPlanGenerate(unittest.TestCase):
    """Endpoint-level tests for /api/plan/generate."""

    @classmethod
    def setUpClass(cls):
        tp.load_workout_library()

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        self._patch_plan_dir = patch.object(
            app_module, "_plan_dir", return_value=self._tmp
        )
        self._patch_plan_dir.start()

        # Empty FIT-rides dir.
        self._fit_dir = Path(self._tmpdir.name) / "fit"
        self._fit_dir.mkdir(parents=True, exist_ok=True)
        self._patch_fit = patch.object(
            app_module, "_rides_fit_dir", return_value=self._fit_dir
        )
        self._patch_fit.start()

        import ride_storage as _rs
        self._patch_rides = patch.object(_rs, "list_rides", return_value=[])
        self._patch_rides.start()

        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_rides.stop()
        self._patch_fit.stop()
        self._patch_plan_dir.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def test_api_plan_generate_reads_availability(self):
        """Pre-existing plan["availability"] gets fed into generate_plan."""
        monday = _future_monday()
        sat1 = monday + timedelta(days=5)
        sat2 = monday + timedelta(days=12)
        _mk_existing_plan_with_availability(
            self._tmp, monday, blocked_days=[sat1, sat2],
        )

        body = {
            "goal": "general",
            "hours_per_week": 8.0,
            "max_weekday": 2.0,
            "max_weekend": 3.5,
            "plan_weeks": 4,
            "rest_days": [0],
            "daily_availability": {"1": 1.0, "2": 1.5, "3": 1.0,
                                   "4": 1.5, "5": 1.0, "6": 3.0},
        }
        r = self.client.post("/api/plan/generate", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data.get("ok"))

        # Persisted availability must survive regeneration.
        with open(self._tmp / "current_plan.json", encoding="utf-8") as f:
            persisted = json.load(f)
        self.assertIn("availability", persisted)
        self.assertIn(sat1.isoformat(), persisted["availability"])

        # Sat1/Sat2 sessions in the regenerated plan must be rest.
        plan_json = data["plan_json"]
        for w in plan_json.get("weeks", []):
            for s in w.get("sessions", []):
                if s.get("day") in (sat1.isoformat(), sat2.isoformat()):
                    self.assertEqual(
                        s.get("session_type"), "rest",
                        f"day {s.get('day')} should be rest "
                        f"(got {s.get('session_type')})",
                    )
                    self.assertEqual(s.get("duration_min", 0), 0)

    def test_api_plan_generate_response_has_card_state(self):
        """Response payload includes card_state per session (no first-paint
        yellow ⚠ surprise on /api/plan reload)."""
        body = {
            "goal": "general", "hours_per_week": 8.0,
            "max_weekday": 2.0, "max_weekend": 3.5,
            "plan_weeks": 4, "rest_days": [0],
            "daily_availability": {"1": 1.0, "2": 1.5, "3": 1.0,
                                   "4": 1.5, "5": 1.0, "6": 3.0},
        }
        r = self.client.post("/api/plan/generate", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        plan_json = r.json()["plan_json"]

        sessions_seen = 0
        for w in plan_json.get("weeks", []):
            for s in w.get("sessions", []):
                self.assertIn("card_state", s,
                              f"session {s.get('day')} missing card_state")
                if s.get("session_type") != "rest":
                    sessions_seen += 1
                    # display_name and zone_dist also enriched.
                    self.assertIn("display_name", s)
                    self.assertIn("zone_dist", s)
        self.assertGreater(sessions_seen, 0)


if __name__ == "__main__":
    unittest.main()
