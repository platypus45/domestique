"""v1.0.7 IMPL-TAU-FIT-WIRING — is_race column + setter + endpoint tests.

Locks the new ``activities.is_race INTEGER DEFAULT 0`` column (PATCH G11),
the ``db._set_is_race(activity_id, is_race)`` setter, and the
``POST /api/activity/{id}/race`` endpoint behaviour.

Tests:
  1. Column exists after init_db() and defaults to 0 on insert.
  2. _set_is_race() flips the column and persists (rowcount-aware).
  3. _set_is_race() returns False when the activity_id is not present.
  4. POST /api/activity/{id}/race body {is_race:true} → 204 + DB row updated.
  5. POST /api/activity/{id}/race for a missing id → 404.
  6. _maybe_add_column is_race idempotent (re-running init_db() doesn't error).
"""
from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path


class IsRaceColumnTests(unittest.TestCase):
    def setUp(self):
        import db
        self.tmp = Path(tempfile.mkdtemp())
        self.dbfile = self.tmp / "is_race.db"
        db.set_db_path(self.dbfile)
        db.close_all_connections()
        db.init_db()
        self._db = db

    def tearDown(self):
        self._db.close_all_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_is_race_column_exists_with_default_zero(self):
        """1. ALTER TABLE ADD COLUMN is_race INTEGER DEFAULT 0 lands."""
        conn = self._db.get_db()
        cols = {r[1]: r for r in conn.execute("PRAGMA table_info(activities)").fetchall()}
        self.assertIn("is_race", cols, f"is_race column missing; got {list(cols)}")
        # Insert a row without specifying is_race → column reads 0.
        conn.execute(
            "INSERT INTO activities (id, date, name, sport, duration_sec, tss) "
            "VALUES ('test-1', '2026-01-01', 'a', 'Ride', 3600, 100.0)"
        )
        conn.commit()
        row = conn.execute(
            "SELECT is_race FROM activities WHERE id = 'test-1'"
        ).fetchone()
        self.assertEqual(int(row[0] or 0), 0)

    def test_set_is_race_flips_value(self):
        """2. _set_is_race(True) writes 1; _set_is_race(False) writes 0."""
        conn = self._db.get_db()
        conn.execute(
            "INSERT INTO activities (id, date, name, sport, duration_sec, tss) "
            "VALUES ('a-1', '2026-01-02', 'a', 'Ride', 3600, 100.0)"
        )
        conn.commit()

        self.assertTrue(self._db._set_is_race("a-1", True))
        row = conn.execute(
            "SELECT is_race FROM activities WHERE id = 'a-1'"
        ).fetchone()
        self.assertEqual(int(row[0]), 1)

        # Toggle back to 0.
        self.assertTrue(self._db._set_is_race("a-1", False))
        row = conn.execute(
            "SELECT is_race FROM activities WHERE id = 'a-1'"
        ).fetchone()
        self.assertEqual(int(row[0]), 0)

    def test_set_is_race_returns_false_for_missing_id(self):
        """3. UPDATE missing row → rowcount=0 → returns False."""
        # No activity inserted; setter should report False.
        self.assertFalse(self._db._set_is_race("nonexistent", True))

    def test_set_is_race_rejects_empty_id(self):
        """3a. _set_is_race must validate non-empty string."""
        with self.assertRaises(ValueError):
            self._db._set_is_race("", True)
        with self.assertRaises(ValueError):
            self._db._set_is_race(None, True)  # type: ignore[arg-type]

    def test_init_db_idempotent_for_is_race(self):
        """6. Calling init_db() twice doesn't raise on the duplicate column."""
        # First call already happened in setUp; call again to confirm it's
        # idempotent (the _maybe_add_column duplicate-column swallow).
        self._db.init_db()
        # Column still exists after the second pass.
        conn = self._db.get_db()
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(activities)"
        ).fetchall()]
        self.assertIn("is_race", cols)


class RaceEndpointTests(unittest.TestCase):
    """4-5. POST /api/activity/{id}/race endpoint behaviour."""

    def setUp(self):
        import db
        self.tmp = Path(tempfile.mkdtemp())
        db.set_db_path(self.tmp / "race_endpoint.db")
        db.close_all_connections()
        db.init_db()
        self._db = db
        # Seed one activity so the happy-path test has a row to flip.
        conn = db.get_db()
        conn.execute(
            "INSERT INTO activities (id, date, name, sport, duration_sec, tss) "
            "VALUES ('endp-1', '2026-02-01', 'a', 'Ride', 3600, 100.0)"
        )
        conn.commit()

        from fastapi.testclient import TestClient
        import app as _app_module
        self._app_module = _app_module
        self.client = TestClient(_app_module.app)

    def tearDown(self):
        self._db.close_all_connections()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_post_race_endpoint_returns_204_and_updates_row(self):
        """4. POST {"is_race":true} → 204; DB row reflects is_race=1."""
        r = self.client.post(
            "/api/activity/endp-1/race",
            json={"is_race": True},
        )
        self.assertEqual(r.status_code, 204,
                         f"expected 204; got {r.status_code} body={r.text!r}")
        conn = self._db.get_db()
        row = conn.execute(
            "SELECT is_race FROM activities WHERE id = 'endp-1'"
        ).fetchone()
        self.assertEqual(int(row[0]), 1)

        # Toggle off.
        r = self.client.post(
            "/api/activity/endp-1/race",
            json={"is_race": False},
        )
        self.assertEqual(r.status_code, 204)
        row = conn.execute(
            "SELECT is_race FROM activities WHERE id = 'endp-1'"
        ).fetchone()
        self.assertEqual(int(row[0]), 0)

    def test_post_race_endpoint_returns_404_for_missing_id(self):
        """5. Missing id → 404 (not 500)."""
        r = self.client.post(
            "/api/activity/missing-id/race",
            json={"is_race": True},
        )
        self.assertEqual(r.status_code, 404)


if __name__ == "__main__":
    unittest.main()
