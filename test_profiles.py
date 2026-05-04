"""Tests for profile_manager.py — Multi-user profile system.

Covers the ProfileManager singleton, profile CRUD, switching, migration,
config proxy, edge cases, and concurrency.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_base(tmp: str) -> Path:
    """Return a .domestique base dir inside the given temp directory."""
    base = Path(tmp) / ".domestique"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _bootstrap_default_profile(base: Path) -> None:
    """Create a minimal default profile so the manager can load without rebuild."""
    profiles_dir = base / "profiles" / "default"
    profiles_dir.mkdir(parents=True, exist_ok=True)
    (profiles_dir / "athlete.json").write_text(json.dumps({
        "ftp": 250, "weight_kg": 72.0, "lbm_kg": 58.0,
        "lthr": 175, "max_hr": 196,
        "hrv_baseline_mean": None, "hrv_baseline_sd": None,
        "rhr_baseline": None, "targets_override": None,
    }), encoding="utf-8")
    (profiles_dir / ".env").write_text(
        "ICU_ATHLETE_ID=i12345\nICU_API_KEY=key_abc\n", encoding="utf-8"
    )
    (profiles_dir / "user_prefs.json").write_text(json.dumps({
        "hours_per_week": 10.0, "available_days": [0,1,2,3,4,5,6], "rest_days": [0]
    }), encoding="utf-8")
    (profiles_dir / "device_prefs.json").write_text(json.dumps({}), encoding="utf-8")

    registry = {
        "version": 1, "active_profile": "default", "skip_picker": True,
        "profiles": [{
            "id": "default", "name": "Martijn", "color": "#3b82f6",
            "created": "2026-04-01T10:00:00", "last_used": "2026-04-12T10:00:00",
        }],
    }
    (base / "profiles.json").write_text(json.dumps(registry), encoding="utf-8")


def _fresh_manager(tmp: str):
    """Return a fresh ProfileManager whose home is the temp directory.

    Resets the singleton so each test is isolated.
    """
    from profile_manager import ProfileManager
    ProfileManager._instance = None
    with patch("pathlib.Path.home", return_value=Path(tmp)):
        pm = ProfileManager.get()
    return pm


# ==============================================================================
# Unit Tests — ProfileManager
# ==============================================================================

class TestSingleton(unittest.TestCase):
    """1. get() returns the same instance."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_singleton(self):
        with patch("pathlib.Path.home", return_value=Path(self.tmp)):
            from profile_manager import ProfileManager
            ProfileManager._instance = None
            a = ProfileManager.get()
            b = ProfileManager.get()
            self.assertIs(a, b)


class TestCreateProfile(unittest.TestCase):
    """2. create_profile creates directory, athlete.json, .env, user_prefs.json."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_profile(self):
        slug = self.pm.create_profile("Anna")
        profile_dir = Path(self.tmp) / ".domestique" / "profiles" / slug
        self.assertTrue(profile_dir.is_dir())
        self.assertTrue((profile_dir / "athlete.json").exists())
        self.assertTrue((profile_dir / ".env").exists())
        self.assertTrue((profile_dir / "user_prefs.json").exists())
        self.assertTrue((profile_dir / "device_prefs.json").exists())
        # athlete.json should have default ftp
        data = json.loads((profile_dir / "athlete.json").read_text())
        self.assertEqual(data["ftp"], 200)


class TestCreateProfileSlug(unittest.TestCase):
    """3. Slug generation: spaces to dashes, special chars removed."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_slug_spaces_to_dashes(self):
        slug = self.pm.create_profile("My Cool Profile")
        self.assertEqual(slug, "my-cool-profile")

    def test_slug_special_chars_removed(self):
        slug = self.pm.create_profile("Anna's Profile!!")
        self.assertEqual(slug, "annas-profile")

    def test_slug_unicode_stripped(self):
        slug = self.pm.create_profile("Rene & Co.")
        self.assertEqual(slug, "rene--co")


class TestCreateProfileDuplicate(unittest.TestCase):
    """4. Duplicate ID gets counter appended."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_duplicate_id_appends_counter(self):
        slug1 = self.pm.create_profile("Anna")
        slug2 = self.pm.create_profile("Anna")
        self.assertEqual(slug1, "anna")
        self.assertEqual(slug2, "anna-1")

    def test_triple_duplicate(self):
        self.pm.create_profile("Anna")
        self.pm.create_profile("Anna")
        slug3 = self.pm.create_profile("Anna")
        self.assertEqual(slug3, "anna-2")


class TestDeleteProfile(unittest.TestCase):
    """5. delete_profile removes directory and registry entry."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)
        self.pm.create_profile("ToDelete")

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_profile(self):
        profile_dir = Path(self.tmp) / ".domestique" / "profiles" / "todelete"
        self.assertTrue(profile_dir.exists())
        self.pm.delete_profile("todelete")
        self.assertFalse(profile_dir.exists())
        ids = [p["id"] for p in self.pm.list_profiles()]
        self.assertNotIn("todelete", ids)


class TestDeleteActiveProfileRaises(unittest.TestCase):
    """6. Cannot delete active profile."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)
        self.pm.create_profile("Other")

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_active_raises(self):
        with self.assertRaises(ValueError) as ctx:
            self.pm.delete_profile("default")
        self.assertIn("active", str(ctx.exception).lower())


class TestDeleteLastClearsActive(unittest.TestCase):
    """7. Deleting the last profile succeeds and clears active_id.

    v1.12 intentionally relaxed delete_profile so the user can purge their
    sole profile and the app falls into the empty-state wizard. The old
    "delete-last raises ValueError" contract was retired — see the in-line
    comment in profile_manager.delete_profile() for the rationale.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_delete_last_clears_active(self):
        # Only one profile exists ("default") — deleting it should succeed,
        # clear the active pointer, and leave a 0-profile registry.
        self.assertEqual(len(self.pm.list_profiles()), 1)
        result = self.pm.delete_profile("default")
        self.assertTrue(result)
        self.assertIsNone(self.pm.active_id)
        self.assertEqual(len(self.pm.list_profiles()), 0)
        self.assertFalse(self.pm.has_any_profile())


class TestSwitchProfile(unittest.TestCase):
    """8. switch() changes active_id and loads athlete data."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)
        slug = self.pm.create_profile("Partner")
        # Write custom athlete data to the partner profile
        partner_dir = Path(self.tmp) / ".domestique" / "profiles" / slug
        athlete = {
            "ftp": 180, "weight_kg": 60.0, "lbm_kg": 48.0,
            "lthr": 165, "max_hr": 185,
            "hrv_baseline_mean": None, "hrv_baseline_sd": None,
            "rhr_baseline": None, "targets_override": None,
        }
        (partner_dir / "athlete.json").write_text(
            json.dumps(athlete, indent=2), encoding="utf-8"
        )
        self._partner_slug = slug

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch.dict(os.environ, {}, clear=True)
    def test_switch_profile(self):
        self.assertEqual(self.pm.active_id, "default")
        self.assertEqual(self.pm.ftp, 250)

        # Mock db module to avoid importing real db
        mock_db = MagicMock()
        with patch.dict("sys.modules", {"db": mock_db, "training_planner": MagicMock()}):
            self.pm.switch(self._partner_slug)

        self.assertEqual(self.pm.active_id, self._partner_slug)
        self.assertEqual(self.pm.ftp, 180)
        self.assertEqual(self.pm.weight_kg, 60.0)


class TestSwitchNonexistentRaises(unittest.TestCase):
    """9. switch() raises for nonexistent profile."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_switch_nonexistent(self):
        with self.assertRaises(ValueError) as ctx:
            mock_db = MagicMock()
            with patch.dict("sys.modules", {"db": mock_db}):
                self.pm.switch("no-such-profile")
        self.assertIn("not found", str(ctx.exception).lower())


class TestSaveAthlete(unittest.TestCase):
    """10. save_athlete writes athlete.json and properties update."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_athlete(self):
        self.pm.save_athlete({"ftp": 280, "weight_kg": 75.0})
        self.assertEqual(self.pm.ftp, 280)
        self.assertEqual(self.pm.weight_kg, 75.0)

        # Verify file on disk
        path = self.pm.active_dir / "athlete.json"
        data = json.loads(path.read_text())
        self.assertEqual(data["ftp"], 280)
        self.assertEqual(data["weight_kg"], 75.0)

    def test_save_athlete_wprime_tags_manual_source(self):
        """v3.6.0-fix26 §4.1: a user-supplied wprime_j goes through
        `_set_wprime(..., source="manual")` so later ICU / Monod writes
        can't clobber it."""
        self.pm.save_athlete({"ftp": 280, "weight_kg": 75.0, "wprime_j": 22000})
        self.assertEqual(self.pm.wprime_j, 22000)
        self.assertEqual(self.pm.wprime_source, "manual")

        path = self.pm.active_dir / "athlete.json"
        data = json.loads(path.read_text())
        self.assertEqual(data["wprime_j"], 22000)
        self.assertEqual(data["wprime_source"], "manual")

class TestSetWprime(unittest.TestCase):
    """v3.6.0-fix26 §4.1 _set_wprime shared helper.

    Priority: manual > icu > monod > fallback. Higher-priority writes
    overwrite lower; equal-priority writes also overwrite (the freshest
    wins for same source). Lower-priority writes are ignored without
    raising.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_icu_sets_wprime(self):
        ok = self.pm._set_wprime(21500, "icu")
        self.assertTrue(ok)
        self.assertEqual(self.pm.wprime_j, 21500)
        self.assertEqual(self.pm.wprime_source, "icu")

    def test_manual_overrides_icu(self):
        self.pm._set_wprime(21500, "icu")
        self.pm._set_wprime(19800, "manual")
        self.assertEqual(self.pm.wprime_j, 19800)
        self.assertEqual(self.pm.wprime_source, "manual")

    def test_icu_does_not_override_manual(self):
        self.pm._set_wprime(19000, "manual")
        ok = self.pm._set_wprime(24000, "icu")
        self.assertFalse(ok)
        self.assertEqual(self.pm.wprime_j, 19000)
        self.assertEqual(self.pm.wprime_source, "manual")

    def test_monod_does_not_override_icu(self):
        self.pm._set_wprime(20500, "icu")
        ok = self.pm._set_wprime(19200, "monod")
        self.assertFalse(ok)
        self.assertEqual(self.pm.wprime_source, "icu")

    def test_rejects_out_of_range(self):
        ok = self.pm._set_wprime(1000, "icu")  # < 5000
        self.assertFalse(ok)
        ok = self.pm._set_wprime(999999, "icu")  # > 40000
        self.assertFalse(ok)

    def test_rejects_unknown_source(self):
        with self.assertRaises(ValueError):
            self.pm._set_wprime(20000, "strava")

    def test_write_persists_to_disk(self):
        """Atomic write through _write_json → athlete.json picks up both
        fields on re-read."""
        self.pm._set_wprime(21000, "monod")
        data = json.loads((self.pm.active_dir / "athlete.json").read_text())
        self.assertEqual(data["wprime_j"], 21000)
        self.assertEqual(data["wprime_source"], "monod")


class TestSaveEnv(unittest.TestCase):
    """11. save_env writes .env and updates os.environ."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch.dict(os.environ, {}, clear=False)
    def test_save_env(self):
        self.pm.save_env("NEW_ID", "NEW_KEY")
        self.assertEqual(self.pm.icu_athlete_id, "NEW_ID")
        self.assertEqual(self.pm.icu_api_key, "NEW_KEY")
        self.assertEqual(os.environ["ICU_ATHLETE_ID"], "NEW_ID")
        self.assertEqual(os.environ["ICU_API_KEY"], "NEW_KEY")

        # Verify file on disk
        env_text = (self.pm.active_dir / ".env").read_text()
        self.assertIn("ICU_ATHLETE_ID=NEW_ID", env_text)
        self.assertIn("ICU_API_KEY=NEW_KEY", env_text)


class TestSavePrefs(unittest.TestCase):
    """12. save_prefs writes user_prefs.json."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_prefs(self):
        self.pm.save_prefs({"hours_per_week": 12.0, "rest_days": [0, 6]})
        self.assertEqual(self.pm.prefs["hours_per_week"], 12.0)
        self.assertEqual(self.pm.prefs["rest_days"], [0, 6])

        path = self.pm.active_dir / "user_prefs.json"
        data = json.loads(path.read_text())
        self.assertEqual(data["hours_per_week"], 12.0)


class TestListProfiles(unittest.TestCase):
    """13. list_profiles returns all profiles from registry."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_list_profiles(self):
        profiles = self.pm.list_profiles()
        self.assertEqual(len(profiles), 1)
        self.assertEqual(profiles[0]["id"], "default")

        self.pm.create_profile("Anna")
        self.pm.create_profile("Bob")
        profiles = self.pm.list_profiles()
        self.assertEqual(len(profiles), 3)
        ids = {p["id"] for p in profiles}
        self.assertIn("default", ids)
        self.assertIn("anna", ids)
        self.assertIn("bob", ids)


class TestUpdateProfile(unittest.TestCase):
    """14. update_profile persists name/color changes."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_update_profile(self):
        self.pm.update_profile("default", name="Martijn H.", color="#ef4444")
        # Check in memory
        self.assertEqual(self.pm.profile_name, "Martijn H.")
        self.assertEqual(self.pm.profile_color, "#ef4444")
        # Check persisted on disk
        reg = json.loads(
            (Path(self.tmp) / ".domestique" / "profiles.json").read_text()
        )
        p = [x for x in reg["profiles"] if x["id"] == "default"][0]
        self.assertEqual(p["name"], "Martijn H.")
        self.assertEqual(p["color"], "#ef4444")


class TestProfileColorRoundRobin(unittest.TestCase):
    """15. Colors cycle through 8 presets."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_color_round_robin(self):
        from profile_manager import PROFILE_COLORS
        # Default profile already has color index 0 ("#3b82f6")
        # Next profiles should get colors at indices 1, 2, 3, ...
        created_colors = []
        for i in range(8):
            slug = self.pm.create_profile(f"Rider{i}")
            prof = [p for p in self.pm.list_profiles() if p["id"] == slug][0]
            created_colors.append(prof["color"])

        # Each new profile picks color at index (num_existing % 8)
        # At creation time the profile count was 1,2,3,...,8 so indices are 1,2,...,0
        for i, c in enumerate(created_colors):
            expected_idx = (i + 1) % len(PROFILE_COLORS)
            self.assertEqual(c, PROFILE_COLORS[expected_idx])


class TestRebuildRegistryFromDirs(unittest.TestCase):
    """18. Rebuilds registry when profiles.json is missing/corrupt."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        base = _make_base(self.tmp)
        _bootstrap_default_profile(base)
        # Also create a second profile directory manually
        partner_dir = base / "profiles" / "partner"
        partner_dir.mkdir(parents=True, exist_ok=True)
        (partner_dir / "athlete.json").write_text(json.dumps({
            "ftp": 180, "weight_kg": 60.0, "lbm_kg": 48.0,
            "lthr": 165, "max_hr": 185,
            "hrv_baseline_mean": None, "hrv_baseline_sd": None,
            "rhr_baseline": None, "targets_override": None,
        }), encoding="utf-8")

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_rebuild_from_dirs_corrupt(self):
        """Corrupt profiles.json triggers rebuild from directory scan."""
        reg_path = Path(self.tmp) / ".domestique" / "profiles.json"
        reg_path.write_text("{invalid json", encoding="utf-8")

        pm = _fresh_manager(self.tmp)
        profiles = pm.list_profiles()
        ids = {p["id"] for p in profiles}
        # Both directories should be discovered
        self.assertIn("default", ids)
        self.assertIn("partner", ids)

    def test_rebuild_from_dirs_missing(self):
        """Missing profiles.json triggers rebuild from directory scan."""
        reg_path = Path(self.tmp) / ".domestique" / "profiles.json"
        reg_path.unlink()

        pm = _fresh_manager(self.tmp)
        profiles = pm.list_profiles()
        ids = {p["id"] for p in profiles}
        self.assertIn("default", ids)
        self.assertIn("partner", ids)


class TestAtomicJsonWrite(unittest.TestCase):
    """19. _write_json uses .tmp then rename (atomic write)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_atomic_write(self):
        test_path = Path(self.tmp) / "test_atomic.json"
        data = {"key": "value", "number": 42}
        self.pm._write_json(test_path, data)

        # File should exist with correct contents
        self.assertTrue(test_path.exists())
        loaded = json.loads(test_path.read_text())
        self.assertEqual(loaded, data)

        # .tmp should NOT exist (it was renamed)
        self.assertFalse(test_path.with_suffix(".tmp").exists())


# ==============================================================================
# Integration Tests
# ==============================================================================

class TestMigrationFreshInstall(unittest.TestCase):
    """20. Fresh install with no existing data creates default profile."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        # Create base dir but NO profiles.json and NO profiles/ subdirectory
        _make_base(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_fresh_install(self):
        pm = _fresh_manager(self.tmp)
        # Per C7, _rebuild_registry no longer auto-creates "Rider". The app
        # must detect the empty state via has_any_profile() and redirect to
        # /setup. The empty registry is still persisted to disk.
        self.assertEqual(len(pm.list_profiles()), 0)
        self.assertFalse(pm.has_any_profile())
        # Registry file should still be persisted (empty profiles list)
        reg_path = Path(self.tmp) / ".domestique" / "profiles.json"
        self.assertTrue(reg_path.exists())

        # After explicitly creating a profile, has_any_profile() becomes True
        pm.create_profile("Rider")
        self.assertTrue(pm.has_any_profile())


class TestMigrationExistingData(unittest.TestCase):
    """21. Existing .env and user_prefs.json get copied to profiles/default/."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        base = _make_base(self.tmp)
        # Simulate pre-migration layout: files at base level, no profiles.json
        (base / ".env").write_text(
            "ICU_ATHLETE_ID=old_id\nICU_API_KEY=old_key\n", encoding="utf-8"
        )
        (base / "user_prefs.json").write_text(json.dumps({
            "hours_per_week": 6.0, "rest_days": [6]
        }), encoding="utf-8")

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migration_existing_data(self):
        """Simulate the migration function from the plan."""
        base = Path(self.tmp) / ".domestique"
        registry = base / "profiles.json"
        # Registry does not exist yet (pre-migration)
        self.assertFalse(registry.exists())

        default_dir = base / "profiles" / "default"
        default_dir.mkdir(parents=True, exist_ok=True)

        # Copy files as migrate_to_profiles() would
        for f in [".env", "user_prefs.json"]:
            src = base / f
            if src.exists():
                shutil.copy2(str(src), str(default_dir / f))

        # Create a minimal athlete.json
        (default_dir / "athlete.json").write_text(json.dumps({
            "ftp": 200, "weight_kg": 70.0, "lbm_kg": 56.0,
            "lthr": 170, "max_hr": 190,
            "hrv_baseline_mean": None, "hrv_baseline_sd": None,
            "rhr_baseline": None, "targets_override": None,
        }), encoding="utf-8")

        # Write registry (marks migration complete)
        reg_data = {
            "version": 1, "active_profile": "default", "skip_picker": True,
            "profiles": [{"id": "default", "name": "Rider", "color": "#3b82f6",
                          "created": "2026-04-12T00:00:00",
                          "last_used": "2026-04-12T00:00:00"}],
        }
        registry.write_text(json.dumps(reg_data), encoding="utf-8")

        # Now load the manager — should pick up migrated data
        pm = _fresh_manager(self.tmp)
        self.assertEqual(pm.active_id, "default")
        self.assertEqual(pm.icu_athlete_id, "old_id")
        self.assertEqual(pm.icu_api_key, "old_key")
        self.assertEqual(pm.prefs["hours_per_week"], 6.0)


class TestMigrationIdempotent(unittest.TestCase):
    """22. Running migration twice does not break anything."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        base = _make_base(self.tmp)
        _bootstrap_default_profile(base)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_migration_idempotent(self):
        """Loading the manager twice produces consistent state."""
        from profile_manager import ProfileManager
        pm1 = _fresh_manager(self.tmp)
        ftp_1 = pm1.ftp
        profiles_1 = len(pm1.list_profiles())

        # Reset singleton and reload
        ProfileManager._instance = None
        pm2 = _fresh_manager(self.tmp)
        ftp_2 = pm2.ftp
        profiles_2 = len(pm2.list_profiles())

        self.assertEqual(ftp_1, ftp_2)
        self.assertEqual(profiles_1, profiles_2)


class TestConfigGetattrProxy(unittest.TestCase):
    """24. config.ATHLETE_FTP_W resolves to active profile's value."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_config_getattr_proxy(self):
        """Simulate what config.__getattr__ does: resolve from ProfileManager."""
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        # The plan's config.__getattr__ maps ATHLETE_FTP_W -> pm.ftp
        attr_map = {
            "ATHLETE_FTP_W": pm.ftp,
            "ATHLETE_WEIGHT_KG": pm.weight_kg,
            "ATHLETE_LBM_KG": pm.lbm_kg,
            "ATHLETE_LTHR": pm.lthr,
            "ATHLETE_MAX_HR": pm.max_hr,
        }
        self.assertEqual(attr_map["ATHLETE_FTP_W"], 250)
        self.assertEqual(attr_map["ATHLETE_WEIGHT_KG"], 72.0)
        self.assertEqual(attr_map["ATHLETE_LTHR"], 175)


class TestConfigGetattrAfterSwitch(unittest.TestCase):
    """25. Config values change after profile switch."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)
        slug = self.pm.create_profile("Partner")
        partner_dir = Path(self.tmp) / ".domestique" / "profiles" / slug
        athlete = {
            "ftp": 180, "weight_kg": 60.0, "lbm_kg": 48.0,
            "lthr": 165, "max_hr": 185,
            "hrv_baseline_mean": None, "hrv_baseline_sd": None,
            "rhr_baseline": None, "targets_override": None,
        }
        (partner_dir / "athlete.json").write_text(
            json.dumps(athlete, indent=2), encoding="utf-8"
        )
        self._partner_slug = slug

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch.dict(os.environ, {}, clear=False)
    def test_config_values_change_after_switch(self):
        from profile_manager import ProfileManager
        pm = ProfileManager.get()

        # Before switch: default profile values
        self.assertEqual(pm.ftp, 250)
        self.assertEqual(pm.weight_kg, 72.0)

        # Switch to partner
        mock_db = MagicMock()
        with patch.dict("sys.modules", {"db": mock_db, "training_planner": MagicMock()}):
            pm.switch(self._partner_slug)

        # After switch: partner profile values
        self.assertEqual(pm.ftp, 180)
        self.assertEqual(pm.weight_kg, 60.0)


# ==============================================================================
# Edge Cases
# ==============================================================================

class TestEmptyIcuCredentials(unittest.TestCase):
    """26. New profile with empty ICU credentials."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch.dict(os.environ, {}, clear=False)
    def test_empty_icu_credentials(self):
        slug = self.pm.create_profile("NewRider")
        # Switch to the new profile to load its data
        mock_db = MagicMock()
        with patch.dict("sys.modules", {"db": mock_db, "training_planner": MagicMock()}):
            self.pm.switch(slug)

        # ICU credentials should be empty strings (not None, not error)
        self.assertEqual(self.pm.icu_athlete_id, "")
        self.assertEqual(self.pm.icu_api_key, "")


class TestCorruptAthleteJson(unittest.TestCase):
    """27. Malformed athlete.json returns default values."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_corrupt_athlete_json(self):
        # Corrupt the athlete.json
        athlete_path = (
            Path(self.tmp) / ".domestique" / "profiles" / "default" / "athlete.json"
        )
        athlete_path.write_text("{not valid json!!!", encoding="utf-8")

        pm = _fresh_manager(self.tmp)
        # Should fall back to defaults (from property .get() fallbacks)
        self.assertEqual(pm.ftp, 200)       # default
        self.assertEqual(pm.weight_kg, 70.0) # default


class TestMissingProfileDirectory(unittest.TestCase):
    """28. Phantom ID in registry is handled when directory is missing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        base = _make_base(self.tmp)
        _bootstrap_default_profile(base)
        # Add a phantom profile to registry (no directory on disk)
        reg_path = base / "profiles.json"
        reg = json.loads(reg_path.read_text())
        reg["profiles"].append({
            "id": "phantom", "name": "Ghost", "color": "#ef4444",
            "created": "2026-04-01T00:00:00", "last_used": "2026-04-01T00:00:00",
        })
        reg_path.write_text(json.dumps(reg), encoding="utf-8")

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_phantom_profile_in_registry(self):
        pm = _fresh_manager(self.tmp)
        # Phantom is listed in registry but has no directory
        ids = {p["id"] for p in pm.list_profiles()}
        self.assertIn("phantom", ids)

        # Switching to phantom should raise (directory not found)
        with self.assertRaises(ValueError):
            mock_db = MagicMock()
            with patch.dict("sys.modules", {"db": mock_db}):
                pm.switch("phantom")


class TestConcurrentSwitchBlocked(unittest.TestCase):
    """29. Second switch waits for the lock held by the first."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        _make_base(self.tmp)
        _bootstrap_default_profile(_make_base(self.tmp))
        self.pm = _fresh_manager(self.tmp)
        # Create two alternate profiles to switch between
        self.pm.create_profile("ProfileA")
        self.pm.create_profile("ProfileB")
        partner_dir_a = Path(self.tmp) / ".domestique" / "profiles" / "profilea"
        partner_dir_b = Path(self.tmp) / ".domestique" / "profiles" / "profileb"
        for d in [partner_dir_a, partner_dir_b]:
            (d / "athlete.json").write_text(json.dumps({
                "ftp": 200, "weight_kg": 70.0, "lbm_kg": 56.0,
                "lthr": 170, "max_hr": 190,
                "hrv_baseline_mean": None, "hrv_baseline_sd": None,
                "rhr_baseline": None, "targets_override": None,
            }), encoding="utf-8")
            (d / ".env").write_text("ICU_ATHLETE_ID=\nICU_API_KEY=\n", encoding="utf-8")
            (d / "user_prefs.json").write_text("{}", encoding="utf-8")
            (d / "device_prefs.json").write_text("{}", encoding="utf-8")

    def tearDown(self):
        from profile_manager import ProfileManager
        ProfileManager._instance = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    @patch.dict(os.environ, {}, clear=False)
    def test_concurrent_switch_blocked(self):
        """Verify that the switch lock serialises concurrent switches."""
        pm = self.pm
        results = {"thread_blocked": False, "thread_completed": False}
        barrier = threading.Event()

        original_load = pm._load_active_profile

        def slow_load():
            """Simulate a slow profile load to hold the lock longer."""
            barrier.set()  # signal that we hold the lock
            time.sleep(0.3)
            original_load()

        def thread_switch():
            """Try to switch in a second thread (should block on lock)."""
            barrier.wait(timeout=2)  # wait until lock is held
            time.sleep(0.05)  # give a moment to ensure lock contention
            # This switch should block until the first one releases the lock
            acquired = pm._switch_lock.acquire(timeout=0)
            if not acquired:
                results["thread_blocked"] = True
                # Wait for lock to be released, then acquire
                pm._switch_lock.acquire()
            pm._switch_lock.release()
            results["thread_completed"] = True

        mock_db = MagicMock()
        with patch.dict("sys.modules", {"db": mock_db, "training_planner": MagicMock()}):
            with patch.object(pm, "_load_active_profile", side_effect=slow_load):
                t = threading.Thread(target=thread_switch)
                t.start()
                pm.switch("profilea")
                t.join(timeout=5)

        # The second thread should have been blocked at least momentarily
        self.assertTrue(results["thread_blocked"])
        self.assertTrue(results["thread_completed"])


# ==============================================================================
# Entry point
# ==============================================================================

if __name__ == "__main__":
    unittest.main()
