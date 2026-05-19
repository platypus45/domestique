"""v1.8.3 BUG-B — HRV-toast suppressed when recent rides have DFA computed.

User reported a false-positive popup: "Your last ride had HR but no beat-to-beat
HRV — enable HRV recording on your Garmin to unlock DFA α1 fatigue tracking."
Their actual most-recent ride landed with ``dfa_alpha1_status='computed'`` (the
v1.8.1 RR sentinel filter + v1.7.5 45s DFA timeout pipeline worked). The toast
was reading a stale signal from an earlier ride OR the most-recent ride had a
transient strap dropout while older rides were healthy.

Fix: ``/api/wellness/hrv-recording-status`` now walks the last 3 rides; if ANY
of them has ``dfa_alpha1_status='computed'`` the toast is suppressed.

Coverage:
  1. Endpoint returns ``should_show_prompt=False`` when latest 3 rides include
     at least one with ``dfa_alpha1_status='computed'`` (even if the
     most-recent ride was ``no_rr_data``).
  2. Endpoint returns ``should_show_prompt=True`` when all latest 3 rides have
     ``dfa_alpha1_status='no_rr_data'`` (the genuine signal — rider has never
     enabled HRV recording on their device).
  3. Dismiss persistence still works (v1.0.7 behavior unchanged) — POSTing
     ``level='version'`` silences the toast even when the no-computed signal
     would otherwise raise it.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).parent
REPO_ROOT = HERE.parent
sys.path.insert(0, str(REPO_ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402


class TestHrvPromptSuppressOnRecentComputed(unittest.TestCase):
    """v1.8.3 BUG-B: suppress when recent rides have DFA computed."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._orig_data_dir = app_module.DATA_DIR
        app_module.DATA_DIR = self.tmp_path
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.DATA_DIR = self._orig_data_dir
        self._tmp.cleanup()

    def _stub_rides(self, rides: list[dict]):
        """Patch _load_all_rides_safe to return a fixed newest-first list."""
        return patch.object(
            app_module, "_load_all_rides_safe", lambda: list(rides)
        )

    def test_suppresses_when_recent_ride_has_computed(self):
        """User's bug: latest ride no_rr_data but a recent ride was computed.

        Mimics the live signal — a single misfire (HR strap dropped) on the
        most-recent ride while older rides captured HRV cleanly. Toast must
        not fire.
        """
        rides = [
            # Newest (today) — strap glitch, no RR.
            {
                "ride_id": "icu_today",
                "source": "icu",
                "external_id": "today",
                "dfa_alpha1_status": "no_rr_data",
                "device_product_name": "Fēnix 8",
            },
            # Yesterday — clean HRV.
            {
                "ride_id": "icu_yesterday",
                "source": "icu",
                "external_id": "yesterday",
                "dfa_alpha1_status": "computed",
                "dfa_alpha1_avg": 0.92,
                "device_product_name": "Fēnix 8",
            },
            # Day-before — clean HRV.
            {
                "ride_id": "icu_dby",
                "source": "icu",
                "external_id": "dby",
                "dfa_alpha1_status": "computed",
                "dfa_alpha1_avg": 0.88,
                "device_product_name": "Fēnix 8",
            },
        ]
        with self._stub_rides(rides):
            r = self.client.get("/api/wellness/hrv-recording-status")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertFalse(
            d["should_show_prompt"],
            f"expected suppress (recent ride was computed), got {d}",
        )
        # The endpoint still reports the most-recent ride's id (UI sanity).
        self.assertEqual(d["last_ride_id"], "icu_today")

    def test_shows_when_all_recent_rides_lack_rr(self):
        """All last 3 rides have dfa_alpha1_status='no_rr_data' → toast fires.

        This is the genuine educational case — rider has never enabled HRV
        recording on their Garmin, so no ride in recent history captured RR.
        """
        rides = [
            {
                "ride_id": "icu_r1",
                "dfa_alpha1_status": "no_rr_data",
                "device_product_name": "Edge 530",
            },
            {
                "ride_id": "icu_r2",
                "dfa_alpha1_status": "no_rr_data",
                "device_product_name": "Edge 530",
            },
            {
                "ride_id": "icu_r3",
                "dfa_alpha1_status": "no_rr_data",
                "device_product_name": "Edge 530",
            },
        ]
        with self._stub_rides(rides):
            r = self.client.get("/api/wellness/hrv-recording-status")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(
            d["should_show_prompt"],
            f"expected show (all recent rides no_rr_data), got {d}",
        )
        self.assertEqual(d["device_product_name"], "Edge 530")
        self.assertEqual(d["last_ride_id"], "icu_r1")

    def test_dismiss_version_silences_even_with_no_rr_signal(self):
        """v1.0.7 behaviour unchanged — version-dismissal overrides everything.

        Even when the no_rr_data signal would normally fire the toast, a
        ``level='version'`` dismissal silences it for the current VERSION.
        """
        rides = [
            {
                "ride_id": "icu_no_rr_only",
                "dfa_alpha1_status": "no_rr_data",
                "device_product_name": "Edge 1040",
            },
            {
                "ride_id": "icu_no_rr_only_2",
                "dfa_alpha1_status": "no_rr_data",
                "device_product_name": "Edge 1040",
            },
        ]
        with self._stub_rides(rides):
            # 1) Pre-dismiss: would show (no computed in recent history).
            r1 = self.client.get("/api/wellness/hrv-recording-status")
            self.assertTrue(r1.json()["should_show_prompt"])

            # 2) POST dismiss.
            r2 = self.client.post(
                "/api/wellness/hrv-recording-dismiss",
                json={"level": "version"},
            )
            self.assertEqual(r2.status_code, 200, r2.text)
            self.assertTrue(r2.json()["ok"])

            # 3) Post-dismiss: silenced.
            r3 = self.client.get("/api/wellness/hrv-recording-status")
            d3 = r3.json()
            self.assertFalse(d3["should_show_prompt"])
            self.assertEqual(d3["dismissal_state"], "version")


if __name__ == "__main__":
    unittest.main()
