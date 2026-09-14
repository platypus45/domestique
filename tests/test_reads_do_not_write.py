"""No GET changes the plan (week-view contract P8 and A4; decision D8).

Until 2026-09-14 reading the dashboard rewrote the rider's plan:
- /api/today-session ran the continuous deload advance, which rebuilds the
  current week and writes it;
- GET /api/plan/auto-recalc rebuilt the plan when the last recalc was a week
  old, and the Plan tab and the morning adapter called it as a read;
- /api/weekly-plan and /api/week-summary applied an eFTP drift to the
  profile and cleared the caches;
- the lazy ICU sync those reads kick off ran the ride-sync adaptation, which
  reconciles, refits or rebuilds the plan once a day even with no new ride.

The writes happen on POSTs now, on the same triggers: the home and Plan tab
loads POST /api/rides/sync, whose adaptation runs the deload check and the
eFTP auto-apply; auto-recalc is a POST.

The instrument: every parameterless GET under /api/, against a plan and ride
history set up so each of those writers fires (a continuous plan whose rides
trip Foster's monotony, and whose last recalc is weeks old), with the plan
writer and the eFTP auto-apply recorded. The plan file's bytes are compared
too, for any writer this list does not know."""
from __future__ import annotations

import hashlib
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
import training_planner as tp
from test_340_continuous_w2 import _mk_continuous_plan, _monotone_rides

TODAY = date(2026, 9, 15)   # a Tuesday


def _get_routes():
    out = []
    for r in app_module.app.routes:
        path = getattr(r, "path", "")
        if "GET" in (getattr(r, "methods", None) or set()) and path.startswith("/api/") \
                and "{" not in path:
            out.append(path)
    return sorted(set(out))


class ReadsDoNotWrite(unittest.TestCase):
    def setUp(self):
        clock.freeze(TODAY)
        self.tmp = Path(tempfile.mkdtemp(prefix="reads_"))
        # Two weeks, both load weeks: short of the rolling horizon, so
        # auto-recalc extends; no stepback next, so the deload may advance.
        plan = _mk_continuous_plan(TODAY, flags=(False, False))
        plan["recalc_date"] = (TODAY - timedelta(days=30)).isoformat() + "T08:00:00"
        self.pp = self.tmp / "current_plan.json"
        self.pp.write_text(json.dumps(plan))
        self.original = self.pp.read_bytes()
        self.writes: list[str] = []
        real_write = tp.atomic_write_plan

        def _recording_write(path, data, *a, **k):
            self.writes.append(str(path))
            return real_write(path, data, *a, **k)

        self._patches = [
            patch.object(app_module, "_plan_dir", return_value=self.tmp),
            patch.object(tp, "atomic_write_plan", _recording_write),
            patch.object(app_module, "_load_all_rides_safe", return_value=_monotone_rides(TODAY)),
            patch.object(app_module, "get_today_metrics", lambda: {"ctl": 50.0, "atl": 60.0, "tsb": -10.0}),
            patch.object(tp, "check_and_auto_apply_eftp",
                         side_effect=lambda *a, **k: self.writes.append("eftp_auto_apply")),
            # The lazy sync runs inline, so what it would write lands in this
            # request, not on a thread after the assertion.
            patch.object(app_module, "_kick_lazy_icu_sync",
                         side_effect=lambda **k: app_module._maybe_lazy_icu_sync(**k)),
            patch.object(app_module, "_icu_credentials_present", return_value=True),
            patch.object(app_module, "_read_last_sync_at", return_value=None),
            patch.object(app_module, "_read_last_wellness_sync_at", return_value=0),
            patch("training.fetch_recent_activities", return_value=[]),
            # An eFTP well above the profile's FTP, every day for two weeks.
            patch.object(app_module, "fetch_wellness", side_effect=lambda days=7, **k: [
                {"id": (TODAY - timedelta(days=i)).isoformat(), "sportInfo": [{"eftp": 400}]}
                for i in range(days)]),
        ]
        for p in self._patches:
            p.start()
        cache.clear_cache()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in reversed(self._patches):
            p.stop()
        clock.unfreeze()
        cache.clear_cache()

    def test_the_fixture_trips_the_writers(self):
        """A control: each writer, called directly, does write on this fixture,
        so a clean run of the GETs is evidence, not an idle fixture."""
        chip = app_module._maybe_advance_continuous_deload(json.loads(self.pp.read_text()), self.pp)
        self.assertTrue(chip, "the monotone rides must trip the deload advance")
        self.writes.clear()
        out = app_module.api_plan_auto_recalc()
        self.assertTrue(self.writes, f"auto-recalc must rebuild this plan: {str(out)[:200]}")
        self.writes.clear()
        app_module._guarded_check_and_auto_apply_eftp(app_module.fetch_wellness(14))
        self.assertEqual(self.writes, ["eftp_auto_apply"])

    def test_no_get_writes_the_plan(self):
        routes = _get_routes()
        self.assertGreater(len(routes), 60)
        offenders = {}
        for path in routes:
            before = hashlib.sha256(self.pp.read_bytes()).hexdigest()
            self.writes.clear()
            try:
                self.client.get(path)
            except Exception:  # noqa: BLE001 - a read that raises still must not write
                pass
            after = hashlib.sha256(self.pp.read_bytes()).hexdigest()
            if self.writes or before != after:
                offenders[path] = list(self.writes) or ["bytes changed"]
                self.pp.write_bytes(self.original)
        self.assertEqual(offenders, {})

    def test_auto_recalc_is_a_post(self):
        self.assertEqual(self.client.get("/api/plan/auto-recalc").status_code, 405)

    def test_the_lazy_sync_does_not_adapt(self):
        calls = []
        with patch.object(app_module, "_maybe_auto_reforecast",
                          side_effect=lambda *a, **k: calls.append(a)):
            app_module._maybe_lazy_icu_sync(force_if_today_missing=True)
            self.assertEqual(calls, [])
            self.client.post("/api/rides/sync")
            self.assertEqual(len(calls), 1, "the POST sync still adapts")


if __name__ == "__main__":
    unittest.main()
