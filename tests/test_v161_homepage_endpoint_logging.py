"""v1.6.1 — homepage backend-endpoint logging tests.

Asserts the new ``E_*`` codes land in the diag ring buffer when the
homepage endpoints fail. Uses ``unittest.mock.patch`` to inject a
synthetic failure into one of each endpoint's compute helpers, then
hits the route and inspects ``app._DIAG_RING``.

Status code check: each endpoint surfaces the exception (FastAPI
returns 500), but the structured code ALWAYS lands in the ring before
the re-raise. The test only asserts the ring; status-code stability
across endpoints depends on internal cache state and is not the focus.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import error_codes as ec


def _ring_codes():
    with app_module._DIAG_RING_LOCK:
        return [e["code"] for e in list(app_module._DIAG_RING)]


def _ring_clear():
    with app_module._DIAG_RING_LOCK:
        app_module._DIAG_RING.clear()


def _drain_caches():
    """Clear the cached() short-TTL backing dict so synthetic failures
    inject into the actual compute helper rather than returning a stale
    {}.
    """
    app_module._cache.clear()
    app_module._cache_ts.clear()


class WellnessEndpointLoggingTests(unittest.TestCase):
    def setUp(self):
        _ring_clear()
        _drain_caches()
        self.client = TestClient(app_module.app, raise_server_exceptions=False)

    def test_wellness_failure_logs_E_WELLNESS_FETCH_FAILED(self):
        """Patch _augment_wellness_with_3d_fitness to raise; the outer
        try in /api/wellness logs E_WELLNESS_FETCH_FAILED before
        re-raising. _augment_wellness is on the SQLite-fallback path so
        we must also stub fetch_wellness so the flow reaches it.
        """
        with patch.object(app_module, "_augment_wellness_with_3d_fitness",
                          side_effect=RuntimeError("synthetic augment crash")):
            self.client.get("/api/wellness?days=7")
        codes = _ring_codes()
        self.assertIn(ec.Codes.WELLNESS_FETCH_FAILED, codes,
                      f"expected E_WELLNESS_FETCH_FAILED, got {codes}")


class ActivitiesEndpointLoggingTests(unittest.TestCase):
    def setUp(self):
        _ring_clear()
        _drain_caches()
        self.client = TestClient(app_module.app, raise_server_exceptions=False)

    def test_activities_failure_logs_E_ACTIVITIES_LIST_FAILED(self):
        """Force the SQLite final-fallback path by emptying the upstream
        sources (cached training + _load_all_rides_safe), then patch
        db.query_activities to raise so the outer try in /api/activities
        catches + logs E_ACTIVITIES_LIST_FAILED before re-raising.
        """
        # Make cached() short-circuit to {} for "training" so
        # recent_activities=[] and the SQLite branch is reached.
        original_cached = app_module.cached
        def _stub_cached(key, fn, ttl=300):
            if key == "training":
                return {}
            return original_cached(key, fn, ttl)
        with patch.object(app_module, "cached", side_effect=_stub_cached), \
             patch.object(app_module, "_load_all_rides_safe", return_value=[]), \
             patch.object(app_module.db, "query_activities",
                          side_effect=RuntimeError("synthetic activities crash")):
            self.client.get("/api/activities")
        codes = _ring_codes()
        self.assertIn(ec.Codes.ACTIVITIES_LIST_FAILED, codes,
                      f"expected E_ACTIVITIES_LIST_FAILED, got {codes}")


class TodaySessionEndpointLoggingTests(unittest.TestCase):
    def setUp(self):
        _ring_clear()
        _drain_caches()
        self.client = TestClient(app_module.app, raise_server_exceptions=False)

    def test_today_session_failure_logs_E_TODAY_SESSION_LOOKUP_FAILED(self):
        """Patch the impl helper to raise; the public api_today_session
        wrapper logs E_TODAY_SESSION_LOOKUP_FAILED before re-raising.
        """
        with patch.object(app_module, "_api_today_session_impl",
                          side_effect=RuntimeError("synthetic today crash")):
            self.client.get("/api/today-session")
        codes = _ring_codes()
        self.assertIn(ec.Codes.TODAY_SESSION_LOOKUP_FAILED, codes,
                      f"expected E_TODAY_SESSION_LOOKUP_FAILED, got {codes}")


if __name__ == "__main__":
    unittest.main()
