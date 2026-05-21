"""v1.8.8 Bug 9 — energy-system backfill returns per-ride reasons.

Master decisions §Bug 9: ``/api/wellness/backfill-3d-fitness`` returns a
``results`` list with ``{ride_id, skipped_reason}`` for every ride.
``skipped_reason`` is one of:

  - ``"no_power_stream"``   ICU/FIT fetch returned nothing
  - ``"all_zero"``          stream present but all samples == 0
  - ``"fit_missing"``        FIT-sourced ride but the .fit file is gone
  - ``"cutoff"``             ride predates v1.0.6 SS cutoff
  - ``"already_cached"``     a row exists for this day already
  - ``None``                 successful backfill
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


class TestBackfillReasons(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)

    def test_per_ride_reasons_returned(self):
        """Each ride surfaces a skipped_reason (or None on success)."""
        rides = [
            # Pre-cutoff → cutoff
            {"ride_id": "icu_pre_v106",
             "source": "icu", "external_id": "old",
             "started_at": "2020-01-01T08:00:00Z"},
            # ICU, fetch returns no streams → no_power_stream
            {"ride_id": "icu_nostreams",
             "source": "icu", "external_id": "nostream",
             "started_at": "2026-05-10T08:00:00Z"},
            # ICU, fetch returns all-zero stream → all_zero
            {"ride_id": "icu_allzero",
             "source": "icu", "external_id": "allzero",
             "started_at": "2026-05-11T08:00:00Z"},
            # FIT, no _fit_path → fit_missing
            {"ride_id": "fit_lost",
             "source": "fit",
             "started_at": "2026-05-12T08:00:00Z"},
        ]
        # ICU streams fetcher: nostream → empty, allzero → list of zeros.
        def _fake_streams(ext_id):
            if ext_id == "nostream":
                return {}
            if ext_id == "allzero":
                return {"watts": [0, 0, 0, 0]}
            return {}
        with patch("ride_storage.load_all_rides", return_value=rides), \
             patch("db.query_metric_history", return_value=[]), \
             patch("training.fetch_activity_streams", side_effect=_fake_streams), \
             patch.object(app_module, "_v136_extract_fit_power_series",
                          return_value=[]):
            r = self.client.post("/api/wellness/backfill-3d-fitness")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("results", data, data)
        results = data["results"]
        self.assertEqual(len(results), len(rides))
        by_id = {row["ride_id"]: row["skipped_reason"] for row in results}
        self.assertEqual(by_id["icu_pre_v106"], "cutoff")
        self.assertEqual(by_id["icu_nostreams"], "no_power_stream")
        self.assertEqual(by_id["icu_allzero"], "all_zero")
        self.assertEqual(by_id["fit_lost"], "fit_missing")

    def test_results_list_always_present_even_when_empty(self):
        """No rides → ``results: []`` (not missing)."""
        with patch("ride_storage.load_all_rides", return_value=[]), \
             patch("db.query_metric_history", return_value=[]):
            r = self.client.post("/api/wellness/backfill-3d-fitness")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertIn("results", data)
        self.assertEqual(data["results"], [])


if __name__ == "__main__":
    unittest.main()
