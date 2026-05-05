"""v1.0.7 IMPL-HRV-PROMPT — tests for the home-page HRV-recording educational toast.

Coverage:
  1. ``fit_activity.parse_device_info`` resolves a known Garmin product ID
     to the friendly name ("Fēnix 8" for 4426).
  2. ``parse_device_info`` falls back to "Unknown Garmin product ID <n>" for
     IDs not in our lookup table.
  3. ``GET /api/wellness/hrv-recording-status`` returns
     ``should_show_prompt=True`` when the most-recent ride has
     ``dfa_alpha1_status == 'no_rr_data'`` and no dismissal flag is set.
  4. ``GET /api/wellness/hrv-recording-status`` returns
     ``should_show_prompt=False`` when the most-recent ride's
     ``dfa_alpha1_status == 'computed'``.
  5. ``POST /api/wellness/hrv-recording-dismiss`` with ``level='version'``
     persists the flag and subsequent status calls return
     ``should_show_prompt=False``.
"""
from __future__ import annotations

import json
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
import fit_activity  # noqa: E402


# ── helpers ─────────────────────────────────────────────────────────────────


def _build_fit_with_garmin_product(garmin_product_id: int) -> Path:
    """Synthesise a minimal FIT carrying a FileIdMessage with a given garmin_product."""
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer

    b = FitFileBuilder()
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.manufacturer = Manufacturer.GARMIN.value
    # `garmin_product` field accepts either the enum or the raw int.
    fid.garmin_product = garmin_product_id
    fid.serial_number = 4242
    b.add(fid)
    ff = b.build()
    tf = tempfile.NamedTemporaryFile(suffix=".fit", delete=False)
    tf.close()
    ff.to_file(tf.name)
    return Path(tf.name)


# ── 1 + 2 — parse_device_info friendly-name resolution ──────────────────────


class TestParseDeviceInfo(unittest.TestCase):
    """parse_device_info maps numeric IDs to friendly names."""

    def test_fenix_8_resolves_to_friendly_name(self):
        """garmin_product=4426 → garmin_product_name='Fēnix 8' (per
        ``_GARMIN_PRODUCT_NAMES`` in fit_activity.py).
        """
        fit_path = _build_fit_with_garmin_product(4426)
        try:
            info = fit_activity.parse_device_info(fit_path)
            self.assertIsInstance(info, dict)
            self.assertEqual(info["manufacturer"], "garmin")
            self.assertEqual(info["garmin_product"], 4426)
            self.assertEqual(info["garmin_product_id"], 4426)
            self.assertEqual(info["garmin_product_name"], "Fēnix 8")
        finally:
            fit_path.unlink(missing_ok=True)

    def test_unknown_product_id_falls_back(self):
        """An ID not in our lookup table returns 'Unknown Garmin product ID <n>'.

        Picks an ID inside the FIT uint16 range (0..65535) but unmapped in
        ``_GARMIN_PRODUCT_NAMES``. 5000 is well above the highest mapped
        Forerunner / Edge / Fēnix slot we cover.
        """
        unmapped_id = 5000
        self.assertNotIn(
            unmapped_id, fit_activity._GARMIN_PRODUCT_NAMES,
            "test fixture is no longer 'unknown' — pick a different unmapped ID",
        )
        fit_path = _build_fit_with_garmin_product(unmapped_id)
        try:
            info = fit_activity.parse_device_info(fit_path)
            self.assertIsInstance(info, dict)
            self.assertEqual(info["garmin_product"], unmapped_id)
            self.assertEqual(
                info["garmin_product_name"],
                f"Unknown Garmin product ID {unmapped_id}",
            )
        finally:
            fit_path.unlink(missing_ok=True)


# ── 3 + 4 + 5 — endpoint behaviour ──────────────────────────────────────────


class TestHrvRecordingStatusEndpoints(unittest.TestCase):
    """/api/wellness/hrv-recording-status + /api/wellness/hrv-recording-dismiss."""

    def setUp(self):
        # Disposable DATA_DIR so the dismissal-flag JSON doesn't leak between
        # tests or stomp the user's real ~/.domestique state.
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_path = Path(self._tmp.name)
        self._orig_data_dir = app_module.DATA_DIR
        app_module.DATA_DIR = self.tmp_path
        self.client = TestClient(app_module.app)

    def tearDown(self):
        app_module.DATA_DIR = self._orig_data_dir
        self._tmp.cleanup()

    def _stub_last_ride(self, ride_dict: dict):
        """Patch _load_all_rides_safe to return one synthetic ride."""
        return patch.object(
            app_module, "_load_all_rides_safe", lambda: [ride_dict]
        )

    def test_status_should_show_when_no_rr_data(self):
        """should_show_prompt=True when last ride has dfa_alpha1_status='no_rr_data'."""
        synth_ride = {
            "ride_id": "icu_synthetic_no_rr",
            "source": "icu",
            "external_id": "synthetic_no_rr",
            "dfa_alpha1_status": "no_rr_data",
            "device_product_name": "Edge 530",
        }
        with self._stub_last_ride(synth_ride):
            r = self.client.get("/api/wellness/hrv-recording-status")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertTrue(d["should_show_prompt"], d)
        self.assertEqual(d["device_product_name"], "Edge 530")
        self.assertEqual(d["last_ride_id"], "icu_synthetic_no_rr")
        self.assertEqual(d["dismissal_state"], "none")

    def test_status_should_not_show_when_computed(self):
        """should_show_prompt=False when last ride has dfa_alpha1_status='computed'."""
        synth_ride = {
            "ride_id": "icu_synthetic_computed",
            "source": "icu",
            "external_id": "synthetic_computed",
            "dfa_alpha1_status": "computed",
            "dfa_alpha1_avg": 0.92,
            "device_product_name": "Fēnix 8",
        }
        with self._stub_last_ride(synth_ride):
            r = self.client.get("/api/wellness/hrv-recording-status")
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        self.assertFalse(d["should_show_prompt"], d)

    def test_dismiss_version_silences_subsequent_status(self):
        """POST /dismiss with level=version sets the flag; status returns False."""
        synth_ride = {
            "ride_id": "icu_synthetic_no_rr_2",
            "source": "icu",
            "external_id": "synthetic_no_rr_2",
            "dfa_alpha1_status": "no_rr_data",
            "device_product_name": "Edge 1040",
        }
        with self._stub_last_ride(synth_ride):
            # 1) Pre-dismissal: should_show_prompt=True.
            r1 = self.client.get("/api/wellness/hrv-recording-status")
            self.assertEqual(r1.status_code, 200)
            self.assertTrue(r1.json()["should_show_prompt"])

            # 2) POST dismiss with version-level.
            r2 = self.client.post(
                "/api/wellness/hrv-recording-dismiss",
                json={"level": "version"},
            )
            self.assertEqual(r2.status_code, 200, r2.text)
            d2 = r2.json()
            self.assertTrue(d2["ok"])
            self.assertEqual(d2["level"], "version")

            # 3) Post-dismissal: should_show_prompt=False, dismissal_state='version'.
            r3 = self.client.get("/api/wellness/hrv-recording-status")
            self.assertEqual(r3.status_code, 200)
            d3 = r3.json()
            self.assertFalse(d3["should_show_prompt"])
            self.assertEqual(d3["dismissal_state"], "version")

        # 4) Sanity-check the on-disk flag file landed in the tmp DATA_DIR.
        flag_file = self.tmp_path / "hrv_prompt_state.json"
        self.assertTrue(flag_file.exists(), "dismissal-flag JSON not written")
        state = json.loads(flag_file.read_text(encoding="utf-8"))
        self.assertIn(app_module._VERSION, state.get("version_dismissed", {}))


if __name__ == "__main__":
    unittest.main()
