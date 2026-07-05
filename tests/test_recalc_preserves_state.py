"""Regression — the weekly auto-recalc tier must not lose plan state.

Two pre-existing gaps (found while fixing the lossy Goal reconstruction):

(1) ENGINE: ``recalculate_plan`` rebuilt future weeks but never re-ran the
    race/taper passes (_enforce_event_taper_eve, _apply_secondary_event_tapers,
    _apply_race_week_shape, _mark_race_days) that generate_plan /
    regenerate_from_today / refit_remaining_week all run — so a weekly recalc
    dropped every B/C race row + mini-taper/opener from the rebuilt weeks. It
    also never gathered §6.12 preserved sessions from the old future weeks
    (its swap-skip guards were dead code), so user_moved / done / dismissed
    sessions on future calendar cells were silently re-prescribed.

(2) ENDPOINT: /api/plan/auto-recalc round-tripped sessions through an inline
    6-field load + 8-field save instead of the canonical
    _planned_session_from_json/_planned_session_to_json helpers, wiping
    is_race/race/status/user_moved/dismissed_at/completion_matches (and, on
    load, even zwo_file) from ALL weeks — past ones included — on every
    weekly recalc write. The fixed-key save also dropped unknown top-level
    plan keys.

The endpoint test drives the real HTTP path hermetically (the
test_regen_goal_fidelity pattern: patched _plan_dir + cached + db, aged
recalc_date). It deliberately relies only on THIS fix — B-race-row rebuild
through the endpoint additionally needs the Goal-reconstruction fix
(goal.events), which is covered engine-level here and endpoint-level in
test_regen_goal_fidelity.py.
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


def _sessions_by_day(weeks) -> dict:
    """date -> PlannedSession over a list of PlannedWeek DTOs."""
    return {s.day: s for w in weeks for s in w.sessions}


def _json_sessions_by_day(plan: dict) -> dict:
    """iso-date -> session dict over a persisted plan's weeks."""
    return {s["day"]: s for w in plan.get("weeks", [])
            for s in w.get("sessions", [])}


class TestRecalcEnginePreservesState(unittest.TestCase):
    """recalculate_plan: race/taper passes + §6.12 preservation on rebuild."""

    def setUp(self):
        today = date.today()
        self.a_date = _next_sunday(today + timedelta(days=56))
        self.b_date = _next_sunday(today + timedelta(days=28))
        self.goal = tp.Goal(
            goal_type="event",
            target_date=self.a_date,
            event_name="Gran Fondo",
            event_km=120, event_climb_m=1500, event_type="granfondo",
            hours_per_week=10.0,
            max_weekday_hours=2.0, max_weekend_hours=4.0,
            rest_days=[0],
            longest_ride_h_90d=3.0,
            events=[tp.TargetEvent(date=self.b_date, priority="B",
                                   name="Kermis", event_type="crit",
                                   event_km=80, event_climb_m=200)],
        )

    def tearDown(self):
        tp.set_active_distribution("polarized", None)

    def test_recalc_rebuild_keeps_races_tapers_and_rider_state(self):
        _phases, weeks = tp.generate_plan(self.goal, current_ctl=45)
        by_day = _sessions_by_day(weeks)
        self.assertTrue(getattr(by_day[self.b_date], "is_race", False),
                        "generate must place the B race row (test setup)")

        # Mark rider state on two plain future-week sessions, well clear of
        # today's week, both race days and their taper/opener windows.
        today = date.today()
        lo, hi = today + timedelta(days=13), self.b_date - timedelta(days=4)
        candidates = [
            s for w in weeks if w.start > today for s in w.sessions
            if lo <= s.day <= hi
            and not getattr(s, "is_race", False)
            and s.session_type != "rest"
            and getattr(s, "status", "pending") == "pending"
        ]
        self.assertGreaterEqual(len(candidates), 2, "test setup: need 2 slots")
        moved, done = candidates[0], candidates[-1]
        self.assertNotEqual(moved.day, done.day)
        moved.user_moved = True
        moved.zwo_file = "KEEP_ME.zwo"
        moved.description = "user-moved marker"
        done.status = "done"
        done.completion_matches = [{"activity_id": 42, "tss": 55}]

        # CTL 45 vs a granfondo target CTL (85+) → >8% deviation → rebuild.
        _p2, all_weeks, info = tp.recalculate_plan(
            self.goal, weeks, current_ctl=45)
        self.assertEqual(info.get("action"), "recalculated")

        out = _sessions_by_day(all_weeks)

        # (1) race rows rebuilt from goal.events / target_date.
        b_row = out.get(self.b_date)
        self.assertIsNotNone(b_row, "no session row on the B race date")
        self.assertTrue(getattr(b_row, "is_race", False),
                        "B race row vanished on weekly recalc")
        self.assertEqual((getattr(b_row, "race", None) or {}).get("priority"), "B")
        a_row = out.get(self.a_date)
        self.assertIsNotNone(a_row, "no session row on the A race date")
        self.assertTrue(getattr(a_row, "is_race", False),
                        "A race row vanished on weekly recalc")

        # (2) SM3 mini-taper: the B-race eve carries the opener.
        eve = out.get(self.b_date - timedelta(days=1))
        self.assertIsNotNone(eve, "no session row on the B-race eve")
        self.assertTrue(getattr(eve, "is_opener", False),
                        "B-race eve opener lost (mini-taper pass skipped)")

        # (3) §6.12: preserved sessions survive the future-week rebuild.
        kept_moved = out.get(moved.day)
        self.assertIsNotNone(kept_moved)
        self.assertTrue(getattr(kept_moved, "user_moved", False),
                        "user_moved session re-prescribed by weekly recalc")
        self.assertEqual(getattr(kept_moved, "zwo_file", ""), "KEEP_ME.zwo")
        kept_done = out.get(done.day)
        self.assertIsNotNone(kept_done)
        self.assertEqual(getattr(kept_done, "status", ""), "done",
                         "done session re-prescribed by weekly recalc")
        self.assertTrue(getattr(kept_done, "completion_matches", None))


class TestAutoRecalcWritePreservesState(unittest.TestCase):
    """/api/plan/auto-recalc: canonical session round-trip + top-level keys."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="recalcpres_"))
        self._patch = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()
        tp.set_active_distribution("polarized", None)

    def _mocks(self):
        stub = {"training": {"ctl": 45}, "wellness_7": []}
        return (
            patch.object(app_module, "cached",
                         side_effect=lambda k, fn, **kw: stub[k] if k in stub else fn()),
            patch.object(app_module.db, "query_activities", return_value=[]),
        )

    def _generate(self):
        body = dict(
            goal="ftp", plan_weeks=16, hours_per_week=10,
            max_weekday=2.0, max_weekend=4.0, rest_days=[0],
            daily_availability={str(d): (2.0 if d < 5 else 4.0) for d in range(1, 7)},
        )
        r = self.client.post("/api/plan/generate", json=body)
        self.assertEqual(r.status_code, 200, r.text)
        return json.loads((self.tmp / "current_plan.json").read_text())

    def test_recalc_write_preserves_rider_state_and_race_rows(self):
        m1, m2 = self._mocks()
        with m1, m2:
            plan = self._generate()
            today = date.today()

            cur_week = next(w for w in plan["weeks"]
                            if date.fromisoformat(w["start"]) <= today
                            <= date.fromisoformat(w["end"]))
            fut_week = [w for w in plan["weeks"]
                        if date.fromisoformat(w["start"]) > today][1]

            # Rider state + a persisted race row in the KEPT (current) week —
            # the current week is preserved verbatim by the engine, so any loss
            # is the endpoint's load/save round-trip.
            c_done, c_race, c_moved = cur_week["sessions"][:3]
            c_done.update(status="done", zwo_file="done_keep.zwo",
                          completion_matches=[{"activity_id": 42, "tss": 55}])
            c_race.update(is_race=True,
                          race={"name": "Kermis", "km": 80, "climb_m": 200,
                                "type": "crit", "priority": "B"})
            c_moved.update(user_moved=True, moved_from="2026-01-01")

            # Rider state in a rebuilt FUTURE week — exercises the engine's
            # §6.12 gather + swap through the real endpoint.
            f_moved, f_dismissed = fut_week["sessions"][1], fut_week["sessions"][3]
            f_moved.update(user_moved=True, zwo_file="future_keep.zwo")
            f_dismissed.update(status="dismissed",
                               dismissed_at="2026-07-01T10:00:00")

            # Unknown top-level keys must survive the save (shallow-copy base).
            plan["_test_marker"] = "survives"
            # Age the plan past the 7-day freshness gate.
            plan["recalc_date"] = (datetime.now() - timedelta(days=8)).isoformat()
            (self.tmp / "current_plan.json").write_text(json.dumps(plan))

            r = self.client.get("/api/plan/auto-recalc")
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(r.json().get("action"), "recalculated", r.text)

            saved = json.loads((self.tmp / "current_plan.json").read_text())
            self.assertEqual(saved.get("_test_marker"), "survives",
                             "recalc save dropped unknown top-level plan keys")
            rows = _json_sessions_by_day(saved)

            row = rows[c_done["day"]]
            self.assertEqual(row.get("status"), "done")
            self.assertEqual(row.get("zwo_file"), "done_keep.zwo")
            self.assertTrue(row.get("completion_matches"),
                            "completion_matches wiped by recalc write")
            row = rows[c_race["day"]]
            self.assertTrue(row.get("is_race"),
                            "race row wiped by recalc write")
            self.assertEqual((row.get("race") or {}).get("priority"), "B")
            row = rows[c_moved["day"]]
            self.assertTrue(row.get("user_moved"))
            self.assertEqual(row.get("moved_from"), "2026-01-01")

            row = rows.get(f_moved["day"])
            self.assertIsNotNone(row, "future user_moved day missing")
            self.assertTrue(row.get("user_moved"),
                            "future user_moved session re-prescribed")
            self.assertEqual(row.get("zwo_file"), "future_keep.zwo")
            row = rows.get(f_dismissed["day"])
            self.assertIsNotNone(row, "future dismissed day missing")
            self.assertEqual(row.get("status"), "dismissed",
                             "dismissed session re-prescribed")
            self.assertEqual(row.get("dismissed_at"), "2026-07-01T10:00:00")


if __name__ == "__main__":
    unittest.main()
