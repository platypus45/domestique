"""v4.5.0 IMPL-SYNC-WELLNESS — wellness sync tests.

Verifies:
  1. fetch_recent_wellness returns normalized records (mock ICU response).
  2. persist_wellness writes idempotently to ~/.domestique/wellness/<date>.json.
  3. load_recent_wellness returns records sorted newest-first.
  4. /api/readiness uses local wellness fallback when ICU live returns {}.
  5. compute_local_atl returns numeric over a sample rides list.
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import ride_storage
import training as training_module


_RAW_ICU_WELLNESS = [
    {
        "id": "2026-04-28",
        "hrv": 58.0,
        "hrv_baseline": 55.0,
        "restingHR": 52,
        "sleepSecs": 27000,
        "sleepScore": 88,
        "weight": 72.4,
        "fatigue": 2,
        "stress": 1,
        "soreness": 2,
        "sportInfo": [{"eftp": 252}],
        "ctl": 51.2,
        "atl": 47.8,
    },
    {
        "id": "2026-04-29",
        "hrv": 62.0,
        "hrv_baseline": 56.0,
        "restingHR": 51,
        "sleepSecs": 28800,
        "sleepScore": 91,
        "weight": 72.2,
        "ctl": 52.5,
        "atl": 49.0,
    },
    {
        "id": "2026-04-30",
        "hrv": 60.0,
        "restingHR": 53,
        "sleepSecs": 25200,
        "sleepScore": 80,
        "ctl": 53.0,
        "atl": 50.1,
    },
]


class TestFetchRecentWellness(unittest.TestCase):
    """§1 — fetch_recent_wellness returns normalized ICU wellness records."""

    def test_fetch_wellness_returns_normalized_records(self):
        with patch.object(
            training_module, "fetch_wellness",
            return_value=list(_RAW_ICU_WELLNESS),
        ):
            out = training_module.fetch_recent_wellness(days=14)
        self.assertEqual(len(out), 3)
        # Required keys per spec: id, hrv, restingHR, sleepSecs, sleepScore,
        # weight, sportInfo, ctl, atl.
        first = out[0]
        for k in ("id", "hrv", "restingHR", "sleepSecs", "sleepScore"):
            self.assertIn(k, first, f"missing key: {k}")
        self.assertEqual(first["id"], "2026-04-28")
        self.assertEqual(first["hrv"], 58.0)
        self.assertEqual(first["ctl"], 51.2)


class _WellnessDirBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._wellness_dir = Path(self._tmp.name) / "wellness"
        self._wellness_dir.mkdir(parents=True, exist_ok=True)
        self._patches = [
            patch.object(
                ride_storage, "_wellness_dir",
                return_value=self._wellness_dir,
            ),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class TestPersistWellness(_WellnessDirBase):
    """§2 — persist_wellness is idempotent (overwrite same file)."""

    def test_persist_wellness_idempotent(self):
        rec = dict(_RAW_ICU_WELLNESS[0])
        # First write
        path1 = ride_storage.persist_wellness(rec)
        self.assertIsNotNone(path1)
        self.assertTrue(path1.exists())
        # Mutate + re-write same id → file overwritten, no duplicate
        rec2 = dict(rec)
        rec2["restingHR"] = 99
        path2 = ride_storage.persist_wellness(rec2)
        self.assertEqual(path1, path2)
        files = list(self._wellness_dir.glob("*.json"))
        self.assertEqual(len(files), 1, [f.name for f in files])
        on_disk = json.loads(path2.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["restingHR"], 99)
        # Reject malformed records
        self.assertIsNone(ride_storage.persist_wellness({}))
        self.assertIsNone(ride_storage.persist_wellness({"id": "../etc"}))


class TestLoadRecentWellness(_WellnessDirBase):
    """§3 — load_recent_wellness returns records sorted by date desc."""

    def test_load_recent_wellness_sorted_desc(self):
        for rec in _RAW_ICU_WELLNESS:
            ride_storage.persist_wellness(rec)
        out = ride_storage.load_recent_wellness(days=10)
        # Newest first
        ids = [r["id"] for r in out]
        self.assertEqual(ids, ["2026-04-30", "2026-04-29", "2026-04-28"])
        # days=2 caps the output
        capped = ride_storage.load_recent_wellness(days=2)
        self.assertEqual(len(capped), 2)
        self.assertEqual(capped[0]["id"], "2026-04-30")


class TestReadinessUsesLocalWellnessWhenIcuEmpty(_WellnessDirBase):
    """§4 — /api/readiness uses load_recent_wellness when ICU live returns {}."""

    def test_readiness_uses_local_wellness_when_icu_empty(self):
        # Persist a wellness record with HRV/RHR/sleep so the local fallback
        # has something to surface.
        for rec in _RAW_ICU_WELLNESS:
            ride_storage.persist_wellness(rec)

        # Force ICU live wellness path empty + CTL/ATL fallback path empty
        # so we deterministically take the local-wellness branch.
        app_module.clear_cache()
        with patch(
            "app.get_today_metrics", return_value={}
        ), patch(
            "app.get_sleep_metrics", return_value={}
        ):
            client = TestClient(app_module.app)
            r = client.get("/api/readiness")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # Sleep block should now reflect local wellness (not all None)
        sleep = data.get("sleep") or {}
        self.assertIsNotNone(sleep.get("rhr_today"),
                             f"rhr_today is None despite local wellness: {sleep}")
        # data_status should signal local_wellness path
        readiness = data.get("readiness") or {}
        self.assertEqual(readiness.get("data_status"), "local_wellness", readiness)


class TestComputeLocalAtl(unittest.TestCase):
    """§5 — compute_local_atl returns a numeric ATL over a sample rides list."""

    def test_compute_local_atl_returns_numeric(self):
        today = date(2026, 4, 30)
        rides = [
            {"started_at": (today - timedelta(days=i)).isoformat() + "T08:00:00",
             "summary": {"tss": 60.0}}
            for i in range(7)
        ]
        atl = ride_storage.compute_local_atl(rides, today=today, days=7)
        self.assertIsNotNone(atl)
        self.assertIsInstance(atl, float)
        # Constant 60 TSS for 7 days → ATL converges toward 60 (but not all
        # the way; just sanity-check it's positive and well within bound).
        self.assertGreater(atl, 0)
        self.assertLess(atl, 80)
        # Empty list → None
        self.assertIsNone(ride_storage.compute_local_atl([], today=today))
        # Rides without TSS → None
        self.assertIsNone(ride_storage.compute_local_atl(
            [{"started_at": today.isoformat() + "T08:00:00", "summary": {}}],
            today=today,
        ))


if __name__ == "__main__":
    unittest.main()
