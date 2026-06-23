"""v2.2.9 — saving a manual FTP must refresh the LIVE config immediately.

Regression: /api/profile/update-ftp wrote athlete.json + the ftp_test_history
graph but left the running process's config.ATHLETE_FTP_W at its startup value.
The topbar + settings field read config.ATHLETE_FTP_W, so a saved FTP (e.g. 258)
appeared to "revert" to the old cached value (e.g. an eFTP of 243) until the app
was restarted — while the graph correctly showed the new value.
"""
import unittest
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import app as app_module
import config


class TestFtpConfigRefresh(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)
        self._orig_ftp = config.ATHLETE_FTP_W

    def tearDown(self):
        config.ATHLETE_FTP_W = self._orig_ftp

    def test_update_ftp_refreshes_live_config(self):
        config.ATHLETE_FTP_W = 243  # stale startup value (e.g. an old eFTP)
        fake_pm = MagicMock()
        fake_pm.ftp = 258
        fake_pm.ftp_source = "manual"
        fake_pm.ftp_test_history = []
        with patch("profile_manager.ProfileManager.get", return_value=fake_pm):
            r = self.client.post("/api/profile/update-ftp",
                                 json={"ftp": 258, "method": "manual", "applied": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(config.ATHLETE_FTP_W, 258,
                         "live config.ATHLETE_FTP_W not refreshed after manual FTP save")
        fake_pm.update_ftp.assert_called_once()

    def test_unapplied_ftp_does_not_touch_config(self):
        config.ATHLETE_FTP_W = 243
        fake_pm = MagicMock()
        fake_pm.ftp = 243
        fake_pm.ftp_source = "manual"
        fake_pm.ftp_test_history = []
        with patch("profile_manager.ProfileManager.get", return_value=fake_pm):
            r = self.client.post("/api/profile/update-ftp",
                                 json={"ftp": 300, "method": "ramp", "applied": False})
        self.assertEqual(r.status_code, 200, r.text)
        # applied=False records the test in the graph but must NOT change the
        # active FTP (config stays put).
        self.assertEqual(config.ATHLETE_FTP_W, 243)
        fake_pm.update_ftp.assert_not_called()


if __name__ == "__main__":
    unittest.main()
