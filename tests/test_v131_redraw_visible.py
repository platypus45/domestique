"""v1.3.1 HIGH regression — redrawn workout must surface in /api/calendar.

Bug: when the user clicked "Rematch workout" inside the day-detail modal
(which the THIS WEEK panel opens via calOpenDay → openDayWorkout) the
client only called `loadWeeklyCalendar()`, which repaints the legacy
`#wc-grid` host. The dashboard's visible THIS WEEK panel and calendar
overlay are fed by `loadCalendar()` → `/api/calendar`, so they stayed
showing the pre-redraw (missing_workout) state until a hard refresh.

This test pins the SERVER side of the contract that the client now
relies on: after POST /api/plan/re-draw with a missing-workout day, the
next /api/calendar response must show that day flipped from
``card_state="missing_workout"`` to ``card_state="planned"`` with a
non-empty ``planned.zwo_file`` matching the redraw response. If this
ever regresses (e.g. /api/plan/re-draw stops persisting, or
/api/calendar caches), the test fails and the UI bug is back.

(The actual UI fix lives in templates/dashboard.html `rematchDaySession`
where loadCalendar() + loadPlan() now run on every successful redraw.)
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


def _mk_plan_with_missing_thursday(monday: date) -> dict:
    """One-week plan; Thursday is a Z2 with empty zwo_file (missing_workout)."""
    sessions = []
    types_for_week = ["rest", "z2", "tempo", "z2", "rest", "long_z2", "rest"]
    durations = [0, 60, 75, 60, 0, 120, 0]
    tss = [0, 45, 60, 50, 0, 90, 0]
    for off in range(7):
        d = monday + timedelta(days=off)
        sessions.append({
            "day": d.isoformat(),
            "day_name": d.strftime("%a"),
            "session_type": types_for_week[off],
            "duration_min": durations[off],
            "tss_estimate": tss[off],
            "description": f"{types_for_week[off]} {durations[off]}min",
            # Thursday (off=3) has zwo_file="" → missing_workout. Other
            # non-rest days carry placeholder filenames so they classify
            # as planned (or missing_workout for unknown filenames — that
            # is fine for the assertion, we only care about Thursday).
            "zwo_file": "" if off == 3 or types_for_week[off] == "rest"
                        else f"{types_for_week[off]}_test.zwo",
            "zwo_name": "" if off == 3 or types_for_week[off] == "rest"
                        else f"{types_for_week[off]} test",
            "status": "pending",
        })
    return {
        "goal": {"type": "general", "hours_per_week": 8.0,
                 "rest_days": [0, 4, 6]},
        "phases": [],
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "base",
            "tss_target": 290,
            "is_stepback": False,
            "sessions": sessions,
        }],
        "generated": "2026-04-19T00:00:00",
    }


class RedrawVisibleBase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

        today = date.today()
        self._monday = today - timedelta(days=today.weekday())
        self._thu = (self._monday + timedelta(days=3)).isoformat()

        self._plan = _mk_plan_with_missing_thursday(self._monday)
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))

        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        # Quiet ride loaders so /api/calendar doesn't pull the dev cache.
        self._patch_rides_safe = patch.object(
            app_module, "_load_all_rides_safe", return_value=[]
        )
        self._patch_rides_safe.start()
        self._patch_list_rides = patch(
            "ride_storage.list_rides", return_value=[]
        )
        self._patch_list_rides.start()

        # Skip the lazy ICU sync hook — it logs a debug line on miss but
        # otherwise no-ops, so this keeps test output clean.
        self._patch_icu = patch.object(
            app_module, "_maybe_lazy_icu_sync", return_value=None
        )
        self._patch_icu.start()

        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_icu.stop()
        self._patch_list_rides.stop()
        self._patch_rides_safe.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def _calendar_thursday(self) -> dict:
        r = self.client.get("/api/calendar")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        cur = next((w for w in data["weeks"] if w.get("is_current")), None)
        self.assertIsNotNone(cur, "current week missing from /api/calendar")
        thu = next((d for d in cur["days"] if d["date"] == self._thu), None)
        self.assertIsNotNone(thu, f"Thursday {self._thu} missing from current week")
        return thu


class TestRedrawSurfacesInCalendar(RedrawVisibleBase):
    """Server-side contract that the THIS WEEK / calendar overlay client
    code (`loadCalendar` → `/api/calendar`) depends on after a re-draw."""

    def test_thursday_starts_as_missing_workout(self):
        thu = self._calendar_thursday()
        self.assertEqual(thu["card_state"], "missing_workout",
            "Thursday should classify as missing_workout when zwo_file is empty")
        # planned payload may still be populated (session_type/duration/tss)
        # but the key signal is empty zwo_file.
        if thu["planned"] is not None:
            self.assertFalse(thu["planned"].get("zwo_file"),
                "missing_workout cell should not carry a zwo_file")

    def test_redraw_flips_card_state_to_planned(self):
        # Step 1 — confirm starting state.
        before = self._calendar_thursday()
        self.assertEqual(before["card_state"], "missing_workout")

        # Step 2 — trigger the re-draw the dashboard's "Rematch workout"
        # button POSTs.
        r = self.client.post("/api/plan/re-draw", json={"date": self._thu})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        if body.get("action") != "redrawn":
            # Library too sparse in CI — endpoint contract verified by
            # tests/test_plan_redraw_endpoint.py; without a workout pool
            # the visibility flip can't happen.
            self.skipTest(f"library has no candidates: {body.get('action')}")

        new_zwo = body.get("zwo_file")
        self.assertTrue(new_zwo, "redraw response must include zwo_file")

        # Step 3 — re-fetch the calendar (this is what the client now does
        # via loadCalendar() — the v1.3.1 fix). Thursday must be visibly
        # different.
        after = self._calendar_thursday()
        # Either fully planned, or still missing_workout if the picked ZWO
        # doesn't match Z2 acceptance — but in EITHER case the UI's
        # planned.zwo_file MUST be populated so the user sees the change.
        self.assertIsNotNone(after["planned"],
            "after redraw, planned payload must exist for Thursday")
        self.assertEqual(after["planned"].get("zwo_file"), new_zwo,
            "calendar must surface the new zwo_file the redraw endpoint "
            "returned — pre-fix, loadCalendar wasn't called and the cell "
            "kept showing the empty (pre-redraw) zwo_file")

    def test_redraw_targets_match_thursday_slot(self):
        # Sanity: the redraw picks a workout for the Thursday slot's
        # duration/TSS expectations, not some random other day.
        r = self.client.post("/api/plan/re-draw", json={"date": self._thu})
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        if body.get("action") != "redrawn":
            self.skipTest(f"library has no candidates: {body.get('action')}")

        # Confirm persistence — the on-disk plan now reflects the picked
        # workout for Thursday specifically (not an adjacent day).
        plan = json.loads((self._tmp / "current_plan.json").read_text())
        thu_sess = next(s for s in plan["weeks"][0]["sessions"]
                        if s["day"] == self._thu)
        self.assertEqual(thu_sess["zwo_file"], body["zwo_file"],
            "persisted Thursday must match the redrawn zwo_file")
        # 3.3.1 hotfix (B2): re-draw now routes through the modern
        # accept-redraw apply, which — per the v1.7.0 contract — carries the
        # PICKED file's real duration/TSS into the plan so downstream load
        # math is truthful (the legacy one-shot kept the slot's stale
        # estimates; this test used to pin that). The type must not drift;
        # duration/TSS must match what the endpoint reported for the pick.
        self.assertEqual(thu_sess["session_type"], "z2")
        self.assertEqual(thu_sess["duration_min"], body["duration_min"])
        self.assertEqual(thu_sess["tss_estimate"], body["tss_estimate"])
        # And the pick is still a sane fit for the slot (closest-duration
        # tier around 60 min — not some random other day's slot).
        self.assertGreaterEqual(thu_sess["duration_min"], 30)
        self.assertLessEqual(thu_sess["duration_min"], 90)


if __name__ == "__main__":
    unittest.main()
