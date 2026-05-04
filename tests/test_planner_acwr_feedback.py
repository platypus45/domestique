"""v4.6.6 IMPL-A — ACWR injury-prevention feedback loops.

Five tests cover the new injury-prevention paths:
  G4 ACWR weekly scaling (Gabbett 2016 Br J Sports Med 50:273-280)
    1. test_acwr_under_threshold_no_scaling   — 100% week is fine
    2. test_acwr_over_threshold_scales_next_week — 200% week → 0.85× next
    3. test_acwr_decrements_hit_per_week      — same → hit_per_week -= 1
  generate_weekly_plan rolling-deficit subtract path (Soligard 2016
  IOC consensus Br J Sports Med 50:1030-1041)
    4. test_rollover_surplus_subtracts        — 130%+ overshoot → cut
  /api/rides/sync post-ride sync hook (Foster 1998 Med Sci Sports
  Exerc 30:1164-1168)
    5. test_rides_sync_emits_load_alert       — TSS=2× estimate → flag

These three citations form the Wave-2 injury-prevention backbone — the
existing reforecast() code only consumed TSB. v4.6.6 closes the gap so
last week's actual/planned ratio actually scales next week's load.
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


def _mk_planned_week(
    week_num: int,
    start: date,
    *,
    tss_target: float = 400.0,
    hit_per_week: int = 3,
    is_stepback: bool = False,
    phase: str = "build1",
    sessions: list | None = None,
) -> tp.PlannedWeek:
    return tp.PlannedWeek(
        week_num=week_num,
        start=start,
        end=start + timedelta(days=6),
        phase=phase,
        tss_target=tss_target,
        is_stepback=is_stepback,
        sessions=sessions or [],
        hit_per_week=hit_per_week,
    )


def _mk_activity(d: date, tss: float) -> dict:
    return {
        "date": d.isoformat(),
        "tss": tss,
        "type": "ride",
    }


class TestACWRReforecastGate(unittest.TestCase):
    """G4: reforecast() must scale next week when last week's ratio > 1.5."""

    def setUp(self):
        # Anchor "today" on a Monday so the previous full week ends Sunday.
        # Today is the start of week N+1; last completed week is N (Mon-Sun).
        today = date.today()
        self.monday_today = today - timedelta(days=today.weekday())
        self.last_mon = self.monday_today - timedelta(days=7)
        self.last_sun = self.monday_today - timedelta(days=1)
        # 3 weeks: last (completed) → current → next (the scale target)
        self.weeks = [
            _mk_planned_week(1, self.last_mon, tss_target=400.0, hit_per_week=3),
            _mk_planned_week(2, self.monday_today, tss_target=420.0, hit_per_week=3),
            _mk_planned_week(3, self.monday_today + timedelta(days=7),
                             tss_target=440.0, hit_per_week=3),
        ]
        # Flat TSB neutral — TSB-driven downshifts must not interfere.
        self.tsb_series = {
            self.weeks[0].start + timedelta(days=i): 0.0 for i in range(7)
        }
        self.tsb_series.update({
            self.weeks[1].start + timedelta(days=i): 0.0 for i in range(7)
        })
        self.tsb_series.update({
            self.weeks[2].start + timedelta(days=i): 0.0 for i in range(7)
        })
        self.goal = tp.Goal(goal_type="general", hours_per_week=8.0)

    def test_acwr_under_threshold_no_scaling(self):
        """Last week 100% of plan → ratio 1.0 → no scaling."""
        rides = [_mk_activity(self.last_mon + timedelta(days=2), 400.0)]
        original_tss = self.weeks[2].tss_target
        original_hit = self.weeks[2].hit_per_week
        _, info = tp.reforecast(self.goal, self.weeks,
                                tsb_series=self.tsb_series,
                                recent_activities=rides)
        self.assertAlmostEqual(self.weeks[2].tss_target, original_tss)
        self.assertEqual(self.weeks[2].hit_per_week, original_hit)
        self.assertFalse(self.weeks[2].auto_acwr_scaled)
        self.assertIsNone(info["acwr_scaled_week"])
        self.assertLess(info["acwr_ratio"], 1.5)

    def test_acwr_over_threshold_scales_next_week(self):
        """Last week 200% (800 vs planned 400) → next week ×0.85.

        ``self.weeks`` is anchored so weeks[0] is the last completed week,
        weeks[1] is the in-progress week (today is its Monday), and
        weeks[2] is the next FUTURE week — that's the one G4 must scale.
        """
        rides = [
            _mk_activity(self.last_mon + timedelta(days=1), 400.0),
            _mk_activity(self.last_mon + timedelta(days=3), 400.0),
        ]
        original_tss = self.weeks[2].tss_target
        _, info = tp.reforecast(self.goal, self.weeks,
                                tsb_series=self.tsb_series,
                                recent_activities=rides)
        # 800/400 = 2.0 ≫ 1.5 → next future non-stepback week scaled ×0.85
        self.assertGreater(info["acwr_ratio"], 1.5)
        self.assertEqual(info["acwr_scaled_week"], 3)  # weeks[2].week_num == 3
        self.assertLessEqual(self.weeks[2].tss_target, original_tss * 0.85 + 0.01)
        self.assertTrue(self.weeks[2].auto_acwr_scaled)

    def test_acwr_decrements_hit_per_week(self):
        """200% week → next future week hit_per_week decremented by 1 (floor 1)."""
        rides = [_mk_activity(self.last_mon + timedelta(days=2), 850.0)]
        # weeks[2] is the next FUTURE non-stepback week (weeks[1] is in-progress
        # so reforecast skips it — we only scale future weeks).
        _, _ = tp.reforecast(self.goal, self.weeks,
                             tsb_series=self.tsb_series,
                             recent_activities=rides)
        # Floor at 1 — even huge ratios shouldn't zero out HIT.
        self.assertEqual(self.weeks[2].hit_per_week, 2)
        self.assertTrue(self.weeks[2].auto_acwr_scaled)


class TestRolloverSurplusSubtract(unittest.TestCase):
    """generate_weekly_plan() must subtract surplus when last week >130% target."""

    def test_rollover_surplus_subtracts(self):
        """Last week 200% of weekly_tss → current week reduced + HIT cut.

        Compares the surplus case against the baseline (no recent activities)
        with identical phase/goal to isolate the subtract path. The directive
        is to "subtract min(surplus, weekly_tss × 0.20) AND decrement
        hit_per_week by 1" — both observable effects are asserted.
        """
        from datetime import date as _d
        today = _d.today()
        monday = today - timedelta(days=today.weekday())
        last_week_start = monday - timedelta(days=7)
        phase = tp.Phase(
            name="base",
            start=monday - timedelta(days=30),
            end=monday + timedelta(days=60),
            weeks=12,
            focus="aerobic base",
            weekly_tss_target=400.0,
            z2_pct=80.0,
            hit_per_week=2,
            session_types=["z2", "threshold", "vo2max"],
        )
        goal = tp.Goal(goal_type="general", hours_per_week=8.0,
                       rest_days=[0], available_days=[1, 2, 3, 4, 5, 6])
        # Baseline: no activities → surplus path skipped entirely.
        week_baseline = tp.generate_weekly_plan(
            goal=goal, current_phase=phase, current_ctl=50.0,
            recent_activities=[],
        )
        baseline_hit_count = sum(
            1 for s in week_baseline.sessions
            if s is not None and s.session_type in {
                "vo2max", "threshold", "overunder",
                "sweetspot", "sprint", "tempo",
            }
        )
        # Surplus case: last week 600 TSS = 150% of 400 → trips 1.3× gate.
        recent_activities = [
            {"date": (last_week_start + timedelta(days=i)).isoformat(),
             "tss": 200.0}
            for i in (1, 3, 5)
        ]
        week_surplus = tp.generate_weekly_plan(
            goal=goal, current_phase=phase, current_ctl=50.0,
            recent_activities=recent_activities,
        )
        surplus_hit_count = sum(
            1 for s in week_surplus.sessions
            if s is not None and s.session_type in {
                "vo2max", "threshold", "overunder",
                "sweetspot", "sprint", "tempo",
            }
        )
        # 1) tss_target visibly reduced vs baseline (the cut went through
        #    weekly_tss → fewer/shorter sessions placed downstream).
        self.assertLess(
            week_surplus.tss_target, week_baseline.tss_target,
            f"surplus tss_target ({week_surplus.tss_target}) should be "
            f"strictly less than baseline ({week_baseline.tss_target})",
        )
        # The reduction must respect the 20%-of-weekly_tss cap (≤80 cut)
        # plus rounding/HIT-placement slack.
        reduction = week_baseline.tss_target - week_surplus.tss_target
        self.assertLessEqual(
            reduction, 400 * 0.20 + 50,
            f"reduction {reduction} exceeded 20%-of-target cap (+slack)",
        )
        # 2) hit_per_week decremented by 1 → at least one fewer HIT placed.
        self.assertLess(
            surplus_hit_count, baseline_hit_count,
            f"surplus HIT count ({surplus_hit_count}) should be "
            f"strictly less than baseline ({baseline_hit_count})",
        )


class TestRidesSyncLoadAlert(unittest.TestCase):
    """/api/rides/sync must emit plan_load_alert when same-day TSS spikes."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        # Write a minimal plan with today as a planned 100-TSS session.
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        sessions = []
        for i in range(7):
            d = monday + timedelta(days=i)
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": "threshold" if d == today else "rest",
                "duration_min": 60 if d == today else 0,
                "tss_estimate": 100 if d == today else 0,
                "description": "today threshold" if d == today else "",
                "status": "pending",
            })
        plan = {
            "goal": {"type": "general"},
            "weeks": [{
                "week_num": 1,
                "start": monday.isoformat(),
                "end": (monday + timedelta(days=6)).isoformat(),
                "phase": "build1",
                "tss_target": 400,
                "is_stepback": False,
                "sessions": sessions,
            }],
        }
        (self._tmp / "current_plan.json").write_text(json.dumps(plan))

        # Stub the actual ICU sync (no network).
        self._patch_sync_acts = patch.object(
            app_module, "_sync_icu_activities",
            return_value={"added": 0, "updated": 0, "total": 1,
                          "status": "ok", "last_sync_at": 0},
        )
        self._patch_sync_acts.start()
        self._patch_sync_combined = patch.object(
            app_module, "_sync_icu_rides_and_wellness",
            return_value={"added": 0, "updated": 0, "total": 1,
                          "status": "ok", "last_sync_at": 0,
                          "wellness_added": 0, "wellness_total": 0},
        )
        self._patch_sync_combined.start()

        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_sync_combined.stop()
        self._patch_sync_acts.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def test_rides_sync_emits_load_alert(self):
        """Same-day ride at 200 TSS (2× the 100 estimate) → plan_load_alert True."""
        today = date.today()
        spiked_ride = {
            "ride_id": "icu_test1",
            "source": "icu",
            "date": today.isoformat(),
            "started_at": today.isoformat() + "T08:00:00",
            "start_date_local": today.isoformat() + "T08:00:00",
            "tss": 200.0,
        }
        with patch.object(app_module, "_load_all_rides_safe",
                          return_value=[spiked_ride]):
            r = self.client.post("/api/rides/sync")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(
            body.get("plan_load_alert"),
            f"expected plan_load_alert=True, body={body}"
        )


if __name__ == "__main__":
    unittest.main()
