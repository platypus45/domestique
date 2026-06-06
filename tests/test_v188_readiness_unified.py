"""v1.8.8 Bug 8 — unified readiness scales.

Master decisions §Bug 8: ``/api/readiness`` returns BOTH ``score_0_10``
AND ``score_0_100``. ``/api/readiness/composite`` is marked
``deprecated: true`` in its payload.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


class TestReadinessUnified(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)

    def test_readiness_returns_both_scales(self):
        """`/api/readiness` carries score_0_10 and score_0_100 as top-level keys."""
        # Stub the upstream metric fetchers so we always have enough
        # components to compute a real score regardless of environment.
        fake_training = {"ctl": 50, "atl": 40, "tsb": 10}
        fake_sleep = {
            "ln_rmssd_7d": 3.5, "swc_lower": 2.5, "swc_upper": 4.0,
            "sleep_h": 7.5, "rhr_today": 50, "rhr_delta": -1,
            "hrv_ms": 60, "hrv_status": "ok", "rhr_status": "ok",
            "sleep_score": 80,
        }
        with patch.object(app_module, "cached",
                          side_effect=lambda k, fn: fake_training if k == "training" else fake_sleep), \
             patch.object(app_module, "_local_sleep_metrics", return_value={}), \
             patch.object(app_module, "_get_soreness_subjective", return_value=7.0), \
             patch.object(app_module, "_recent_dfa_and_decoupling",
                          return_value=([], None, None, None)):  # v1.8.16 4-tuple
            r = self.client.get("/api/readiness")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("score_0_10", data, f"missing score_0_10 in {data.keys()}")
        self.assertIn("score_0_100", data, f"missing score_0_100 in {data.keys()}")
        s10 = data["score_0_10"]
        s100 = data["score_0_100"]
        # Both should be numeric (or both None if compute_readiness bailed).
        self.assertIsInstance(s10, (int, float))
        self.assertIsInstance(s100, (int, float))
        # Consistency: score_0_100 ≈ score_0_10 * 10 (±2 tolerance).
        self.assertAlmostEqual(s100, s10 * 10, delta=2.0)

    def test_readiness_composite_marked_deprecated(self):
        """/api/readiness/composite payload carries ``deprecated: true``."""
        with patch("readiness_composite.compute_readiness_composite",
                   return_value={"score": 6.5, "status": "static_weights",
                                 "components": {}, "weights": {},
                                 "confidence": 1.0, "advice": ""}), \
             patch("readiness_composite.compute_training_severity",
                   return_value={}):
            r = self.client.get("/api/readiness/composite")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertTrue(data.get("deprecated"),
                        f"expected deprecated=true in {data}")


if __name__ == "__main__":
    unittest.main()
