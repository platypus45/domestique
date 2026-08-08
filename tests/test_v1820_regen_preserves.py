"""v1.8.20 — /api/plan/regenerate must NOT destroy user edits or top-level
plan keys. Pre-fix it round-tripped 8 of 22 session fields and rebuilt the plan
from a fixed key set, silently wiping user_moved / status / dismissed_at /
completion_matches AND the availability calendar + reforecast_date on every
(auto-firing) regen.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _session(day, st="z2", **extra):
    s = {"day": day, "day_name": "Mon", "session_type": st, "duration_min": 60,
         "tss_estimate": 50, "description": "x", "zwo_file": "z2_steady_56pct_60min.zwo",
         "zwo_name": "Z2"}
    s.update(extra)
    return s


class TestRegenPreserves(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="regen_"))
        self.plan_p = self.tmp / "current_plan.json"
        self._patch = patch.object(app_module, "_plan_dir", return_value=self.tmp)
        self._patch.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch.stop()

    def _build_plan(self):
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        weeks = []
        # 4 weeks: 2 past, current, 1 future.
        for wk in range(-2, 2):
            wstart = monday + timedelta(weeks=wk)
            wend = wstart + timedelta(days=6)
            sessions = []
            for d in range(7):
                day = (wstart + timedelta(days=d)).isoformat()
                sessions.append(_session(day))
            weeks.append({
                "week_num": wk + 3, "start": wstart.isoformat(), "end": wend.isoformat(),
                "phase": "build", "tss_target": 300, "is_stepback": False,
                "sessions": sessions,
            })
        # Plant edits: a dismissed FUTURE session + a user_moved current session
        # + completion_matches on a past session.
        fut = weeks[3]["sessions"][2]
        fut["status"] = "dismissed"; fut["dismissed_at"] = "2026-06-08T10:00:00"
        cur = weeks[2]["sessions"][1]
        cur["user_moved"] = True; cur["moved_from"] = "2026-01-01"
        past = weeks[0]["sessions"][0]
        past["completion_matches"] = [{"activity_id": "iX", "tss": 55}]
        plan = {
            "goal": {"type": "general", "event_date": (today + timedelta(weeks=8)).isoformat()},
            "phases": [], "weeks": weeks,
            "generated": (today - timedelta(days=40)).isoformat(),
            "availability": {"2026-06-15": {"hours": 0, "type": "unavailable"}},
            "reforecast_date": today.isoformat(),
            "last_reforecast_info": {"note": "keep me"},
        }
        self.plan_p.write_text(json.dumps(plan), encoding="utf-8")
        return today

    def test_regenerate_preserves_edits_and_toplevel_keys(self):
        self._build_plan()
        # Force regeneration regardless of gap state by mocking the gap detector
        # to report needs_regeneration (the endpoint itself always regenerates;
        # we just need CTL + activities to be benign).
        with patch.object(app_module, "cached", side_effect=lambda k, fn, **kw: fn() if k != "training" else {"ctl": 40}), \
             patch.object(app_module.db, "query_activities", return_value=[]):
            r = self.client.post("/api/plan/regenerate")
        self.assertEqual(r.status_code, 200, r.text)
        out = json.loads(self.plan_p.read_text())

        # Top-level keys survive.
        self.assertIn("availability", out, "availability calendar wiped")
        self.assertEqual(out["availability"], {"2026-06-15": {"hours": 0, "type": "unavailable"}})
        self.assertIn("reforecast_date", out, "reforecast_date wiped")
        self.assertEqual(out.get("last_reforecast_info"), {"note": "keep me"})

        # Flatten all sessions.
        alls = [s for w in out["weeks"] for s in w["sessions"]]
        # Past completion_matches survived (past weeks kept verbatim — never
        # rebuilt regardless of recovery ramp).
        self.assertTrue(
            any(s.get("completion_matches") for s in alls),
            "past completion_matches wiped",
        )
        # NOTE: this scenario has a 2-week gap → build_recovery_ramp rebuilds the
        # current + future weeks as Z2 reconditioning, legitimately superseding
        # current/future EDITS (the plan fundamentally changes after a 2-week
        # absence). What MUST survive unconditionally — the availability
        # calendar, reforecast_date, and all other top-level plan keys — is
        # asserted above. Edits surviving a NON-ramp routine regen (the common
        # case) is proven directly in TestRegenGatherBroadened below.


class TestRegenGatherBroadened(unittest.TestCase):
    """v1.8.20 grill-B2 — regenerate_from_today gathers preserved sessions from
    FUTURE weeks too (not only the current week), so a future dismissal survives
    a routine regen that does NOT insert a grid-shifting recovery ramp."""

    def test_future_dismissal_survives_when_no_ramp(self):
        import training_planner as tp
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        # All weeks current+future (no past) → no missed weeks → absence_days=0
        # → no recovery ramp → the weekly grid stays aligned to `monday`.
        old_weeks = []
        for wk in range(0, 3):
            wstart = monday + timedelta(weeks=wk)
            wend = wstart + timedelta(days=6)
            sessions = [
                tp.PlannedSession(
                    day=wstart + timedelta(days=d), day_name="D",
                    session_type="z2", duration_min=60, tss_estimate=50,
                    description="x", zwo_file="z2_steady_56pct_60min.zwo", zwo_name="Z2",
                )
                for d in range(7)
            ]
            # Dismiss a FUTURE session (week 2, day 2).
            if wk == 2:
                sessions[2].status = "dismissed"
                sessions[2].dismissed_at = "2026-06-08T10:00:00"
            old_weeks.append(tp.PlannedWeek(
                week_num=wk + 1, start=wstart, end=wend, phase="build",
                tss_target=300, is_stepback=False, sessions=sessions,
            ))
        goal = tp.Goal(goal_type="general", target_date=today + timedelta(weeks=6))
        _phases, all_weeks, _info = tp.regenerate_from_today(
            goal=goal, old_plan_weeks=old_weeks, current_ctl=40,
            unavailable_periods=[], activities=[], seed_salt=1,
        )
        alls = [s for w in all_weeks for s in w.sessions]
        self.assertTrue(
            any(getattr(s, "status", "") == "dismissed" for s in alls),
            "future dismissed session was re-prescribed despite aligned grid",
        )


if __name__ == "__main__":
    unittest.main()
