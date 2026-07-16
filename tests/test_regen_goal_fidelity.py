"""Regression — the regenerate/recalc cores must rebuild the FULL Goal.

Pre-fix, ``_regenerate_plan_dict`` (and the /api/plan/auto-recalc scheduler)
reconstructed the Goal INLINE, carrying plan_mode/template_id but DROPPING
distribution / custom_bands / events / block_periodization / start_date /
entry_mode / available_days / daily_max_hours. Consequences under test here:

(1) the regen core never re-pinned the active intensity-distribution model,
    so after an app restart (sticky process-global back at "polarized") a
    pyramidal/threshold/custom plan regenerated with polarized budgets;
(2) goal.events=[] starved _mark_race_days + _apply_secondary_event_tapers,
    so the B/C race row and its mini-taper/opener vanished from the rebuilt
    future weeks (a pending race row is NOT preserved by the §6.12 predicate
    — it is normally REBUILT from goal.events, which were gone).

Both fixed by routing through _goal_from_plan_dict + set_active_distribution.
Drives the real HTTP /api/plan/generate + /api/plan/regenerate path (the
FS1-regen-persistence pattern).
"""
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app as app_module
import training_planner as tp
from fastapi.testclient import TestClient


def _next_sunday(d: date) -> date:
    return d + timedelta(days=(6 - d.weekday()) % 7)


def _sessions_by_day(plan: dict) -> dict:
    return {s["day"]: s for w in plan.get("weeks", [])
            for s in w.get("sessions", [])}


def _race_rows(plan: dict) -> dict:
    return {d: s for d, s in _sessions_by_day(plan).items() if s.get("is_race")}


class TestRegenGoalFidelity(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="regenfid_"))
        self._patch = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch.start()
        # 3.4.3 hermetic-fs gate: export_plan_md writes plan_<date>.md via
        # tp.PLAN_DIR (NOT app._plan_dir) — unpatched, this suite was
        # writing into the machine's REAL profile plan dir on every gate
        # run (masked because that dir always existed; the sandbox home
        # surfaced it as ENOENT). Patch both roots to the same tmp.
        self._patch_tp = patch.object(tp, "PLAN_DIR", self.tmp)
        self._patch_tp.start()
        self.client = TestClient(app_module.app)
        # B race on a Sunday ~4-5 weeks out: always a trainable day (rest_days
        # =[0] Monday), well inside the plan and outside any taper span.
        self.b_date = _next_sunday(date.today() + timedelta(days=28))

    def tearDown(self):
        self._patch.stop()
        self._patch_tp.stop()
        # Never leak a pinned model into other suites.
        tp.set_active_distribution("polarized", None)

    def _mocks(self):
        stub = {"training": {"ctl": 45}, "wellness_7": []}
        return (
            patch.object(app_module, "cached",
                         side_effect=lambda k, fn, **kw: stub[k] if k in stub else fn()),
            patch.object(app_module.db, "query_activities", return_value=[]),
        )

    def _generate(self, **extra):
        body = dict(
            goal="ftp", plan_weeks=16, hours_per_week=10,
            max_weekday=2.0, max_weekend=4.0, rest_days=[0],
            daily_availability={str(d): (2.0 if d < 5 else 4.0) for d in range(1, 7)},
            events=[{"date": self.b_date.isoformat(), "priority": "B",
                     "name": "Kermis", "event_km": 80, "event_climb_m": 200,
                     "event_type": "crit"}],
        )
        body.update(extra)
        r = self.client.post("/api/plan/generate", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return json.loads((self.tmp / "current_plan.json").read_text())

    def test_pyramidal_and_b_race_survive_regenerate(self):
        m1, m2 = self._mocks()
        with m1, m2:
            gen = self._generate(distribution="pyramidal")
            self.assertEqual(gen["goal"].get("distribution"), "pyramidal")
            b_iso = self.b_date.isoformat()
            self.assertIn(b_iso, _race_rows(gen), "generate must place the B race row")

            # Simulate an app restart: the sticky process-global reverts.
            tp.set_active_distribution("polarized", None)

            r2 = self.client.post("/api/plan/regenerate")
            self.assertEqual(r2.status_code, 200, r2.text)
            # The regen core must re-pin the plan's model from its goal block
            # (pre-fix: budgets stayed polarized).
            self.assertEqual(tp.get_active_distribution(), "pyramidal",
                             "regenerate ran on polarized budgets")

            reg = json.loads((self.tmp / "current_plan.json").read_text())
            self.assertEqual(reg["goal"].get("distribution"), "pyramidal")
            rows = _race_rows(reg)
            self.assertIn(b_iso, rows,
                          "B race row vanished on regenerate (goal.events dropped)")
            self.assertEqual((rows[b_iso].get("race") or {}).get("priority"), "B")
            # SM3 mini-taper: the B-race eve carries the opener.
            eve = _sessions_by_day(reg).get((self.b_date - timedelta(days=1)).isoformat())
            self.assertIsNotNone(eve, "no session row on the B-race eve")
            self.assertTrue(eve.get("is_opener"),
                            "B-race eve opener lost on regenerate (mini-taper skipped)")

    def test_custom_bands_survive_regenerate(self):
        m1, m2 = self._mocks()
        with m1, m2:
            gen = self._generate(custom_bands={"tempo_ss": 70, "threshold": 20,
                                               "vo2": 5, "sprint": 5})
            self.assertEqual(gen["goal"].get("distribution"), "custom")

            tp.set_active_distribution("polarized", None)  # app restart

            r2 = self.client.post("/api/plan/regenerate")
            self.assertEqual(r2.status_code, 200, r2.text)
            self.assertEqual(tp.get_active_distribution(), "custom",
                             "regenerate lost the custom distribution")
            reg = json.loads((self.tmp / "current_plan.json").read_text())
            self.assertEqual(reg["goal"].get("custom_bands", {}).get("tempo_ss"), 70)

    def test_auto_recalc_repins_distribution(self):
        m1, m2 = self._mocks()
        with m1, m2:
            self._generate(distribution="pyramidal")
            # Age the plan past the 7-day freshness gate so the scheduler
            # takes the rebuild branch (which reconstructs the Goal).
            plan = json.loads((self.tmp / "current_plan.json").read_text())
            plan["recalc_date"] = (datetime.now() - timedelta(days=8)).isoformat()
            (self.tmp / "current_plan.json").write_text(json.dumps(plan))

            tp.set_active_distribution("polarized", None)  # app restart

            r = self.client.get("/api/plan/auto-recalc")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(tp.get_active_distribution(), "pyramidal",
                             "weekly recalc ran on polarized budgets")


if __name__ == "__main__":
    unittest.main()
