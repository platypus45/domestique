"""H1 regression (v3.1.0 evaluator) — /api/plan/generate with the REAL UI
payload shape: the form's plan-weeks slider is TODAY-anchored, so a backdated
event plan must have its week budget recomputed server-side from the
start_date→event span. Pre-fix: weeks_available() short-circuited on the
stale plan_weeks=12 and the fill-to-taper stretch dumped the 4-week error
into a 7-week PEAK block."""
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
from fastapi.testclient import TestClient  # noqa: E402


class TestBackdatedGenerateRecomputesWeeks(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="entry_api_"))
        self._patch = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()

    def test_ui_stale_weeks_overridden_by_full_runway(self):
        today = date.today()
        start = today - timedelta(days=28)     # 4 weeks in
        event = today + timedelta(days=84)     # 12 more weeks to race day
        r = self.client.post("/api/plan/generate", json={
            "goal": "event",
            "event_date": event.isoformat(),
            "start_date": start.isoformat(),
            "entry_mode": "declared",
            "weeks": 12,                       # the stale TODAY-anchored slider value
            "hours_per_week": 8.0,
            "event_km": 150, "event_climb": 1500, "event_type": "granfondo",
        })
        self.assertEqual(r.status_code, 200, r.text)

        plan = json.loads((self.tmp / "current_plan.json").read_text())
        weeks = plan["weeks"]
        # Full ~16-week runway (16/17 rows by weekday alignment), anchored on
        # the backdated start; the H1 bug produced the UI's TODAY-anchored 12.
        self.assertEqual(weeks[0]["start"], start.isoformat())
        self.assertGreaterEqual(len(weeks), 15,
                                f"stale plan_weeks won (H1): {len(weeks)} weeks")
        self.assertLessEqual(len(weeks), 17)
        self.assertEqual(plan["goal"].get("start_date"), start.isoformat())
        # H1 signature was a stretched multi-week peak: cap consecutive peaks.
        phases = [w.get("phase") for w in weeks]
        longest_peak = cur = 0
        for p in phases:
            cur = cur + 1 if p == "peak" else 0
            longest_peak = max(longest_peak, cur)
        self.assertLessEqual(longest_peak, 3,
                             f"stretched peak block (H1): {phases}")
        # Elapsed rows sessionless; nothing scheduled before today.
        for w in weeks:
            if w["end"] < today.isoformat():
                self.assertEqual(w["sessions"], [])
            for s in w.get("sessions", []):
                self.assertGreaterEqual(s["day"], today.isoformat())


class TestEntryScanEndpoint(unittest.TestCase):
    """MODE 2 — GET /api/plan/entry-scan (IP B-LOCKED-3): scan→propose is
    read-only; params mirror /api/plan/preview; missing goal/target params
    400 cleanly (not 422/500)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="entry_scan_"))
        self._plan_patch = patch.object(app_module, "_plan_dir",
                                        return_value=self.tmp)
        self._plan_patch.start()
        today = date.today()
        rides = []
        for w in range(1, 5):  # 4 whole compliant weeks back from today
            for days_back in (7 * w, 7 * w - 3):
                rides.append({
                    "started_at": (today - timedelta(days=days_back)).isoformat()
                                  + "T09:00:00",
                    "tss": None,                 # exercise the cascade …
                    "icu_training_load": 300.0,  # … icu_training_load fallback
                })
        self._rides_patch = patch.object(app_module, "_load_all_rides_safe",
                                         return_value=rides)
        self._rides_patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._rides_patch.stop()
        self._plan_patch.stop()

    def test_scan_shape_proposal_and_zero_writes(self):
        r = self.client.get("/api/plan/entry-scan",
                            params={"goal": "general", "plan_weeks": 12})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertEqual(set(d.keys()), {"proposal_weeks",
                                         "equivalent_start_date", "capped",
                                         "weeks"})
        self.assertEqual(d["proposal_weeks"], 4)
        self.assertEqual(d["equivalent_start_date"],
                         (date.today() - timedelta(days=28)).isoformat())
        self.assertTrue(d["capped"])
        self.assertEqual(len(d["weeks"]), 4)
        for row in d["weeks"]:
            self.assertEqual(set(row.keys()),
                             {"index", "window_start", "actual_tss",
                              "target_tss", "qualifies", "shape_note"})
            self.assertTrue(row["qualifies"])
        # Zero writes: the scan proposes, only Generate persists.
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_scan_event_goal_derives_runway_from_event_date(self):
        event = date.today() + timedelta(days=84)
        r = self.client.get("/api/plan/entry-scan",
                            params={"goal": "event",
                                    "event_date": event.isoformat()})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["proposal_weeks"], 4)

    def test_scan_400s_on_missing_goal_or_target_params(self):
        # No goal at all.
        self.assertEqual(self.client.get("/api/plan/entry-scan").status_code,
                         400)
        # Event goal without an event date.
        self.assertEqual(
            self.client.get("/api/plan/entry-scan",
                            params={"goal": "event"}).status_code, 400)
        # Non-event goal without any week budget or end date.
        self.assertEqual(
            self.client.get("/api/plan/entry-scan",
                            params={"goal": "general",
                                    "plan_weeks": 0}).status_code, 400)


class TestRecognizedEntryModePersistence(unittest.TestCase):
    """B-LOCKED-7: entry_mode="recognized" is provenance — it must survive
    generate → plan dict → _goal_from_plan_dict so regenerate reuses the
    stored anchor instead of silently re-running the recognizer."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="entry_recog_"))
        self._patch = patch.object(app_module, "_plan_dir",
                                   return_value=self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()

    def test_recognized_roundtrip_through_generate(self):
        start = date.today() - timedelta(days=14)
        r = self.client.post("/api/plan/generate", json={
            "goal": "general", "weeks": 10, "hours_per_week": 8.0,
            "start_date": start.isoformat(), "entry_mode": "recognized",
        })
        self.assertEqual(r.status_code, 200, r.text)
        plan = json.loads((self.tmp / "current_plan.json").read_text())
        self.assertEqual(plan["goal"].get("entry_mode"), "recognized")
        self.assertEqual(plan["goal"].get("start_date"), start.isoformat())
        goal = app_module._goal_from_plan_dict(plan["goal"])
        self.assertEqual(goal.entry_mode, "recognized")
        self.assertEqual(goal.start_date, start)


if __name__ == "__main__":
    unittest.main()
