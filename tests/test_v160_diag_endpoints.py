"""v1.6.0 — /api/diag/* endpoint contract tests.

Covers:
  - GET /api/diag/health returns 200, ``ok`` boolean, ``checks`` dict
  - corrupted current_plan.json → checks.plan_readable.code = E_PLAN_PARSE_CORRUPT
  - POST /api/diag/frontend-error with valid + invalid codes → both 200,
    invalid coerced to E_FRONTEND_GENERIC
  - GET /api/diag/recent-errors?limit=N respects the limit parameter
  - context scrubbing strips ``path`` etc unless verbose=1
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


import contextlib
import tempfile
from unittest import mock


@contextlib.contextmanager
def _corrupt_plan(text: str):
    """v3.0.0: isolated tempdir + app._plan_dir patch (see the note in
    test_v160_silent_swallow_fixes — the old helper touched the REAL
    ~/.domestique plan and broke under per-profile plans + parallel runs)."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "current_plan.json"
        p.write_text(text)
        with mock.patch.object(app_module, "_plan_dir", lambda: Path(td)):
            yield p


class DiagHealthTests(unittest.TestCase):
    def setUp(self):
        # Reset the 60s health cache between tests so each test gets a
        # fresh evaluation against current disk state.
        app_module._DIAG_HEALTH_CACHE["result"] = None
        app_module._DIAG_HEALTH_CACHE["ts"] = 0.0
        self.client = TestClient(app_module.app)

    def test_health_endpoint_returns_200_with_shape(self):
        r = self.client.get("/api/diag/health")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertIn("ok", body)
        self.assertIn("checks", body)
        self.assertIn("ts", body)
        self.assertIn("ring_size", body)
        # Five checks declared in app.py
        for check in ("plan_readable", "workout_library", "enrich",
                      "rides_dir", "log_dir"):
            self.assertIn(check, body["checks"], f"missing check: {check}")

    def test_health_with_corrupt_plan_flags_E_PLAN_PARSE_CORRUPT(self):
        with _corrupt_plan("{ this is not json"):
            # bust the 60s cache
            app_module._DIAG_HEALTH_CACHE["result"] = None
            app_module._DIAG_HEALTH_CACHE["ts"] = 0.0
            r = self.client.get("/api/diag/health")
            self.assertEqual(r.status_code, 200)
            body = r.json()
            self.assertFalse(body["ok"], "ok should be False with corrupt plan")
            self.assertEqual(
                body["checks"]["plan_readable"]["code"],
                ec.Codes.PLAN_PARSE_CORRUPT,
            )

    def test_health_endpoint_caches_for_60s(self):
        r1 = self.client.get("/api/diag/health").json()
        ts1 = r1["ts"]
        r2 = self.client.get("/api/diag/health").json()
        ts2 = r2["ts"]
        self.assertEqual(ts1, ts2, "health endpoint should cache identical response")


class DiagFrontendErrorTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)
        # Drain ring so test starts clean
        with app_module._DIAG_RING_LOCK:
            app_module._DIAG_RING.clear()

    def test_valid_code_is_logged_verbatim(self):
        r = self.client.post("/api/diag/frontend-error", json={
            "code": "E_FRONTEND_LOADHOME",
            "context": {"msg": "test"},
            "url": "http://localhost/",
            "user_agent": "pytest",
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["code"], "E_FRONTEND_LOADHOME")

    def test_unknown_code_coerced_to_generic(self):
        r = self.client.post("/api/diag/frontend-error", json={
            "code": "E_GHOST_MADE_UP",
            "context": {},
        })
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["code"], "E_FRONTEND_GENERIC")

    def test_missing_body_returns_200_with_generic_code(self):
        # FastAPI rejects missing-body for json parsers but our handler
        # catches any json failure and proceeds.
        r = self.client.post(
            "/api/diag/frontend-error",
            content="not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(r.status_code, 200)


class DiagRecentErrorsTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)
        with app_module._DIAG_RING_LOCK:
            app_module._DIAG_RING.clear()

    def test_recent_errors_respects_limit(self):
        for i in range(7):
            app_module._log_error(ec.Codes.CACHE_GENERIC,
                                  exc=ValueError(f"e{i}"),
                                  cache_key=f"k{i}")
        r = self.client.get("/api/diag/recent-errors?limit=3")
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["count"], 3)
        self.assertEqual(len(body["items"]), 3)

    def test_recent_errors_strips_pii_keys_unless_verbose(self):
        app_module._log_error(ec.Codes.PLAN_PARSE_CORRUPT,
                              exc=ValueError("bad"),
                              path="/secret/path/plan.json",
                              other_safe="safe-value")
        r = self.client.get("/api/diag/recent-errors?limit=5")
        items = r.json()["items"]
        self.assertEqual(len(items), 1)
        ctx = items[0]["context"]
        self.assertNotIn("path", ctx)
        self.assertEqual(ctx["other_safe"], "safe-value")

        rv = self.client.get("/api/diag/recent-errors?limit=5&verbose=1")
        ctxv = rv.json()["items"][0]["context"]
        self.assertEqual(ctxv["path"], "/secret/path/plan.json")

    def test_recent_errors_newest_first(self):
        for i in range(3):
            app_module._log_error(ec.Codes.CACHE_GENERIC,
                                  exc=ValueError(f"e{i}"),
                                  cache_key=f"k{i}")
            time.sleep(0.001)
        r = self.client.get("/api/diag/recent-errors?limit=3")
        items = r.json()["items"]
        self.assertEqual(items[0]["context"]["cache_key"], "k2")
        self.assertEqual(items[2]["context"]["cache_key"], "k0")


if __name__ == "__main__":
    unittest.main()
