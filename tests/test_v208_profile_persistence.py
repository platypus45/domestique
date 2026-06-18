"""v2.1.0 — Block A (Windows feedback): fresh-install profile persistence +
accented-name id.

- Fix A: `_rebuild_registry` must NOT persist an empty profiles.json (which made
  `migrate_to_profiles`' `registry.exists()` guard skip creating `default`), and
  the lifespan migrates BEFORE the first `ProfileManager.get()`. Without this, a
  fresh install had no active profile → every property fell back to defaults
  (FTP 200 / 70kg) and saves evaporated on reopen.
- Fix B: an accented name ("Raphaël") must slug to a valid ASCII id; the
  ASCII-only validator/path boundary previously 400'd "raphaël".
"""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestBlockAProfilePersistence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        from profile_manager import ProfileManager
        ProfileManager._instance = None

    def _home(self):
        return patch("pathlib.Path.home", return_value=Path(self.tmp))

    def test_empty_rebuild_does_not_persist_registry(self):
        # Fix A core: instantiating on a fresh (empty) data dir must NOT write
        # profiles.json — else migrate_to_profiles' registry.exists() guard fires
        # and the `default` profile is never created.
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        with self._home():
            ProfileManager.get()  # triggers _rebuild_registry on the empty dir
        reg = Path(self.tmp) / ".domestique" / "profiles.json"
        self.assertFalse(
            reg.exists(),
            "empty rebuild must NOT persist profiles.json (would block migrate)",
        )

    def test_fresh_install_boot_creates_active_default(self):
        # Reorder + non-destructive rebuild: the lifespan boot order (migrate
        # THEN get) on a fresh install yields a persistent, ACTIVE default — not
        # the no-active-profile state that returned FTP 200 / 70kg.
        from profile_manager import ProfileManager
        from migrate_profiles import migrate_to_profiles
        ProfileManager._instance = None
        with self._home():
            migrate_to_profiles()          # creates default + profiles.json
            pm = ProfileManager.get()      # reads the populated registry
            self.assertTrue(
                pm.has_any_profile(), "fresh install must create a default profile")
            self.assertTrue(
                pm.active_id,
                "default must be ACTIVE (not None → no 200/70 fallback)")
        self.assertTrue(
            (Path(self.tmp) / ".domestique" / "profiles.json").exists(),
            "registry must persist across reopen")

    def test_accented_name_slugs_to_valid_ascii_id(self):
        # Fix B: "Raphaël" must produce an ASCII id that passes the validator, so
        # save/switch no longer 400 with "invalid profile id".
        from profile_manager import ProfileManager, _PROFILE_ID_RE
        from migrate_profiles import migrate_to_profiles
        ProfileManager._instance = None
        with self._home():
            migrate_to_profiles()
            pm = ProfileManager.get()
            pid = pm.create_profile("Raphaël")
            self.assertEqual(pid, "raphael", f"accented name slugged to {pid!r}")
            self.assertRegex(
                pid, _PROFILE_ID_RE.pattern,
                f"id {pid!r} must pass the ASCII validator (no 400 on switch)")


if __name__ == "__main__":
    unittest.main()
