"""v4.5.3 FIX-AUTO-ATHLETE-ID — auto-detect athlete ID from API key.

Verifies:
  (a) discover_athlete_id() returns the athlete dict on a 200 response from
      /api/v1/athlete/0 with a populated `id` field.
  (b) discover_athlete_id() returns None on HTTP 403 (revoked/typo key).
  (c) /api/setup/save with only api_key (no athlete_id) auto-detects the ID
      and the response surfaces `athlete_id_detected` + `athlete_name`.
  (d) /api/setup/save with both submitted, both matching → no `warning`.
  (e) /api/setup/save with both submitted, mismatch → response carries a
      `warning` field while still honouring the user-submitted ID.
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
import training as training_module


class TestDiscoverAthleteIdHelper(unittest.TestCase):
    """Unit tests for the training.discover_athlete_id helper."""

    def test_200_returns_dict_with_id(self):
        """200 with valid JSON → returns the parsed dict."""
        body = json.dumps({"id": "i225278", "name": "Test Athlete"}).encode()
        fake_resp = io.BytesIO(body)
        fake_resp.__enter__ = lambda s: s
        fake_resp.__exit__ = lambda s, *a: None
        with patch.object(training_module.urllib.request, "urlopen", return_value=fake_resp):
            result = training_module.discover_athlete_id("test-key")
        self.assertIsNotNone(result)
        self.assertEqual(result.get("id"), "i225278")
        self.assertEqual(result.get("name"), "Test Athlete")

    def test_403_returns_none(self):
        """HTTP 403 (auth failure) → returns None."""
        err = urllib.error.HTTPError(
            "https://intervals.icu/api/v1/athlete/0", 403, "Forbidden", {}, None
        )
        with patch.object(training_module.urllib.request, "urlopen", side_effect=err):
            result = training_module.discover_athlete_id("bad-key")
        self.assertIsNone(result)

    def test_network_error_returns_none(self):
        """URLError / OSError → returns None (graceful degradation)."""
        with patch.object(
            training_module.urllib.request,
            "urlopen",
            side_effect=urllib.error.URLError("dns lookup failed"),
        ):
            result = training_module.discover_athlete_id("any-key")
        self.assertIsNone(result)

    def test_empty_key_returns_none(self):
        """Empty api_key → short-circuit None (no network call)."""
        result = training_module.discover_athlete_id("")
        self.assertIsNone(result)


class _SetupSaveBase(unittest.TestCase):
    """Shared scaffolding mirroring test_settings_icu_hotreload — tmp dirs +
    pm singleton snapshot/restore so we don't trample real profile creds."""

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
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class TestSetupSaveAutoDetect(_SetupSaveBase):
    def test_only_api_key_triggers_autodetect(self):
        """(c) POST {api_key} only → discover called, ID auto-saved, response
        surfaces athlete_id_detected + athlete_name."""
        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module, "_get", return_value={"id": "i225278"}), \
             patch.object(
                 training_module,
                 "discover_athlete_id",
                 return_value={"id": "i225278", "name": "Haringo"},
             ):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            resp = self.client.post(
                "/api/setup/save",
                json={"icu_api_key": "valid-key-no-id"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload.get("ok"))
            self.assertEqual(payload.get("athlete_id_detected"), "i225278")
            self.assertEqual(payload.get("athlete_name"), "Haringo")
            self.assertNotIn("warning", payload)

        # save_env was called with the auto-detected ID, not "" or None.
        args, kwargs = mock_save.call_args
        self.assertEqual(args[0], "i225278")
        self.assertEqual(args[1], "valid-key-no-id")

    def test_both_submitted_matching_no_warning(self):
        """(d) Both fields submitted and equal → no `warning`, no
        athlete_id_detected (user knew the ID)."""
        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module, "_get", return_value={"id": "i225278"}), \
             patch.object(
                 training_module,
                 "discover_athlete_id",
                 return_value={"id": "i225278", "name": "Haringo"},
             ):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            resp = self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "i225278", "icu_api_key": "valid-key"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload.get("ok"))
            self.assertNotIn("warning", payload)
            # athlete_id_detected only populated when we actually filled in a
            # missing ID — when user submitted a matching one we don't need to.
            self.assertNotIn("athlete_id_detected", payload)

    def test_both_submitted_mismatch_emits_warning(self):
        """(e) Both submitted but ID differs from /athlete/0 → warning is
        emitted but the user-submitted ID is honoured (override semantics)."""
        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module, "_get", return_value={"id": "i225278"}), \
             patch.object(
                 training_module,
                 "discover_athlete_id",
                 return_value={"id": "i225278", "name": "Haringo"},
             ):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            resp = self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "i999999", "icu_api_key": "valid-key"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload.get("ok"))
            self.assertIn("warning", payload)
            self.assertIn("i225278", payload["warning"])

        # Override: pm.save_env got the user-submitted ID, not the discovered one.
        args, _ = mock_save.call_args
        self.assertEqual(args[0], "i999999")

    def test_discover_failure_falls_back_gracefully(self):
        """If discover_athlete_id returns None (e.g. revoked key) and the user
        DID supply an athlete_id, the save still proceeds with their value —
        we just don't auto-fill. This protects the existing happy path."""
        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module, "_get", return_value={"id": "i225278"}), \
             patch.object(
                 training_module,
                 "discover_athlete_id",
                 return_value=None,
             ):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            resp = self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "i225278", "icu_api_key": "valid-key"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload.get("ok"))
            self.assertNotIn("athlete_id_detected", payload)
            self.assertNotIn("warning", payload)


if __name__ == "__main__":
    unittest.main()
