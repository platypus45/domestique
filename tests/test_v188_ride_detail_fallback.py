"""v1.8.8 Bug 1 — ride-detail endpoint bare-id fallback.

Master decisions §Bug 1: when the homepage passes a bare ICU id (no
``icu_`` prefix — legacy / cached routes.json), ``/api/ride/{id}/detail``
must fall back to the ICU storage lookup instead of returning 404.
"""
from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module


class TestRideDetailBareIdFallback(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)

    def _make_rec(self, ext_id: str) -> dict:
        return {
            "ride_id": f"icu_{ext_id}",
            "source": "icu",
            "external_id": ext_id,
            "name": "test ride",
            "started_at": "2026-05-01T08:00:00Z",
            "duration_s": 3600,
            "moving_s": 3600,
            "distance_km": 30.0,
            "summary": {},
        }

    def test_bare_id_falls_back_to_icu_lookup(self):
        """A bare id (no ``icu_`` prefix) routes through ICU fallback."""
        rec = self._make_rec("99999999")

        def _icu_lookup(rid):
            # Endpoint calls get_icu_ride() twice in the bare-id path:
            # the icu_-prefixed branch is skipped, then the fallback calls
            # get_icu_ride(rid) where rid is the bare id.
            return rec if rid == "99999999" else None

        with patch("ride_storage.get_icu_ride", side_effect=_icu_lookup), \
             patch.object(app_module, "_maybe_enrich_icu_record",
                          side_effect=lambda r: r):
            r = self.client.get("/api/ride/99999999/detail")
        self.assertEqual(r.status_code, 200, r.text)
        data = r.json()
        self.assertEqual(data["external_id"], "99999999")

    def test_prefixed_id_still_works(self):
        """Existing ``icu_<id>`` path is unchanged."""
        rec = self._make_rec("12345678")
        with patch("ride_storage.get_icu_ride",
                   side_effect=lambda rid: rec if rid in ("icu_12345678",
                                                          "12345678") else None), \
             patch.object(app_module, "_maybe_enrich_icu_record",
                          side_effect=lambda r: r):
            r = self.client.get("/api/ride/icu_12345678/detail")
        self.assertEqual(r.status_code, 200, r.text)

    def test_unknown_bare_id_logs_and_404s(self):
        """When nothing matches, the endpoint logs the failed lookup."""
        with patch("ride_storage.get_icu_ride", return_value=None), \
             patch("ride_storage.get_ride", return_value=None), \
             self.assertLogs(app_module._log, level="INFO") as cm:
            r = self.client.get("/api/ride/unknown_legacy_id/detail")
        self.assertEqual(r.status_code, 404, r.text)
        # 404 log line was emitted with the id.
        joined = "\n".join(cm.output)
        self.assertIn("unknown_legacy_id", joined)


if __name__ == "__main__":
    unittest.main()
