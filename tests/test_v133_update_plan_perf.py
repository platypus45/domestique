"""v1.3.3 PERF regression — UPDATE plan flow snappy.

Pre-fix (v1.3.2 ship): clicking the availability-calendar UPDATE button
fired ``POST /api/plan/save-availability`` then ``GET /api/calendar``.
The save-availability handler called ``tp.reforecast()`` without
``tsb_series``, so reforecast's per-day ``_tsb_at`` callback fell through
to ``get_today_metrics()`` which makes 2 ICU HTTPS calls per future hard
session (~270 ms each). On a 12–16 week plan that was 5–13 s of network
I/O on every UPDATE click on a machine with ICU credentials configured.

Post-fix (v1.3.3): save-availability passes ``tsb_series={}`` because
this endpoint's only job is the availability rescale; TSB-driven
downshifts belong to ``/api/plan/reforecast``. The empty dict makes
``_tsb_at`` return None for every day and short-circuits the network
fallback. ``reforecast()`` core logic is untouched.

Test asserts: warm UPDATE click → dashboard data ready in < 1.5 s.
Cold/warm distinction is moot here because the fix removes the work
entirely (no caching needed).
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _mk_plan_dict(monday: date, weeks_count: int = 12) -> dict:
    """A 12-week plan rich with HARD sessions on every Tue/Thu/Sat. The pre-fix
    bug scaled with the count of future HARD sessions (vo2max/threshold/
    sweetspot/tempo/long_z2) so we want plenty of them in the future window."""
    weeks = []
    phases = ["base", "build1", "build2", "peak"]
    for w_idx in range(weeks_count):
        wstart = monday + timedelta(weeks=w_idx)
        # Mon REST, Tue VO2MAX, Wed Z2, Thu THRESHOLD, Fri REST, Sat LONG_Z2, Sun SWEETSPOT
        types_for_week = ["rest", "vo2max", "z2", "threshold", "rest", "long_z2", "sweetspot"]
        durations = [0, 60, 60, 75, 0, 120, 90]
        tss = [0, 80, 50, 90, 0, 100, 75]
        sessions = []
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
            "tss_target": 400,
            "is_stepback": (w_idx % 4 == 3),
            "sessions": sessions,
            "hit_per_week": 3,
        })
    return {
        "goal": {"type": "general", "hours_per_week": 10.0, "rest_days": [0, 4]},
        "phases": [],
        "weeks": weeks,
        "generated": "2026-04-19T00:00:00",
    }


@pytest.mark.release_serial
class UpdatePlanPerfTest(unittest.TestCase):
    """v1.3.3 — assert the save-availability + calendar round-trip is fast.

    The bug surfaced only on machines with ICU credentials configured (because
    that's when the network fallback fires). The test PROACTIVELY simulates
    creds-present by patching ``training.get_today_metrics`` to add a 250 ms
    sleep — that mirrors a real ICU round-trip. Without the fix the patched
    sleep would be hit ~once per future hard session (~36 calls on a 12-week
    plan) blowing the 1500 ms budget. With the fix tsb_series={} short-circuits
    the callback so the patched sleep is never hit at all.
    """

    BUDGET_MS = 1500.0  # warm click-to-paint budget

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._tmp = Path(self._tmpdir.name)

        # Anchor on tomorrow's Monday so every plan day is in the future —
        # forces reforecast's availability + TSB loops to consider every day.
        today = date.today()
        self._monday = today + timedelta(days=(7 - today.weekday()) % 7 or 7)
        self._plan = _mk_plan_dict(self._monday, weeks_count=12)
        (self._tmp / "current_plan.json").write_text(json.dumps(self._plan))

        self._orig_plan_dir = tp.PLAN_DIR
        tp.PLAN_DIR = self._tmp

        # Stub fit + ICU dirs so the calendar endpoint's ride scan is empty.
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
        # Disable the lazy ICU sync hook on /api/calendar so the timing
        # measures only the merge work, not a network probe.
        self._patch_lazy_sync = patch.object(
            app_module, "_maybe_lazy_icu_sync", return_value=None
        )
        self._patch_lazy_sync.start()

        # Simulate "ICU credentials configured" — get_today_metrics sleeps
        # 250 ms (a typical ICU round-trip). With the v1.3.3 fix tsb_series={}
        # bypasses this; without the fix it gets hit once per future hard
        # session.
        self._metrics_calls = 0
        def _slow_metrics():
            self._metrics_calls += 1
            time.sleep(0.25)
            return {"tsb": 5.0, "ctl": 50.0, "atl": 45.0}
        self._patch_metrics = patch.object(
            tp, "get_today_metrics", side_effect=_slow_metrics
        )
        self._patch_metrics.start()

        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._patch_metrics.stop()
        self._patch_lazy_sync.stop()
        self._patch_rides.stop()
        self._patch_fit_dir_rs.stop()
        self._patch_icu_dir.stop()
        self._patch_fit.stop()
        tp.PLAN_DIR = self._orig_plan_dir
        self._tmpdir.cleanup()

    def test_update_click_to_paint_under_1500ms_warm(self):
        """Warm round-trip = save-availability + calendar < 1500 ms."""
        # Build a realistic UPDATE payload — first week's worth of days
        # rescaled. Same shape the dashboard sends.
        avail = {}
        for i in range(7):
            d = self._monday + timedelta(days=i)
            avail[d.isoformat()] = {
                "hours": 1.5 if i not in (0, 4) else 0,
                "type": "available" if i not in (0, 4) else "rest",
            }

        # Warm-up: import paths, lazy modules, library cache, etc.
        self.client.get("/api/calendar")
        # Reset the metrics call counter after warm-up (warm-up itself
        # shouldn't hit get_today_metrics, but be defensive).
        self._metrics_calls = 0

        # Take 3 samples and use the median to soak up jitter on shared CI.
        samples_ms = []
        for _ in range(3):
            t0 = time.perf_counter()
            r1 = self.client.post(
                "/api/plan/save-availability",
                json={"availability": avail},
            )
            self.assertEqual(r1.status_code, 200, r1.text)
            r2 = self.client.get("/api/calendar")
            self.assertEqual(r2.status_code, 200, r2.text)
            samples_ms.append((time.perf_counter() - t0) * 1000.0)

        median_ms = sorted(samples_ms)[len(samples_ms) // 2]
        self.assertLess(
            median_ms, self.BUDGET_MS,
            f"UPDATE click-to-paint median {median_ms:.0f} ms exceeds "
            f"{self.BUDGET_MS:.0f} ms budget. Samples: {samples_ms}. "
            f"get_today_metrics calls: {self._metrics_calls} "
            f"(should be 0 — v1.3.3 fix passes tsb_series={{}} so the "
            f"per-day _tsb_at fallback never fires for save-availability)."
        )

        # Sanity: with the v1.3.3 fix, save-availability must NOT call
        # get_today_metrics at all. /api/calendar may legitimately call it
        # (other code paths) but on this shape with no rides + no plan
        # changes that affect dashboard summary, it shouldn't either.
        self.assertEqual(
            self._metrics_calls, 0,
            f"save-availability triggered {self._metrics_calls} ICU metric "
            f"fetches — pre-fix bug. Expected 0 (tsb_series={{}} short-circuit)."
        )


if __name__ == "__main__":
    unittest.main()
