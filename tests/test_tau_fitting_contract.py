"""v1.0.7 IMPL-TAU-FIT-CORE — CONTRACT lock.

Per /tmp/MASTER_DECISIONS_v107_v110_v120_PATCH.md G14, this test locks the
``fit_tau_per_athlete(profile_id, persist, horizon_end_date)`` signature
+ semantics + the ``count_weighted_markers`` weighting against PATCH G9.
v1.2.0's IMPL-V120-OOS-VALIDATION agent depends on these guarantees.

  A. The function signature accepts both positional and keyword
     ``persist`` and ``horizon_end_date``.
  B. ``fit_tau_per_athlete(profile_id, persist=False)`` does NOT write
     to ``athlete_metrics`` (so v1.2.0 holdout-fits don't pollute live
     state).
  C. ``fit_tau_per_athlete(profile_id, horizon_end_date=date(2025,12,1))``
     excludes data after that date.
  D. ``count_weighted_markers`` returns the expected weighted total for
     the locked fixture: 2 races + 3 eFTP step changes + 1 FTP test
     = 2.0 + 1.5 + 0.8 = 4.3.

NOTE: do NOT relax these contracts. v1.2.0 ships against this exact API.
"""
from __future__ import annotations

import inspect
import shutil
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

import tau_fitting


class TestSignatureContract(unittest.TestCase):
    """A — fit_tau_per_athlete signature lock."""

    def test_signature_accepts_persist_and_horizon_end_date(self):
        sig = inspect.signature(tau_fitting.fit_tau_per_athlete)
        params = sig.parameters
        # All three locked parameters must be present in the right order.
        self.assertEqual(list(params.keys()),
                         ["profile_id", "persist", "horizon_end_date"])
        # Defaults: persist=True, horizon_end_date=None.
        self.assertTrue(params["persist"].default is True)
        self.assertTrue(params["horizon_end_date"].default is None)

    def test_count_weighted_markers_signature(self):
        sig = inspect.signature(tau_fitting.count_weighted_markers)
        params = sig.parameters
        self.assertEqual(list(params.keys()), ["profile_id", "horizon_days"])

    def test_callable_with_positional_and_keyword(self):
        """Signature must accept BOTH calling conventions.

        v1.2.0 will call ``fit_tau_per_athlete(pid, persist=False, ...)``
        with keyword args; preserving positional support keeps the v1.0.7
        WIRING agent's options open.
        """
        # Build a tmp DB so the function doesn't crash on `db.get_db()`.
        import db
        tmp = Path(tempfile.mkdtemp())
        try:
            db.set_db_path(tmp / "contract.db")
            db.close_all_connections()
            db.init_db()
            # Positional persist
            r1 = tau_fitting.fit_tau_per_athlete("default", False)
            self.assertEqual(r1["fit_status"], "insufficient_data")
            # Keyword persist
            r2 = tau_fitting.fit_tau_per_athlete("default", persist=False)
            self.assertEqual(r2["fit_status"], "insufficient_data")
            # Both kwargs
            r3 = tau_fitting.fit_tau_per_athlete(
                "default", persist=False,
                horizon_end_date=date(2025, 12, 1),
            )
            self.assertEqual(r3["fit_status"], "insufficient_data")
        finally:
            db.close_all_connections()
            shutil.rmtree(tmp, ignore_errors=True)


class TestPersistFalseDoesNotWrite(unittest.TestCase):
    """B — persist=False MUST NOT write nls_fit rows.

    v1.2.0 OOS-validation calls fit_tau_per_athlete(persist=False) every
    holdout fold. If the v1.0.7 implementation drifts and starts writing
    rows on persist=False, the holdout fits would silently pollute the
    live athlete_metrics table — the entire OOS validation becomes a
    self-fulfilling prophecy.
    """

    def setUp(self):
        import db
        self.tmp = Path(tempfile.mkdtemp())
        db.set_db_path(self.tmp / "contract.db")
        db.close_all_connections()
        db.init_db()
        self._db = db
        self._populate_for_success_fit()

    def tearDown(self):
        self._db.close_all_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _populate_for_success_fit(self):
        """Seed enough data to clear the success gate, then check that
        persist=False still doesn't write.

        Populates a 12-month synthetic log so the fit has any chance of
        clearing weighted_n ≥ 10, then if/when it does we still must
        observe zero nls_fit rows after persist=False.
        """
        # Inline a minimal synthetic-log builder rather than depending on
        # tests/test_tau_fitting.py — the latter isn't a sibling-import
        # under pytest's rootdir layout.
        import numpy as np
        import sqlite3
        from datetime import date, timedelta
        conn = self._db.get_db()
        # WIRING's db.init_db() now adds is_race by default — swallow the
        # duplicate-column error so this still works against a pre-WIRING DB.
        try:
            conn.execute("ALTER TABLE activities ADD COLUMN is_race INTEGER DEFAULT 0")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

        rng = np.random.default_rng(2026)
        days = 365
        today = date.today()
        start = today - timedelta(days=days - 1)
        # Daily TSS with weekly periodicity; rest Mon/Sun.
        for i in range(days):
            d = start + timedelta(days=i)
            dow = d.weekday()
            if dow in (0, 6):
                tss = 0.0
            elif dow in (1, 3):
                tss = 120.0 + rng.normal(0, 8)
            elif dow == 5:
                tss = 160.0 + rng.normal(0, 12)
            else:
                tss = 40.0 + rng.normal(0, 5)
            tss = max(0.0, tss)
            conn.execute(
                "INSERT INTO activities (id, date, name, sport, duration_sec, tss, is_race) "
                "VALUES (?, ?, 'synthetic', 'Ride', 3600, ?, 0)",
                (f"synth-{i}", d.isoformat(), tss),
            )
        # Weekly eFTP rows trending up over the year (ensures step changes).
        for w in range(0, days, 7):
            d = start + timedelta(days=w)
            conn.execute(
                "INSERT INTO athlete_metrics (date, metric, value, source) "
                "VALUES (?, 'eftp', ?, 'intervals.icu')",
                (d.isoformat(), 220.0 + (w * 0.05) + rng.normal(0, 1.0)),
            )
        # 12 races so weighted_n is comfortably ≥ 10.
        for i in range(0, 84, 7):
            conn.execute(
                "UPDATE activities SET is_race = 1 WHERE id = ?",
                (f"synth-{i}",),
            )
        conn.commit()

    def test_persist_false_writes_no_nls_fit_rows(self):
        result = tau_fitting.fit_tau_per_athlete(
            "default", persist=False,
        )
        # Even if fit_status is 'success', no row should be written.
        conn = self._db.get_db()
        rows = conn.execute(
            "SELECT * FROM athlete_metrics WHERE source = 'nls_fit'"
        ).fetchall()
        self.assertEqual(len(rows), 0,
                         f"persist=False MUST NOT write — got rows={rows}, "
                         f"fit_status={result['fit_status']}")


class TestHorizonEndDateExcludesLaterData(unittest.TestCase):
    """C — horizon_end_date excludes activity / metric rows after that date.

    v1.2.0 fits on N-4-weeks-ago to keep a holdout block. If horizon_end_date
    isn't honoured, the holdout block leaks into the fit and the OOS
    validation becomes meaningless.
    """

    def setUp(self):
        import db
        self.tmp = Path(tempfile.mkdtemp())
        db.set_db_path(self.tmp / "contract.db")
        db.close_all_connections()
        db.init_db()
        self._db = db

    def tearDown(self):
        self._db.close_all_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_horizon_end_date_truncates_eftp_inputs(self):
        """An eFTP row dated AFTER horizon_end_date must NOT be in the
        n_markers count returned by the fit.

        We don't directly inspect the internal data slice — we observe
        the n_markers field of the result dict, which is the count of
        eFTP rows that made it into the fit window.
        """
        conn = self._db.get_db()
        # Insert 10 eFTP rows: 5 in 2025 (before horizon end) + 5 in 2026.
        for i in range(5):
            conn.execute(
                "INSERT INTO athlete_metrics (date, metric, value, source) "
                "VALUES (?, 'eftp', ?, 'intervals.icu')",
                (f"2025-0{6 + i}-01", 220.0 + i),
            )
        for i in range(5):
            conn.execute(
                "INSERT INTO athlete_metrics (date, metric, value, source) "
                "VALUES (?, 'eftp', ?, 'intervals.icu')",
                (f"2026-0{1 + i}-01", 230.0 + i),
            )
        conn.commit()

        # horizon_end=2025-12-01 should truncate the 2026 rows.
        result = tau_fitting.fit_tau_per_athlete(
            "default",
            persist=False,
            horizon_end_date=date(2025, 12, 1),
        )
        # We seeded 5 markers ≤ 2025-12-01; n_markers should be 5
        # (independent of fit_status which is naturally insufficient_data
        # at this scale, but the n_markers count is what we're asserting).
        self.assertEqual(result["n_markers"], 5,
                         f"horizon_end_date didn't truncate; got "
                         f"n_markers={result['n_markers']}, "
                         f"fit_status={result['fit_status']}")


class TestCountWeightedMarkersFixture(unittest.TestCase):
    """D — count_weighted_markers fixture lock.

    PATCH G14 specifies: "2 races + 3 eFTP steps + 1 FTP test → 4.3".
    The breakdown is:

      * 2 races × 1.0 = 2.0
      * 3 eFTP step changes × 0.5 = 1.5
      * 1 FTP test × 0.8 = 0.8

    Total = 4.3
    """

    def setUp(self):
        import db
        self.tmp = Path(tempfile.mkdtemp())
        db.set_db_path(self.tmp / "contract.db")
        db.close_all_connections()
        db.init_db()
        self._db = db

    def tearDown(self):
        self._db.close_all_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fixture_yields_4_point_3(self):
        import sqlite3
        conn = self._db.get_db()
        # Add is_race column up front — production schema (post-WIRING)
        # has it; the contract test is for the post-WIRING world. Idempotent
        # since WIRING's db.init_db() now adds it by default (PATCH G11).
        try:
            conn.execute("ALTER TABLE activities ADD COLUMN is_race INTEGER DEFAULT 0")
        except sqlite3.OperationalError as e:
            if "duplicate column" not in str(e).lower():
                raise

        today = date.today()
        # 2 races
        for i, days_ago in enumerate((10, 50)):
            d = (today - timedelta(days=days_ago)).isoformat()
            conn.execute(
                "INSERT INTO activities "
                "(id, date, name, sport, duration_sec, tss, is_race) "
                "VALUES (?, ?, 'Race A', 'Ride', 3600, 100.0, 1)",
                (f"race-{i}", d),
            )
        # 1 FTP test (named 'ftp_test' in activities — count_weighted_markers
        # picks this up via name substring).
        ftp_test_date = (today - timedelta(days=120)).isoformat()
        conn.execute(
            "INSERT INTO activities "
            "(id, date, name, sport, duration_sec, tss, is_race) "
            "VALUES ('ftp-1', ?, 'ftp_test_2026-01', 'Ride', 3600, 90.0, 0)",
            (ftp_test_date,),
        )
        # 4 eFTP rows giving 3 step changes ≥ 3 W.
        # 220 → 224 (Δ4) → 228 (Δ4) → 232 (Δ4) ⇒ 3 step changes
        eftp_rows = [
            ((today - timedelta(days=200)).isoformat(), 220.0),
            ((today - timedelta(days=150)).isoformat(), 224.0),
            ((today - timedelta(days=100)).isoformat(), 228.0),
            ((today - timedelta(days=50)).isoformat(), 232.0),
        ]
        for d, v in eftp_rows:
            conn.execute(
                "INSERT INTO athlete_metrics (date, metric, value, source) "
                "VALUES (?, 'eftp', ?, 'intervals.icu')",
                (d, v),
            )
        conn.commit()

        weighted = tau_fitting.count_weighted_markers("default", horizon_days=365)
        self.assertAlmostEqual(weighted, 4.3, places=4,
                               msg=f"PATCH G14 fixture → expected 4.3, "
                                   f"got {weighted}")


if __name__ == "__main__":
    unittest.main()
