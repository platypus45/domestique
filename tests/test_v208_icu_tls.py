"""v2.1.0 — L1 (ICU TLS). In a frozen Windows build urllib had no CA store, so
HTTPS to intervals.icu failed cert verification → URLError → "ICUNetworkError" on
every credential save / sync. Fix: bundle certifi (domestique.spec) + point urllib
at it via SSL_CERT_FILE on win32 (launcher.configure_tls_ca). macOS uses system
certs and is left unchanged.

Windows-only runtime path can't be exercised on macOS CI; these assert the two
mechanisms of the fix (a valid CA bundle is present; the win32 hook sets the env
and the macOS path is a no-op).
"""
import os
import unittest
from unittest.mock import patch


class TestIcuTlsCaBundle(unittest.TestCase):
    def test_certifi_bundle_is_a_valid_ca_file(self):
        # The CA bundle the spec ships + launcher points urllib at must exist and
        # be a real PEM, so cert verification can succeed in the frozen app.
        import certifi
        p = certifi.where()
        self.assertTrue(os.path.isfile(p), f"certifi CA bundle missing: {p}")
        self.assertGreater(os.path.getsize(p), 50_000, "CA bundle implausibly small")
        with open(p, encoding="utf-8") as fh:
            self.assertIn("BEGIN CERTIFICATE", fh.read(4000))

    def test_win32_points_urllib_at_certifi(self):
        from launcher import configure_tls_ca
        import certifi
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSL_CERT_FILE", None)
            os.environ.pop("SSL_CERT_DIR", None)
            ca = configure_tls_ca(platform="win32")
            self.assertEqual(ca, certifi.where())
            self.assertEqual(os.environ.get("SSL_CERT_FILE"), certifi.where())
            self.assertTrue(os.path.isfile(os.environ["SSL_CERT_FILE"]))

    def test_win32_respects_user_override(self):
        from launcher import configure_tls_ca
        with patch.dict(os.environ, {"SSL_CERT_FILE": "/custom/ca.pem"}, clear=False):
            configure_tls_ca(platform="win32")
            # setdefault must NOT clobber a user-provided bundle.
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/custom/ca.pem")

    def test_macos_is_unchanged(self):
        from launcher import configure_tls_ca
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSL_CERT_FILE", None)
            self.assertIsNone(configure_tls_ca(platform="darwin"))
            self.assertNotIn("SSL_CERT_FILE", os.environ)


if __name__ == "__main__":
    unittest.main()
