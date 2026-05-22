"""v1.8.8 Bug 7 — Apply Rest Day persistence.

Master decisions §Bug 7: POST ``/api/plan/auto-adjust`` with
``scope='day'``, ``dry_run=false`` must rewrite *tomorrow's* session to
``rest`` in the on-disk plan, persist via atomic_write_plan, and return
``ok=true`` with an ``applied`` list.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


class TestApplyRestDay(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._plan_path = self._tmp / "current_plan.json"
        # Build a minimal plan: a single week covering today + tomorrow.
        self._today = date.today()
        self._tomorrow = self._today + timedelta(days=1)
        plan = {
            "weeks": [{
                "week_num": 1,
                "start": self._today.isoformat(),
                "sessions": [
                    {
                        "day": self._today.isoformat(),
                        "day_name": "Today",
                        "session_type": "z2_endurance",
                        "duration_min": 60,
                        "tss_estimate": 55,
                        "zwo_file": "z2.zwo",
                        "zwo_name": "Z2",
                        "status": "pending",
                    },
                    {
                        "day": self._tomorrow.isoformat(),
                        "day_name": "Tomorrow",
                        "session_type": "vo2_short",
                        "duration_min": 75,
                        "tss_estimate": 110,
                        "zwo_file": "vo2.zwo",
                        "zwo_name": "VO2 Short",
                        "status": "pending",
                    },
                ],
            }],
            "availability": {},
        }
        self._plan_path.write_text(json.dumps(plan), encoding="utf-8")
        self._patch_dir = patch.object(app_module, "_plan_dir",
                                        return_value=self._tmp)
        self._patch_dir.start()
        # Don't run real reforecast / DB queries.
        self._patch_reforecast = patch.object(
            tp, "reforecast_dict",
            side_effect=lambda working, **kw: (working, [], None),
        )
        self._patch_reforecast.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_dir.stop()
        self._patch_reforecast.stop()
        self._tmpdir.cleanup()

    def test_apply_rest_day_writes_tomorrow_to_disk(self):
        """scope='day' + severity='rest' + dry_run=false replaces tomorrow.

        v1.8.9 Bug 9 (master §9/§10): `applied` is a bool (true when the
        persistence step changed the plan); the per-day list moved to
        `applied_sessions`."""
        with patch("readiness_composite.compute_training_severity",
                   return_value={"severity": "rest", "source": "user"}):
            r = self.client.post(
                "/api/plan/auto-adjust",
                json={"scope": "day", "dry_run": False, "severity": "rest"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data["ok"])
        self.assertEqual(data["severity"], "rest")
        # v1.8.9 Bug 9 — `applied` is a bool now, persistence DID happen.
        self.assertIs(data["applied"], True)
        # The per-day list moved to `applied_sessions`.
        self.assertEqual(len(data["applied_sessions"]), 1)
        self.assertEqual(data["applied_sessions"][0]["date"],
                         self._tomorrow.isoformat())
        self.assertEqual(data["applied_sessions"][0]["session_type"], "rest")
        # Persisted to disk.
        stored = json.loads(self._plan_path.read_text(encoding="utf-8"))
        sessions = stored["weeks"][0]["sessions"]
        tomorrow_session = next(
            s for s in sessions if s["day"] == self._tomorrow.isoformat()
        )
        self.assertEqual(tomorrow_session["session_type"], "rest")
        self.assertEqual(tomorrow_session["tss_estimate"], 0)
        self.assertEqual(tomorrow_session["zwo_file"], "")

    def test_apply_rest_day_dry_run_does_not_persist(self):
        """dry_run=true returns the action but leaves disk untouched."""
        before = self._plan_path.read_text(encoding="utf-8")
        with patch("readiness_composite.compute_training_severity",
                   return_value={"severity": "rest", "source": "user"}):
            r = self.client.post(
                "/api/plan/auto-adjust",
                json={"scope": "day", "dry_run": True, "severity": "rest"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # actions populated, applied_sessions empty in dry-run.
        self.assertEqual(len(data["actions"]), 1)
        self.assertEqual(data["applied_sessions"], [])
        # v1.8.9 Bug 9 — `applied` is False because dry-run never persisted.
        self.assertIs(data["applied"], False)
        # Disk unchanged.
        self.assertEqual(self._plan_path.read_text(encoding="utf-8"), before)

    def test_v189_bug9_round_trip_reads_json_from_disk(self):
        """v1.8.9 Bug 9 — explicit round-trip: POST, then re-open the plan
        JSON from disk and assert the targeted day's session is rest.

        Locks master §9 contract: persistence step ACTUALLY changed disk,
        and `applied: True` matches the on-disk reality."""
        with patch("readiness_composite.compute_training_severity",
                   return_value={"severity": "rest", "source": "user"}):
            r = self.client.post(
                "/api/plan/auto-adjust",
                json={"scope": "day", "dry_run": False, "severity": "rest"},
            )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIs(data["applied"], True)
        # ROUND-TRIP: re-read the plan JSON from disk.
        with open(self._plan_path, encoding="utf-8") as f:
            disk_plan = json.load(f)
        sessions = disk_plan["weeks"][0]["sessions"]
        tomorrow_session = next(
            s for s in sessions if s["day"] == self._tomorrow.isoformat()
        )
        # Master §9 contract: rest day means type=rest, duration=0, tss=0.
        self.assertEqual(tomorrow_session["session_type"], "rest")
        self.assertEqual(tomorrow_session["duration_min"], 0)
        self.assertEqual(tomorrow_session["tss_estimate"], 0)

    def test_v189_bug9_applied_false_when_severity_normal(self):
        """v1.8.9 Bug 9 — severity=normal short-circuits with applied=False."""
        with patch("readiness_composite.compute_training_severity",
                   return_value={"severity": "normal", "source": "tsb"}):
            r = self.client.post(
                "/api/plan/auto-adjust",
                json={"scope": "today", "dry_run": False},
            )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIs(data["applied"], False)
        self.assertEqual(data["applied_sessions"], [])
        self.assertEqual(data["actions"], [])


if __name__ == "__main__":
    unittest.main()
