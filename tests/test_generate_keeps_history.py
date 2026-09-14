"""A Generate keeps what the rider was prescribed before it (week-view contract
P4, A5).

Regenerate, recalculate and extend keep a plan's elapsed weeks; Generate wrote
a fresh plan over the file, so the calendar's history rows and the Last-week
card lost every week before the new plan: "no plan on record" for a week the
rider had been given, and ridden against. The replaced plan's elapsed rows are
kept under ``replaced_weeks``, which only the readers (src/week_view.py, the
calendar) read: inside ``weeks`` they would be a later Regenerate's
``past_weeks``, shifting its week numbers and its 3:1 rhythm onto rows of a
goal the rider no longer has."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import cache
import clock

TODAY = date(2026, 9, 10)                        # a Thursday
THIS_MONDAY = TODAY - timedelta(days=TODAY.weekday())
LAST_MONDAY = THIS_MONDAY - timedelta(days=7)


def _week(num, monday, tss_by_offset, target):
    sessions = []
    for off in range(7):
        d = monday + timedelta(days=off)
        tss = tss_by_offset.get(off, 0)
        sessions.append({
            "day": d.isoformat(), "day_name": d.strftime("%a"),
            "session_type": "z2" if tss else "rest", "duration_min": tss and 60,
            "tss_estimate": tss, "description": "", "zwo_file": "", "zwo_name": "",
            "status": "pending"})
    return {"week_num": num, "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(), "phase": "base",
            "tss_target": target, "is_stepback": False, "sessions": sessions}


def _old_plan():
    return {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
        "phases": [],
        "weeks": [
            _week(1, LAST_MONDAY, {1: 80, 2: 60, 3: 70, 5: 122}, 340.0),   # 332 planned
            _week(2, THIS_MONDAY, {1: 50, 2: 55, 3: 60, 5: 100}, 280.0),   # Tue, Wed ridden before today
            _week(3, THIS_MONDAY + timedelta(days=7), {1: 90, 5: 150}, 300.0),
        ],
        "generated": f"{LAST_MONDAY.isoformat()}T09:00:00",
    }


class GenerateKeepsHistory(unittest.TestCase):
    def setUp(self):
        clock.freeze(TODAY)
        self.tmp = Path(tempfile.mkdtemp(prefix="gen_history_"))
        self._patches = [
            patch.object(app_module, "_plan_dir", return_value=self.tmp),
            patch.object(app_module, "get_today_metrics", lambda: {"ctl": 50.0, "tsb": 0.0}),
        ]
        for p in self._patches:
            p.start()
        self.pp = self.tmp / "current_plan.json"
        self.pp.write_text(json.dumps(_old_plan()))
        cache.clear_cache()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        clock.unfreeze()
        cache.clear_cache()

    def _generate(self):
        r = self.client.post("/api/plan/generate", json={
            "goal": "general", "weeks": 6, "max_weekday": 1.5, "max_weekend": 3.0,
            "rest_days": [0]})
        self.assertEqual(r.status_code, 200, r.text[:300])
        cache.clear_cache()
        return json.loads(self.pp.read_text())

    def _get(self, path):
        r = self.client.get(path)
        self.assertEqual(r.status_code, 200, r.text[:300])
        return r.json()

    def test_elapsed_rows_survive_generate(self):
        plan = self._generate()
        new_start = min(w["start"] for w in plan["weeks"])
        cutoff = min(new_start, TODAY.isoformat())
        kept = plan.get("replaced_weeks") or []
        days = [s["day"] for w in kept for s in w["sessions"]]
        self.assertIn(LAST_MONDAY.isoformat(), [w["start"] for w in kept])
        self.assertTrue(days and all(d < cutoff for d in days), (cutoff, days))
        self.assertEqual(len(days), len(set(days)))
        self.assertTrue(all(w.get("carried_from_generate") == TODAY.isoformat() for w in kept))
        # Nothing of the replaced plan's future is kept.
        self.assertNotIn((THIS_MONDAY + timedelta(days=7)).isoformat(), days)
        # And a replaced row is never a Regenerate's past week.
        self.assertFalse(any(w.get("carried_from_generate") for w in plan["weeks"]))

    def test_the_readers_see_what_was_prescribed(self):
        self._generate()
        cal = self._get("/api/calendar")
        row = next(w for w in cal["weeks"] if w["start_date"] == LAST_MONDAY.isoformat())
        self.assertEqual(row["planned_tss"], 332.0)
        before = next(w for w in cal["weeks"]
                      if w["start_date"] == (LAST_MONDAY - timedelta(days=7)).isoformat())
        self.assertIsNone(before["planned_tss"])
        summary = self._get("/api/week-summary?week_offset=-1")
        self.assertEqual(summary["tss_target"], 332.0)
        weekly = self._get("/api/weekly-plan?week_offset=-1")
        self.assertTrue(weekly["on_record"])
        self.assertEqual(weekly["planned_tss"], 332.0)

    def test_this_weeks_budget_is_the_new_plans(self):
        """A mid-week Generate leaves this week covered by two rows: the
        replaced plan's Monday..Wednesday and the new plan's stub. The budget
        is the new plan's."""
        plan = self._generate()
        stub = next(w for w in plan["weeks"]
                    if w["start"] <= TODAY.isoformat() <= w["end"])
        weekly = self._get("/api/weekly-plan")
        self.assertEqual(weekly["budget"], float(stub["tss_target"]))
        self.assertEqual(weekly["week_num"], stub["week_num"])
        # The calendar's row for the week agrees: the new plan's phase and
        # flag, not the replaced row's (the second review, S4). Make the
        # replaced row look like a recovery week of another phase, with files.
        plan = json.loads(self.pp.read_text())
        for w in plan["replaced_weeks"]:
            w.update(phase="oldphase", is_stepback=True)
            for x in w["sessions"]:
                x["zwo_file"] = x["zwo_file"] or "endurance_steady_74pct_70min.zwo"
        self.pp.write_text(json.dumps(plan))
        cache.clear_cache()
        row = next(w for w in self._get("/api/calendar")["weeks"] if w["is_current"])
        self.assertEqual((row["phase"], row["is_stepback"]), (stub["phase"], bool(stub["is_stepback"])))

    def test_a_second_generate_keeps_the_first_history(self):
        self._generate()
        plan = self._generate()
        kept = plan.get("replaced_weeks") or []
        days = [s["day"] for w in kept for s in w["sessions"]]
        self.assertEqual(len(days), len(set(days)))
        last = [w for w in kept if w["start"] == LAST_MONDAY.isoformat()]
        self.assertEqual(len(last), 1)
        self.assertEqual(sum(s["tss_estimate"] for s in last[0]["sessions"]), 332)

    def test_regenerate_keeps_it(self):
        """Every other writer copies the plan dict; Regenerate is the one a
        rider presses next."""
        before = self._generate().get("replaced_weeks")
        r = self.client.post("/api/plan/regenerate", json={})
        self.assertEqual(r.status_code, 200, r.text[:300])
        after = json.loads(self.pp.read_text())
        self.assertIn("regenerated", after)          # it did write the plan
        self.assertTrue(before)
        self.assertEqual(after.get("replaced_weeks"), before)

if __name__ == "__main__":
    unittest.main()
