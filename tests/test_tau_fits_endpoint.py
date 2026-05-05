"""v1.0.7 IMPL-TAU-FIT-WIRING — /api/profile/tau-fits endpoint tests.

Locks the GET /api/profile/tau-fits response shape and the
training._effective_taus_from_db() reader behaviour.

Tests:
  1. Endpoint response shape — all tau keys, status enum, conventional block.
  2. Insufficient-data fallback — empty DB returns fit_status='insufficient_data',
     all tau values None, conventional block populated.
  3. Endpoint never writes nls_fit rows (persist=False contract honoured).
  4. training._effective_taus_from_db() respects manual > nls_fit > conventional.
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import date
from pathlib import Path


class TauFitsEndpointTests(unittest.TestCase):
    """1-3. /api/profile/tau-fits behaviour."""

    def setUp(self):
        import db
        self.tmp = Path(tempfile.mkdtemp())
        db.set_db_path(self.tmp / "tau_fits.db")
        db.close_all_connections()
        db.init_db()
        self._db = db

        from fastapi.testclient import TestClient
        import app as _app_module
        self.client = TestClient(_app_module.app)

    def tearDown(self):
        self._db.close_all_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_endpoint_returns_required_shape(self):
        """1. Response has every locked field for the UI."""
        r = self.client.get("/api/profile/tau-fits")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        for k in (
            "ctl_tau_fit", "atl_tau_fit",
            "ctl_tau_ci_low", "ctl_tau_ci_high",
            "atl_tau_ci_low", "atl_tau_ci_high",
            "cp_tau1_fit", "cp_tau2_fit",
            "wprime_tau1_fit", "wprime_tau2_fit",
            "pmax_tau1_fit", "pmax_tau2_fit",
            "fit_residual_r2", "n_markers", "weighted_n",
            "fit_horizon_days", "fit_status", "conventional",
        ):
            self.assertIn(k, d, f"endpoint missing required key: {k}")
        self.assertIn(d["fit_status"],
                      ("success", "low_confidence", "insufficient_data"))
        conv = d["conventional"]
        self.assertEqual(conv["ctl_tau"], 42.0)
        self.assertEqual(conv["atl_tau"], 7.0)
        self.assertEqual(conv["cp_tau1"], 52.0)

    def test_endpoint_insufficient_data_fallback(self):
        """2. Empty DB returns insufficient_data + None tau values."""
        r = self.client.get("/api/profile/tau-fits")
        d = r.json()
        self.assertEqual(d["fit_status"], "insufficient_data")
        self.assertIsNone(d["ctl_tau_fit"])
        self.assertIsNone(d["atl_tau_fit"])
        self.assertEqual(d["conventional"]["ctl_tau"], 42.0)

    def test_endpoint_does_not_persist_nls_fit_rows(self):
        """3. PATCH G4 - endpoint reads tau_fitting with persist=False."""
        for _ in range(3):
            self.client.get("/api/profile/tau-fits")
        conn = self._db.get_db()
        rows = conn.execute(
            "SELECT * FROM athlete_metrics WHERE source = 'nls_fit'"
        ).fetchall()
        self.assertEqual(len(rows), 0,
                         f"endpoint must not persist; got rows={rows}")


class EffectiveTausReaderTests(unittest.TestCase):
    """4. training._effective_taus_from_db source-tier ladder."""

    def setUp(self):
        import db
        self.tmp = Path(tempfile.mkdtemp())
        db.set_db_path(self.tmp / "eff_taus.db")
        db.close_all_connections()
        db.init_db()
        self._db = db

    def tearDown(self):
        self._db.close_all_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_falls_back_to_conventional_on_empty_db(self):
        """4a. No rows -> conventional defaults; ctl_atl_source='conventional'."""
        import training
        out = training._effective_taus_from_db()
        self.assertEqual(out["ctl_tau"], 42.0)
        self.assertEqual(out["atl_tau"], 7.0)
        self.assertEqual(out["cp_tau1"], 52.0)
        self.assertEqual(out["ctl_atl_source"], "conventional")

    def test_nls_fit_overrides_conventional(self):
        """4b. nls_fit row -> reader adopts the fitted tau."""
        import training
        today = date.today().isoformat()
        self._db.log_metric(today, "ctl_tau_fit", 38.4, source="nls_fit")
        self._db.log_metric(today, "atl_tau_fit", 8.1, source="nls_fit")
        out = training._effective_taus_from_db()
        self.assertAlmostEqual(out["ctl_tau"], 38.4)
        self.assertAlmostEqual(out["atl_tau"], 8.1)
        self.assertEqual(out["ctl_atl_source"], "nls_fit")

    def test_manual_beats_nls_fit(self):
        """4c. manual row at the same date wins - manual > nls_fit."""
        import training
        today = date.today().isoformat()
        self._db.log_metric(today, "ctl_tau_fit", 38.4, source="nls_fit")
        self._db.log_metric(today, "ctl_tau_fit", 50.0, source="manual")
        out = training._effective_taus_from_db()
        self.assertAlmostEqual(out["ctl_tau"], 50.0)
        self.assertEqual(out["ctl_atl_source"], "manual")


if __name__ == "__main__":
    unittest.main()
