"""Issue #3 R2 — the unified "Today" card reads ONE canonical endpoint.

The home page used to show 5 separate readiness/today surfaces with two
different scales (0-10 composite vs 0-100) and three action voices, which read
as contradictions. The fix consolidates them into one card sourced from
``/api/readiness`` — so that endpoint must now also carry the Hooper/composite
``severity`` (the action driver) alongside the canonical ``score_0_100``.

This guards the backend half: severity/source/severity_reasons are chained onto
``/api/readiness`` (additive; absence tolerated as normal).
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


class TestUnifiedTodayEndpoint(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)

    def _stub_upstream(self):
        fake_training = {"ctl": 50, "atl": 40, "tsb": 10}
        fake_sleep = {
            "ln_rmssd_7d": 3.5, "swc_lower": 2.5, "swc_upper": 4.0,
            "sleep_h": 7.5, "rhr_today": 50, "rhr_delta": -1,
            "hrv_ms": 60, "hrv_status": "ok", "rhr_status": "ok",
            "sleep_score": 80,
        }
        return (
            patch.object(app_module, "cached",
                         side_effect=lambda k, fn: fake_training if k == "training" else fake_sleep),
            patch.object(app_module, "_local_sleep_metrics", return_value={}),
            patch.object(app_module, "_get_soreness_subjective", return_value=7.0),
            patch.object(app_module, "_recent_dfa_and_decoupling",
                         return_value=([], None, None, None)),
        )

    def test_readiness_carries_severity(self):
        """`/api/readiness` exposes severity/source/severity_reasons from the
        chained compute_training_severity, so the unified card needs no second
        (deprecated) endpoint for the action."""
        sev = {"score": None, "severity": "tier_down", "source": "hooper",
               "reasons": ["Hooper index 15 in 14-17 — tier-down recommended"],
               "hooper_index": 15, "tsb": 10}
        c1, c2, c3, c4 = self._stub_upstream()
        with c1, c2, c3, c4, \
             patch("readiness_composite.compute_training_severity", return_value=sev):
            r = self.client.get("/api/readiness")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data.get("severity"), "tier_down")
        self.assertEqual(data.get("source"), "hooper")
        self.assertIn("severity_reasons", data)
        self.assertTrue(data["severity_reasons"])
        # The canonical 0-100 number must still be present (single scale).
        self.assertIn("score_0_100", data)

    def test_severity_absence_is_tolerated(self):
        """If compute_training_severity raises, the endpoint still returns 200
        with severity=None (card falls back to 'train as planned')."""
        c1, c2, c3, c4 = self._stub_upstream()
        with c1, c2, c3, c4, \
             patch("readiness_composite.compute_training_severity",
                   side_effect=RuntimeError("no daily_log table")):
            r = self.client.get("/api/readiness")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIsNone(data.get("severity"))
        self.assertEqual(data.get("severity_reasons"), [])


class TestUnifiedTodayMarkup(unittest.TestCase):
    """Frontend guard: the 3 separate home surfaces are merged into one card."""

    @classmethod
    def setUpClass(cls):
        from pathlib import Path
        cls.html = (Path(__file__).resolve().parent.parent
                    / "src" / "templates" / "dashboard.html").read_text(encoding="utf-8")

    def test_one_today_card(self):
        self.assertEqual(self.html.count('id="today-card"'), 1)
        self.assertEqual(self.html.count('id="today-verdict"'), 1)

    def test_prescription_and_legcheck_moved_into_card(self):
        # The prescription + leg-check hosts still exist (moved, not deleted)…
        self.assertEqual(self.html.count('id="home-recommendation"'), 1)
        self.assertEqual(self.html.count('id="morning-log-content"'), 1)
        # …and both now sit AFTER the unified card opens (i.e. inside it),
        # before the next sibling card ("This Week").
        card = self.html.index('id="today-card"')
        week = self.html.index('>This Week<')
        for needle in ('id="today-verdict"', 'id="home-recommendation"',
                       'id="morning-log-content"', 'id="home-snapshot-dfa"'):
            pos = self.html.index(needle)
            self.assertTrue(card < pos < week, f"{needle} not inside #today-card")

    def test_old_separate_surfaces_removed(self):
        for dead in ('id="readiness-composite-card"', 'id="readiness-composite-content"',
                     'id="morning-log-card"', "<h3>Today's Recommendation</h3>",
                     "<h3>Readiness today</h3>", "<h3>Leg Check</h3>"):
            self.assertNotIn(dead, self.html, f"stale surface still present: {dead}")

    def test_verdict_reads_canonical_endpoint(self):
        # loadReadinessComposite must fetch the canonical /api/readiness, not the
        # deprecated composite endpoint (only the in-comment mention may remain).
        self.assertIn("getElementById('today-verdict')", self.html)
        self.assertNotIn("fetch('/api/readiness/composite')", self.html)


if __name__ == "__main__":
    unittest.main()
