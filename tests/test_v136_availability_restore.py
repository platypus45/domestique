"""v1.3.6 BUG hot-fix — restoring availability re-adds sessions.

User flow that broke pre-fix:

1. Open availability calendar → set Sat=0h, Sun=0h → click UPDATE → Sat
   and Sun become REST (works post v1.3.5 ``f30405e7``).
2. User changes mind → set Sat=4h, Sun=4h → click UPDATE → Sat AND Sun
   stay REST. No new training is scheduled.

Asymmetric: reforecast un-rests nothing when hours goes from 0 to
positive.

Root cause: ``training_planner.reforecast()`` line 5025-5029 only
multiplies ``s.duration_min`` by ``scale``. When the session is already
``"rest"``, ``duration_min == 0`` so ``new_dur = 0`` and the session
stays REST. The ``hours > 0`` branch never re-derives ``session_type``,
re-fills ``duration_min`` from the user's hours, or restores the
description.

Fix: in the ``else`` branch (hours > 0), if ``session_type == "rest"``,
set ``session_type = "z2"`` (Layer-1 endurance default), set
``duration_min = round(hours * 60)`` (literal — not scale-multiplied),
recompute ``tss_estimate``, clear ``zwo_file/zwo_name`` so the
dashboard's match-on-render path picks a fresh workout, and write a
restoration description.

Wave 2 Grill #2 also forced a fix to ``current_mins <= 0``: pre-fix
this short-circuited the entire week, so a holiday week (every override
day at 0) could never be restored.
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

# 2026-07-06 fix (Monday flake): the suite anchored on the REAL current week
# and dodged "today is Monday" by re-anchoring to LAST week — which put
# Saturday in the past, so save-availability's past-day gate correctly
# refused and sessions_modified == 0 every Monday. Freeze the clock at a
# fixed WEDNESDAY of the pin week instead (mid-week = the geometry the suite
# always intended: Monday past, Sat/Sun future).
_FROZEN_TODAY = PLANNER_PIN_ANCHOR + timedelta(days=2)  # Wed 2026-01-07


class _FrozenWed(date):
    @classmethod
    def today(cls):
        return cls(_FROZEN_TODAY.year, _FROZEN_TODAY.month, _FROZEN_TODAY.day)


class _FrozenSun(date):
    """For the Sunday-boundary test (pw.end == today, Sun 2026-01-11)."""
    @classmethod
    def today(cls):
        return cls(2026, 1, 11)


def _mk_plan_dict(monday: date, weeks_count: int = 2,
                  weekend_already_rest: bool = False) -> dict:
    """Plan with optional weekend-already-rest setup for restore tests."""
    weeks = []
    phases = ["base", "build1"]
    for w_idx in range(weeks_count):
        wstart = monday + timedelta(weeks=w_idx)
        sessions = []
        # Mon REST, Tue Z2, Wed REST, Thu TEMPO, Fri REST, Sat LONG_Z2, Sun Z2
        types_for_week = ["rest", "z2", "rest", "tempo", "rest", "long_z2", "z2"]
        durations = [0, 60, 0, 60, 0, 120, 90]
        tss = [0, 45, 0, 60, 0, 90, 65]
        if weekend_already_rest:
            # Pre-set Sat/Sun to REST as if a prior 0h save already
            # zeroed them. duration_min=0, zwo cleared.
            types_for_week = ["rest", "z2", "rest", "tempo", "rest", "rest", "rest"]
            durations = [0, 60, 0, 60, 0, 0, 0]
            tss = [0, 45, 0, 60, 0, 0, 0]
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
        "goal": {"type": "general", "hours_per_week": 8.0,
                 "rest_days": [0, 2, 4]},
        "phases": [],
        "weeks": weeks,
        "generated": "2026-04-19T00:00:00",
    }


class _AvailRestoreBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

        # Frozen clock (2026-07-06): anchor on the PIN week's Monday with
        # "today" frozen mid-week (Wed) so the pw.end < today gate is
        # exercised but doesn't fire — deterministic on every real weekday.
        self._patch_app_date = patch.object(app_module, "date", _FrozenWed)
        self._patch_tp_date = patch.object(tp, "date", _FrozenWed)
        self._patch_app_date.start()
        self._patch_tp_date.start()
        self._monday = PLANNER_PIN_ANCHOR
        self._sat = self._monday + timedelta(days=5)
        self._sun = self._monday + timedelta(days=6)

        # Default plan has Sat/Sun pre-rested so the restore branch
        # has something to restore.
        self._plan = _mk_plan_dict(self._monday, weeks_count=2,
                                   weekend_already_rest=True)
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


class TestRestRestoredToZ2(_AvailRestoreBase):
    """v1.3.6 — REST day with hours>0 restores to z2."""

    def test_rest_day_restored_to_z2_when_hours_positive(self):
        # Pre-condition: Sat is REST/0min (set by setUp).
        before_plan = self._read_plan()
        before_sat = self._session(before_plan, self._sat)
        self.assertEqual(before_sat["session_type"], "rest")
        self.assertEqual(before_sat["duration_min"], 0)

        body = {
            "availability": {
                self._sat.isoformat(): {"hours": 4, "type": "available"},
            }
        }
        r = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertGreaterEqual(r.json().get("sessions_modified", 0), 1)

        after_plan = self._read_plan()
        after_sat = self._session(after_plan, self._sat)
        self.assertEqual(after_sat["session_type"], "z2",
                         "Sat must restore to z2 when hours raised from 0")
        self.assertEqual(after_sat["duration_min"], 240,
                         "duration_min must reflect 4h × 60min")
        self.assertGreater(after_sat["tss_estimate"], 0,
                           "tss_estimate must be positive after restore")
        self.assertEqual(after_sat["zwo_file"], "",
                         "zwo_file cleared so renderer re-matches")
        self.assertEqual(after_sat["zwo_name"], "")


class TestRoundTripRestThenRestore(_AvailRestoreBase):
    """v1.3.6 — round trip: 0h then 4h restores."""

    def setUp(self):
        super().setUp()
        # Override: start with weekend NOT rested (the v1.3.5 starting
        # point). We'll first set hours=0 then hours=4.
        self._plan = _mk_plan_dict(self._monday, weeks_count=2,
                                   weekend_already_rest=False)
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))

    def test_round_trip_zero_then_restore(self):
        # Step 1: zero out Sat.
        body0 = {
            "availability": {
                self._sat.isoformat(): {"hours": 0, "type": "holiday"},
            }
        }
        r0 = self.client.post("/api/plan/save-availability", json=body0)
        self.assertEqual(r0.status_code, 200, r0.text)
        plan0 = self._read_plan()
        sat0 = self._session(plan0, self._sat)
        self.assertEqual(sat0["session_type"], "rest")
        self.assertEqual(sat0["duration_min"], 0)

        # Step 2: restore Sat to 4h.
        body1 = {
            "availability": {
                self._sat.isoformat(): {"hours": 4, "type": "available"},
            }
        }
        r1 = self.client.post("/api/plan/save-availability", json=body1)
        self.assertEqual(r1.status_code, 200, r1.text)
        plan1 = self._read_plan()
        sat1 = self._session(plan1, self._sat)
        self.assertEqual(sat1["session_type"], "z2",
                         "round-trip restore: Sat must un-rest")
        self.assertEqual(sat1["duration_min"], 240)


class TestPartialRestore(_AvailRestoreBase):
    """v1.3.6 — restoring one day leaves the other rested."""

    def test_partial_restore_one_of_two_weekend_days(self):
        # Sat + Sun pre-rested (setUp). Restore Sat only.
        body = {
            "availability": {
                self._sat.isoformat(): {"hours": 4, "type": "available"},
                self._sun.isoformat(): {"hours": 0, "type": "holiday"},
            }
        }
        r = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r.status_code, 200, r.text)

        after_plan = self._read_plan()
        after_sat = self._session(after_plan, self._sat)
        after_sun = self._session(after_plan, self._sun)

        self.assertEqual(after_sat["session_type"], "z2")
        self.assertEqual(after_sat["duration_min"], 240)
        self.assertEqual(after_sun["session_type"], "rest",
                         "Sun must remain REST when override is 0")
        self.assertEqual(after_sun["duration_min"], 0)


class TestSundayBoundary(_AvailRestoreBase):
    """v1.3.6 — pw.end < today gate must not fire when today == pw.end."""

    def test_today_is_sunday_at_pw_end(self):
        # 2026-07-06: was skip-unless-real-Sunday (so it effectively never
        # ran); now freezes "today" to the pin week's Sunday = pw.end.
        self._patch_app_date.stop(); self._patch_tp_date.stop()
        self._patch_app_date = patch.object(app_module, "date", _FrozenSun)
        self._patch_tp_date = patch.object(tp, "date", _FrozenSun)
        self._patch_app_date.start(); self._patch_tp_date.start()
        body = {
            "availability": {
                self._sun.isoformat(): {"hours": 4, "type": "available"},
            }
        }
        r = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        after_plan = self._read_plan()
        after_sun = self._session(after_plan, self._sun)
        self.assertEqual(after_sun["session_type"], "z2")


class TestNonRestSessionScalingUnchanged(_AvailRestoreBase):
    """v1.3.6 — non-rest sessions still scale (regression-guard)."""

    def setUp(self):
        super().setUp()
        # Plan WITHOUT weekend pre-rested so Sat starts as long_z2 120min.
        self._plan = _mk_plan_dict(self._monday, weeks_count=2,
                                   weekend_already_rest=False)
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))

    def test_non_rest_session_scaling_unchanged(self):
        # Sat starts long_z2 120min. Set hours=2.5h. This is the EXISTING
        # branch: scaling. Sun also stays at hours_default in the dict so
        # the test only mutates Sat.
        body = {
            "availability": {
                self._sat.isoformat(): {"hours": 2.5, "type": "available"},
            }
        }
        r = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        after_plan = self._read_plan()
        after_sat = self._session(after_plan, self._sat)
        # Sat scales (single-day override means scale = 150/120 = 1.25);
        # session_type remains long_z2.
        self.assertEqual(after_sat["session_type"], "long_z2",
                         "Non-rest scaling must not change session_type")
        self.assertEqual(after_sat["duration_min"], 150,
                         "Sat scales to 2.5h = 150min")


class TestPartialRestoreOnlyOneKeyInDict(_AvailRestoreBase):
    """v1.3.6 — keys missing from override dict are not touched."""

    def test_partial_restore_only_one_day_missing(self):
        # Sat + Sun pre-rested. Save just Sat=4 in the override dict.
        body = {
            "availability": {
                self._sat.isoformat(): {"hours": 4, "type": "available"},
            }
        }
        r = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        after_plan = self._read_plan()
        after_sat = self._session(after_plan, self._sat)
        after_sun = self._session(after_plan, self._sun)
        self.assertEqual(after_sat["session_type"], "z2")
        self.assertEqual(after_sun["session_type"], "rest",
                         "Sun missing from dict → not touched, stays REST")


class TestFullWeekRestThenRestore(_AvailRestoreBase):
    """v1.3.6 Wave 2 Grill #2 — current_mins=0 must permit per-day restore."""

    def setUp(self):
        super().setUp()
        # ALL DAYS REST (holiday week scenario).
        plan = _mk_plan_dict(self._monday, weeks_count=2,
                             weekend_already_rest=False)
        for w in plan["weeks"]:
            for s in w["sessions"]:
                s["session_type"] = "rest"
                s["duration_min"] = 0
                s["tss_estimate"] = 0
                s["zwo_file"] = ""
                s["zwo_name"] = ""
                s["description"] = "Rest"
        # ALSO pre-populate availability dict so reforecast can
        # see the prior 0h state for every weekday.
        avail = {}
        for w in plan["weeks"]:
            for s in w["sessions"]:
                avail[s["day"]] = {"hours": 0, "type": "holiday"}
        plan["availability"] = avail
        self._plan = plan
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))

    def test_full_week_was_rest_then_one_day_restored(self):
        # Restore Tue=4h while every other day stays at 0h.
        tue = self._monday + timedelta(days=1)
        avail = {
            d.isoformat(): {"hours": 0, "type": "holiday"}
            for d in (self._monday + timedelta(days=i) for i in range(7))
        }
        avail[tue.isoformat()] = {"hours": 4, "type": "available"}
        # Cover next week too (just keep all rest).
        for off in range(7):
            d = self._monday + timedelta(days=7 + off)
            avail[d.isoformat()] = {"hours": 0, "type": "holiday"}

        body = {"availability": avail}
        r = self.client.post("/api/plan/save-availability", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        after_plan = self._read_plan()
        after_tue = self._session(after_plan, tue)
        self.assertEqual(after_tue["session_type"], "z2",
                         "Tue must restore to z2 even when current_mins=0 across the week")
        self.assertEqual(after_tue["duration_min"], 240)


if __name__ == "__main__":
    unittest.main()
