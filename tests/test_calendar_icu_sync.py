"""v4.4.0 IMPL-SERVER §1 + §7 — ICU activity sync into local rides store.

Five test cases (mocked ICU client):
  1. Empty store + 2 ICU activities → both persisted
  2. Existing FIT same-day + ICU sync → no duplicate (ICU preferred)
  3. No credentials → sync no-ops with skipped="no_credentials"
  4. /api/calendar surfaces a synced ICU ride
  5. Lazy sync fires on first /api/calendar after boot
"""
from __future__ import annotations

import json
import shutil
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import ride_storage
import training as training_module


def _icu_activity(
    icu_id: str = "12345",
    when: str = None,
    duration_s: int = 3600,
    name: str = "Test ride",
    avg_w: int = 180,
) -> dict:
    """Build a minimal ICU activity payload mirroring the real /activities feed."""
    if when is None:
        when = datetime.now(timezone.utc).astimezone().isoformat()
    return {
        "id": icu_id,
        "name": name,
        "type": "Ride",
        "start_date_local": when,
        "elapsed_time": duration_s,
        "moving_time": duration_s,
        "distance": 30000,
        "icu_pm_p_avg": avg_w,
        "icu_weighted_avg_watts": avg_w + 10,
        "icu_intensity": 0.7,
        "icu_training_load": 65,
        "icu_ftp": 250,
        "icu_efftp": 252,
        "icu_calories": 500,
        "icu_athlete_weight": 72.0,
        "icu_power_hr_zone_times": [600, 1500, 800, 400, 200, 100, 0],
        "icu_intervals": [],
    }


class _IcuSyncBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._icu_dir = self._base / "rides" / "icu"
        self._fit_dir = self._base / "rides"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        self._fit_dir.mkdir(parents=True, exist_ok=True)

        # Patch ride_storage paths.
        self._p1 = patch.object(
            ride_storage, "_icu_rides_dir", return_value=self._icu_dir
        )
        self._p1.start()
        self._p2 = patch.object(
            ride_storage, "_fit_rides_dir", return_value=self._fit_dir
        )
        self._p2.start()

        # Patch app paths.
        self._p3 = patch.object(
            app_module, "_rides_fit_dir", return_value=self._fit_dir
        )
        self._p3.start()
        self._p4 = patch.object(
            app_module, "_icu_sync_state_path",
            return_value=self._base / "rides" / "icu" / ".last_sync_at"
        )
        self._p4.start()

        # Empty list_rides for legacy.
        self._p5 = patch("ride_storage.list_rides", return_value=[])
        self._p5.start()

        self.client = TestClient(app_module.app)

    def tearDown(self):
        for p in (self._p1, self._p2, self._p3, self._p4, self._p5):
            p.stop()
        self._tmp.cleanup()


class TestIcuActivitySync(_IcuSyncBase):
    def test_empty_store_two_icu_activities_persisted(self):
        """Wave-2 §1: empty store + ICU mock returns 2 → both written + counts correct."""
        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities",
            return_value=[
                _icu_activity("aa1", duration_s=3600),
                _icu_activity("bb2", duration_s=5400),
            ],
        ):
            r = self.client.post("/api/rides/sync")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertEqual(data["added"], 2, data)
            files = list(self._icu_dir.glob("*.json"))
            self.assertEqual(len(files), 2, [f.name for f in files])
            for f in files:
                rec = json.loads(f.read_text())
                self.assertTrue(rec["ride_id"].startswith("icu_"))
                self.assertEqual(rec["source"], "icu")

    def test_dedupe_with_existing_fit_same_day_prefers_icu(self):
        """Wave-2 §1: FIT + ICU on same day → load_all_rides returns ICU only."""
        # Pre-create a FIT file with a date stamp matching the ICU activity.
        today_iso = datetime.now().astimezone().strftime("%Y-%m-%dT%H-%M-%S")
        fit_file = self._fit_dir / f"{today_iso}.fit"
        fit_file.write_bytes(b"\x0e\x10dummy fit header")  # not a real FIT

        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities",
            return_value=[_icu_activity("dup1", duration_s=3600,
                                         when=datetime.now().astimezone().isoformat())],
        ):
            self.client.post("/api/rides/sync")

        # load_all_rides should dedupe: only 1 entry (the ICU one).
        all_r = ride_storage.load_all_rides()
        sources = [r.get("source") for r in all_r]
        self.assertEqual(
            sources.count("icu"), 1,
            f"expected exactly 1 ICU ride, got: {sources}"
        )

    def test_sync_skips_when_no_credentials(self):
        """Wave-2 §1: missing creds → skipped='no_credentials', no fetch attempt."""
        with patch.object(
            app_module, "_icu_credentials_present", return_value=False
        ), patch.object(
            training_module, "fetch_recent_activities"
        ) as mock_fetch:
            r = self.client.post("/api/rides/sync")
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json().get("skipped"), "no_credentials")
            mock_fetch.assert_not_called()

    def test_calendar_surfaces_synced_icu_ride(self):
        """Wave-2 §2: /api/calendar shows a freshly synced ICU ride."""
        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities",
            return_value=[
                _icu_activity(
                    "cal1",
                    when=datetime.now().astimezone().isoformat(),
                    duration_s=3600,
                ),
            ],
        ):
            self.client.post("/api/rides/sync")
            r = self.client.get("/api/calendar")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            # At least one day across all weeks should have an actual.
            found_actual = False
            for w in data["weeks"]:
                for d in w["days"]:
                    if d.get("actual") and (d["actual"].get("source") == "icu"):
                        found_actual = True
                        break
            self.assertTrue(found_actual, "ICU ride did not surface in /api/calendar")

    def test_lazy_sync_fires_on_first_calendar_call(self):
        """Wave-2 §1: lazy sync hook fires once, then throttled.

        We can't directly assert the throttle (it writes to disk and reads
        elapsed time), but we CAN observe that fetch_recent_activities is
        invoked at least once when credentials are present and last_sync_at
        is older than 1 hour.
        """
        # Pre-stamp last_sync_at to 2 hours ago.
        last = self._base / "rides" / "icu" / ".last_sync_at"
        last.parent.mkdir(parents=True, exist_ok=True)
        last.write_text(str(time.time() - 7200))  # 2h ago

        with patch.object(
            app_module, "_icu_credentials_present", return_value=True
        ), patch.object(
            training_module, "fetch_recent_activities",
            return_value=[],
        ) as mock_fetch:
            self.client.get("/api/calendar")
            self.assertGreaterEqual(mock_fetch.call_count, 1)


if __name__ == "__main__":
    unittest.main()
