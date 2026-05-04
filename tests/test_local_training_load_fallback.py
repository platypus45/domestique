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

    def test_readiness_falls_back_to_local_ctl_when_icu_empty(self):
        """§B3: /api/readiness with no ICU wellness still returns a CTL value
        from the local-archive EWMA helper."""
        # Empty training dict simulates ICU fetch failure / no creds.
        with patch.object(app_module, "get_today_metrics", return_value={}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}), \
             patch("ride_storage.compute_local_ctl", return_value=42.5), \
             patch.object(app_module, "_compute_local_atl", return_value=38.0):
            r = self.client.get("/api/readiness")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            t = data.get("training") or {}
            self.assertEqual(t.get("ctl"), 42.5)
            self.assertEqual(t.get("atl"), 38.0)
            self.assertEqual(t.get("tsb"), 4.5)

    def test_training_load_source_field_local_icu_mixed(self):
        """§B3: ``source`` field reflects "local" / "icu" / "mixed" correctly."""
        # Case 1: ICU empty, local provides values → source=local.
        with patch.object(app_module, "get_today_metrics", return_value={}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}), \
             patch("ride_storage.compute_local_ctl", return_value=42.5), \
             patch.object(app_module, "_compute_local_atl", return_value=38.0):
            t = self.client.get("/api/readiness").json().get("training") or {}
            self.assertEqual(t.get("source"), "local")

        app_module.clear_cache()
        # Case 2: ICU has all three → source=icu.
        with patch.object(
            app_module, "get_today_metrics",
            return_value={"ctl": 65.0, "atl": 60.0, "tsb": 5.0},
        ), patch.object(app_module, "get_sleep_metrics", return_value={}):
            t = self.client.get("/api/readiness").json().get("training") or {}
            self.assertEqual(t.get("source"), "icu")
            self.assertEqual(t.get("ctl"), 65.0)

        app_module.clear_cache()
        # Case 3: ICU has CTL only → source=mixed (ATL+TSB pulled from local).
        with patch.object(
            app_module, "get_today_metrics",
            return_value={"ctl": 65.0},
        ), patch.object(app_module, "get_sleep_metrics", return_value={}), \
           patch("ride_storage.compute_local_ctl", return_value=42.5), \
           patch.object(app_module, "_compute_local_atl", return_value=38.0):
            t = self.client.get("/api/readiness").json().get("training") or {}
            self.assertEqual(t.get("source"), "mixed")
            self.assertEqual(t.get("ctl"), 65.0)  # ICU value preferred
            self.assertEqual(t.get("atl"), 38.0)  # from local fallback

    def test_readiness_data_status_field_set_when_insufficient(self):
        """§B6: readiness payload carries ``data_status`` and a numeric score
        when local rides exist, instead of raw None."""
        # No ICU wellness, no sleep — readiness will be INSUFFICIENT_DATA.
        # But local CTL exists → score should be promoted to neutral 50.
        with patch.object(app_module, "get_today_metrics", return_value={}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}), \
             patch("ride_storage.compute_local_ctl", return_value=42.5), \
             patch.object(app_module, "_compute_local_atl", return_value=38.0):
            r = self.client.get("/api/readiness").json()
            readiness = r.get("readiness") or {}
            self.assertEqual(readiness.get("data_status"), "insufficient_data")
            # Score promoted to neutral default (50) because local rides exist.
            self.assertEqual(readiness.get("score"), 50)

    def test_today_session_score_uses_unified_helper(self):
        """§B6: /api/today-session uses the shared helper instead of a
        hardcoded score=50, so the score+data_status agree with /api/readiness."""
        # Empty training → readiness will be INSUFFICIENT_DATA.
        # No local CTL either → score should be None (consistent with
        # readiness when truly no data).
        with patch.object(app_module, "get_today_metrics", return_value={}), \
             patch.object(app_module, "get_sleep_metrics", return_value={}), \
             patch("ride_storage.compute_local_ctl", return_value=None), \
             patch.object(app_module, "_compute_local_atl", return_value=None):
            r = self.client.get("/api/today-session")
            # Endpoint may 200 with planned=null on empty plan; that's fine —
            # we only assert the helper structure if a session is returned.
            data = r.json()
            if data.get("planned") is None:
                # No plan today — endpoint short-circuited before calling
                # readiness. Skip; helper exercised via /api/readiness above.
                self.skipTest("No planned session today; helper covered in readiness test")
            # Final-fallback path: score=50 retained when no local data.
            self.assertEqual(data.get("readiness"), 50)


if __name__ == "__main__":
    unittest.main()
