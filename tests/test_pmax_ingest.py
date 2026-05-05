"""v1.0.6 IMPL-3D-INGEST — Pmax ingestion tests.

Verifies the four pmax pipelines wired by IMPL-3D-INGEST-V106:

  1. ICU sync writes ``pmax`` row to ``athlete_metrics`` when wellness JSON
     has ``sportInfo[0].pMax`` (live: 1,114.7 W on 2026-05-05).
  2. Manual override (``source='manual'``) blocks ICU re-write — same
     guard pattern as the existing W' / eFTP behaviour.
  3. Profile fallback: ``profile.pmax_w`` returns ``int(ftp * 1.30)`` when
     no athlete_metrics row exists (Coggan 2-min approximation).
  4. ``_refresh_pmax_from_metrics()`` mirrors the newest ICU row into the
     active profile via ``_set_pmax(..., 'icu')``.

All tests run against an isolated tmp DB + tmp profile dir to avoid
clobbering the real one.
"""
from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


def _make_base(tmp: str) -> Path:
    base = Path(tmp) / ".domestique"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _bootstrap_profile(base: Path) -> None:
    p = base / "profiles" / "default"
    p.mkdir(parents=True, exist_ok=True)
    (p / "athlete.json").write_text(json.dumps({
        "ftp": 250, "weight_kg": 72.0, "lbm_kg": 58.0,
        "lthr": 175, "max_hr": 196,
    }), encoding="utf-8")
    (p / ".env").write_text("ICU_ATHLETE_ID=i1\nICU_API_KEY=k1\n", encoding="utf-8")
    (p / "user_prefs.json").write_text(json.dumps({}), encoding="utf-8")
    (p / "device_prefs.json").write_text(json.dumps({}), encoding="utf-8")
    (base / "profiles.json").write_text(json.dumps({
        "version": 1, "active_profile": "default", "skip_picker": True,
        "profiles": [{
            "id": "default", "name": "Test", "color": "#3b82f6",
            "created": "2026-04-01T00:00:00", "last_used": "2026-04-01T00:00:00",
        }],
    }), encoding="utf-8")


def _fresh_pm(tmp: str):
    from profile_manager import ProfileManager
    ProfileManager._instance = None
    with patch("pathlib.Path.home", return_value=Path(tmp)):
        return ProfileManager.get()


class _DBProfileFixture(unittest.TestCase):
    """Shared scaffold: tmp DB + tmp profile dir + fresh ProfileManager."""

    def setUp(self):
        import db
        from profile_manager import ProfileManager
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_profile(_make_base(self.tmp))
        self._home_patch = patch("pathlib.Path.home", return_value=Path(self.tmp))
        self._home_patch.start()
        ProfileManager._instance = None
        self.pm = ProfileManager.get()
        # tmp DB
        self.dbfile = Path(self.tmp) / "t.db"
        db.set_db_path(self.dbfile)
        db.close_all_connections()
        db.init_db()
        # Reset health state so the sync path doesn't refuse early.
        db._auth_disabled = False
        db._consecutive_failures = 0
        db._last_sync_error = None

    def tearDown(self):
        from profile_manager import ProfileManager
        self._home_patch.stop()
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestICUSyncWritesPmax(_DBProfileFixture):
    """§1 — ICU sync_wellness writes pmax row when sportInfo[0].pMax is set."""

    def test_icu_sync_writes_pmax_to_athlete_metrics(self):
        import db
        # Live ICU shape from ~/.domestique/wellness/2026-05-05.json: the
        # pMax field is a float (best 1s power, watts).
        icu_payload = [{
            "id": "2026-05-05",
            "sportInfo": [{"eftp": 248.0, "wPrime": 20695.0, "pMax": 1114.743}],
            "ctl": 50, "atl": 40,
        }]
        with patch.object(db, "fetch_wellness", return_value=icu_payload):
            db.sync_wellness(days=7)

        conn = db.get_db()
        row = conn.execute(
            "SELECT value, source FROM athlete_metrics WHERE date = ? AND metric = 'pmax'",
            ("2026-05-05",),
        ).fetchone()
        self.assertIsNotNone(row, "pmax row should be written by sync_wellness")
        self.assertEqual(round(row[0]), 1115, "pmax should be rounded watts")
        self.assertEqual(row[1], "intervals.icu")


class TestManualOverrideBlocksICU(_DBProfileFixture):
    """§2 — A manual-source pmax row survives an ICU sync_wellness pass."""

    def test_manual_pmax_not_overwritten_by_icu(self):
        import db
        today = "2026-05-04"
        db.log_metric(today, "pmax", 1200, source="manual")

        icu_payload = [{
            "id": today,
            "sportInfo": [{"pMax": 1500, "eftp": 250, "wPrime": 20000}],
            "ctl": 50, "atl": 40,
        }]
        with patch.object(db, "fetch_wellness", return_value=icu_payload):
            db.sync_wellness(days=7)

        conn = db.get_db()
        row = conn.execute(
            "SELECT value, source FROM athlete_metrics WHERE date = ? AND metric = 'pmax'",
            (today,),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row[0], 1200, "manual pmax must not be overwritten")
        self.assertEqual(row[1], "manual")

    def test_icu_source_is_overwritten_by_icu(self):
        """Sanity: only 'manual' is locked. An existing 'intervals.icu' row
        is allowed to be refreshed by a later sync."""
        import db
        today = "2026-05-04"
        db.log_metric(today, "pmax", 1050, source="intervals.icu")

        icu_payload = [{
            "id": today,
            "sportInfo": [{"pMax": 1200}],
            "ctl": 50, "atl": 40,
        }]
        with patch.object(db, "fetch_wellness", return_value=icu_payload):
            db.sync_wellness(days=7)

        conn = db.get_db()
        row = conn.execute(
            "SELECT value, source FROM athlete_metrics WHERE date = ? AND metric = 'pmax'",
            (today,),
        ).fetchone()
        self.assertEqual(row[0], 1200)
        self.assertEqual(row[1], "intervals.icu")


class TestProfilePmaxFallback(_DBProfileFixture):
    """§3 — pm.pmax_w falls back to int(ftp * 1.30) when no override stored."""

    def test_pmax_w_property_falls_back_to_ftp_times_1_30(self):
        # Bootstrap profile sets ftp=250; pmax_w should fall back to 250 * 1.30 = 325.
        self.assertEqual(self.pm.ftp, 250)
        self.assertEqual(self.pm.pmax_w, int(250 * 1.30))
        # And pmax_source is empty (= "fallback")
        self.assertEqual(self.pm.pmax_source, "")

    def test_pmax_w_uses_stored_value_when_present(self):
        ok = self.pm._set_pmax(1115, "icu")
        self.assertTrue(ok)
        self.assertEqual(self.pm.pmax_w, 1115)
        self.assertEqual(self.pm.pmax_source, "icu")


class TestRefreshPmaxFromMetrics(_DBProfileFixture):
    """§4 — _refresh_pmax_from_metrics mirrors newest ICU row into profile."""

    def test_refresh_mirrors_icu_row(self):
        import db
        # Insert ICU-source row directly (skipping sync_wellness so we test
        # the mirror in isolation).
        db.log_metric("2026-05-03", "pmax", 1100, source="intervals.icu")
        db.log_metric("2026-05-05", "pmax", 1115, source="intervals.icu")

        db._refresh_pmax_from_metrics()

        # Should pick the newest (2026-05-05).
        self.assertEqual(self.pm.pmax_w, 1115)
        self.assertEqual(self.pm.pmax_source, "icu")

    def test_refresh_does_not_promote_manual_row(self):
        """Belt-and-braces: a 'manual' row in athlete_metrics shouldn't get
        re-written into the profile under source='icu' (manual writes go
        through save_athlete directly)."""
        import db
        # First plant an icu row so the profile has *something* for pmax.
        db.log_metric("2026-05-01", "pmax", 1050, source="intervals.icu")
        db._refresh_pmax_from_metrics()
        self.assertEqual(self.pm.pmax_source, "icu")

        # Now make the newest row 'manual' — refresh should ignore it.
        db.log_metric("2026-05-05", "pmax", 1200, source="manual")
        db._refresh_pmax_from_metrics()
        # Profile pmax stayed at the icu-mirrored value (1050).
        self.assertEqual(self.pm.pmax_w, 1050)
        self.assertEqual(self.pm.pmax_source, "icu")

    def test_refresh_no_op_when_no_rows(self):
        import db
        # Empty table — refresh should not raise + not change profile state.
        db._refresh_pmax_from_metrics()
        # No row written → property still falls back.
        self.assertEqual(self.pm.pmax_w, int(250 * 1.30))


class TestSetPmaxPriority(_DBProfileFixture):
    """Priority ladder mirrors _set_wprime: manual > icu > computed > fallback."""

    def test_manual_overrides_icu(self):
        self.pm._set_pmax(1100, "icu")
        self.pm._set_pmax(1150, "manual")
        self.assertEqual(self.pm.pmax_w, 1150)
        self.assertEqual(self.pm.pmax_source, "manual")

    def test_icu_does_not_override_manual(self):
        self.pm._set_pmax(1150, "manual")
        ok = self.pm._set_pmax(1200, "icu")
        self.assertFalse(ok)
        self.assertEqual(self.pm.pmax_w, 1150)
        self.assertEqual(self.pm.pmax_source, "manual")

    def test_rejects_out_of_range(self):
        # 300-2500 range from the brief.
        self.assertFalse(self.pm._set_pmax(100, "icu"))
        self.assertFalse(self.pm._set_pmax(9999, "icu"))

    def test_rejects_unknown_source(self):
        with self.assertRaises(ValueError):
            self.pm._set_pmax(1100, "strava")


if __name__ == "__main__":
    unittest.main()
