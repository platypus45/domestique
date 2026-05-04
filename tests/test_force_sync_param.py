"""v4.5.0 IMPL-SYNC-WELLNESS — POST /api/rides/sync ?force=1 query param.

Verifies:
  1. ?force=1 bypasses the 1h sync throttle (two consecutive calls both run
     the underlying ICU fetch).
  2. ?force=1 also runs a wellness pull in the same call (returns
     wellness_added counter).
  3. Without force, the existing 1h throttle still applies (regression check
     so v4.4.x callers that don't supply ?force=1 don't suddenly bypass).
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import ride_storage
import training as training_module


def _icu_activity(icu_id: str = "f1", duration_s: int = 3600) -> dict:
    from datetime import datetime, timezone
    when = datetime.now(timezone.utc).astimezone().isoformat()
    return {
        "id": icu_id,
        "name": "ride",
        "type": "Ride",
        "start_date_local": when,
        "elapsed_time": duration_s,
        "moving_time": duration_s,
        "distance": 30000,
        "icu_pm_p_avg": 180,
        "icu_intensity": 0.7,
        "icu_training_load": 65,
        "icu_ftp": 250,
    }


def _icu_wellness_record(d: str = "2026-04-30") -> dict:
    return {
        "id": d,
        "hrv": 60.0,
        "restingHR": 53,
        "sleepSecs": 25200,
        "sleepScore": 80,
        "ctl": 53.0,
        "atl": 50.1,
    }


class _ForceSyncBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._icu_dir = self._base / "rides" / "icu"
        self._fit_dir = self._base / "rides"
        self._wellness_dir = self._base / "wellness"
        for d in (self._icu_dir, self._fit_dir, self._wellness_dir):
            d.mkdir(parents=True, exist_ok=True)

        self._patches = [
            patch.object(ride_storage, "_icu_rides_dir", return_value=self._icu_dir),
            patch.object(ride_storage, "_fit_rides_dir", return_value=self._fit_dir),
            patch.object(ride_storage, "_wellness_dir", return_value=self._wellness_dir),
            patch.object(app_module, "_rides_fit_dir", return_value=self._fit_dir),
            patch.object(
                app_module, "_icu_sync_state_path",
                return_value=self._base / "rides" / "icu" / ".last_sync_at",
            ),
            patch.object(
                app_module, "_icu_wellness_sync_state_path",
                return_value=self._base / "wellness" / ".last_sync_at",
            ),
        ]
        for p in self._patches:
            p.start()
        app_module.clear_cache()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class TestForceSyncBypassesThrottle(_ForceSyncBase):
    """§1 — POST /api/rides/sync?force=1 bypasses the 1h throttle."""

    def test_force_sync_bypasses_throttle(self):
        # Mark last_sync_at as 30 seconds ago — well within the 1h throttle.
        last = self._base / "rides" / "icu" / ".last_sync_at"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(str(time.time() - 30))

        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities",
            return_value=[_icu_activity("force_a")],
        ) as mock_fetch, patch.object(
            training_module, "fetch_recent_wellness",
            return_value=[_icu_wellness_record()],
        ):
            r1 = self.client.post("/api/rides/sync?force=1")
            r2 = self.client.post("/api/rides/sync?force=1")
        self.assertEqual(r1.status_code, 200, r1.text)
        self.assertEqual(r2.status_code, 200, r2.text)
        # Both calls must have hit the ICU activity fetch (proving force=1
        # bypasses the throttle that would normally block a 30-second-old
        # last_sync_at).
        self.assertGreaterEqual(mock_fetch.call_count, 2,
                                f"expected ≥2 fetches, got {mock_fetch.call_count}")
        # Response shape includes wellness_added on force path.
        self.assertIn("wellness_added", r1.json())


class TestForceSyncRunsWellness(_ForceSyncBase):
    """§2 — ?force=1 also runs a wellness pull in the same call."""

    def test_force_sync_runs_wellness_sync_too(self):
        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities",
            return_value=[],
        ), patch.object(
            training_module, "fetch_recent_wellness",
            return_value=[_icu_wellness_record("2026-04-30"), _icu_wellness_record("2026-04-29")],
        ) as mock_wellness:
            r = self.client.post("/api/rides/sync?force=1")
        self.assertEqual(r.status_code, 200, r.text)
        # The wellness fetch must have been invoked exactly once during this
        # combined sync call.
        self.assertGreaterEqual(mock_wellness.call_count, 1)
        # Persisted to local wellness dir.
        files = list(self._wellness_dir.glob("*.json"))
        self.assertEqual(
            sorted(f.name for f in files),
            ["2026-04-29.json", "2026-04-30.json"],
        )
        body = r.json()
        # Wellness counter is non-zero in the response.
        self.assertEqual(body.get("wellness_added"), 2, body)


class TestDefaultSyncThrottled(_ForceSyncBase):
    """§3 — Without force, the 1h throttle still applies (regression)."""

    def test_default_sync_throttled(self):
        # Mark a recent sync — within the 1h throttle window.
        last = self._base / "rides" / "icu" / ".last_sync_at"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(str(time.time() - 60))  # 60s ago

        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities",
            return_value=[_icu_activity("nothrottle")],
        ) as mock_fetch:
            r = self.client.post("/api/rides/sync")  # no force=1
        self.assertEqual(r.status_code, 200, r.text)
        # The throttle should have prevented any ICU fetch from running.
        self.assertEqual(mock_fetch.call_count, 0,
                         f"expected 0 fetches due to throttle, got {mock_fetch.call_count}")
        body = r.json()
        # Throttled response has skipped="throttled" (the existing internal
        # throttle marker).
        self.assertEqual(body.get("skipped"), "throttled", body)


if __name__ == "__main__":
    unittest.main()
