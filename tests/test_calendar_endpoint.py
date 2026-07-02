"""v4.3.0 IMPL-SERVER — /api/calendar contract tests (MASTER §3 + §4).

The calendar endpoint merges the stored plan with the ride archive into a
single payload for the dashboard's "intervals.icu-style" overlay. These
tests assert the §3 schema shape AND the merge invariants:

  - empty plan + no rides → empty weeks (only history shells, also empty)
  - plan only, no rides → planned populated, actual all null
  - rides only, no plan → planned all null, actual populated (history)
  - plan + matching ride → both populated, completion_pct correct
  - phase rolling correctly across weeks
  - z1/z2 / z3/z4 / z5+ split correct from FIT zone data
  - is_stepback flag propagated
  - current_iso_week + is_today markers correct
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _mk_plan_dict(monday: date, weeks_count: int = 2) -> dict:
    """Multi-week plan with mixed session types so phase + zone splits assert."""
    weeks = []
    phases = ["base", "build1"]
    for w_idx in range(weeks_count):
        wstart = monday + timedelta(weeks=w_idx)
        sessions = []
        types_for_week = ["rest", "z2", "tempo", "vo2max", "rest", "long_z2", "rest"]
        durations = [0, 60, 75, 60, 0, 120, 0]
        tss = [0, 45, 60, 75, 0, 90, 0]
        for off in range(7):
            d = wstart + timedelta(days=off)
            s = {
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": types_for_week[off],
                "duration_min": durations[off],
                "tss_estimate": tss[off],
                "description": f"{types_for_week[off]} {durations[off]}min",
                "zwo_file": "" if types_for_week[off] == "rest" else f"{types_for_week[off]}_test.zwo",
                "zwo_name": "" if types_for_week[off] == "rest" else f"{types_for_week[off]} test",
                "status": "pending",
            }
            sessions.append(s)
        weeks.append({
            "week_num": w_idx + 1,
            "start": wstart.isoformat(),
            "end": (wstart + timedelta(days=6)).isoformat(),
            "phase": phases[w_idx % len(phases)],
            "tss_target": 270,
            "is_stepback": (w_idx == 1),  # second week is stepback
            "sessions": sessions,
        })
    return {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0, 4, 6]},
        "phases": [],
        "weeks": weeks,
        "generated": "2026-04-19T00:00:00",
    }


class CalendarBase(unittest.TestCase):
    def setUp(self):
        # v3.0.0 gate: bust app's TTL response cache — a preceding suite's
        # client calls (e.g. test_plan_api) prime ride/calendar entries from
        # the REAL archive; within the 300s TTL those leak into this class's
        # temp-redirected world (order-dependent failures in full runs).
        try:
            app_module.clear_cache()
        except Exception:
            pass
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        today = date.today()
        self._monday = today - timedelta(days=today.weekday())
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

        # v4.4.0 §1 — also redirect ride_storage's ICU + FIT dirs so the
        # test process doesn't see the developer's cached ~/.domestique
        # entries via load_all_rides().
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

        # Empty list_rides by default — individual tests override.
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


class TestCalendarEndpoint(CalendarBase):
    """§4 — eight tests covering schema + merge invariants."""

    def test_empty_plan_no_rides_returns_history_only(self):
        # Wipe the plan so only the 12-week history shell remains.
        (self._tmp / "current_plan.json").unlink()
        r = self.client.get("/api/calendar")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("today", data)
        self.assertIn("current_iso_week", data)
        self.assertIn("weeks", data)
        # 12 history weeks generated with empty actuals.
        self.assertGreaterEqual(len(data["weeks"]), 1)
        for w in data["weeks"]:
            for d in w["days"]:
                self.assertIsNone(d["actual"])
                self.assertIsNone(d["planned"])

    def test_plan_only_no_rides_planned_populated(self):
        r = self.client.get("/api/calendar")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # Find the current-week (matches our planted plan).
        cur = next((w for w in data["weeks"] if w.get("is_current")), None)
        self.assertIsNotNone(cur, "current week must be flagged")
        self.assertEqual(cur["start_date"], self._monday.isoformat())
        self.assertEqual(cur["phase"], "base")
        # Planned aggregates non-zero, actual all zero.
        self.assertGreater(cur["planned_tss"], 0)
        self.assertEqual(cur["actual_tss"], 0)
        # Each non-rest day has planned_payload populated; actual is None.
        for d in cur["days"]:
            self.assertIsNone(d["actual"])
        # Tuesday should be z2 with non-empty planned_payload.
        tue = cur["days"][1]
        self.assertEqual(tue["dow"], 2)
        self.assertIsNotNone(tue["planned"])
        self.assertEqual(tue["planned"]["session_type"], "z2")

    def test_rides_only_no_plan_actual_populated(self):
        (self._tmp / "current_plan.json").unlink()
        # Plant a ride 3 days ago (so it lands in the history block).
        ride_d = (date.today() - timedelta(days=3)).isoformat()
        ride = {
            "id": "ride_x", "ride_id": "ride_x", "source": "json",
            "started_at": f"{ride_d}T08:00:00+00:00",
            "summary": {"duration_sec": 3600, "tss": 50, "avg_power": 180,
                        "decoupling_pct": 3.5},
            "zones": {"power": {"Z1": 300, "Z2": 2700, "Z3": 600, "Z4": 0,
                                "Z5": 0, "Z6": 0, "Z7": 0}},
        }
        with patch("ride_storage.list_rides", return_value=[ride]), \
             patch("ride_storage.get_ride", return_value=ride):
            r = self.client.get("/api/calendar")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # Find the day of the ride.
        all_days = [d for w in data["weeks"] for d in w["days"]]
        match = next((d for d in all_days if d["date"] == ride_d), None)
        self.assertIsNotNone(match, "ride day must surface in history")
        self.assertIsNotNone(match["actual"])
        self.assertEqual(match["actual"]["tss"], 50)
        self.assertGreater(match["actual"]["z1z2_min"], 0)

    def test_plan_plus_matching_ride_completion_pct(self):
        # Tuesday this week is z2 60min 45TSS. Plant a matching ride.
        tue = (self._monday + timedelta(days=1)).isoformat()
        ride = {
            "id": "ride_a", "ride_id": "ride_a", "source": "json",
            "started_at": f"{tue}T17:00:00+00:00",
            "summary": {"duration_sec": 3600, "tss": 45, "avg_power": 175},
            "zones": {"power": {"Z1": 300, "Z2": 2700, "Z3": 600, "Z4": 0,
                                "Z5": 0, "Z6": 0, "Z7": 0}},
        }
        with patch("ride_storage.list_rides", return_value=[ride]), \
             patch("ride_storage.get_ride", return_value=ride):
            r = self.client.get("/api/calendar")
        data = r.json()
        cur = next(w for w in data["weeks"] if w.get("is_current"))
        # Ride ON Tuesday must populate the day's actual.
        tue_day = next(d for d in cur["days"] if d["date"] == tue)
        self.assertIsNotNone(tue_day["actual"])
        self.assertEqual(tue_day["actual"]["tss"], 45)
        # Both populated → completion_pct > 0.
        self.assertGreater(cur["completion_pct"], 0.0)
        # Card state must be "completed" for that day.
        self.assertEqual(tue_day["card_state"], "completed")

    def test_phase_rolling_across_weeks(self):
        # Our seed plan has weeks: [base, build1]. Calendar must surface both.
        r = self.client.get("/api/calendar")
        data = r.json()
        plan_weeks = [w for w in data["weeks"]
                      if w.get("phase") in {"base", "build1"}]
        self.assertEqual(len(plan_weeks), 2)
        self.assertEqual(plan_weeks[0]["phase"], "base")
        self.assertEqual(plan_weeks[1]["phase"], "build1")

    def test_z_split_from_zones_block(self):
        ride_d = (self._monday + timedelta(days=1)).isoformat()
        ride = {
            "id": "ride_b", "ride_id": "ride_b", "source": "json",
            "started_at": f"{ride_d}T18:00:00+00:00",
            "summary": {"duration_sec": 3600, "tss": 70, "avg_power": 200},
            # 10min Z1+Z2, 20min Z3+Z4, 30min Z5+
            "zones": {"power": {"Z1": 300, "Z2": 300, "Z3": 600, "Z4": 600,
                                "Z5": 1200, "Z6": 600, "Z7": 0}},
        }
        with patch("ride_storage.list_rides", return_value=[ride]), \
             patch("ride_storage.get_ride", return_value=ride):
            r = self.client.get("/api/calendar")
        data = r.json()
        cur = next(w for w in data["weeks"] if w.get("is_current"))
        d = next(d for d in cur["days"] if d["date"] == ride_d)
        self.assertAlmostEqual(d["actual"]["z1z2_min"], 10.0, delta=0.1)
        self.assertAlmostEqual(d["actual"]["z3z4_min"], 20.0, delta=0.1)
        self.assertAlmostEqual(d["actual"]["z5plus_min"], 30.0, delta=0.1)

    def test_is_stepback_propagated(self):
        r = self.client.get("/api/calendar")
        data = r.json()
        # Our seed plan marked week 2 as stepback.
        plan_weeks = [w for w in data["weeks"]
                      if w.get("phase") in {"base", "build1"}]
        self.assertFalse(plan_weeks[0]["is_stepback"])
        self.assertTrue(plan_weeks[1]["is_stepback"])

    def test_current_iso_week_marked(self):
        r = self.client.get("/api/calendar")
        data = r.json()
        today = date.today()
        iso = today.isocalendar()
        self.assertEqual(data["current_iso_week"]["year"], iso[0])
        self.assertEqual(data["current_iso_week"]["week"], iso[1])
        # Exactly one week in the response is_current True.
        cur_count = sum(1 for w in data["weeks"] if w.get("is_current"))
        self.assertEqual(cur_count, 1)
        # The is_today flag fires on exactly one day in the current week.
        cur = next(w for w in data["weeks"] if w.get("is_current"))
        today_count = sum(1 for d in cur["days"] if d.get("is_today"))
        self.assertEqual(today_count, 1)


if __name__ == "__main__":
    unittest.main()
