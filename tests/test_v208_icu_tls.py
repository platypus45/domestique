"""v2.1.0 L1 + v2.3.0 MAC-TLS-FIX (ICU TLS). A frozen build's bundled Python has no
CA store for urllib, so HTTPS to intervals.icu fails cert verification → URLError →
"ICUNetworkError" on every credential save / sync. Fix: bundle certifi
(domestique.spec) + point urllib at it via SSL_CERT_FILE in launcher.configure_tls_ca.
v2.1.0 did this on win32 only, assuming macOS used system certs — WRONG: the
notarized .app hit the same error (reported on Mac mini + MacBook Air). v2.3.0
extends it to the frozen macOS .app (frozen darwin); dev macOS/Linux keep the system
store. The darwin-frozen path IS exercisable here (unlike the win32 runtime).
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

    def test_macos_dev_is_unchanged(self):
        # DEV macOS (not frozen) keeps the system store — no-op.
        from launcher import configure_tls_ca
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSL_CERT_FILE", None)
            self.assertIsNone(configure_tls_ca(platform="darwin", frozen=False))
            self.assertNotIn("SSL_CERT_FILE", os.environ)

    def test_macos_frozen_app_points_urllib_at_certifi(self):
        # v2.3.0 MAC-TLS-FIX: the notarized .app (frozen darwin) DOES get the CA —
        # this is the fix for the "ICUNetworkError on Mac mini + MacBook Air" report.
        from launcher import configure_tls_ca
        import certifi
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSL_CERT_FILE", None)
            os.environ.pop("SSL_CERT_DIR", None)
            ca = configure_tls_ca(platform="darwin", frozen=True)
            self.assertEqual(ca, certifi.where())
            self.assertEqual(os.environ.get("SSL_CERT_FILE"), certifi.where())
            self.assertTrue(os.path.isfile(os.environ["SSL_CERT_FILE"]))

    def test_macos_frozen_respects_user_override(self):
        from launcher import configure_tls_ca
        with patch.dict(os.environ, {"SSL_CERT_FILE": "/custom/ca.pem"}, clear=False):
            configure_tls_ca(platform="darwin", frozen=True)
            self.assertEqual(os.environ["SSL_CERT_FILE"], "/custom/ca.pem")

    def test_frozen_linux_is_patched_too(self):
        """This class used to assert Linux is NEVER patched, on the premise
        that "the system store works". It works on the distro we BUILD on.

        The AppImage ships its own libcrypto and PyInstaller puts _internal on
        LD_LIBRARY_PATH, so that copy wins over the host's — and its
        compiled-in OPENSSLDIR is Debian's /usr/lib/ssl, a path that does not
        exist on Fedora/RHEL/Rocky/Alma and differs on Arch and openSUSE. So
        urllib failed cert verification there even with ca-certificates
        installed and current.

        The partial nature is what made it vicious: the OAuth exchange runs on
        httpx with its own certifi and SUCCEEDED, so the app said "connected"
        while every sync, FIT upload and calendar push — all urllib — failed
        forever with CERTIFICATE_VERIFY_FAILED.
        """
        import certifi
        from launcher import configure_tls_ca
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSL_CERT_FILE", None)
            ca = configure_tls_ca(platform="linux", frozen=True)
            self.assertEqual(ca, certifi.where())
            self.assertEqual(os.environ.get("SSL_CERT_FILE"), certifi.where())
            self.assertTrue(os.path.isfile(os.environ["SSL_CERT_FILE"]))

    def test_linux_dev_checkout_keeps_the_system_store(self):
        """Only the FROZEN build carries its own OpenSSL. A dev checkout runs
        on the system Python, whose cert paths match the machine it is on."""
        from launcher import configure_tls_ca
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SSL_CERT_FILE", None)
            self.assertIsNone(configure_tls_ca(platform="linux", frozen=False))
            self.assertNotIn("SSL_CERT_FILE", os.environ)


if __name__ == "__main__":
    unittest.main()
