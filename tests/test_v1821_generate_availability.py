"""v1.8.21 — generating a plan with new weekly hours must repopulate the
per-day availability calendar across the whole plan span with those hours,
while preserving explicit user blocks (holiday / injury / illness).

Pre-fix /api/plan/generate carried the OLD dense `plan["availability"]`
verbatim, so after changing weekday/weekend hours the calendar still showed
the stale per-day values.
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
from fastapi.testclient import TestClient  # noqa: E402


class TestGenerateRepopulatesAvailability(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gen_avail_"))
        self._patch = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch.start()
        self.pp = self.tmp / "current_plan.json"
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()

    def test_new_hours_fill_calendar_and_preserve_blocks(self):
        today = date.today()
        # Seed an OLD plan: stale 1h weekdays everywhere + one explicit holiday.
        old_avail = {}
        for i in range(40):
            d = today + timedelta(days=i)
            old_avail[d.isoformat()] = {"hours": 1.0, "type": "available"}
        # Put a holiday on a weekday (not a rest day) so we can tell it apart.
        holiday = None
        for i in range(1, 20):
            d = today + timedelta(days=i)
            if d.weekday() in (1, 2, 3):  # Tue/Wed/Thu
                holiday = d.isoformat()
                old_avail[holiday] = {"hours": 0, "type": "holiday"}
                break
        self.pp.write_text(json.dumps({
            "goal": {"type": "general"}, "generated": today.isoformat(),
            "weeks": [], "availability": old_avail,
        }))

        r = self.client.post("/api/plan/generate", json={
            "goal": "general", "weeks": 6,
            "max_weekday": 3.0, "max_weekend": 5.0,
            "daily_availability": {"0": 3.0, "1": 3.0, "2": 3.0, "3": 3.0,
                                   "4": 3.0, "5": 5.0, "6": 5.0},
            "rest_days": [0],
        })
        self.assertEqual(r.status_code, 200, r.text)
        av = json.loads(self.pp.read_text()).get("availability", {})
        self.assertTrue(av, "availability calendar empty after generate")

        # A weekday (Tue) in the span must now read the NEW 3h default.
        tue = next((today + timedelta(days=i) for i in range(14)
                    if (today + timedelta(days=i)).weekday() == 1), None)
        if tue and tue.isoformat() != holiday:
            self.assertEqual(av[tue.isoformat()]["hours"], 3.0)
        # A weekend (Sat) must read the NEW 5h default.
        sat = next((today + timedelta(days=i) for i in range(14)
                    if (today + timedelta(days=i)).weekday() == 5), None)
        if sat:
            self.assertEqual(av[sat.isoformat()]["hours"], 5.0)
        # The explicit holiday must be preserved verbatim.
        if holiday:
            self.assertEqual(av[holiday]["type"], "holiday")
            self.assertEqual(av[holiday]["hours"], 0)
        # No stale 1h weekday entries remain (every non-block day reflects new
        # defaults: 0/3/5).
        stale = [k for k, v in av.items()
                 if isinstance(v, dict) and v.get("type") == "available"
                 and v.get("hours") == 1.0]
        self.assertEqual(stale, [], f"stale 1h entries survived: {stale[:5]}")


if __name__ == "__main__":
    unittest.main()
