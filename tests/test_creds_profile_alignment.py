"""v4.5.4 FIX-CREDS-PROFILE — credentials match between save and sync.

Verifies the bug behind the 3rd escalation: user pastes API key in Settings,
save returns success, click Sync → "Connect Intervals.icu in Settings".

Root cause: ICU calls (discover_athlete_id, _get) used the default
``Python-urllib/3.x`` User-Agent, which Cloudflare in front of intervals.icu
rejects with HTTP 403 (error 1010). Therefore:
  * discover_athlete_id() always returned None on real networks
  * .env was written with empty ICU_ATHLETE_ID even though API key was valid
  * _icu_credentials_present() = bool('' AND key) = False
  * /api/rides/sync short-circuits with "no_credentials" → UI toast

Fix: every ICU urllib request now sets a custom User-Agent header
(``Domestique/<ver>``) so Cloudflare passes the call through.

Tests:
  (a) discover_athlete_id sends a User-Agent header (regression test for the
      Cloudflare 403 problem)
  (b) _get also sends a User-Agent header
  (c) /api/setup/save with only api_key → discover succeeds → response carries
      athlete_id_detected and pm._env has it set
  (d) After save with creds, /api/rides/sync no longer returns
      "no_credentials" — it sees the just-saved creds via the active profile
  (e) Two profiles exist; save writes to the ACTIVE profile (not "default"
      hardcoded) — ensures save and sync target the same .env
  (f) db._auth_disabled cleared on save (regression check from v4.5.2 — was
      already implemented; this asserts it actually fires)
  (g) last_sync_at reset to 0 on save (regression check from v4.5.2)
"""
from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import config
import training as training_module


class TestUserAgentOnIcuCalls(unittest.TestCase):
    """(a) + (b): every ICU urllib.Request must include a User-Agent header.

    Cloudflare returns HTTP 403 (error 1010) on the default urllib UA. Without
    this header, both ICU sync AND athlete-id auto-detect silently fail —
    which was the actual root cause of the 3rd-escalation creds-profile bug.
    """

    def test_discover_athlete_id_sends_user_agent(self):
        body = json.dumps({"id": "i225278", "name": "Test"}).encode()
        fake_resp = io.BytesIO(body)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: None
        captured = {}

        def _capture(req, *args, **kwargs):
            captured["req"] = req
            return fake_resp

        with patch.object(training_module.urllib.request, "urlopen", side_effect=_capture):
            training_module.discover_athlete_id("any-key")

        ua = captured["req"].get_header("User-agent")
        self.assertTrue(ua, "discover_athlete_id must send a User-Agent header")
        self.assertIn("Domestique", ua)

    def test_get_sends_user_agent(self):
        body = json.dumps([]).encode()
        fake_resp = io.BytesIO(body)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: None
        captured = {}

        def _capture(req, *args, **kwargs):
            captured["req"] = req
            return fake_resp

        # Stub credentials so _require_credentials passes.
        with patch.object(training_module, "_require_credentials"), \
             patch.object(training_module, "_auth_header", return_value={"Authorization": "Basic xxx"}), \
             patch.object(training_module.urllib.request, "urlopen", side_effect=_capture):
            training_module._get("athlete/i999999/activities")

        ua = captured["req"].get_header("User-agent")
        self.assertTrue(ua, "_get must send a User-Agent header")
        self.assertIn("Domestique", ua)


class _CredsProfileBase(unittest.TestCase):
    """Shared scaffolding mirroring test_settings_icu_hotreload."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._icu_dir = self._base / "rides" / "icu"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        self._sync_state = self._icu_dir / ".last_sync_at"
        self._wellness_state = self._base / "wellness" / ".last_sync_at"
        self._wellness_state.parent.mkdir(parents=True, exist_ok=True)
        self._patches = [
            patch.object(
                app_module, "_icu_sync_state_path", return_value=self._sync_state,
            ),
            patch.object(
                app_module, "_icu_wellness_sync_state_path", return_value=self._wellness_state,
            ),
        ]
        for p in self._patches:
            p.start()

        from profile_manager import ProfileManager
        self._pm = ProfileManager.get()
        ProfileManager._instance = self._pm
        self._orig_env = dict(self._pm._env)
        self.client = TestClient(app_module.app)

    def tearDown(self):
        try:
            self._pm._env = self._orig_env
        except Exception:
            pass
        for _attr in ("ICU_ATHLETE_ID", "ICU_API_KEY"):
            try:
                delattr(config, _attr)
            except AttributeError:
                pass
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class TestSaveWritesToActiveProfile(_CredsProfileBase):
    """(c)+(e): save_env writes to pm.active_dir; pm._env updates immediately.

    Both tests stub ``_write_env_atomic`` so we never touch the user's real
    ``~/.domestique/profiles/<id>/.env`` from CI.
    """

    def test_save_env_targets_active_profile_dir(self):
        """save_env writes to active_dir/.env, not to a hardcoded 'default'."""
        # Verify pm.save_env writes to pm.active_dir — this is the contract
        # that guarantees save and sync target the same .env even when multiple
        # profiles exist on disk.
        with patch.object(self._pm, "_write_env_atomic") as mock_write:
            self._pm.save_env("i123", "key-abc")
            mock_write.assert_called_once()
            written_path = mock_write.call_args[0][0]
            self.assertEqual(written_path, self._pm.active_dir / ".env")

    def test_save_updates_pm_env_immediately(self):
        """pm._env reflects new creds without restart."""
        with patch.object(self._pm, "_write_env_atomic"):
            self._pm.save_env("i_new", "fresh_key")
        self.assertEqual(self._pm._env["ICU_ATHLETE_ID"], "i_new")
        self.assertEqual(self._pm._env["ICU_API_KEY"], "fresh_key")
        self.assertEqual(config.ICU_ATHLETE_ID, "i_new")
        self.assertEqual(config.ICU_API_KEY, "fresh_key")


class TestSetupSaveAutoDetectsAthleteId(_CredsProfileBase):
    """(c): /api/setup/save with only api_key → discover succeeds → response
    carries athlete_id_detected. The discover network call now sends a UA so
    Cloudflare doesn't 403 it. The save flow stores the discovered id in .env
    so subsequent sync calls see both creds."""

    def test_save_with_key_only_writes_discovered_id_to_env(self):
        body = json.dumps({"id": "i_discovered", "name": "Auto"}).encode()
        fake_resp = io.BytesIO(body)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: None

        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module.urllib.request, "urlopen", return_value=fake_resp), \
             patch.object(training_module, "_get", return_value={"id": "i_discovered"}):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            resp = self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "", "icu_api_key": "real_key"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertEqual(payload.get("athlete_id_detected"), "i_discovered")
            # save_env was called with the discovered id (not empty string)
            mock_save.assert_called_once_with("i_discovered", "real_key")


class TestSyncSeesCredsAfterSave(_CredsProfileBase):
    """(d): after /api/setup/save with creds, /api/rides/sync sees them."""

    def test_sync_no_longer_says_no_credentials_after_save(self):
        # 1. Save creds (mock the probe + discover so we don't hit network).
        body = json.dumps({"id": "i_post_save", "name": "Saved"}).encode()
        fake_resp = io.BytesIO(body)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: None

        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module.urllib.request, "urlopen", return_value=fake_resp), \
             patch.object(training_module, "_get", return_value={"id": "i_post_save"}):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            r1 = self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "", "icu_api_key": "valid_key_xyz"},
            )
            self.assertEqual(r1.status_code, 200)

        # 2. Now sync should see creds (no_credentials → would have been the
        #    bug). We mock fetch_recent_activities so we don't hit the real ICU.
        with patch.object(training_module, "fetch_recent_activities", return_value=[]):
            r2 = self.client.post("/api/rides/sync?force=1")
            self.assertEqual(r2.status_code, 200)
            payload = r2.json()
            self.assertNotEqual(
                payload.get("status"),
                "no_credentials",
                f"sync after save still reports no_credentials: {payload}",
            )


class TestSaveResetsThrottleAndAuthDisabled(_CredsProfileBase):
    """(f)+(g): regression — db._auth_disabled cleared, last_sync_at reset.

    These were implemented in v4.5.2 but had no explicit test that the side
    effects fired in the same path that got tickled in production.
    """

    def test_save_creds_clears_db_auth_disabled(self):
        import db as _db
        _db._auth_disabled = True
        _db._consecutive_failures = 5

        body = json.dumps({"id": "i_clear"}).encode()
        fake_resp = io.BytesIO(body)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: None

        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module.urllib.request, "urlopen", return_value=fake_resp), \
             patch.object(training_module, "_get", return_value={"id": "i_clear"}):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            resp = self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "i_clear", "icu_api_key": "fresh"},
            )
            self.assertEqual(resp.status_code, 200)

        self.assertFalse(_db._auth_disabled)
        self.assertEqual(_db._consecutive_failures, 0)

    def test_save_creds_resets_last_sync_at(self):
        import time
        # Pre-seed a recent throttle marker.
        self._sync_state.write_text(str(time.time()))

        body = json.dumps({"id": "i_reset"}).encode()
        fake_resp = io.BytesIO(body)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: None

        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module.urllib.request, "urlopen", return_value=fake_resp), \
             patch.object(training_module, "_get", return_value={"id": "i_reset"}):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "i_reset", "icu_api_key": "fresh"},
            )

        self.assertEqual(self._sync_state.read_text().strip(), "0")


if __name__ == "__main__":
    unittest.main()
