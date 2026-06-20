"""FS1 — a fixed_core / template plan must stay FIXED across regenerate.

Regression for two bugs found by end-to-end probing (the unit tests on the
generate path missed them): (1) regenerate_from_today / reforecast rebuilt
`adjusted_goal` WITHOUT plan_mode → it defaulted to "auto" and the sampler
reshuffled the build weeks back to mixed HIT; (2) `_regenerate_plan_dict`
reconstructs the Goal INLINE (separate from _goal_from_plan_dict) and also
omitted plan_mode. Both fixed. This drives the real HTTP /api/plan/generate +
/api/plan/regenerate path and asserts the plan never gains a 2nd HIT type per
week (the owner's "one HIT type per week" contract).
"""
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import app as app_module
from fastapi.testclient import TestClient

_HIT = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}


def _max_hit_types_per_week(plan):
    """Worst-case distinct HIT types in any single week (the contract caps at 1)."""
    worst = 0
    for w in plan.get("weeks", []):
        types = {s.get("session_type") for s in w["sessions"]
                 if s.get("session_type") in _HIT}
        worst = max(worst, len(types))
    return worst


class TestFixedCoreSurvivesRegen(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fs1regen_"))
        self._patch = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()

    def test_fixed_core_plan_stays_single_hit_type_after_regenerate(self):
        body = dict(
            goal="ftp", plan_mode="fixed_core", plan_weeks=16, hours_per_week=10,
            max_weekday=2.0, max_weekend=4.0, rest_days=[0],
            daily_availability={str(d): (2.0 if d < 5 else 4.0) for d in range(1, 7)},
        )
        with patch.object(app_module, "cached",
                          side_effect=lambda k, fn, **kw: fn() if k != "training" else {"ctl": 45}), \
             patch.object(app_module.db, "query_activities", return_value=[]):
            r = self.client.post("/api/plan/generate", json=body)
            self.assertEqual(r.status_code, 200, r.text)
            gen = json.loads((self.tmp / "current_plan.json").read_text())
            self.assertEqual(gen["goal"].get("plan_mode"), "fixed_core")
            self.assertLessEqual(_max_hit_types_per_week(gen), 1,
                                 "generate produced >1 HIT type in some week")

            r2 = self.client.post("/api/plan/regenerate")
            self.assertEqual(r2.status_code, 200, r2.text)
            reg = json.loads((self.tmp / "current_plan.json").read_text())
            # plan_mode persists AND the build weeks didn't get reshuffled to
            # mixed HIT (the bug: regen defaulted to "auto" → sampler).
            self.assertEqual(reg["goal"].get("plan_mode"), "fixed_core",
                             "plan_mode lost on regenerate")
            self.assertLessEqual(
                _max_hit_types_per_week(reg), 1,
                "regenerate reshuffled a fixed_core plan to >1 HIT type/week")


if __name__ == "__main__":
    unittest.main()
