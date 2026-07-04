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


if __name__ == "__main__":
    unittest.main()
