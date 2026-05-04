"""v4.6.6 IMPL-C — Hooper morning-leg-check form + ICU feel/RPE persistence.

Four tests covering the data plumbing IMPL-B's G6/G7 gates depend on:

  1. POST /api/daily-log persists all 4 Hooper fields and returns the row
     with `hooper_index = sleep + fatigue + stress + soreness` (4..28).
     Hooper & Mackinnon 1995 — wellness composite.
  2. POST /api/daily-log with an out-of-range value (>7) returns 400.
  3. ICU activity with `feel` + `perceivedExertion` populates the persisted
     ride record's `feel` (1..5) and `perceived_exertion` (1..10) — Foster
     1998 session-RPE inputs for the G7 mean-RPE gate.
  4. Legacy ride records (no feel/perceived_exertion key) load with both
     fields defaulting to None — no exception.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient


def _icu_activity_with_feel(
    icu_id: str = "rpe123",
    feel: int = 4,
    perceived_exertion: int = 8,
    duration_s: int = 3600,
) -> dict:
    """ICU activity payload including the per-ride RPE fields."""
    when = datetime.now(timezone.utc).astimezone().isoformat()
    return {
        "id": icu_id,
        "name": "RPE test ride",
        "type": "Ride",
        "start_date_local": when,
        "elapsed_time": duration_s,
        "moving_time": duration_s,
        "distance": 30000,
        "icu_pm_p_avg": 200,
        "icu_intensity": 0.75,
        "icu_training_load": 70,
        "icu_ftp": 250,
        "icu_athlete_weight": 72.0,
        "icu_power_hr_zone_times": [600, 1500, 800, 400, 200, 100, 0],
        "icu_intervals": [],
        "feel": feel,
        "perceivedExertion": perceived_exertion,
    }


class TestHooperFormPersistence(unittest.TestCase):
    """POST /api/daily-log — full Hooper composite roundtrip."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._db_path = Path(self._tmp.name) / "test.sqlite"

        import db as db_module
        # v4.6.6 WAVE-4-FIX MEDIUM-1: capture original DB_PATH so tearDown
        # can restore it. Pre-fix the temp path leaked into 5 downstream
        # tests that expect the production DB_PATH (worked around in
        # app.py via defensive try/except wraps; better to fix at source).
        self._original_db_path = db_module.DB_PATH
        db_module.close_all_connections()
        db_module.set_db_path(self._db_path)
        db_module.init_db()
        self._db_module = db_module

        import app as app_module
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._db_module.close_all_connections()
        # Restore the original DB_PATH so subsequent tests don't run
        # against the now-deleted temp sqlite file.
        self._db_module.set_db_path(self._original_db_path)
        self._tmp.cleanup()

    def test_post_daily_log_persists_4_fields(self):
        """All 4 Hooper fields persist; hooper_index is their sum."""
        r = self.client.post(
            "/api/daily-log",
            json={"sleep_quality": 5, "fatigue": 6, "stress": 4, "soreness": 7},
        )
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertTrue(body.get("ok"))
        entry = body["entry"]
        self.assertEqual(entry["sleep_quality"], 5)
        self.assertEqual(entry["fatigue"], 6)
        self.assertEqual(entry["stress"], 4)
        self.assertEqual(entry["soreness"], 7)
        self.assertEqual(entry["hooper_index"], 22)  # 5+6+4+7

        # GET the row back — same values.
        g = self.client.get("/api/daily-log")
        self.assertEqual(g.status_code, 200)
        today = g.json().get("today") or {}
        self.assertEqual(today["sleep_quality"], 5)
        self.assertEqual(today["fatigue"], 6)
        self.assertEqual(today["stress"], 4)
        self.assertEqual(today["soreness"], 7)
        self.assertEqual(today["hooper_index"], 22)

    def test_post_daily_log_validates_range(self):
        """Out-of-range value (>7) → 400 Bad Request."""
        r = self.client.post(
            "/api/daily-log",
            json={"sleep_quality": 4, "fatigue": 4, "stress": 4, "soreness": 8},
        )
        self.assertEqual(r.status_code, 400, r.text)

        # Boundary check — 1 and 7 are both valid.
        r2 = self.client.post(
            "/api/daily-log",
            json={"sleep_quality": 1, "fatigue": 7, "stress": 1, "soreness": 7},
        )
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["entry"]["hooper_index"], 16)


class TestIcuRidePersistsFeelAndRpe(unittest.TestCase):
    """ICU activity → persisted ride record carries feel + perceived_exertion."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._icu_dir = Path(self._tmp.name) / "icu"
        self._icu_dir.mkdir(parents=True)

        import ride_storage
        self._p1 = patch.object(
            ride_storage, "_icu_rides_dir", return_value=self._icu_dir
        )
        self._p1.start()
        self._ride_storage = ride_storage

    def tearDown(self):
        self._p1.stop()
        self._tmp.cleanup()

    def test_icu_feel_field_persists(self):
        """ICU activity {feel:4, perceivedExertion:8} → persisted ride has both."""
        act = _icu_activity_with_feel(icu_id="rpe123", feel=4, perceived_exertion=8)
        path = self._ride_storage.persist_icu_activity(act)
        self.assertIsNotNone(path)
        self.assertTrue(path.exists())

        loaded = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(loaded["feel"], 4)
        self.assertEqual(loaded["perceived_exertion"], 8)

        # Round-trip through load_icu_rides too.
        rides = self._ride_storage.load_icu_rides()
        self.assertEqual(len(rides), 1)
        self.assertEqual(rides[0]["feel"], 4)
        self.assertEqual(rides[0]["perceived_exertion"], 8)

    def test_legacy_ride_without_feel_loads(self):
        """Legacy ride record with no feel/perceived_exertion keys loads with None."""
        # Hand-write a legacy-shape record that has no feel/perceived_exertion.
        legacy = {
            "ride_id": "icu_legacy1",
            "source": "icu",
            "external_id": "legacy1",
            "name": "Legacy ride",
            "started_at": "2024-01-01T08:00:00+00:00",
            "duration_s": 3600,
            "moving_s": 3500,
            "distance_km": 30.0,
            "tss": 60.0,
            "avg_hr": 140,
            "time_in_zone": [0, 0, 0, 0, 0, 0, 0],
        }
        (self._icu_dir / "legacy1.json").write_text(
            json.dumps(legacy), encoding="utf-8"
        )

        rides = self._ride_storage.load_icu_rides()
        self.assertEqual(len(rides), 1)
        r = rides[0]
        # Legacy records simply lack the keys; .get() returns None — that's the
        # contract IMPL-B's _last_3d_mean_feel will read against.
        self.assertIsNone(r.get("feel"))
        self.assertIsNone(r.get("perceived_exertion"))


if __name__ == "__main__":
    unittest.main()
