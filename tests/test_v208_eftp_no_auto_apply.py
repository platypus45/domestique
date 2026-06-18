"""v2.1.0 — I1: ICU's eFTP is unreliable; it must NOT silently rewrite the
athlete's FTP (and every FTP-derived zone). The 7-day sustained-drift
auto-apply (check_and_auto_apply_eftp) is now OPT-IN via
user_prefs.json {"eftp_auto_apply": true}; default off = detect + surface,
never auto-write.
"""
import unittest
from unittest import mock

import config
import profile_manager
import training_planner as tp


def _drift_series(eftp, days=8):
    # newest-last; every day's eFTP above the +3% drift threshold
    return [{"id": f"2026-06-{10 + i:02d}", "sportInfo": [{"eftp": eftp}]}
            for i in range(days)]


class _StubPM:
    def __init__(self, prefs):
        self.prefs = prefs
        self.applied = []

    def record_ftp_test(self, **kw):
        self.applied.append(("record", kw))

    def update_ftp(self, *a, **kw):
        self.applied.append(("update", a, kw))


class TestEftpNoAutoApply(unittest.TestCase):
    def setUp(self):
        self._old_ftp = config.ATHLETE_FTP_W
        config.ATHLETE_FTP_W = 200  # eFTP 250 > 200*1.03 → sustained up-drift

    def tearDown(self):
        config.ATHLETE_FTP_W = self._old_ftp

    def test_default_prefs_do_not_auto_apply(self):
        stub = _StubPM(prefs={})  # no opt-in
        with mock.patch.object(profile_manager.ProfileManager, "get",
                               return_value=stub):
            out = tp.check_and_auto_apply_eftp(_drift_series(250))
        self.assertIsNone(out, "eFTP must not auto-apply without opt-in")
        self.assertEqual(stub.applied, [], "FTP/zones must not be rewritten")

    def test_opt_in_preserves_auto_apply_path(self):
        stub = _StubPM(prefs={"eftp_auto_apply": True})
        with mock.patch.object(profile_manager.ProfileManager, "get",
                               return_value=stub):
            out = tp.check_and_auto_apply_eftp(_drift_series(250))
        self.assertIsNotNone(out, "opt-in must keep the auto-apply capability")
        self.assertTrue(any(c[0] == "update" for c in stub.applied),
                        "opt-in path should still call update_ftp")


if __name__ == "__main__":
    unittest.main()
