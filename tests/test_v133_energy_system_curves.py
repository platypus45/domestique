"""v1.3.3 BUG-FIX regression — energy-system breakdown chart now renders
actual CP / W' / Pmax curves instead of the placeholder text.

v1.3.2 made loadHome() *call* energySystemChart(), but the chart itself
fell through to the friendly "3D fitness curves will populate after
IMPL-3D-MODEL has computed Banister components for the wellness window."
placeholder because /api/wellness was returning ``cp_fitness=None``,
``w_prime_fitness=None``, ``pmax_fitness=None`` on every record. The
per-ride writer (``ride_storage.compute_ride_xss``) was correctly stamping
``ss_cp_daily`` / ``ss_w_prime_daily`` / ``ss_pmax_daily`` into
athlete_metrics, but no code was running ``strain_score.banister()`` over
those impulse series to produce the per-day fitness/fatigue curves the
chart consumes.

v1.3.3 fix: ``app._augment_wellness_with_3d_fitness`` reads the per-day
SS_x history once, runs Banister with Kontro Fig. S2 τ defaults
(CP 52/10 d, W' 5/5 d, Pmax 10/4 d), and stamps the six keys onto each
wellness record before /api/wellness returns. This test seeds
athlete_metrics with three days of SS_x impulses and asserts the API
returns numeric (not None) Banister values.
"""
from __future__ import annotations

import datetime as _dt
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestV133BanisterAugmenter(unittest.TestCase):
    """The augmenter must produce numeric CP/W'/Pmax fitness when SS_x data
    is present in athlete_metrics, and gracefully no-op (keys absent or None)
    when nothing is present.

    Tests touch the augmenter directly — independent of the FastAPI route
    plumbing — to keep the regression focused on the v1.3.3 fix-point.
    """

    def setUp(self):
        # Sandbox DB in a temp dir so we don't pollute the real profile.
        self.tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self.tmp.name)
        # Patch DB path resolver to point at a fresh sqlite under tmp.
        import db
        self._orig_get_db = db.get_db
        self._con = sqlite3.connect(self.tmp_path / "test.db")
        self._con.row_factory = sqlite3.Row
        # Bootstrap the schema by replaying the CREATE TABLE that db.py emits.
        self._con.execute("""
            CREATE TABLE IF NOT EXISTS athlete_metrics (
                date TEXT NOT NULL,
                metric TEXT NOT NULL,
                value REAL,
                source TEXT,
                notes TEXT,
                PRIMARY KEY (date, metric)
            )
        """)
        self._con.commit()
        db.get_db = lambda: self._con  # type: ignore[assignment]

    def tearDown(self):
        import db
        db.get_db = self._orig_get_db  # type: ignore[assignment]
        self._con.close()
        self.tmp.cleanup()

    def _seed_ss(self, day: str, ss_cp: float, ss_wp: float, ss_pm: float):
        for metric, v in (("ss_cp_daily", ss_cp),
                          ("ss_w_prime_daily", ss_wp),
                          ("ss_pmax_daily", ss_pm)):
            self._con.execute(
                "INSERT OR REPLACE INTO athlete_metrics(date, metric, value, source) VALUES(?,?,?,?)",
                (day, metric, v, "computed"),
            )
        self._con.commit()

    def test_augmenter_populates_three_curves_when_ss_history_exists(self):
        """Three days of SS_x impulses → six numeric keys on the wellness rec."""
        from app import _augment_wellness_with_3d_fitness
        today = _dt.date.today()
        d0 = (today - _dt.timedelta(days=2)).isoformat()
        d1 = (today - _dt.timedelta(days=1)).isoformat()
        d2 = today.isoformat()
        self._seed_ss(d0, 80.0, 30.0, 12.0)
        self._seed_ss(d1, 60.0, 25.0,  8.0)
        self._seed_ss(d2, 90.0, 35.0, 14.0)
        records = [
            {"id": d2, "ctl": 50, "atl": 40},
            {"id": d1, "ctl": 49, "atl": 41},
            {"id": d0, "ctl": 48, "atl": 42},
        ]
        _augment_wellness_with_3d_fitness(records)
        for rec in records:
            for key in ("cp_fitness", "cp_fatigue",
                        "w_prime_fitness", "w_prime_fatigue",
                        "pmax_fitness", "pmax_fatigue"):
                self.assertIn(key, rec, f"{rec['id']} missing {key}")
                self.assertIsNotNone(rec[key],
                                     f"{rec['id']}.{key} should be numeric")
                self.assertIsInstance(rec[key], float)
        # CP fitness should monotonically grow toward "today" because each
        # extra impulse adds to the cumulative Banister convolution.
        rec_today = next(r for r in records if r["id"] == d2)
        rec_d0    = next(r for r in records if r["id"] == d0)
        self.assertGreater(rec_today["cp_fitness"], rec_d0["cp_fitness"])

    def test_augmenter_noop_when_no_ss_history(self):
        """Empty athlete_metrics → keys absent (chart shows placeholder)."""
        from app import _augment_wellness_with_3d_fitness
        records = [{"id": _dt.date.today().isoformat(), "ctl": 0, "atl": 0}]
        _augment_wellness_with_3d_fitness(records)
        # No SS_x data → augmenter must NOT manufacture zeros, so the
        # placeholder ("3D fitness curves will populate...") still applies.
        rec = records[0]
        for key in ("cp_fitness", "w_prime_fitness", "pmax_fitness"):
            self.assertTrue(rec.get(key) in (None,) or key not in rec,
                            f"{key} unexpectedly populated when no SS data")

    def test_augmenter_does_not_clobber_upstream_values(self):
        """If upstream already wrote a value, the augmenter must respect it."""
        from app import _augment_wellness_with_3d_fitness
        today = _dt.date.today().isoformat()
        self._seed_ss(today, 80.0, 30.0, 12.0)
        records = [{"id": today, "cp_fitness": 999.9}]
        _augment_wellness_with_3d_fitness(records)
        self.assertEqual(records[0]["cp_fitness"], 999.9)
        # Other keys should still be filled.
        self.assertIsNotNone(records[0].get("w_prime_fitness"))


if __name__ == "__main__":
    unittest.main()
