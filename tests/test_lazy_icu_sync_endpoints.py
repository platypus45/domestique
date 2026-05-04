"""v4.4.2 §B1+B2 — lazy ICU sync wired into more endpoints.

Verifies that the lazy ICU sync hook fires from /api/activities,
/api/today-session, and /api/calendar (not just /api/calendar as in
v4.4.0/v4.4.1), and that force-resync triggers when today's date isn't
represented locally.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import ride_storage
import training as training_module


def _icu_activity(icu_id: str = "12345", when: str = None, duration_s: int = 3600) -> dict:
    if when is None:
        when = datetime.now(timezone.utc).astimezone().isoformat()
    return {
        "id": icu_id,
        "name": "Today's ride",
        "type": "Ride",
        "start_date_local": when,
        "elapsed_time": duration_s,
        "moving_time": duration_s,
        "distance": 30000,
        "icu_pm_p_avg": 180,
        "icu_weighted_avg_watts": 190,
        "icu_intensity": 0.7,
        "icu_training_load": 65,
        "icu_ftp": 250,
        "icu_efftp": 252,
        "icu_calories": 500,
        "icu_athlete_weight": 72.0,
        "icu_power_hr_zone_times": [600, 1500, 800, 400, 200, 100, 0],
        "icu_intervals": [],
    }


class _LazySyncBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._icu_dir = self._base / "rides" / "icu"
        self._fit_dir = self._base / "rides"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        self._fit_dir.mkdir(parents=True, exist_ok=True)

        self._patches = [
            patch.object(ride_storage, "_icu_rides_dir", return_value=self._icu_dir),
            patch.object(ride_storage, "_fit_rides_dir", return_value=self._fit_dir),
            patch.object(app_module, "_rides_fit_dir", return_value=self._fit_dir),
            patch.object(
                app_module, "_icu_sync_state_path",
                return_value=self._base / "rides" / "icu" / ".last_sync_at",
            ),
            patch("ride_storage.list_rides", return_value=[]),
        ]
        for p in self._patches:
            p.start()

        # Drop any cached training metrics so /api/activities, /api/today-session
        # and /api/readiness re-evaluate against the patched ICU state.
        app_module.clear_cache()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class TestLazyIcuSyncOnActivities(_LazySyncBase):
    def test_api_activities_triggers_sync_when_last_sync_stale(self):
        """§B1: /api/activities triggers a sync when last_sync_at > 1h ago."""
        last = self._base / "rides" / "icu" / ".last_sync_at"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(str(time.time() - 7200))  # 2h ago

        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities", return_value=[]
        ) as mock_fetch:
            self.client.get("/api/activities")
            self.assertGreaterEqual(mock_fetch.call_count, 1)


class TestLazyIcuSyncOnTodaySession(_LazySyncBase):
    def test_api_today_session_triggers_sync_when_last_sync_stale(self):
        """§B1: /api/today-session triggers a sync when last_sync_at > 1h ago."""
        last = self._base / "rides" / "icu" / ".last_sync_at"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(str(time.time() - 7200))

        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities", return_value=[]
        ) as mock_fetch:
            # Don't care about response payload — just that sync is invoked.
            self.client.get("/api/today-session")
            self.assertGreaterEqual(mock_fetch.call_count, 1)


class TestLazyIcuSyncOnCalendarUnchanged(_LazySyncBase):
    def test_api_calendar_still_triggers_sync_existing_behavior(self):
        """§B1: /api/calendar continues to trigger lazy sync (v4.4.0 behavior preserved)."""
        last = self._base / "rides" / "icu" / ".last_sync_at"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(str(time.time() - 7200))

        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities", return_value=[]
        ) as mock_fetch:
            self.client.get("/api/calendar")
            self.assertGreaterEqual(mock_fetch.call_count, 1)


class TestForceResyncWhenTodayMissing(_LazySyncBase):
    def test_force_resync_fires_when_today_missing_and_last_sync_30min_old(self):
        """§B2: force-resync triggers when today's date isn't in local rides AND
        last sync was > 30min ago, even if < 1h ago (so the standard throttle
        path would normally skip).
        """
        # last_sync_at = 45 min ago — between 30 min force-window and 1h throttle.
        last = self._base / "rides" / "icu" / ".last_sync_at"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(str(time.time() - 45 * 60))

        # No local rides → today's date can't possibly be represented.
        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            app_module, "_load_all_rides_safe", return_value=[]
        ), patch.object(
            training_module, "fetch_recent_activities",
            return_value=[_icu_activity("force1")],
        ) as mock_fetch:
            self.client.get("/api/calendar")
            # Must have called the underlying ICU fetch — proves force-path
            # ran even though we're inside the 1h throttle window.
            self.assertGreaterEqual(mock_fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
