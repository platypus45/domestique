"""Wave 1 (v2.5.0) — /api/plan/generate surfaces F4b/F4d rejections as 400.

F4b (D1): target_date <= today → tp.generate_plan raises ValueError with a
user-facing message; the endpoint must return 400 (was: silent empty plan,
then post-guard a generic 500).
F4d (SM4): a second priority-A event in events[] → 400 "one A event per plan"
(was: silently dropped).
"""
from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import app as app_module
import training_planner as tp
from conftest import PLANNER_PIN_ANCHOR as ANCHOR, FrozenPlannerDate
from fastapi.testclient import TestClient


class TestGenerateEndpointRejections(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="evt_api_w1_"))
        self._patches = [
            patch.object(app_module, "_plan_dir", return_value=self.tmp),
            # Pin the planner clock (the F4b guard compares against tp's
            # date.today()) and keep the endpoint's pre-generate fetches
            # off the network.
            patch.object(tp, "date", FrozenPlannerDate),
            patch.object(tp, "get_today_metrics", lambda: {}),
            patch.object(app_module, "cached", lambda key, fn, **kw: {}),
        ]
        for p in self._patches:
            p.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in self._patches:
            p.stop()

    def test_f4b_past_target_date_is_400(self):
        r = self.client.post("/api/plan/generate", json={
            "goal": "event",
            "event_date": (ANCHOR - timedelta(days=30)).isoformat(),
            "event_name": "PastFondo", "event_km": 150, "event_climb": 1500,
        })
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("past", r.json()["detail"])
        self.assertFalse((self.tmp / "current_plan.json").exists(),
                         "a rejected plan must not be persisted")

    def test_f4b_today_target_date_is_400(self):
        r = self.client.post("/api/plan/generate", json={
            "goal": "event", "event_date": ANCHOR.isoformat(),
            "event_name": "TodayFondo", "event_km": 150,
        })
        self.assertEqual(r.status_code, 400, r.text)

    def test_f4d_second_priority_a_event_is_400(self):
        r = self.client.post("/api/plan/generate", json={
            "goal": "event",
            "event_date": (ANCHOR + timedelta(days=112)).isoformat(),
            "event_name": "TestFondo", "event_km": 150, "event_climb": 1500,
            "events": [{"date": (ANCHOR + timedelta(days=70)).isoformat(),
                        "priority": "A", "name": "Rogue A", "event_km": 120}],
        })
        self.assertEqual(r.status_code, 400, r.text)
        self.assertIn("one A event per plan", r.json()["detail"])


if __name__ == "__main__":
    unittest.main()
