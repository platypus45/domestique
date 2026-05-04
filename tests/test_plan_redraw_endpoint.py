"""Wave-1 IMPL-WEEKLY (Sprint B) regression tests for /api/plan/re-draw.

Endpoint contract (v4.1.0 FIX-CONTRACT, surfaced as a thin wrapper around
the path-param form ``/api/plan/rematch/{day}`` from v4.1.0 P6):

- POST /api/plan/re-draw with ``{date: "YYYY-MM-DD"}`` body
- 200 + ``{ok, action: "redrawn", day, zwo_file, zwo_name, variation}``
  on success
- 400 on missing/invalid date
- 200 + ``{ok: false, action: "rest_day"|"no_candidate"}`` on benign
  rejections
- A second call for the SAME day must return a DIFFERENT zwo_file (the
  variation counter feeds the seed in match_zwo, and the in-week
  exclusion set blocks repeats of the just-drawn workout).

These three behaviors are what the dashboard ⟳ button depends on. If the
second call returned the same zwo, users would see "no change" and lose
trust in the button.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _mk_plan_dict(monday: date) -> dict:
    """Single-week plan with 4 distinct session types so re-draw has options."""
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


class RedrawEndpointBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        today = date.today()
        self._monday = today - timedelta(days=today.weekday())
        self._plan = _mk_plan_dict(self._monday)
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp
        self.client = TestClient(app_module.app)

    def tearDown(self):
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _read_plan(self) -> dict:
        return json.loads((self._tmp / "current_plan.json").read_text())


class TestRedrawEndpointBasic(RedrawEndpointBase):
    """v4.1.0 FIX-CONTRACT C4 contract — body shape + status codes."""

    def test_missing_date_returns_400(self):
        r = self.client.post("/api/plan/re-draw", json={})
        self.assertEqual(r.status_code, 400)
        self.assertIn("date", r.json().get("error", "").lower())

    def test_invalid_date_returns_400(self):
        r = self.client.post("/api/plan/re-draw", json={"date": "not-a-date"})
        self.assertEqual(r.status_code, 400)

    def test_rest_day_returns_rest_day_action(self):
        # Monday in the seed plan is REST.
        mon = self._monday.isoformat()
        r = self.client.post("/api/plan/re-draw", json={"date": mon})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertFalse(data.get("ok"))
        self.assertEqual(data.get("action"), "rest_day")

    def test_redraw_non_rest_day_returns_zwo(self):
        # Tuesday Z2 — should match a real zwo from the on-disk library.
        tue = (self._monday + timedelta(days=1)).isoformat()
        r = self.client.post("/api/plan/re-draw", json={"date": tue})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # If the on-disk library has any z2 candidates, action=redrawn.
        # If it has none (CI env without workouts/), no_candidate is OK too.
        self.assertIn(data.get("action"), {"redrawn", "no_candidate"})
        if data.get("action") == "redrawn":
            self.assertTrue(data.get("ok"))
            self.assertIn("zwo_file", data)
            self.assertIn("zwo_name", data)
            self.assertIn("variation", data)


class TestRedrawTwiceGivesDifferent(RedrawEndpointBase):
    """v4.2.0 IMPL-WEEKLY Sprint B — re-draw same day twice should pick a
    different workout (variation counter + in-week exclusion set)."""

    def test_consecutive_redraws_yield_distinct_zwo_files(self):
        tue = (self._monday + timedelta(days=1)).isoformat()
        r1 = self.client.post("/api/plan/re-draw", json={"date": tue})
        self.assertEqual(r1.status_code, 200, r1.text)
        d1 = r1.json()
        if d1.get("action") != "redrawn":
            # Library too sparse in CI; nothing to assert beyond shape.
            self.skipTest(f"library has no candidates: {d1.get('action')}")

        first_file = d1.get("zwo_file")
        first_var = d1.get("variation", 0)
        self.assertTrue(first_file)

        # Second call SAME day. The endpoint adds the just-drawn zwo's name
        # to the exclusion set (via every other session's name in the week)
        # and bumps the variation counter so the seed flips. Result: a
        # different workout, OR no_candidate if the bucket is exhausted.
        r2 = self.client.post("/api/plan/re-draw", json={"date": tue})
        self.assertEqual(r2.status_code, 200, r2.text)
        d2 = r2.json()
        self.assertIn(d2.get("action"), {"redrawn", "no_candidate"})
        if d2.get("action") == "redrawn":
            second_file = d2.get("zwo_file")
            second_var = d2.get("variation", 0)
            self.assertTrue(second_file)
            self.assertNotEqual(first_file, second_file,
                "second re-draw must return a different zwo_file")
            self.assertGreater(second_var, first_var,
                "variation counter must increment between calls")

        # Plan JSON must reflect the latest pick (persistence under lock).
        plan = self._read_plan()
        tue_sess = next(s for s in plan["weeks"][0]["sessions"]
                        if s["day"] == tue)
        if d2.get("action") == "redrawn":
            self.assertEqual(tue_sess["zwo_file"], d2["zwo_file"])
        else:
            # rest of the contract: even on no_candidate, the *first* draw
            # must have persisted.
            self.assertEqual(tue_sess["zwo_file"], first_file)


class TestRedrawDayOfWeekIndexFallback(RedrawEndpointBase):
    """The endpoint accepts {day: 0..6} as a legacy convenience — verify."""

    def test_day_index_resolves_to_iso_in_current_week(self):
        # day=2 → Wednesday → sweetspot session.
        r = self.client.post("/api/plan/re-draw", json={"day": 2})
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # Either redrawn or no_candidate. Either way the resolved day must
        # be Wednesday in the *current* plan week.
        self.assertIn(data.get("action"), {"redrawn", "no_candidate"})
        wed = (self._monday + timedelta(days=2)).isoformat()
        self.assertEqual(data.get("day"), wed)


if __name__ == "__main__":
    unittest.main()
