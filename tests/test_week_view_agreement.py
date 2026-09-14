"""Every card that shows the week reads one object (notes/week-view-contract.md,
A1, A2, P4). These are the contract's instruments: each fails on today's code
for the reason its docstring names, as a strict expected failure, and turns
green when Wave 1 lands. No test in the suite compared two endpoints on the
same week before this file (the audit's test lens, 2026-09-14).

The fixture is the owner's own Monday record of 2026-09-14: a session labelled
sweetspot serving threshold_3x15min-5min_100pct_76min.zwo, in a plan whose
first week starts on a weekday (a Monday-pinned fixture hides half of the
disagreements, notes/review/http.md section 4)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
import app as app_module  # noqa: E402
import cache  # noqa: E402
import ride_storage  # noqa: E402
import training_planner as tp  # noqa: E402

TODAY = date(2026, 9, 10)          # a Thursday; ISO week 2026-09-07..13
WEEK_TARGET = 234.0


class _FrozenDate(date):
    @classmethod
    def today(cls):
        return cls(TODAY.year, TODAY.month, TODAY.day)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        d = cls(TODAY.year, TODAY.month, TODAY.day, 12, 0, 0)
        return d if tz is None else d.replace(tzinfo=timezone.utc).astimezone(tz)


def _session(day, session_type, minutes, tss, zwo_file, zwo_name):
    return {"day": day.isoformat(), "day_name": day.strftime("%a"),
            "session_type": session_type, "duration_min": minutes,
            "tss_estimate": tss, "description": f"{session_type} ({minutes}min)",
            "zwo_file": zwo_file, "zwo_name": zwo_name, "status": "pending"}


def _rest(day):
    return _session(day, "rest", 0, 0, "", "")


def _plan() -> dict:
    thu = TODAY
    w1 = [
        # The owner's record: labelled sweetspot, serving a threshold file.
        _session(thu, "sweetspot", 79, 105,
                 "threshold_3x15min-5min_100pct_76min.zwo", "Threshold 3x15min (76min)"),
        _session(thu + timedelta(days=1), "z2", 73, 55,
                 "endurance_steady_74pct_70min.zwo", "Endurance Steady (70min)"),
        _rest(thu + timedelta(days=2)),
        _session(thu + timedelta(days=3), "long_z2", 150, 74,
                 "endurance_steady_68pct_150min.zwo", "Endurance Steady (150min)"),
    ]
    mon2 = thu + timedelta(days=4)
    w2 = [_rest(mon2)] + [
        _session(mon2 + timedelta(days=i), "z2", 70, 50,
                 "endurance_steady_74pct_70min.zwo", "Endurance Steady (70min)")
        for i in range(1, 7)]
    return {
        "goal": {"type": "continuous", "hours_per_week": 8.0, "rest_days": [5]},
        "phases": [],
        "weeks": [
            {"week_num": 1, "start": thu.isoformat(), "end": (thu + timedelta(days=3)).isoformat(),
             "phase": "continuous", "tss_target": WEEK_TARGET, "is_stepback": False, "sessions": w1},
            {"week_num": 2, "start": mon2.isoformat(), "end": (mon2 + timedelta(days=6)).isoformat(),
             "phase": "continuous", "tss_target": 300.0, "is_stepback": False, "sessions": w2},
        ],
        "generated": f"{TODAY.isoformat()}T09:00:00",
    }


class WeekViewAgreement(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self._patches = [
            patch.object(app_module, "date", _FrozenDate),
            patch.object(app_module, "datetime", _FrozenDateTime),
            patch.object(tp, "date", _FrozenDate),
            patch.object(app_module, "get_today_metrics", lambda: {"ctl": 50.0}),
            patch.object(app_module, "_rides_fit_dir", return_value=tmp / "fit"),
            patch.object(ride_storage, "_icu_rides_dir", return_value=tmp / "icu"),
            patch.object(ride_storage, "_fit_rides_dir", return_value=tmp / "fit"),
            patch("ride_storage.list_rides", return_value=[]),
        ]
        for p in self._patches:
            p.start()
        (tmp / "fit").mkdir()
        (tmp / "icu").mkdir()
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = tmp
        (tmp / "current_plan.json").write_text(json.dumps(_plan()))
        cache.clear_cache()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        tp.PLAN_DIR = self._orig_plan_dir
        for p in reversed(self._patches):
            p.stop()
        cache.clear_cache()
        self._tmpdir.cleanup()

    def _get(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, f"{path}: {r.text[:200]}")
        return r.json()

    @staticmethod
    def _stored_planned(plan, week_start):
        week_end = week_start + timedelta(days=6)
        return sum(float(s.get("tss_estimate") or 0)
                   for w in plan["weeks"] for s in w["sessions"]
                   if week_start.isoformat() <= s["day"] <= week_end.isoformat()
                   and s.get("session_type") != "rest")

    def test_planned_load_for_this_week_is_one_number(self):
        """A1. Until week_view (2026-09-14) /api/weekly-plan and
        /api/week-summary served a target a second planner regenerated on
        every read (HTTP-2), not the stored week's, so the home badge, the
        rollup and the calendar disagreed: 441 against the stored 234."""
        monday = TODAY - timedelta(days=TODAY.weekday())
        stored = self._stored_planned(self._get("/api/plan")["plan_json"], monday)
        self.assertEqual(stored, WEEK_TARGET)
        weekly = self._get("/api/weekly-plan")
        summary = self._get("/api/week-summary")
        cal = self._get("/api/calendar")
        current = next(w for w in cal["weeks"] if w.get("is_current"))
        answers = {"stored": stored, "weekly-plan": float(weekly.get("tss_target") or 0),
                   "week-summary": float(summary.get("tss_target") or 0),
                   "calendar": float(current.get("planned_tss") or 0)}
        self.assertEqual(len(set(answers.values())), 1, answers)

    def test_todays_label_is_the_served_file(self):
        """A2 / P9. The Today card reads session_type, the This Week cell reads
        zwo_name, and the stored record carries both, disagreeing: a
        sweetspot label on a threshold file. One identity: the served file."""
        today = self._get("/api/today-session")
        planned = today.get("planned") or {}
        lib = {r["File"]: r for r in tp.load_workout_library()}
        row = lib[planned.get("zwo_file") or "threshold_3x15min-5min_100pct_76min.zwo"]
        self.assertEqual(planned.get("session_type"), tp._session_type_from_row(row))

    def test_last_week_has_one_planned_answer(self):
        """P4. Last ISO week has no plan on record. The calendar's history row
        says planned 0; /api/week-summary?week_offset=-1 invents a target from
        the second planner, which ignores its own offset. Both must say "no
        plan on record", and say it the same way."""
        summary = self._get("/api/week-summary?week_offset=-1")
        cal = self._get("/api/calendar")
        last_monday = TODAY - timedelta(days=TODAY.weekday() + 7)
        row = next(w for w in cal["weeks"] if w.get("start_date", w.get("start")) == last_monday.isoformat())
        self.assertIsNone(summary.get("tss_target"))
        self.assertIsNone(row.get("planned_tss"))

    def test_the_calendar_cell_is_the_served_file(self):
        """A2 on the calendar. Its cell sent the stored label, and its
        content_class read a key no library row carries, so it was "" on
        every cell (the audit's readers report, section 3)."""
        cal = self._get("/api/calendar")
        cell = next(d for w in cal["weeks"] for d in w["days"]
                    if d["date"] == TODAY.isoformat())["planned"]
        lib = {r["File"]: r for r in tp.load_workout_library()}
        row = lib[cell["zwo_file"]]
        self.assertEqual(cell["session_type"], tp._session_type_from_row(row))
        self.assertEqual(cell["content_class"], tp._content_class_for_row(row))
        self.assertTrue(cell["content_class"])

    def test_a_week_with_no_plan_is_not_graded(self):
        """P4. Last week has no plan on record, and the rider rode hard in
        it. Nothing was prescribed, so there is no adherence to grade and no
        overreach against a plan: not 0 %, not "40 min above Z2 with 0 min
        planned"."""
        last_wed = TODAY - timedelta(days=TODAY.weekday() + 5)
        ride = {"date": last_wed.isoformat(), "tss": 200, "duration_min": 180,
                "name": "Hard ride", "sport": "Ride", "avg_hr": 250,
                "time_in_zone": {"z1": 0, "z2": 60, "z3": 80, "z4": 30, "z5": 10}}
        with patch.object(app_module, "api_activities", return_value=[ride]):
            summary = self._get("/api/week-summary?week_offset=-1")
        self.assertEqual(summary["tss_done"], 200)
        self.assertIsNone(summary["tss_target"])
        self.assertIsNone(summary["tss_adherence_pct"])
        self.assertFalse(summary["overreach"], summary.get("overreach_reasons"))

    def test_planned_band_minutes_are_one_answer(self):
        """A6. /api/week-summary banded planned minutes by the session's label
        through its own table; /api/weekly-plan bands them by the served
        file's zone shares. The rollup and the home bars read one answer."""
        weekly = self._get("/api/weekly-plan")
        summary = self._get("/api/week-summary")
        self.assertEqual(summary["exposure_minutes_planned"], weekly["exposure_minutes_planned"])
        self.assertGreater(sum(weekly["exposure_minutes_planned"].values()), 0)

    def test_the_fixture_is_the_owners_record(self):
        """A control that must pass today: the stored record really carries
        both identities, so the two tests above fail for the stated reason."""
        plan = self._get("/api/plan")["plan_json"]
        thu = next(s for w in plan["weeks"] for s in w["sessions"] if s["day"] == TODAY.isoformat())
        self.assertEqual(thu["session_type"], "sweetspot")
        self.assertTrue(thu["zwo_file"].startswith("threshold_"))


if __name__ == "__main__":
    unittest.main()
