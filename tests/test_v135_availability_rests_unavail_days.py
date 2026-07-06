"""v1.3.5 BUG hot-fix — UPDATE plan now rests unavailable days.

User flow that broke pre-fix: open availability calendar → set Sat=0h
Sun=0h → click UPDATE → "Plan reflowed — N sessions changed" toast →
dashboard reloads → Sat/Sun still showed planned z2/long sessions.

Root cause: ``training_planner.reforecast()`` short-circuited the
availability_overrides loop with ``if pw.start < today: continue`` — which
silently skipped the *current* week (whose Monday is by definition <
today on any non-Monday). The ``generate_plan`` block has no such gate,
which is why a fresh /api/plan/generate honoured the overrides but the
in-place reflow on UPDATE did not.

This test anchors the plan on the *current* week's Monday (pw.start <
today on every weekday except Monday) and asserts:

  - Sat and Sun get session_type='rest', duration_min=0, tss_estimate=0
  - zwo_file/zwo_name are cleared so the dashboard cell renders REST
  - Mon/Tue/Wed/Thu/Fri sessions in the current week are UNCHANGED
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

from conftest import PLANNER_PIN_ANCHOR

# 2026-07-06 fix (Monday flake): same freeze as test_v136 — the real-clock
# anchor re-pointed at LAST week whenever today was Monday, putting Sat/Sun
# in the past where the handler rightly refuses to touch them. Frozen
# Wednesday of the pin week keeps the intended geometry (pw.start < today,
# weekend in the future) on every real weekday.
_FROZEN_TODAY = PLANNER_PIN_ANCHOR + timedelta(days=2)  # Wed 2026-01-07


class _FrozenWed(date):
    @classmethod
    def today(cls):
        return cls(_FROZEN_TODAY.year, _FROZEN_TODAY.month, _FROZEN_TODAY.day)


def _mk_plan_dict(monday: date, weeks_count: int = 2) -> dict:
    """Plan with a real Sat session so availability=0 has something to zero."""
    weeks = []
    phases = ["base", "build1"]
    for w_idx in range(weeks_count):
        wstart = monday + timedelta(weeks=w_idx)
        sessions = []
        # Mon REST, Tue Z2, Wed REST, Thu TEMPO, Fri REST, Sat LONG_Z2, Sun Z2
        types_for_week = ["rest", "z2", "rest", "tempo", "rest", "long_z2", "z2"]
        durations = [0, 60, 0, 60, 0, 120, 90]
        tss = [0, 45, 0, 60, 0, 90, 65]
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
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0, 2, 4]},
        "phases": [],
        "weeks": weeks,
        "generated": "2026-04-19T00:00:00",
    }


class CurrentWeekAvailabilityBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

        # Anchor on the PIN week's Monday with "today" FROZEN mid-week (Wed)
        # — pw.start < today (the gated week the original bug lived on)
        # holds deterministically, and the weekend stays in the future so
        # the handler may zero it. (The old real-clock anchor + Monday dodge
        # put the weekend in the PAST every Monday.)
        self._patch_app_date = patch.object(app_module, "date", _FrozenWed)
        self._patch_tp_date = patch.object(tp, "date", _FrozenWed)
        self._patch_app_date.start()
        self._patch_tp_date.start()
        self._monday = PLANNER_PIN_ANCHOR
        self._sat = self._monday + timedelta(days=5)
        self._sun = self._monday + timedelta(days=6)

        self._plan = _mk_plan_dict(self._monday, weeks_count=2)
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))

        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

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
        self._patch_tp_date.stop()
        self._patch_app_date.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _read_plan(self) -> dict:
        return json.loads((self._tmp / "current_plan.json").read_text())

    def _session(self, plan: dict, d: date) -> dict:
        for w in plan["weeks"]:
            for s in w["sessions"]:
                if s["day"] == d.isoformat():
                    return s
        raise AssertionError(f"Session for {d} not found in plan")


class TestCurrentWeekRestsUnavailableDays(CurrentWeekAvailabilityBase):
    """v1.3.5 — UPDATE plan must rest Sat/Sun in the *current* week."""

    def test_sat_sun_zero_hours_rests_current_week_sessions(self):
        # Pre-condition: Sat is long_z2 120min, Sun is z2 90min.
        before_plan = self._read_plan()
        before_sat = self._session(before_plan, self._sat)
        before_sun = self._session(before_plan, self._sun)
        self.assertEqual(before_sat["session_type"], "long_z2")
        self.assertEqual(before_sat["duration_min"], 120)
        self.assertEqual(before_sun["session_type"], "z2")
        self.assertEqual(before_sun["duration_min"], 90)

        body = {
            "availability": {
                self._sat.isoformat(): {"hours": 0, "type": "holiday"},
                self._sun.isoformat(): {"hours": 0, "type": "holiday"},
            }
        }
        r = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json().get("sessions_modified", 0), 2)

        # Post-condition: Sat AND Sun are zeroed on disk.
        after_plan = self._read_plan()
        after_sat = self._session(after_plan, self._sat)
        after_sun = self._session(after_plan, self._sun)

        self.assertEqual(after_sat["session_type"], "rest",
                         "Sat must become rest after UPDATE with hours=0")
        self.assertEqual(after_sat["duration_min"], 0)
        self.assertEqual(after_sat["tss_estimate"], 0)
        self.assertEqual(after_sat["zwo_file"], "",
                         "rest cell must clear zwo_file so dashboard renders REST")
        self.assertEqual(after_sat["zwo_name"], "")

        self.assertEqual(after_sun["session_type"], "rest",
                         "Sun must become rest after UPDATE with hours=0")
        self.assertEqual(after_sun["duration_min"], 0)
        self.assertEqual(after_sun["tss_estimate"], 0)
        self.assertEqual(after_sun["zwo_file"], "")
        self.assertEqual(after_sun["zwo_name"], "")

    def test_other_days_unchanged_when_only_weekend_unavailable(self):
        # Only mark Sat and Sun unavailable. Tue, Thu (training days) and
        # Mon, Wed, Fri (rest days) must keep their current values.
        body = {
            "availability": {
                self._sat.isoformat(): {"hours": 0, "type": "holiday"},
                self._sun.isoformat(): {"hours": 0, "type": "holiday"},
            }
        }
        r = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r.status_code, 200, r.text)

        after_plan = self._read_plan()
        # Weekday training sessions unchanged.
        for offset, s_type, dur, tss in (
            (1, "z2", 60, 45),       # Tue
            (3, "tempo", 60, 60),    # Thu
        ):
            d = self._monday + timedelta(days=offset)
            sess = self._session(after_plan, d)
            self.assertEqual(sess["session_type"], s_type,
                             f"{d} session_type must not change when only weekend marked unavailable")
            self.assertEqual(sess["duration_min"], dur)
            self.assertEqual(sess["tss_estimate"], tss)


if __name__ == "__main__":
    unittest.main()
