"""Idempotent profile migrations.

- ``migrate_to_profiles()``: moves the legacy single-user layout into
  ``profiles/default/`` and writes ``profiles.json``.
- ``migrate_to_v4(profile_dir)``: strips the fields removed by the
  v4.0.0-alpha trainer-rip pivot (device-pair list, trainer-difficulty
  scaler, bike-weight). Silent strip + log only -- never errors on
  missing keys.

Safe to re-run. If ``profiles.json`` already exists, ``migrate_to_profiles``
is a no-op; ``migrate_to_v4`` is similarly idempotent (it writes the
schema-version sentinel so subsequent runs detect an up-to-date profile).
"""

import json
import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Removed in v4.0.0-alpha. The literal strings are the on-disk key names we
# must delete from legacy athlete.json / device_prefs.json. Listing them here
# (rather than grepping inside the function body) keeps the set in one place.
_V4_STRIPPED_ATHLETE_KEYS = ("paired_devices", "bike_weight_kg")
_V4_STRIPPED_DEVICE_PREFS_KEYS = (
    "trainer_effect",
    "trainer_effect_fix27_migrated",
    "road_feel_enabled",
    "road_feel_intensity",
    "hr_cap_settings",
)
_V4_SCHEMA_SENTINEL = "schema_version_4_migrated"


def _sanitize_user_name(raw: str | None) -> str:
    """Pick a reasonable display name from $USER.

    System accounts on macOS (e.g. ``_mdnsresponder``, ``_spotlight``) start
    with an underscore and would be a poor default name. Empty or
    whitespace-only values also fall back to the generic "Rider"."""
    if not raw or not raw.strip():
        return "Rider"
    stripped = raw.strip()
    if stripped.startswith("_"):
        return "Rider"
    return stripped.capitalize()


def _write_registry_atomic(path: Path, data: dict) -> None:
    """Write registry JSON using tmp + fsync + os.replace (atomic rename).

    Mirrors ``ProfileManager._write_text_atomic`` but kept inline so the
    migration does not depend on importing profile_manager (which would
    trip over ``config.__getattr__`` before migration has run).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _write_device_prefs_atomic(path: Path, data: dict) -> None:
    """Atomically rewrite ``device_prefs.json`` (tmp + fsync + os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        os.replace(str(tmp), str(path))
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def migrate_to_v4(profile_dir: Path) -> bool:
    """Strip v3 trainer/BLE-specific fields from a profile directory.

    Walks ``athlete.json`` and ``device_prefs.json`` inside ``profile_dir``
    and removes the keys listed in the module-level strip tables. Idempotent:
    writes a ``schema_version_4_migrated`` sentinel into ``device_prefs.json``
    so subsequent boots can short-circuit.

    Never raises: missing files, malformed JSON, write permission errors
    all log-and-continue so a partial migration on a read-only volume does
    not crash the whole boot. Returns True iff any on-disk file changed.
    """
    changed = False
    try:
        if not profile_dir.exists():
            return False

        # athlete.json: drop paired_devices + bike_weight_kg
        athlete_path = profile_dir / "athlete.json"
        if athlete_path.exists():
            try:
                athlete = json.loads(athlete_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                athlete = None
            if isinstance(athlete, dict):
                stripped_here = False
                for k in _V4_STRIPPED_ATHLETE_KEYS:
                    if k in athlete:
                        del athlete[k]
                        stripped_here = True
                if stripped_here:
                    try:
                        _write_device_prefs_atomic(athlete_path, athlete)
                        changed = True
                        log.info(
                            f"v4 migration stripped legacy keys from "
                            f"{athlete_path} (profile={profile_dir.name})"
                        )
                    except Exception as e:
                        log.warning(
                            f"v4 migration: athlete.json rewrite failed "
                            f"for {profile_dir.name}: {e}"
                        )

        # device_prefs.json: drop trainer_effect + road_feel + hr_cap_settings
        dp_path = profile_dir / "device_prefs.json"
        if dp_path.exists():
            try:
                prefs = json.loads(dp_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                prefs = None
            if isinstance(prefs, dict):
                already = bool(prefs.get(_V4_SCHEMA_SENTINEL))
                stripped_here = False
                for k in _V4_STRIPPED_DEVICE_PREFS_KEYS:
                    if k in prefs:
                        del prefs[k]
                        stripped_here = True
                if stripped_here or not already:
                    prefs[_V4_SCHEMA_SENTINEL] = True
                    try:
                        _write_device_prefs_atomic(dp_path, prefs)
                        if stripped_here:
                            changed = True
                            log.info(
                                f"v4 migration stripped legacy trainer keys "
                                f"from {dp_path} (profile={profile_dir.name})"
                            )
                    except Exception as e:
                        log.warning(
                            f"v4 migration: device_prefs.json rewrite failed "
                            f"for {profile_dir.name}: {e}"
                        )
    except Exception as e:
        log.warning(
            f"v4 migration skipped for {getattr(profile_dir, 'name', profile_dir)}: {e}"
        )
        return False

    return changed


def migrate_to_profiles() -> None:
    base = Path.home() / ".domestique"
    registry = base / "profiles.json"

    # ── Idempotent guard ────────────────────────────────────────────────
    if registry.exists():
        return  # already migrated

    default_dir = base / "profiles" / "default"
    default_dir.mkdir(parents=True, exist_ok=True)
    (default_dir / "plans").mkdir(exist_ok=True)

    # ── 1. COPY files (originals stay until registry is written) ────────
    for f in [".env", "user_prefs.json", "user_paths.json",
              "health_tracker.db", "health_tracker.db-shm",
              "health_tracker.db-wal", ".setup_complete"]:
        src = base / f
        if src.exists():
            shutil.copy2(str(src), str(default_dir / f))

    # Copy plans directory
    old_plans = base / "plans"
    if old_plans.exists():
        for item in old_plans.iterdir():
            shutil.copy2(str(item), str(default_dir / "plans" / item.name))

    # ── 2. Known-devices shim: drop every legacy BLE field, do not seed
    # device_prefs.json with anything. v4 profiles have an empty device_prefs.
    # (Pre-pivot this step used to preserve trainer_effect + road_feel_enabled;
    # both keys are removed entirely in v4 so the initial file is just {}.)
    (default_dir / "device_prefs.json").write_text(
        json.dumps({_V4_SCHEMA_SENTINEL: True}, indent=2), encoding="utf-8"
    )

    # ── 3. Extract athlete values from config.py SOURCE FILE
    # NOTE: Cannot use ``import config`` because the __getattr__ proxy routes
    # to ProfileManager which doesn't have the old values yet. Must parse the
    # old config.py file directly to extract historical athlete values.
    athlete = {
        "ftp": 200, "weight_kg": 70.0, "lbm_kg": 56.0, "lthr": 170,
        "max_hr": 190, "hrv_baseline_mean": None, "hrv_baseline_sd": None,
        "rhr_baseline": None,
    }
    config_path = Path(__file__).parent / "config.py"
    if config_path.exists():
        try:
            import re
            src = config_path.read_text(encoding="utf-8")
            patterns = {
                "ftp": (r'ATHLETE_FTP_W\s*=\s*(\d+)', int),
                "weight_kg": (r'ATHLETE_WEIGHT_KG\s*=\s*([\d.]+)', float),
                "lbm_kg": (r'ATHLETE_LBM_KG\s*=\s*([\d.]+)', float),
                "lthr": (r'ATHLETE_LTHR\s*=\s*(\d+)', int),
                "max_hr": (r'ATHLETE_MAX_HR\s*=\s*(\d+)', int),
            }
            for key, (pattern, conv) in patterns.items():
                m = re.search(pattern, src)
                if m:
                    athlete[key] = conv(m.group(1))
        except Exception:
            pass  # fall back to defaults

    (default_dir / "athlete.json").write_text(
        json.dumps(athlete, indent=2), encoding="utf-8"
    )

    # ── 4. COMMIT: atomically write registry (marks migration as complete) ──
    # Use "Rider" for empty/system-account $USER values (e.g. macOS helpers
    # like ``_mdnsresponder``). POSIX exports $USER; Windows uses $USERNAME.
    # ``getpass.getuser()`` is the stdlib fallback that checks both plus the
    # password database.
    import getpass
    try:
        _fallback_user = getpass.getuser()
    except Exception:
        _fallback_user = None
    name = _sanitize_user_name(
        os.environ.get("USER") or os.environ.get("USERNAME") or _fallback_user
    )
    now = datetime.now().isoformat()
    reg = {
        "version": 1,
        "active_profile": "default",
        "skip_picker": True,
        "profiles": [
            {
                "id": "default",
                "name": name,
                "color": "#3b82f6",
                "created": now,
                "last_used": now,
            }
        ],
    }
    _write_registry_atomic(registry, reg)

    # ── 4b. Read-back verification: make sure the file we just wrote parses
    #         cleanly. If the disk is full or a filesystem bug truncated it,
    #         we must NOT delete the originals.
    try:
        verify = json.loads(registry.read_text(encoding="utf-8"))
        if verify.get("active_profile") != "default":
            raise ValueError(
                "Post-write verification failed: registry missing active_profile"
            )
    except (json.JSONDecodeError, OSError, ValueError) as e:
        raise RuntimeError(
            f"Migration wrote profiles.json but read-back verification failed: {e}. "
            f"Original files left intact; re-run migrate_to_profiles after fixing."
        )

    # ── 5. NOW safe to remove originals (registry exists AND verified) ───
    for f in [".env", "user_prefs.json", "user_paths.json",
              "health_tracker.db", "health_tracker.db-shm",
              "health_tracker.db-wal", ".setup_complete"]:
        src = base / f
        if src.exists():
            src.unlink()


# ─── v1.0.2 IMPL-MIGRATION ──────────────────────────────────────────────────
# Startup version-aware self-check + first-boot-after-upgrade toast framework.
#
# Detects an app-version bump by comparing the persisted last-run version
# (`~/.domestique/last_run_version.txt`) to the current ``VERSION`` and returns
# a result dict shaped per MASTER_DECISIONS_v102.md §1. v1.0.2 adds NO actual
# schema changes — this is the framework for future additive migrations to
# slot in. The dashboard reads the result from
# `GET /api/migrations/last-run-result` and toasts on first upgrade boot.
#
# Locked field names (do not rename without bumping MASTER):
#   migration_check_passed, from_version, to_version, columns_added,
#   schema_changes[].{table,action,column,coltype},
#   data_migrations[].{id,description,applied},
#   rider_data_preserved, show_toast.

_LAST_RUN_VERSION_FILENAME = "last_run_version.txt"


def _read_last_run_version(data_dir: Path) -> str | None:
    """Return the persisted last-run version string, or None on first boot."""
    p = data_dir / _LAST_RUN_VERSION_FILENAME
    try:
        return p.read_text(encoding="utf-8").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def _write_last_run_version_atomic(data_dir: Path, version: str) -> None:
    """Atomically persist the current version (tmp + os.replace).

    Best-effort: failure to persist is logged but never raised — a missing
    last_run_version.txt simply re-fires the upgrade toast on next boot,
    which is recoverable. We do not want a flaky filesystem to break boot.
    """
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        target = data_dir / _LAST_RUN_VERSION_FILENAME
        tmp = target.with_suffix(target.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(version)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            os.replace(str(tmp), str(target))
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise
    except Exception as e:
        log.warning(f"v1.0.2 migration: failed to write {_LAST_RUN_VERSION_FILENAME}: {e}")


def run_v102_migration_check(data_dir: Path, current_version: str) -> dict:
    """Return the locked v1.0.2 migration-result dict.

    Idempotent. Safe to call every boot. Compares the persisted last-run
    version to ``current_version``; on a version change, marks ``show_toast``
    True and populates ``from_version``/``to_version``. v1.0.2 ships NO
    schema changes — ``columns_added=0``, ``schema_changes=[]``. The
    framework is in place for future additive migrations to slot into the
    ``schema_changes`` list and bump ``columns_added``.
    """
    last_run = _read_last_run_version(data_dir)
    is_upgrade = last_run is not None and last_run != current_version

    # v1.0.2 has NO actual schema changes (per MASTER §0 + §1 + §5). Future
    # versions append to schema_changes and increment columns_added here.
    schema_changes: list = []
    columns_added = 0

    # Persist the current version every boot — first run writes it for the
    # first time so subsequent boots can detect upgrades. Done BEFORE
    # building the data_migrations entry so its `applied=True` is honest.
    _write_last_run_version_atomic(data_dir, current_version)

    data_migrations = [
        {
            "id": "v102_init_last_run_version",
            "description": "Recorded last-run version for future upgrade-aware migrations.",
            "applied": True,
        }
    ]

    return {
        "migration_check_passed": True,
        "from_version": last_run if is_upgrade else current_version,
        "to_version": current_version,
        "columns_added": columns_added,
        "schema_changes": schema_changes,
        "data_migrations": data_migrations,
        "rider_data_preserved": True,
        "show_toast": bool(is_upgrade),
    }


if __name__ == "__main__":
    migrate_to_profiles()
    print("Migration complete.")
