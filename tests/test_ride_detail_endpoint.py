"""v4.4.0 IMPL-SERVER §3 — /api/ride/<id>/detail contract tests.

Four test cases:
  1. Existing ICU ride returns full normalized payload (§3 schema)
  2. ?include=samples adds decimated streams ≤1800 points
  3. Unknown ride id → 404
  4. Path-traversal attempt → 400
"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import ride_storage
import training as training_module


def _seed_icu_record(icu_dir: Path, icu_id: str = "777", n_samples: int = 0) -> dict:
    rec = {
        "ride_id": f"icu_{icu_id}",
        "source": "icu",
        "external_id": icu_id,
        "name": "Detail Test",
        "started_at": datetime.now().astimezone().isoformat(),
        "duration_s": 3600,
        "moving_s": 3500,
        "distance_km": 30.0,
        "elevation_m": 200,
        "avg_power_w": 180,
        "np_w": 195,
        "if_pct": 78.0,
        "tss": 75.0,
        "kj": 650.0,
        "kj_above_ftp": 50.0,
        "kcal": 700,
        "avg_hr": 145,
        "hr_max": 168,
        "avg_cadence": 85,
        "weight_kg": 72.0,
        "ftp_at_ride": 250,
        "eftp_at_ride": 252,
        "decoupling_pct": 4.5,
        "dfa_alpha1": None,
        "time_in_zone": {"z1": 600, "z2": 1500, "z3": 800, "z4": 400, "z5": 200, "z6": 100, "z7": 0},
        "intervals": [],
        "samples_url": f"/api/ride/icu_{icu_id}/detail?include=samples",
    }
    icu_dir.mkdir(parents=True, exist_ok=True)
    (icu_dir / f"{icu_id}.json").write_text(json.dumps(rec))
    return rec


class _DetailBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._icu_dir = self._base / "rides" / "icu"
        self._fit_dir = self._base / "rides"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        self._fit_dir.mkdir(parents=True, exist_ok=True)

        self._p1 = patch.object(
            ride_storage, "_icu_rides_dir", return_value=self._icu_dir
        )
        self._p1.start()
        self._p2 = patch.object(
            app_module, "_rides_fit_dir", return_value=self._fit_dir
        )
        self._p2.start()
        self.client = TestClient(app_module.app)

    def tearDown(self):
        self._p2.stop()
        self._p1.stop()
        self._tmp.cleanup()


class TestRideDetailEndpoint(_DetailBase):

    def test_existing_icu_ride_returns_full_payload(self):
        _seed_icu_record(self._icu_dir, "alpha")
        r = self.client.get("/api/ride/icu_alpha/detail")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        # §3 keys present.
        for k in ("ride_id", "source", "started_at", "duration_s",
                  "tss", "if_pct", "np_w", "avg_power_w", "time_in_zone",
                  "intervals"):
            self.assertIn(k, data, f"missing {k}")
        self.assertEqual(data["ride_id"], "icu_alpha")
        self.assertEqual(data["source"], "icu")
        self.assertEqual(data["tss"], 75.0)
        # No samples key when not requested.
        self.assertNotIn("samples", data)

    def test_samples_query_param_includes_decimated_streams(self):
        _seed_icu_record(self._icu_dir, "beta")
        # Mock streams: 5400 samples (a 90-min ride at 1Hz) → must come back ≤1800.
        big_pwr = list(range(5400))
        big_hr = [120 + (i % 30) for i in range(5400)]
        big_cad = [80] * 5400
        big_alt = list(range(5400))

        with patch.object(
            training_module, "fetch_activity_streams",
            return_value={
                "watts": big_pwr,
                "heartrate": big_hr,
                "cadence": big_cad,
                "altitude": big_alt,
            },
        ):
            r = self.client.get("/api/ride/icu_beta/detail?include=samples")
            self.assertEqual(r.status_code, 200, r.text)
            data = r.json()
            self.assertIn("samples", data)
            samples = data["samples"]
            for k in ("t_s", "power_w", "hr_bpm", "cadence_rpm", "elevation_m"):
                self.assertIn(k, samples)
                self.assertLessEqual(
                    len(samples[k]), 1800,
                    f"{k} exceeded 1800 points: got {len(samples[k])}",
                )

    def test_unknown_ride_returns_404(self):
        r = self.client.get("/api/ride/icu_doesnotexist/detail")
        self.assertEqual(r.status_code, 404, r.text)
        # JSON error payload (not HTML).
        body = r.json()
        self.assertIn("error", body)

    def test_path_traversal_attempt_rejected(self):
        # Slashes are blocked by route + the regex sanitizer.
        r1 = self.client.get("/api/ride/icu_..%2Fetc%2Fpasswd/detail")
        self.assertIn(r1.status_code, (400, 404))
        # Direct payload guard — malformed prefix.
        r2 = self.client.get("/api/ride/icu_%24bad%21/detail")
        self.assertIn(r2.status_code, (400, 404))


if __name__ == "__main__":
    unittest.main()
