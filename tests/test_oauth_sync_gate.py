"""v2.2.8 — the ICU sync gate must recognise OAuth, not just legacy Basic auth.

Regression: a rider who connected via OAuth has an ICU_ACCESS_TOKEN (Bearer) and
an EMPTY ICU_API_KEY. `_icu_credentials_present()` previously required
ICU_API_KEY, so every sync path that gates on it (`_sync_icu_activities`, the
lazy-sync hook) early-returned "no_credentials" and recent rides never synced —
even though training.py authenticates fine via the Bearer token. The gate must
mirror training.py's precedence: access token first, Basic pair as fallback.
"""
import unittest
from unittest.mock import patch

import app as app_module
import config


class TestIcuCredentialGate(unittest.TestCase):
    def _set(self, athlete_id=None, api_key=None, access_token=None):
        # Patch the three config attributes the gate reads.
        return (
            patch.object(config, "ICU_ATHLETE_ID", athlete_id),
            patch.object(config, "ICU_API_KEY", api_key),
            patch.object(config, "ICU_ACCESS_TOKEN", access_token),
        )

    def _check(self, **kw):
        patches = self._set(**kw)
        for p in patches:
            p.start()
        try:
            return app_module._icu_credentials_present()
        finally:
            for p in patches:
                p.stop()

    def test_oauth_only_token_is_present(self):
        # The real-world OAuth shape: token set, API key empty.
        self.assertTrue(self._check(athlete_id="i12345", api_key="", access_token="tok_abc"))

    def test_oauth_token_without_athlete_id_still_present(self):
        # training.py treats a bare access token as configured.
        self.assertTrue(self._check(athlete_id=None, api_key=None, access_token="tok_abc"))

    def test_legacy_basic_pair_still_present(self):
        self.assertTrue(self._check(athlete_id="i12345", api_key="key_xyz", access_token=None))

    def test_nothing_configured_is_absent(self):
        self.assertFalse(self._check(athlete_id=None, api_key=None, access_token=None))

    def test_athlete_id_alone_is_absent(self):
        # An athlete id without any secret is not usable.
        self.assertFalse(self._check(athlete_id="i12345", api_key="", access_token=None))


if __name__ == "__main__":
    unittest.main()
