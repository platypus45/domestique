"""v4.4.2 §B3 + §B6 — local CTL/ATL/TSB fallback in /api/readiness and
unified data_status default in /api/today-session.

Verifies that when ICU wellness data is unavailable, /api/readiness
surfaces local-archive-derived CTL/ATL/TSB and the response includes a
``source`` field disambiguating "local" vs "icu" vs "mixed". Also that
the score-None default is unified between /api/readiness and
/api/today-session via the shared ``data_status`` field.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


class TestLocalTrainingLoadFallback(unittest.TestCase):
    def setUp(self):
        # Reset cache so each test sees the patched get_today_metrics output.
        app_module.clear_cache()
        self.client = TestClient(app_module.app)

    # Since 2026-09-14 CTL/ATL/TSB come from intervals.icu only (the owner's
    # decision; src/fitness.py): live, else ICU's last values as the SQLite
    # wellness table holds them, else unknown. The local EWMA over the FIT-only
    # ride list these tests used to pin is no fallback any more, and "mixed"
    # (ICU CTL beside a local ATL) cannot happen.

    def _store(self, ctl, atl, days_ago=1):
        """ICU's last values as the wellness table would hold them. Stubbed at
        the owner's store read, not written to SQLite: these tests are about
        what the endpoint answers, and a shared test database is order-
        dependent under xdist (seen in the gate of 070ad407).
        tests/test_one_fitness_state.py reads the real table."""
        import clock
        import fitness
        from datetime import timedelta
        d = (clock.today() - timedelta(days=days_ago)).isoformat()
        p = patch.object(fitness, "_cached_row",
                         return_value={"date": d, "ctl": ctl, "atl": atl})
        p.start()
        self.addCleanup(p.stop)

    def _no_store(self):
        import fitness
        p = patch.object(fitness, "_cached_row", return_value=None)
        p.start()
        self.addCleanup(p.stop)

    def test_readiness_falls_back_to_icus_stored_values_when_icu_empty(self):
        """ICU unreachable: the readiness card shows ICU's last stored values,
        not a local EWMA (planted at 42.5 here, and never read)."""
        self._store(40.0, 30.0)
        with patch.object(app_module, "get_today_metrics", return_value={}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}), \
             patch("ride_storage.compute_local_ctl", return_value=42.5):
            r = self.client.get("/api/readiness")
            self.assertEqual(r.status_code, 200, r.text)
            t = r.json().get("training") or {}
            self.assertEqual((t.get("ctl"), t.get("atl"), t.get("tsb")), (40.0, 30.0, 10.0))

    def test_training_load_source_field_icu_cached_none(self):
        """``source`` is "icu", "icu_cached" or "none"; never "local" or "mixed"."""
        self._no_store()
        with patch.object(app_module, "get_today_metrics", return_value={"ctl": 65.0, "atl": 60.0, "tsb": 5.0}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}):
            t = self.client.get("/api/readiness").json().get("training") or {}
            self.assertEqual((t.get("source"), t.get("ctl")), ("icu", 65.0))

        app_module.clear_cache()
        # ICU answers CTL alone: not a mixed answer; with nothing stored, unknown.
        with patch.object(app_module, "get_today_metrics", return_value={"ctl": 65.0}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}), \
             patch("ride_storage.compute_local_ctl", return_value=42.5):
            t = self.client.get("/api/readiness").json().get("training") or {}
            self.assertEqual((t.get("source"), t.get("ctl"), t.get("atl")), ("none", None, None))

        app_module.clear_cache()
        import fitness
        with patch.object(app_module, "get_today_metrics", return_value={}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}), \
             patch.object(fitness, "_cached_row",
                          return_value={"date": "2026-01-01", "ctl": 40.0, "atl": 30.0}):
            t = self.client.get("/api/readiness").json().get("training") or {}
            self.assertEqual((t.get("source"), t.get("ctl")), ("icu_cached", 40.0))

    def test_readiness_data_status_field_set_when_insufficient(self):
        """§B6: readiness payload carries ``data_status`` and a numeric score
        when a training load is known (here ICU's stored values), instead of
        raw None."""
        self._store(40.0, 30.0)
        with patch.object(app_module, "get_today_metrics", return_value={}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}), \
             patch.object(app_module, "_local_sleep_metrics", return_value={}):
            r = self.client.get("/api/readiness").json()
            readiness = r.get("readiness") or {}
            self.assertEqual(readiness.get("data_status"), "insufficient_data")
            self.assertEqual(readiness.get("score"), 50)

    def test_today_session_score_uses_unified_helper(self):
        """§B6: /api/today-session uses the shared helper instead of a
        hardcoded score=50, so the score+data_status agree with /api/readiness."""
        # Empty training → readiness will be INSUFFICIENT_DATA.
        # No local CTL either → score should be None (consistent with
        # readiness when truly no data).
        # A session today, so the endpoint reaches readiness. Without one it
        # returns before readiness and this test used to skip itself (the
        # regenerated fallback week that once supplied one is gone).
        import json, tempfile
        from datetime import timedelta
        from pathlib import Path
        import clock
        today = clock.today()
        monday = today - timedelta(days=today.weekday())
        tmp = Path(tempfile.mkdtemp(prefix="today_score_"))
        (tmp / "current_plan.json").write_text(json.dumps({
            "goal": {"type": "general"}, "phases": [],
            "weeks": [{"week_num": 1, "start": monday.isoformat(),
                       "end": (monday + timedelta(days=6)).isoformat(), "phase": "base",
                       "tss_target": 300, "is_stepback": False,
                       "sessions": [{"day": today.isoformat(), "day_name": today.strftime("%a"),
                                     "session_type": "z2", "duration_min": 60, "tss_estimate": 45,
                                     "description": "", "zwo_file": "", "zwo_name": "",
                                     "status": "pending"}]}]}))
        with patch.object(app_module, "_plan_dir", return_value=tmp), \
             patch.object(app_module, "get_today_metrics", return_value={}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}), \
             patch("ride_storage.compute_local_ctl", return_value=None):
            data = self.client.get("/api/today-session").json()
            self.assertIsNotNone(data.get("planned"))
            # Final-fallback path: score=50 retained when no local data.
            self.assertEqual(data.get("readiness"), 50)


if __name__ == "__main__":
    unittest.main()
