"""v1.8.9 — backend regression tests for Bugs 4 + 7.

Master decisions doc: /tmp/MASTER_DECISIONS_v189_nine_bugs.md
Owner: Wave 2A (backend).

Covered here:
  Bug 4 — /api/profile/fatigue-resistance surfaces `compute_ms`; warm
          calls hit the lru_cache wrapper.
  Bug 7 — /api/profile/dfa-alpha1 always returns 200, with the
          {value, n_rides, message} shape per master §10.

Bugs 1, 2, 3, 9 live in tests/test_power_curve.py,
tests/test_v188_backfill_reasons.py, tests/test_v188_apply_rest_day.py.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


class TestBug7DfaAlpha1Endpoint(unittest.TestCase):
    """Master §7 — endpoint returns 200 with locked shape regardless of data."""

    def setUp(self):
        self.client = TestClient(app_module.app)

    def test_no_hrv_rides_returns_200_with_null_value(self):
        """No HRV-tagged rides → 200 + {value: null, n_rides: 0, message: ...}."""
        with patch.object(app_module, "_recent_dfa_and_decoupling",
                          return_value=([], None)):
            r = self.client.get("/api/profile/dfa-alpha1")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIsNone(data["value"])
        self.assertEqual(data["n_rides"], 0)
        self.assertIn("HRV", data["message"])

    def test_one_hrv_ride_returns_value(self):
        """One HRV ride → 200 + numeric value, n_rides=1."""
        with patch.object(app_module, "_recent_dfa_and_decoupling",
                          return_value=([0.88], 5.0)):
            r = self.client.get("/api/profile/dfa-alpha1")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertAlmostEqual(data["value"], 0.88, places=2)
        self.assertEqual(data["n_rides"], 1)
        self.assertIn("0.88", data["message"])

    def test_three_hrv_rides_returns_average(self):
        """Three HRV rides → average value."""
        with patch.object(app_module, "_recent_dfa_and_decoupling",
                          return_value=([0.80, 0.90, 1.00], None)):
            r = self.client.get("/api/profile/dfa-alpha1")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertAlmostEqual(data["value"], 0.90, places=2)
        self.assertEqual(data["n_rides"], 3)


class TestBug4FatigueComputeMs(unittest.TestCase):
    """Master §4 — endpoint surfaces compute_ms, warm path uses lru_cache."""

    def setUp(self):
        self.client = TestClient(app_module.app)
        # Reset both caches before each test so timings are deterministic.
        app_module._cache.clear()
        app_module._cache_ts.clear()
        try:
            app_module._fatigue_resistance_memoised.cache_clear()
        except Exception:
            pass

    def test_compute_ms_field_present(self):
        """Response always carries a numeric compute_ms field."""
        fake_result = {
            "window_days": 365,
            "n_long_rides": 0,
            "n_long_rides_with_streams": 0,
            "fit_status": "insufficient_data",
            "reason": "no_rides_in_window",
            "kj_threshold": 1500,
            "robustness_score": None,
            "by_duration": [],
            "scatter": [],
        }
        with patch("power_curve.compute_fatigue_resistance",
                   return_value=fake_result), \
             patch("power_curve.latest_ride_id_in_window",
                   return_value="icu_iTEST"):
            r = self.client.get("/api/profile/fatigue-resistance")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("compute_ms", data)
        self.assertIsInstance(data["compute_ms"], int)
        self.assertGreaterEqual(data["compute_ms"], 0)

    def test_warm_cache_short_circuits_compute(self):
        """Second identical call returns the cached result without hitting
        the underlying compute again — confirms cache wiring works."""
        fake_result = {
            "window_days": 365,
            "n_long_rides": 0,
            "n_long_rides_with_streams": 0,
            "fit_status": "insufficient_data",
            "reason": "no_rides_in_window",
            "kj_threshold": 1500,
            "robustness_score": None,
            "by_duration": [],
            "scatter": [],
        }
        with patch("power_curve.compute_fatigue_resistance",
                   return_value=fake_result) as m_compute, \
             patch("power_curve.latest_ride_id_in_window",
                   return_value="icu_iTEST"):
            r1 = self.client.get("/api/profile/fatigue-resistance")
            r2 = self.client.get("/api/profile/fatigue-resistance")
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # The module-level _cache short-circuits before the lru_cache, so
        # only the first call should reach compute_fatigue_resistance.
        self.assertLessEqual(m_compute.call_count, 1,
            "warm path should not re-invoke compute_fatigue_resistance")
        # Both responses carry compute_ms.
        self.assertIn("compute_ms", r1.json())
        self.assertIn("compute_ms", r2.json())

    def test_refresh_busts_lru_cache(self):
        """?refresh=1 forces a re-compute even when the lru_cache has a hit."""
        fake_result = {
            "window_days": 365,
            "n_long_rides": 0,
            "n_long_rides_with_streams": 0,
            "fit_status": "insufficient_data",
            "reason": "no_rides_in_window",
            "kj_threshold": 1500,
            "robustness_score": None,
            "by_duration": [],
            "scatter": [],
        }
        with patch("power_curve.compute_fatigue_resistance",
                   return_value=fake_result) as m_compute, \
             patch("power_curve.latest_ride_id_in_window",
                   return_value="icu_iTEST"):
            self.client.get("/api/profile/fatigue-resistance")
            self.client.get("/api/profile/fatigue-resistance?refresh=1")
        # First call computes; refresh=1 also computes (bypass cache).
        self.assertGreaterEqual(m_compute.call_count, 2)


if __name__ == "__main__":
    unittest.main()
