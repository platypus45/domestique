"""v4.5.5 IMPL-DETAIL-SERVER — /api/ride/<id>/detail zone & interval enrichment.

Five test cases covering the lazy-ICU-fetch + polarization helpers added in
v4.5.5:

  1. Mock ICU /activity/<id> with icu_zone_times → endpoint returns time_in_zone.
  2. Mock ICU /activity/<id>/intervals → endpoint returns intervals list with
     avg_power_w + ftp_pct fields.
  3. Mock ICU returns without zones (HR-only ride) → endpoint returns
     time_in_zone with all zeros (graceful degrade, not crash).
  4. Polarization-index math sanity for {z1z2:48, z3z4:34, z5plus:18}.
  5. Distribution classification for the canonical {base, polarized} cases.
"""
from __future__ import annotations

import json
import math
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import ride_storage
import training as training_module
from analytics import classify_distribution, polarization_index


# Older normalized record (pre-v4.5.5: time_in_zone all zeros, intervals = []).
def _seed_empty_icu_record(icu_dir: Path, icu_id: str) -> dict:
    rec = {
        "ride_id": f"icu_{icu_id}",
        "source": "icu",
        "external_id": icu_id,
        "name": "Detail Zones Test",
        "started_at": datetime.now().astimezone().isoformat(),
        "duration_s": 8908,
        "moving_s": 8908,
        "distance_km": 74.6,
        "elevation_m": 591,
        "avg_power_w": 138,
        "np_w": 219,
        "if_pct": 88.3,
        "tss": 193.0,
        "kj": 1626.2,
        "kj_above_ftp": 156.3,
        "kcal": 1881,
        "avg_hr": 144,
        "hr_max": 179,
        "avg_cadence": 81,
        "weight_kg": 72.0,
        "ftp_at_ride": 248,
        "eftp_at_ride": 248,
        "decoupling_pct": None,
        "dfa_alpha1": None,
        "time_in_zone": {f"z{i}": 0 for i in range(1, 8)},
        "intervals": [],
        "samples_url": f"/api/ride/icu_{icu_id}/detail?include=samples",
    }
    icu_dir.mkdir(parents=True, exist_ok=True)
    (icu_dir / f"{icu_id}.json").write_text(json.dumps(rec))
    return rec


# Pretend ICU /activity/<id> response with full zones + polarization index.
ICU_DETAIL_FIXTURE = {
    "id": "i144492547",
    "name": "Zwolle Fietsen",
    "start_date_local": "2026-05-01T08:14:07",
    "elapsed_time": 10335,
    "moving_time": 8908,
    "distance": 74620.9,
    "icu_ftp": 248,
    "icu_training_load": 193,
    "icu_intensity": 88.3,
    "icu_pm_p_avg": 138,
    "icu_weighted_avg_watts": 219,
    "icu_zone_times": [
        {"id": "Z1", "secs": 2311},
        {"id": "Z2", "secs": 1952},
        {"id": "Z3", "secs": 1796},
        {"id": "Z4", "secs": 1251},
        {"id": "Z5", "secs": 698},
        {"id": "Z6", "secs": 617},
        {"id": "Z7", "secs": 283},
        {"id": "SS", "secs": 1465},
    ],
    "icu_hr_zone_times": [2475, 3758, 1619, 1023, 33, 0, 0],
    "polarization_index": 1.4,
}

ICU_INTERVALS_FIXTURE = [
    {
        "id": 1, "start_index": 0, "moving_time": 631, "elapsed_time": 631,
        "average_watts": 170, "average_heartrate": 132, "type": "RECOVERY",
        "label": None, "zone": 2,
    },
    {
        "id": 2, "start_index": 631, "moving_time": 11, "elapsed_time": 11,
        "average_watts": 396, "average_heartrate": 152, "type": "WORK",
        "label": None, "group_id": "11s@396w88rpm", "zone": 7,
    },
]


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._icu_dir = self._base / "rides" / "icu"
        self._fit_dir = self._base / "rides"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        self._fit_dir.mkdir(parents=True, exist_ok=True)
        self._p1 = patch.object(
            ride_storage, "_icu_rides_dir", return_value=self._icu_dir,
        )
        self._p1.start()
        self._p2 = patch.object(
            app_module, "_rides_fit_dir", return_value=self._fit_dir,
        )
        self._p2.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._p2.stop()
        self._p1.stop()
        self._tmp.cleanup()


class TestIcuLazyFetchZones(_Base):

    def test_zones_lazy_fetched_when_cached_record_lacks_them(self):
        _seed_empty_icu_record(self._icu_dir, "i144492547")

        with patch.object(
            training_module, "fetch_activity_full",
            return_value=ICU_DETAIL_FIXTURE,
        ) as m_full:
            r = self.client.get("/api/ride/icu_i144492547/detail")
        self.assertEqual(r.status_code, 200, r.text)
        m_full.assert_called_once_with("i144492547")
        data = r.json()
        tiz = data["time_in_zone"]
        # Z1..Z7 present and populated from the dict-shape icu_zone_times.
        self.assertEqual(tiz["z1"], 2311)
        self.assertEqual(tiz["z7"], 283)
        # SS bucket (sweet-spot 84-97% FTP) preserved.
        self.assertEqual(tiz.get("ss"), 1465)
        # HR zones come along too.
        hr_tiz = data["hr_time_in_zone"]
        self.assertEqual(hr_tiz["z1"], 2475)
        self.assertEqual(hr_tiz["z7"], 0)
        # Polarization block attached, picked up from ICU index.
        pol = data["polarization"]
        self.assertEqual(pol["polarization_index"], 1.4)
        self.assertIn("classification", pol)

    def test_intervals_returned_with_avg_power_and_ftp_pct(self):
        _seed_empty_icu_record(self._icu_dir, "i144492547")

        merged = dict(ICU_DETAIL_FIXTURE)
        merged["icu_intervals"] = ICU_INTERVALS_FIXTURE

        with patch.object(
            training_module, "fetch_activity_full", return_value=merged,
        ):
            r = self.client.get("/api/ride/icu_i144492547/detail")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        intervals = data["intervals"]
        self.assertEqual(len(intervals), 2)
        first = intervals[0]
        self.assertEqual(first["avg_power_w"], 170)
        self.assertEqual(first["duration_s"], 631)
        # ftp_pct = 170/248 * 100 = 68.5 → rounded to 0 dp = 69
        self.assertEqual(first["ftp_pct"], 69)
        self.assertEqual(first["z_band"], "Z2")
        # WORK interval keeps its type tag.
        self.assertEqual(intervals[1]["type"], "WORK")

    def test_hr_only_ride_yields_zero_zones_not_crash(self):
        _seed_empty_icu_record(self._icu_dir, "hr_only")
        # ICU returns the activity with zone_times absent (HR-only ride).
        hr_only = {
            "id": "hr_only",
            "name": "HR Only",
            "start_date_local": "2026-05-02T07:00:00",
            "moving_time": 1800,
            "elapsed_time": 1800,
            "distance": 10000.0,
            "icu_hr_zone_times": [600, 800, 300, 100, 0, 0, 0],
            # No icu_zone_times key at all.
        }
        with patch.object(
            training_module, "fetch_activity_full", return_value=hr_only,
        ):
            r = self.client.get("/api/ride/icu_hr_only/detail")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # Power zones absent → all zeros, no crash, no polarization block.
        for i in range(1, 8):
            self.assertEqual(data["time_in_zone"][f"z{i}"], 0)
        # HR zones still come back populated.
        self.assertEqual(data["hr_time_in_zone"]["z1"], 600)
        self.assertIsNone(data["polarization"])


class TestPolarizationMath(unittest.TestCase):

    def test_polarization_index_for_user_ride(self):
        # User's May 1 ride (Zwolle): rough Z1+Z2=48%, Z3+Z4=34%, Z5+=18%.
        # log10((48+18)/34) = log10(66/34) = log10(1.941) = 0.288 → 0.29.
        pi = polarization_index(48.0, 34.0, 18.0)
        self.assertAlmostEqual(pi, 0.29, places=2)
        # ICU's "1.40 drempel" comes from ICU's own internal definition;
        # our helper just confirms math is well-behaved.
        self.assertGreater(pi, 0)

    def test_classification_base_polarized(self):
        # v4.5.5 REFINE-CLASSIFY: closest canonical centroid wins.
        # (88, 8, 4) — closer to base (95,3,2) than to pyramidal (80,15,5).
        self.assertEqual(classify_distribution(88, 8, 4), "base")
        # (92, 6, 2) — exactly on the base side.
        self.assertEqual(classify_distribution(92, 6, 2), "base")
        # (78, 5, 17) — closest to polarized (80,5,15).
        self.assertEqual(classify_distribution(78, 5, 17), "polarized")
        # (70, 20, 10) — closest to pyramidal (80,15,5).
        self.assertEqual(classify_distribution(70, 20, 10), "pyramidal")
        # (40, 25, 35) — exactly on the HIIT centroid.
        self.assertEqual(classify_distribution(40, 25, 35), "hiit")


if __name__ == "__main__":
    unittest.main()
