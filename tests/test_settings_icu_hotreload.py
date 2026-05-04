"""v4.5.2 FIX-CREDS-HOTRELOAD — Settings save reloads ICU creds in-memory.

Verifies:
  (a) saving valid ICU creds via /api/setup/save updates pm._env (so
      config.ICU_ATHLETE_ID / config.ICU_API_KEY proxy the new value
      immediately, no restart) and the response surfaces creds_test:"passed"
      after a successful athlete/<id> probe.
  (b) saving invalid creds returns creds_test:"failed: ..." while still
      writing the .env (the user can fix the typo and re-save without losing
      the rest of their wizard input).
  (c) saving creds resets the rides-sync .last_sync_at marker to 0 so the
      next sync isn't blocked by the stale 1h throttle from before the user
      pasted new creds.
"""
from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import app as app_module
import config
import training as training_module


class _IcuHotreloadBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._base = Path(self._tmp.name)
        self._icu_dir = self._base / "rides" / "icu"
        self._icu_dir.mkdir(parents=True, exist_ok=True)
        # Patch the throttle marker into our tmp dir so we can prove it gets
        # reset without trampling the user's real ~/.domestique state.
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

        # Snapshot pm state so we can restore after the test (other tests in
        # the same process rely on the real profile creds). test_profiles.py
        # resets ``ProfileManager._instance = None`` between cases, so we
        # explicitly reinstate our cached singleton via ``get()`` here.
        from profile_manager import ProfileManager
        self._pm = ProfileManager.get()
        # Pin the singleton — without this, a prior test that nulled
        # _instance would cause setup_save to construct a fresh PM that
        # doesn't see our patched save_env.
        ProfileManager._instance = self._pm
        self._orig_env = dict(self._pm._env)

        self.client = TestClient(app_module.app)

    def tearDown(self):
        # Restore the original env on the singleton ProfileManager so other
        # tests still see the real creds.
        try:
            self._pm._env = self._orig_env
        except Exception:
            pass
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class TestIcuHotreloadValid(_IcuHotreloadBase):
    def test_valid_creds_update_pm_in_memory_and_pass_probe(self):
        """(a) Valid creds → pm._env updated immediately + creds_test:passed."""
        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module, "_get", return_value={"id": "i999999"}) as mock_get:

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            resp = self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "i999999", "icu_api_key": "newkey-XYZ"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload.get("ok"))
            self.assertEqual(payload.get("creds_test"), "passed")

        # In-memory cache reflects the new creds — config.ICU_* will too via
        # the __getattr__ proxy (which setup_save un-shadows by deleting any
        # stale module-level config.ICU_ATHLETE_ID written by an earlier
        # override path in training.py).
        self.assertEqual(self._pm._env.get("ICU_ATHLETE_ID"), "i999999")
        self.assertEqual(self._pm._env.get("ICU_API_KEY"), "newkey-XYZ")
        self.assertEqual(config.ICU_ATHLETE_ID, "i999999")
        self.assertEqual(config.ICU_API_KEY, "newkey-XYZ")

    def test_save_unshadows_stale_module_attribute(self):
        """Regression: even if a prior override path wrote
        ``config.ICU_ATHLETE_ID`` as a module-level attribute (shadowing the
        ``__getattr__`` proxy), saving fresh creds must restore the proxy so
        the new ``pm._env`` value is visible immediately."""
        # Simulate what training.fetch_recent_activities used to do — write
        # a module-level attribute that shadows the dynamic proxy.
        config.ICU_ATHLETE_ID = "i_stale"
        config.ICU_API_KEY = "stale_key"
        try:
            with patch.object(self._pm, "save_env") as mock_save, \
                 patch.object(training_module, "_get", return_value={"id": "i_new"}):

                def _fake_save_env(athlete_id, api_key):
                    self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                    self._pm._env["ICU_API_KEY"] = api_key
                mock_save.side_effect = _fake_save_env

                resp = self.client.post(
                    "/api/setup/save",
                    json={"icu_athlete_id": "i_new", "icu_api_key": "fresh_key"},
                )
                self.assertEqual(resp.status_code, 200)

            # The stale shadow attribute is gone — config now proxies through
            # ``__getattr__`` to ``pm._env`` and reflects the fresh creds.
            self.assertNotIn("ICU_ATHLETE_ID", config.__dict__)
            self.assertNotIn("ICU_API_KEY", config.__dict__)
            self.assertEqual(config.ICU_ATHLETE_ID, "i_new")
            self.assertEqual(config.ICU_API_KEY, "fresh_key")
        finally:
            for _attr in ("ICU_ATHLETE_ID", "ICU_API_KEY"):
                try:
                    delattr(config, _attr)
                except AttributeError:
                    pass

    def test_valid_creds_reset_sync_throttle(self):
        """(c) Saving creds resets .last_sync_at so next sync runs immediately."""
        # Pre-seed a recent throttle marker (would normally block sync for ~1h).
        self._sync_state.write_text(str(time.time()))
        self.assertTrue(self._sync_state.exists())

        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(training_module, "_get", return_value={"id": "i999999"}):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            resp = self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "i999999", "icu_api_key": "newkey-XYZ"},
            )
            self.assertEqual(resp.status_code, 200)

        # Throttle marker reset to 0 → 1h gate will see (now - 0) > 3600.
        self.assertTrue(self._sync_state.exists())
        self.assertEqual(self._sync_state.read_text().strip(), "0")


class TestIcuHotreloadInvalid(_IcuHotreloadBase):
    def test_invalid_creds_save_but_creds_test_failed(self):
        """(b) Invalid creds → response says creds_test:failed; .env still written."""
        with patch.object(self._pm, "save_env") as mock_save, \
             patch.object(
                 training_module, "_get",
                 side_effect=training_module.ICUAuthError("HTTP 401 on athlete/i_bad: Unauthorized"),
             ):

            def _fake_save_env(athlete_id, api_key):
                self._pm._env["ICU_ATHLETE_ID"] = athlete_id
                self._pm._env["ICU_API_KEY"] = api_key
            mock_save.side_effect = _fake_save_env

            resp = self.client.post(
                "/api/setup/save",
                json={"icu_athlete_id": "i_bad", "icu_api_key": "wrong-key"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.json()
            self.assertTrue(payload.get("ok"))
            self.assertTrue(
                str(payload.get("creds_test", "")).startswith("failed"),
                f"expected creds_test to start with 'failed', got {payload.get('creds_test')!r}",
            )

        # The .env IS still updated even on probe failure — the user may have
        # entered a key that ICU temporarily rejected (network blip) and we
        # don't want to lose their input.
        self.assertEqual(self._pm._env.get("ICU_ATHLETE_ID"), "i_bad")
        self.assertEqual(self._pm._env.get("ICU_API_KEY"), "wrong-key")


if __name__ == "__main__":
    unittest.main()
