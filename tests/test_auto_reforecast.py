"""v1.0.3 IMPL-WIRING — best-effort auto-reforecast helper.

Four tests cover ``app._maybe_auto_reforecast()``:

  1. test_skip_when_new_rides_zero      — early-return at new_rides<=0; no fire
  2. test_skip_when_recent_reforecast   — debounce: <300 s since last → no fire
  3. test_fires_when_eligible           — both gates pass → reforecast_date advances
  4. test_exception_swallowed           — internal raise → warning logged, no propagation

The helper is the cross-cutting "fire reforecast on ride sync / FIT import"
hook plumbed in three places:

  - ``_sync_icu_activities`` (ICU pull)
  - ``POST /api/rides/sync`` (forced sync wrapper)
  - ``POST /api/ride/import`` (FIT upload, treated as added=1)

Helper contract (per MASTER §1):
  - Skip if ``new_rides <= 0``.
  - Read ``plan["reforecast_date"]`` (ISO datetime). Skip if ``< 300 s`` ago.
  - Acquire ``tp.plan_write_lock()``.
  - Pass ``plan["availability"]`` as ``availability_overrides=`` kwarg.
  - Persist via the same write-back pattern (with the duration_min fix).
  - Update ``plan["reforecast_date"]`` to ``datetime.now().isoformat()``.
  - Wrap entire body in ``try/except``: warns + returns, never raises.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import app as app_module
import training_planner as tp


def _next_monday() -> date:
    today = date.today()
    days_until_mon = (7 - today.weekday()) % 7
    if days_until_mon == 0:
        days_until_mon = 7
    return today + timedelta(days=days_until_mon)


def _mk_plan_dict(monday: date, *, reforecast_date: str | None = None) -> dict:
    """Build a minimal one-week plan with one Z2 session on Monday."""
    sessions = []
    for off in range(7):
        d = monday + timedelta(days=off)
        if off == 0:
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": "z2",
                "duration_min": 60,
                "tss_estimate": 45.0,
                "description": "z2 60min",
                "zwo_file": "",
                "zwo_name": "",
                "status": "pending",
            })
        else:
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": "rest",
                "duration_min": 0,
                "tss_estimate": 0,
                "description": "rest",
                "zwo_file": "",
                "zwo_name": "",
                "status": "pending",
            })
    plan = {
        "goal": {
            "type": "general",
            "hours_per_week": 8.0,
            "rest_days": [6],
            "available_days": [0, 1, 2, 3, 4, 5],
        },
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "base",
            "tss_target": 270.0,
            "is_stepback": False,
            "sessions": sessions,
            "hit_per_week": 1,
        }],
        "availability": {},
        "generated": "2026-04-19T00:00:00",
    }
    if reforecast_date is not None:
        plan["reforecast_date"] = reforecast_date
    return plan


class AutoReforecastBase(unittest.TestCase):
    """Shared scaffolding: writeable plan dir + activity stub."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)
        self._monday = _next_monday()
        self._json_path = self._tmp / "current_plan.json"
        self._json_path.write_text(json.dumps(_mk_plan_dict(self._monday)))

        # Redirect plan dir.
        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        # Stub out activity DB + training metrics (auto-reforecast reads
        # both); empty activities + neutral TSB keeps the run trivial.
        self._patch_activities = patch.object(
            app_module.db, "query_activities", return_value=[]
        )
        self._patch_activities.start()
        self._patch_training = patch.object(
            app_module, "get_today_metrics", return_value={"ctl": 50.0, "tsb": 0.0}
        )
        self._patch_training.start()

    def tearDown(self):
        self._patch_training.stop()
        self._patch_activities.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()


class TestSkipWhenNewRidesZero(AutoReforecastBase):
    """new_rides<=0 → early return; reforecast_date NOT touched."""

    def test_skip_when_new_rides_zero(self):
        app_module._maybe_auto_reforecast("default", 0)
        plan = json.loads(self._json_path.read_text())
        # No reforecast_date was set originally and helper must not have
        # written one.
        self.assertNotIn("reforecast_date", plan)


class TestSkipWhenRecentReforecast(AutoReforecastBase):
    """Debounce: last reforecast was <300 s ago → skip."""

    def test_skip_when_recent_reforecast(self):
        recent = (datetime.now() - timedelta(seconds=60)).isoformat()
        plan = _mk_plan_dict(self._monday, reforecast_date=recent)
        self._json_path.write_text(json.dumps(plan))

        app_module._maybe_auto_reforecast("default", 1)

        loaded = json.loads(self._json_path.read_text())
        # reforecast_date must equal the one we wrote (not advanced).
        self.assertEqual(loaded["reforecast_date"], recent)


class TestFiresWhenEligible(AutoReforecastBase):
    """Both gates pass → reforecast() runs → reforecast_date advances."""

    def test_fires_when_eligible(self):
        # Set an OLD reforecast_date so debounce is satisfied.
        old = (datetime.now() - timedelta(seconds=600)).isoformat()
        plan = _mk_plan_dict(self._monday, reforecast_date=old)
        self._json_path.write_text(json.dumps(plan))

        app_module._maybe_auto_reforecast("default", 1)

        loaded = json.loads(self._json_path.read_text())
        # reforecast_date must have advanced strictly past the old one.
        self.assertIn("reforecast_date", loaded)
        self.assertNotEqual(loaded["reforecast_date"], old)
        # Should also include last_reforecast_info from the call.
        self.assertIn("last_reforecast_info", loaded)


class TestExceptionSwallowed(AutoReforecastBase):
    """Helper internals raise → no propagation, just a logged warning."""

    def test_exception_swallowed(self):
        # Force tp.reforecast to raise; helper must NOT propagate.
        with patch.object(tp, "reforecast", side_effect=RuntimeError("boom")):
            old = (datetime.now() - timedelta(seconds=600)).isoformat()
            plan = _mk_plan_dict(self._monday, reforecast_date=old)
            self._json_path.write_text(json.dumps(plan))

            try:
                app_module._maybe_auto_reforecast("default", 1)
            except Exception as e:  # pragma: no cover — must NOT be raised
                self.fail(f"helper must swallow exceptions, raised {e!r}")

        # reforecast_date untouched (write happens after a successful
        # reforecast call, which we forced to fail).
        loaded = json.loads(self._json_path.read_text())
        self.assertEqual(loaded["reforecast_date"], old)


if __name__ == "__main__":
    unittest.main()
