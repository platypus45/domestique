"""ICU OAuth — per-profile "Connect" (IP_ICU_OAUTH.md).

Mocked end-to-end: no real client_id/secret and no network. Verifies the auth-header
swap (Bearer vs the legacy Basic, with byte parity), the CSRF state store, the
/start redirect, and the /callback token exchange + persistence. The single
un-mockable step (real ICU authorize+exchange) is a manual check once ICU issues
the credentials.
"""
import base64
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import training  # noqa: E402
import app as app_module  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _set_creds(token="", key="", athlete=""):
    # These names are __getattr__-proxied on config; assigning shadows the proxy.
    config.ICU_ACCESS_TOKEN = token
    config.ICU_API_KEY = key
    config.ICU_ATHLETE_ID = athlete


def _clear_creds():
    for a in ("ICU_ACCESS_TOKEN", "ICU_API_KEY", "ICU_ATHLETE_ID"):
        try:
            delattr(config, a)
        except AttributeError:
            pass


class TestAuthHeader(unittest.TestCase):
    def tearDown(self):
        _clear_creds()

    def test_token_uses_bearer(self):
        _set_creds(token="TOK", key="KEY", athlete="i1")  # token wins over key
        self.assertEqual(training._auth_header(), {"Authorization": "Bearer TOK"})

    def test_key_only_uses_basic_and_is_byte_identical(self):
        _set_creds(token="", key="SECRET", athlete="i1")
        expect = "Basic " + base64.b64encode(b"API_KEY:SECRET").decode()
        self.assertEqual(training._auth_header(), {"Authorization": expect})

    def test_neither_raises(self):
        _set_creds(token="", key="", athlete="")
        with self.assertRaises(training.ICUCredentialsMissing):
            training._auth_header()


class TestStateStore(unittest.TestCase):
    def test_single_use_and_ttl(self):
        app_module._icu_oauth_states.clear()
        app_module._icu_oauth_states["fresh"] = {"profile_id": "p", "ts": 1_000_000.0}
        app_module._icu_oauth_states["stale"] = {"profile_id": "p", "ts": 0.0}
        # prune at a time well past the TTL for "stale" but fresh for "fresh"
        app_module._icu_oauth_prune(1_000_000.0 + 10)
        self.assertIn("fresh", app_module._icu_oauth_states)
        self.assertNotIn("stale", app_module._icu_oauth_states)
        # single-use: pop consumes it
        self.assertIsNotNone(app_module._icu_oauth_states.pop("fresh", None))
        self.assertIsNone(app_module._icu_oauth_states.pop("fresh", None))


class TestRoutes(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app_module.app)
        app_module._icu_oauth_states.clear()
        self._orig_id = config.ICU_OAUTH_CLIENT_ID

    def tearDown(self):
        config.ICU_OAUTH_CLIENT_ID = self._orig_id
        app_module._icu_oauth_states.clear()
        _clear_creds()

    def test_start_unavailable_when_no_client(self):
        config.ICU_OAUTH_CLIENT_ID = ""
        r = self.client.get("/oauth/icu/start", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        self.assertIn("icu=unavailable", r.headers["location"])

    def test_start_redirects_to_icu_with_state(self):
        config.ICU_OAUTH_CLIENT_ID = "CID"
        r = self.client.get("/oauth/icu/start", follow_redirects=False)
        self.assertIn(r.status_code, (302, 307))
        loc = r.headers["location"]
        self.assertIn("intervals.icu/oauth/authorize", loc)
        self.assertIn("client_id=CID", loc)
        self.assertIn("state=", loc)
        # the minted state is stored for the callback to verify
        self.assertEqual(len(app_module._icu_oauth_states), 1)

    def test_callback_bad_state_errors(self):
        r = self.client.get("/oauth/icu/callback?code=x&state=nope", follow_redirects=False)
        self.assertIn("icu=error", r.headers["location"])
        self.assertIn("reason=state", r.headers["location"])

    def test_callback_success_persists_token(self):
        # AC3a: the token must be bound to the profile the STATE names ("p"),
        # not whichever profile is active when ICU redirects back. The old
        # assertion was dead — save_icu_token ignored the state's profile.
        app_module._icu_oauth_states["S1"] = {"profile_id": "p", "ts": 9e12}

        class _Resp:
            status_code = 200
            def json(self):
                return {"access_token": "ATOK", "athlete": {"id": "i999", "name": "Z"}}

        captured = {}
        from profile_manager import ProfileManager as _PM
        pm = _PM.get()

        def _capture(pm_arg, profile_id, token, athlete, name, **kw):
            captured.update(profile_id=profile_id, token=token,
                            athlete=athlete, name=name)

        with mock.patch("httpx.post", return_value=_Resp()), \
             mock.patch.object(pm, "list_profiles", return_value=[{"id": "p"}]), \
             mock.patch.object(app_module, "_icu_profile_stored_athlete_id",
                               return_value=""), \
             mock.patch.object(app_module, "_save_icu_token_for_profile",
                               side_effect=_capture):
            r = self.client.get("/oauth/icu/callback?code=C&state=S1", follow_redirects=False)
        self.assertEqual(captured.get("profile_id"), "p")  # bound to the state's profile
        self.assertEqual(captured.get("token"), "ATOK")
        self.assertEqual(captured.get("athlete"), "i999")
        self.assertEqual(captured.get("name"), "Z")  # name captured from token response
        self.assertIn("icu=connected", r.headers["location"])
        self.assertNotIn("S1", app_module._icu_oauth_states)  # consumed

    def test_callback_exchange_failure_errors(self):
        app_module._icu_oauth_states["S2"] = {"profile_id": "p", "ts": 9e12}
        from profile_manager import ProfileManager as _PM
        pm = _PM.get()
        with mock.patch("httpx.post", side_effect=RuntimeError("boom")), \
             mock.patch.object(pm, "list_profiles", return_value=[{"id": "p"}]):
            r = self.client.get("/oauth/icu/callback?code=C&state=S2", follow_redirects=False)
        self.assertIn("icu=error", r.headers["location"])
        self.assertIn("reason=exchange", r.headers["location"])

    # v3.11.3 — phase-specific failure codes. One screenshot ("token exchange
    # failed") used to cover five different faults; each now names itself.

    def _pm_with_profile(self):
        from profile_manager import ProfileManager as _PM
        return _PM.get()

    def test_callback_http_rejection_carries_status(self):
        app_module._icu_oauth_states["S5"] = {"profile_id": "p", "ts": 9e12}

        class _Resp:
            status_code = 404
            text = '{"status":404,"error":"Code not found (expired?)"}'
            def json(self):
                return {}

        pm = self._pm_with_profile()
        with mock.patch("httpx.post", return_value=_Resp()), \
             mock.patch.object(pm, "list_profiles", return_value=[{"id": "p"}]):
            r = self.client.get("/oauth/icu/callback?code=C&state=S5", follow_redirects=False)
        loc = r.headers["location"]
        self.assertIn("reason=exchange", loc)
        self.assertIn("status=404", loc)

    def test_callback_transport_error_is_network(self):
        import httpx
        app_module._icu_oauth_states["S6"] = {"profile_id": "p", "ts": 9e12}
        pm = self._pm_with_profile()
        with mock.patch("httpx.post", side_effect=httpx.ConnectError("refused")), \
             mock.patch.object(pm, "list_profiles", return_value=[{"id": "p"}]):
            r = self.client.get("/oauth/icu/callback?code=C&state=S6", follow_redirects=False)
        self.assertIn("reason=network", r.headers["location"])

    def test_callback_save_failure_is_named_not_exchange(self):
        # The exchange SUCCEEDED; persisting the token raised (profile-folder
        # permissions). Must not be reported as an exchange failure.
        app_module._icu_oauth_states["S7"] = {"profile_id": "p", "ts": 9e12}

        class _Resp:
            status_code = 200
            def json(self):
                return {"access_token": "ATOK", "athlete": {"id": "i1", "name": "Z"}}

        pm = self._pm_with_profile()
        with mock.patch("httpx.post", return_value=_Resp()), \
             mock.patch.object(pm, "list_profiles", return_value=[{"id": "p"}]), \
             mock.patch.object(app_module, "_icu_profile_stored_athlete_id",
                               return_value=""), \
             mock.patch.object(app_module, "_save_icu_token_for_profile",
                               side_effect=OSError("read-only profile dir")):
            r = self.client.get("/oauth/icu/callback?code=C&state=S7", follow_redirects=False)
        loc = r.headers["location"]
        self.assertIn("reason=save", loc)
        self.assertNotIn("reason=exchange", loc)

    def test_callback_purge_failure_is_named(self):
        app_module._icu_oauth_states["S8"] = {"profile_id": "p", "ts": 9e12}

        class _Resp:
            status_code = 200
            def json(self):
                return {"access_token": "ATOK", "athlete": {"id": "new", "name": "Z"}}

        import db as _db
        pm = self._pm_with_profile()
        with mock.patch("httpx.post", return_value=_Resp()), \
             mock.patch.object(pm, "list_profiles", return_value=[{"id": "p"}]), \
             mock.patch.object(app_module, "_icu_profile_stored_athlete_id",
                               return_value="old"), \
             mock.patch.object(_db, "purge_profile_data",
                               side_effect=RuntimeError("db locked")):
            r = self.client.get("/oauth/icu/callback?code=C&state=S8", follow_redirects=False)
        self.assertIn("reason=purge", r.headers["location"])

    def test_callback_deleted_profile_errors(self):
        # AC3a: the state's profile was deleted mid-flow → error redirect,
        # NEVER a bind to whichever profile is active now.
        app_module._icu_oauth_states["S3"] = {"profile_id": "ghost-gone", "ts": 9e12}
        saved = []
        with mock.patch.object(app_module, "_save_icu_token_for_profile",
                               side_effect=lambda *a, **k: saved.append(a)):
            r = self.client.get("/oauth/icu/callback?code=C&state=S3", follow_redirects=False)
        self.assertIn("icu=error", r.headers["location"])
        self.assertIn("reason=profile_gone", r.headers["location"])
        self.assertEqual(saved, [])  # nothing persisted anywhere

    def test_callback_empty_athlete_id_errors(self):
        # AC3d: token exchange succeeds but ICU returns no athlete id →
        # hard error redirect (reason=no_athlete_id), nothing saved (the old
        # behavior persisted the token and sync_skipped forever, silently).
        app_module._icu_oauth_states["S4"] = {"profile_id": "p", "ts": 9e12}

        class _Resp:
            status_code = 200
            def json(self):
                return {"access_token": "ATOK", "athlete": {}}

        saved = []
        from profile_manager import ProfileManager as _PM
        pm = _PM.get()
        with mock.patch("httpx.post", return_value=_Resp()), \
             mock.patch("httpx.get", side_effect=RuntimeError("no fallback")), \
             mock.patch.object(pm, "list_profiles", return_value=[{"id": "p"}]), \
             mock.patch.object(app_module, "_save_icu_token_for_profile",
                               side_effect=lambda *a, **k: saved.append(a)):
            r = self.client.get("/oauth/icu/callback?code=C&state=S4", follow_redirects=False)
        self.assertIn("icu=error", r.headers["location"])
        self.assertIn("reason=no_athlete_id", r.headers["location"])
        self.assertEqual(saved, [])

    def test_connection_method(self):
        _set_creds(token="T", key="", athlete="i1")
        self.assertEqual(self.client.get("/api/icu/connection").json()["method"], "oauth")
        _set_creds(token="", key="K", athlete="i1")
        self.assertEqual(self.client.get("/api/icu/connection").json()["method"], "apikey")
        _set_creds(token="", key="", athlete="")
        self.assertEqual(self.client.get("/api/icu/connection").json()["method"], "none")

    def test_scopes_one_per_area(self):
        """v3.0.2 regression: intervals.icu allows ONE scope per area — sending
        CALENDAR:READ and CALENDAR:WRITE together makes /oauth/authorize fail
        with "Duplicate scope CALENDAR", bricking connect AND reconnect."""
        scopes = config.ICU_OAUTH_SCOPES.split(",")
        areas = [s.split(":")[0] for s in scopes]
        self.assertEqual(len(areas), len(set(areas)),
                         f"duplicate scope area in ICU_OAUTH_SCOPES: {scopes}")
        # The push engine needs calendar write; reads ride on WRITE.
        self.assertIn("CALENDAR:WRITE", scopes)


if __name__ == "__main__":
    unittest.main()


# v3.11.3 — httpx must trust certifi AND the OS store (Windows AV/proxy TLS
# inspection re-signs intervals.icu with a root only the OS store knows).
def test_icu_verify_context_has_certifi_and_os_roots():
    import ssl
    import tls_trust
    ctx = app_module._icu_verify()
    assert isinstance(ctx, ssl.SSLContext)
    assert ctx.verify_mode == ssl.CERT_REQUIRED
    if tls_trust.backend_of(ctx) == tls_trust.OPENSSL:        # macOS / Linux
        assert ctx.cert_store_stats()["x509_ca"] > 100        # certifi loaded
        assert app_module._icu_verify() is ctx                # cached
    else:                                                     # Windows: OS-native, never shared
        assert app_module._icu_verify() is not ctx
