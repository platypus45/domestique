"""v1.3.1 HIGH hot-fix — availability auto-reflow on save.

Pre-fix: ``POST /api/plan/save-availability`` only persisted the
``plan["availability"]`` dict; the popover told the user to "click Generate
Plan" to apply, which left the planned sessions visible on user-blocked days.

Post-fix: the endpoint invokes ``tp.reforecast(availability_overrides=...)``
so per-day hour overrides actually rescale ``duration_min`` /
``tss_estimate`` (and zero hours=0 sessions) on disk. Response surfaces
``sessions_modified`` so the UI can confirm something happened.
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


def _mk_plan_dict(monday: date, weeks_count: int = 2) -> dict:
    """Plan with non-zero Sat session so availability=0 has something to zero."""
    weeks = []
    phases = ["base", "build1"]
    for w_idx in range(weeks_count):
        wstart = monday + timedelta(weeks=w_idx)
        sessions = []
        # Mon REST, Tue Z2, Wed REST, Thu TEMPO, Fri REST, Sat LONG_Z2, Sun REST
        types_for_week = ["rest", "z2", "rest", "tempo", "rest", "long_z2", "rest"]
        durations = [0, 60, 0, 60, 0, 120, 0]
        tss = [0, 45, 0, 60, 0, 90, 0]
        for off in range(7):
            d = wstart + timedelta(days=off)
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": types_for_week[off],
                "duration_min": durations[off],
                "tss_estimate": tss[off],
                "description": f"{types_for_week[off]} {durations[off]}min",
                "zwo_file": "" if types_for_week[off] == "rest" else f"{types_for_week[off]}_test.zwo",
                "zwo_name": "" if types_for_week[off] == "rest" else f"{types_for_week[off]} test",
                "status": "pending",
            })
        weeks.append({
            "week_num": w_idx + 1,
            "start": wstart.isoformat(),
            "end": (wstart + timedelta(days=6)).isoformat(),
            "phase": phases[w_idx % len(phases)],
            "tss_target": 270,
            "is_stepback": (w_idx == 1),
            "sessions": sessions,
            "hit_per_week": 1,
        })
    return {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0, 2, 4, 6]},
        "phases": [],
        "weeks": weeks,
        "generated": "2026-04-19T00:00:00",
    }


class AvailabilityReflowBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

        # Anchor on a future Monday so all plan dates are "future" — ensures
        # reforecast()'s availability_overrides loop touches them (it skips
        # weeks where pw.start < today).
        today = date.today()
        self._monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        self._sat = self._monday + timedelta(days=5)
        self._sun = self._monday + timedelta(days=6)

        self._plan = _mk_plan_dict(self._monday, weeks_count=2)
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))

        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        # Empty FIT-rides dir per test (so glob returns nothing).
        self._fit_dir = Path(self._tmpdir.name) / "fit"
        self._fit_dir.mkdir(parents=True, exist_ok=True)
        self._patch_fit = patch.object(
            app_module, "_rides_fit_dir", return_value=self._fit_dir
        )
        self._patch_fit.start()

        import ride_storage as _rs
        self._icu_dir = Path(self._tmpdir.name) / "icu"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        self._patch_icu_dir = patch.object(
            _rs, "_icu_rides_dir", return_value=self._icu_dir
        )
        self._patch_icu_dir.start()
        self._patch_fit_dir_rs = patch.object(
            _rs, "_fit_rides_dir", return_value=self._fit_dir
        )
        self._patch_fit_dir_rs.start()
        self._patch_rides = patch("ride_storage.list_rides", return_value=[])
        self._patch_rides.start()

        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_rides.stop()
        self._patch_fit_dir_rs.stop()
        self._patch_icu_dir.stop()
        self._patch_fit.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _read_plan(self) -> dict:
        return json.loads((self._tmp / "current_plan.json").read_text())

    def _saturday_session(self, plan: dict) -> dict:
        for w in plan["weeks"]:
            for s in w["sessions"]:
                if s["day"] == self._sat.isoformat():
                    return s
        raise AssertionError("Saturday session not found in plan")


class TestAvailabilityReflow(AvailabilityReflowBase):
    """v1.3.1 HIGH — three tests for the save-availability reflow."""

    def test_sat_zero_hours_zeros_planned_session_on_disk(self):
        # Pre-condition: Saturday is LONG_Z2 120min 90TSS.
        before = self._saturday_session(self._read_plan())
        self.assertEqual(before["session_type"], "long_z2")
        self.assertEqual(before["duration_min"], 120)
        self.assertGreater(before["tss_estimate"], 0)

        body = {
            "availability": {
                self._sat.isoformat(): {"hours": 0, "type": "holiday"},
            }
        }
        r = self.client.post(
            "/api/plan/save-availability",
            json=body,
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data.get("ok"))
        self.assertGreaterEqual(data.get("sessions_modified", 0), 1)

        # Post-condition: Saturday session is zeroed on disk.
        after = self._saturday_session(self._read_plan())
        self.assertEqual(after["session_type"], "rest")
        self.assertEqual(after["duration_min"], 0)
        self.assertEqual(after["tss_estimate"], 0)

    def test_response_reports_sessions_modified_count(self):
        # Mark Sat AND Sun unavailable. Sat had a planned session; Sun was
        # already REST 0min 0TSS — only Sat should count as modified.
        body = {
            "availability": {
                self._sat.isoformat(): {"hours": 0, "type": "holiday"},
                self._sun.isoformat(): {"hours": 0, "type": "holiday"},
            }
        }
        r = self.client.post(
            "/api/plan/save-availability",
            json=body,
        )
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data.get("ok"))
        # Sat changed (long_z2 → rest); Sun was already rest=0/0 so no change.
        self.assertEqual(data.get("sessions_modified"), 1)

    def test_idempotent_second_save_returns_zero_modified(self):
        body = {
            "availability": {
                self._sat.isoformat(): {"hours": 0, "type": "holiday"},
            }
        }
        # First save zeros Sat.
        r1 = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertGreaterEqual(r1.json().get("sessions_modified", 0), 1)

        # Second save with the same body: nothing more to change on disk.
        r2 = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json().get("sessions_modified"), 0)


if __name__ == "__main__":
    unittest.main()
