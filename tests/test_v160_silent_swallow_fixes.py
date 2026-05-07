"""v1.6.0 — silent-swallow site fix tests (the three known render-blockers).

Closes the three holes from /tmp/wave1_silent_swallows.md:
  1. /api/calendar corrupt-plan path now surfaces E_PLAN_PARSE_CORRUPT.
  2. cached() error path returns {} for 30s only (not full 300s).
  3. _enrich_plan_for_response failure logs Codes.ENRICH_FAILED.
"""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import error_codes as ec


def _backup_plan_json():
    p = Path.home() / ".domestique" / "plans" / "current_plan.json"
    bk = p.with_suffix(".v160_swallow_test_backup")
    if p.exists() and not bk.exists():
        p.rename(bk)
    return p, bk


def _restore_plan_json(p: Path, bk: Path):
    if p.exists():
        p.unlink()
    if bk.exists():
        bk.rename(p)


class CorruptPlanCalendarFixTests(unittest.TestCase):
    """Wave 1 §C culprit #1: /api/calendar swallowed corrupt plan and
    returned 12 history-only weeks. v1.6.0 surfaces the error code."""

    def setUp(self):
        self.client = TestClient(app_module.app)
        with app_module._DIAG_RING_LOCK:
            app_module._DIAG_RING.clear()

    def test_corrupt_plan_returns_E_PLAN_PARSE_CORRUPT_in_response(self):
        p, bk = _backup_plan_json()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{this is not json{")
            r = self.client.get("/api/calendar")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertEqual(body.get("error"), ec.Codes.PLAN_PARSE_CORRUPT)
            self.assertEqual(body.get("weeks"), [])
        finally:
            _restore_plan_json(p, bk)

    def test_corrupt_plan_logs_to_ring_buffer(self):
        p, bk = _backup_plan_json()
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("{not json{")
            with app_module._DIAG_RING_LOCK:
                app_module._DIAG_RING.clear()
            self.client.get("/api/calendar")
            with app_module._DIAG_RING_LOCK:
                ring = list(app_module._DIAG_RING)
            codes = [e["code"] for e in ring]
            self.assertIn(ec.Codes.PLAN_PARSE_CORRUPT, codes)
        finally:
            _restore_plan_json(p, bk)


class CachedSwallowFixTests(unittest.TestCase):
    """Wave 1 §C culprit #2: cached() returned {} on fn() raise without
    logging or noting it. v1.6.0 logs E_CACHE_<key> + sticks empty for 30s."""

    def setUp(self):
        # Drain the cache so each test starts fresh
        app_module._cache.clear()
        app_module._cache_ts.clear()

    def test_cached_logs_specific_key_code(self):
        with app_module._DIAG_RING_LOCK:
            app_module._DIAG_RING.clear()

        def boom():
            raise RuntimeError("upstream is down")

        result = app_module.cached("training", boom)
        self.assertEqual(result, {})
        with app_module._DIAG_RING_LOCK:
            ring = list(app_module._DIAG_RING)
        codes = [e["code"] for e in ring]
        self.assertIn(ec.Codes.CACHE_TRAINING, codes,
                      f"expected E_CACHE_TRAINING in ring, got {codes}")

    def test_cached_generic_for_unknown_key(self):
        with app_module._DIAG_RING_LOCK:
            app_module._DIAG_RING.clear()

        def boom():
            raise RuntimeError("x")

        app_module.cached("some_random_key", boom)
        with app_module._DIAG_RING_LOCK:
            ring = list(app_module._DIAG_RING)
        codes = [e["code"] for e in ring]
        self.assertIn(ec.Codes.CACHE_GENERIC, codes)

    def test_cached_error_TTL_is_30s_not_300s(self):
        """The error-empty must expire 30s after caching, not the full
        ttl. Verify by checking _cache_ts is backdated correctly.
        """
        def boom():
            raise RuntimeError("y")

        # use ttl=300 (the default for /api/readiness use)
        app_module.cached("test_key", boom, ttl=300)
        ts = app_module._cache_ts["test_key"]
        now = time.time()
        # ts should be ~ now - 270 (i.e. 30s remaining of 300s window)
        elapsed = now - ts
        self.assertGreaterEqual(elapsed, 269.0, f"elapsed={elapsed}")
        self.assertLessEqual(elapsed, 271.0, f"elapsed={elapsed}")

    def test_cached_short_ttl_does_not_underflow(self):
        """If ttl <= 30, no backdating; cache fills the requested window."""
        def boom():
            raise RuntimeError("z")
        app_module.cached("test_key2", boom, ttl=10)
        ts = app_module._cache_ts["test_key2"]
        now = time.time()
        # ts should be very close to "now"
        self.assertLess(abs(now - ts), 1.0)


class EnrichSwallowFixTests(unittest.TestCase):
    """Wave 1 §C culprit #3: _enrich_plan_for_response wrapped in catch
    that logged at debug-level only. v1.6.0 promotes to E_ENRICH_FAILED."""

    def setUp(self):
        self.client = TestClient(app_module.app)
        with app_module._DIAG_RING_LOCK:
            app_module._DIAG_RING.clear()

    def test_enrich_failure_logs_E_ENRICH_FAILED(self):
        # Patch _enrich_plan_for_response to raise; hit /api/plan
        with patch.object(app_module, "_enrich_plan_for_response",
                          side_effect=RuntimeError("synthetic enrich crash")):
            r = self.client.get("/api/plan")
        # Endpoint must still return 200 — degraded gracefully
        self.assertEqual(r.status_code, 200)
        with app_module._DIAG_RING_LOCK:
            ring = list(app_module._DIAG_RING)
        codes = [e["code"] for e in ring]
        self.assertIn(ec.Codes.ENRICH_FAILED, codes,
                      f"expected E_ENRICH_FAILED, got {codes}")

    def test_enrich_failure_has_endpoint_in_context(self):
        with patch.object(app_module, "_enrich_plan_for_response",
                          side_effect=RuntimeError("crash")):
            self.client.get("/api/plan")
        with app_module._DIAG_RING_LOCK:
            ring = list(app_module._DIAG_RING)
        enrich_entries = [e for e in ring if e["code"] == ec.Codes.ENRICH_FAILED]
        self.assertTrue(enrich_entries, "no E_ENRICH_FAILED entry in ring")
        self.assertEqual(enrich_entries[-1]["context"].get("endpoint"), "/api/plan")


if __name__ == "__main__":
    unittest.main()
