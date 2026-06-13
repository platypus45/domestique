"""v1.8.25 — /api/plan/rematch?apply=1 must be IDEMPOTENT on completion_matches.

The Plan-open catch-up sequence auto-runs apply=1 on every tab open, so a blind
append would stack a duplicate completion_match for the same activity every time.
The fix dedups by activity_id (update-in-place). This test isolates that dedup by
monkeypatching the classifier (tp.rematch_week) to return a fixed "done" match,
then applies twice and asserts the session ends with exactly ONE match.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module  # noqa: E402
import training_planner as tp  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class TestRematchDedup(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="rematch_dedup_"))
        self._patch_dir = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch_dir.start()
        self.pp = self.tmp / "current_plan.json"
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_dir.stop()

    def _seed_plan(self, day_iso):
        # Current week containing one pending session on `day_iso`.
        today = date.today()
        wk_start = today - timedelta(days=today.weekday())
        self.pp.write_text(json.dumps({
            "goal": {"type": "general"}, "generated": today.isoformat(),
            "availability": {},
            "weeks": [{
                "week_num": 1,
                "start": wk_start.isoformat(),
                "end": (wk_start + timedelta(days=6)).isoformat(),
                "phase": "build", "tss_target": 300, "is_stepback": False,
                "sessions": [{
                    "day": day_iso, "day_name": "Mon", "session_type": "z2",
                    "duration_min": 60, "tss_estimate": 50, "status": "pending",
                    "description": "",
                }],
            }],
        }))

    def test_apply_twice_does_not_duplicate_completion_match(self):
        day_iso = date.today().isoformat()
        self._seed_plan(day_iso)

        fake_week = type("W", (), {"sessions": []})()
        preview = {"matches": [{
            "session_date": day_iso, "new_status": "done", "activity_id": 123,
            "matched_axes": ["tss", "duration", "if"], "score": 0.95,
            "axes": {"tss": True}, "details": None,
        }], "summary": {}}

        with patch.object(app_module, "_load_current_week_dto", return_value=(fake_week, 0)), \
             patch.object(app_module, "_collect_week_activities", return_value=[]), \
             patch.object(tp, "rematch_week", return_value=preview):
            r1 = self.client.post("/api/plan/rematch?apply=1")
            self.assertEqual(r1.status_code, 200, r1.text)
            r2 = self.client.post("/api/plan/rematch?apply=1")
            self.assertEqual(r2.status_code, 200, r2.text)

        plan = json.loads(self.pp.read_text())
        sess = plan["weeks"][0]["sessions"][0]
        cm = sess.get("completion_matches", [])
        self.assertEqual(len(cm), 1, f"expected 1 completion_match, got {len(cm)}: {cm}")
        self.assertEqual(cm[0]["activity_id"], 123)
        self.assertEqual(sess["status"], "done")

    def test_different_activity_still_appends(self):
        """Dedup is per activity_id — a genuinely different activity on the same
        session must still add a second match (not be suppressed)."""
        day_iso = date.today().isoformat()
        self._seed_plan(day_iso)
        fake_week = type("W", (), {"sessions": []})()

        def preview_for(aid):
            return {"matches": [{
                "session_date": day_iso, "new_status": "done", "activity_id": aid,
                "matched_axes": ["tss"], "score": 0.9, "axes": {}, "details": None,
            }], "summary": {}}

        with patch.object(app_module, "_load_current_week_dto", return_value=(fake_week, 0)), \
             patch.object(app_module, "_collect_week_activities", return_value=[]):
            with patch.object(tp, "rematch_week", return_value=preview_for(123)):
                self.client.post("/api/plan/rematch?apply=1")
            with patch.object(tp, "rematch_week", return_value=preview_for(456)):
                self.client.post("/api/plan/rematch?apply=1")

        plan = json.loads(self.pp.read_text())
        cm = plan["weeks"][0]["sessions"][0].get("completion_matches", [])
        ids = sorted(e["activity_id"] for e in cm)
        self.assertEqual(ids, [123, 456], f"both activities should be recorded: {cm}")


if __name__ == "__main__":
    unittest.main()
