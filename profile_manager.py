"""Multi-user profile manager for Domestique.

Manages athlete profiles stored in ~/.domestique/profiles/<id>/.
(v3.0.0: data dir migrated from ~/.chickencycling/ — see
`_maybe_migrate_data_dir()` for the one-time move logic that runs on
first boot after the upgrade.)
Each profile has its own athlete.json, .env, training plan, and SQLite DB.
Shared resources (workouts, routes, device registry) stay at the app level.

Usage:
    pm = ProfileManager.get()
    print(pm.ftp)              # reads from active profile's athlete.json
    pm.switch("partner")       # hot-swap to another profile
    pm.create_profile("Anna")  # create new profile directory

Per-profile WORKOUT_DIR resolution order (used on switch):
    1. `<active_dir>/user_paths.json` key "workout_dir" if it exists and the
       directory exists.
    2. `<active_dir>/workouts` if that directory exists.
    3. Otherwise the module-default in training_planner is kept (the
       bundled `workouts/` shipped with the app).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

log = logging.getLogger("domestique.profiles")

# profile_id validator — lowercase alnum / underscore / dash, 1-32 chars, must
# start with alnum to avoid ".env"-style dot-files or hidden names.
_PROFILE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')

# Required athlete keys for a profile to be considered fully configured.
_REQUIRED_ATHLETE_KEYS = ("ftp", "weight_kg")

# v4.0.0-alpha trainer-rip pivot: the profile schema now contains only
# planner/wellness/library fields. The v3 schema had device-pair list,
# trainer-difficulty scaler, and bike-weight; those keys are stripped by
# migrate_profiles.migrate_to_v4() on first boot after the upgrade.
PROFILE_SCHEMA_VERSION = 4

# 8 preset profile colors (assigned round-robin)
PROFILE_COLORS = [
    "#3b82f6",  # blue
    "#22c55e",  # green
    "#f97316",  # orange
    "#a855f7",  # purple
    "#ef4444",  # red
    "#eab308",  # yellow
    "#06b6d4",  # cyan
    "#ec4899",  # pink
]


class ProfileManager:
    """Singleton managing the active profile and its config values."""

    _instance: Optional["ProfileManager"] = None

    def __init__(self):
        self._base = Path.home() / ".domestique"
        self._profiles_dir = self._base / "profiles"
        self._registry_path = self._base / "profiles.json"
        self._active_id: Optional[str] = None
        self._athlete: dict = {}
        self._env: dict = {}
        self._prefs: dict = {}
        self._device_prefs: dict = {}
        self._registry: dict = {}
        self._switch_lock = threading.RLock()
        self._on_switch_callbacks: list[Callable] = []

    @classmethod
    def get(cls) -> "ProfileManager":
        if cls._instance is None:
            cls._instance = cls()
            cls._instance._maybe_migrate_data_dir()
            cls._instance._load_registry()
            cls._instance._load_active_profile()
        return cls._instance

    def _maybe_migrate_data_dir(self) -> None:
        """One-time migration of ~/.chickencycling/ → ~/.domestique/ (v3.0.0).

        Behaviour:
          * If ~/.domestique already exists, do nothing (already migrated, or
            this is a fresh v3 install).
          * Else if ~/.chickencycling does not exist either, create the new
            dir and return (truly fresh install).
          * Else `shutil.move` the legacy dir to the new name. The move is
            atomic on the same filesystem (typical $HOME case) — either the
            old name or the new name exists, never both.
        """
        home = Path.home()
        new = home / ".domestique"
        old = home / ".chickencycling"
        if new.exists():
            return  # Already migrated (or fresh v3 install)
        if not old.exists():
            new.mkdir(parents=True, exist_ok=True)
            return  # Fresh install — create new dir
        log.info("Migrating data dir: ~/.chickencycling → ~/.domestique")
        shutil.move(str(old), str(new))
        log.info("Migration done. Old path no longer exists.")

    # ── Properties: active profile data ──────────────────────────────────

    @property
    def active_id(self) -> Optional[str]:
        return self._active_id

    @property
    def active_dir(self) -> Path:
        # When no profile is active (empty-state), return the profiles root so
        # existing property code paths don't NPE. Callers who care should check
        # `has_any_profile()` / `active_id is None` first.
        if self._active_id is None:
            return self._profiles_dir
        return self._profiles_dir / self._active_id

    @property
    def ftp(self) -> int:
        return self._athlete.get("ftp", 200)

    @property
    def weight_kg(self) -> float:
        return self._athlete.get("weight_kg", 70.0)

    @property
    def lbm_kg(self) -> float:
        return self._athlete.get("lbm_kg", 56.0)

    @property
    def lthr(self) -> int:
        return self._athlete.get("lthr", 170)

    @property
    def max_hr(self) -> int:
        """Max HR in bpm. Falls back to Tanaka 208-0.7*age if age is known,
        else to the legacy 190 default. Without this, the UI's setup hint
        about 220-age had no corresponding server-side fallback (rescan PR3).
        """
        v = self._athlete.get("max_hr")
        if v:
            return int(v)
        age = self._athlete.get("age")
        if age:
            try:
                import zones
                return zones.estimated_hr_max(int(age))
            except Exception:
                pass
        return 190

    @property
    def hrv_baseline_mean(self) -> Optional[float]:
        return self._athlete.get("hrv_baseline_mean")

    @property
    def hrv_baseline_sd(self) -> Optional[float]:
        return self._athlete.get("hrv_baseline_sd")

    @property
    def rhr_baseline(self) -> Optional[int]:
        return self._athlete.get("rhr_baseline")

    @property
    def cp(self) -> int:
        """Critical Power. Falls back to int(ftp * 1.03) per McGrath 2021."""
        v = self._athlete.get("cp")
        return int(v) if v else int(self.ftp * 1.03)

    @property
    def wprime_j(self) -> int:
        """W' in joules. Falls back to int(ftp * 80)."""
        v = self._athlete.get("wprime_j")
        return int(v) if v else int(self.ftp * 80)

    @property
    def age(self) -> int | None:
        v = self._athlete.get("age")
        return int(v) if v else None

    @property
    def sex(self) -> str | None:
        """Returns 'M', 'F', 'O', or None."""
        v = self._athlete.get("sex")
        return v.upper() if isinstance(v, str) and v else None

    @property
    def icu_athlete_id(self) -> str:
        return self._env.get("ICU_ATHLETE_ID", "")

    @property
    def icu_api_key(self) -> str:
        return self._env.get("ICU_API_KEY", "")

    @property
    def db_path(self) -> Path:
        return self.active_dir / "health_tracker.db"

    @property
    def plan_dir(self) -> Path:
        d = self.active_dir / "plans"
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def prefs(self) -> dict:
        return self._prefs

    @property
    def device_prefs(self) -> dict:
        return self._device_prefs

    # v4.0.0-alpha: the device-pair list was removed in the trainer rip.
    # Rides are now imported as FITs from the user's chosen recording app,
    # so Domestique no longer tracks hardware addresses.

    @property
    def profile_name(self) -> str:
        for p in self._registry.get("profiles", []):
            if p["id"] == self._active_id:
                return p.get("name", self._active_id or "")
        return self._active_id or ""

    @property
    def profile_color(self) -> str:
        for p in self._registry.get("profiles", []):
            if p["id"] == self._active_id:
                return p.get("color", PROFILE_COLORS[0])
        return PROFILE_COLORS[0]

    # ── Public helpers for callers to detect empty / partial state ───────

    def has_any_profile(self) -> bool:
        """True if at least one profile is registered. Used by app.py to
        decide whether to redirect to /setup on first run."""
        return bool(self._registry.get("profiles"))

    def is_fully_configured(self) -> bool:
        """True if the active profile has all required athlete keys set.
        Callers can use this to force users back into /setup mid-configuration."""
        if self._active_id is None:
            return False
        for k in _REQUIRED_ATHLETE_KEYS:
            if k not in self._athlete or self._athlete.get(k) in (None, ""):
                return False
        return True

    # ── Profile management ───────────────────────────────────────────────

    def list_profiles(self) -> list[dict]:
        return self._registry.get("profiles", [])

    def create_profile(self, name: str, color: str = "") -> str:
        """Create a new profile directory with default athlete.json.

        Wrapped in `_switch_lock` to prevent two concurrent POSTs racing to
        pick the same slug. On any failure after mkdir the half-created
        directory is removed (rollback) so `_rebuild_registry` doesn't later
        pick it up as a phantom profile.
        """
        with self._switch_lock:
            slug = name.lower().replace(" ", "-")
            slug = "".join(c for c in slug if c.isalnum() or c == "-")[:32]
            if not slug:
                slug = f"profile-{len(self.list_profiles()) + 1}"

            # Avoid duplicate IDs
            existing_ids = {p["id"] for p in self.list_profiles()}
            base_slug = slug
            counter = 1
            while slug in existing_ids:
                slug = f"{base_slug}-{counter}"
                counter += 1

            # Pick color (round-robin from presets)
            if not color:
                idx = len(self.list_profiles()) % len(PROFILE_COLORS)
                color = PROFILE_COLORS[idx]

            # Create directory (rollback on any subsequent failure)
            profile_dir = self._profiles_dir / slug
            profile_dir.mkdir(parents=True, exist_ok=True)

            try:
                (profile_dir / "plans").mkdir(exist_ok=True)

                # Default athlete.json
                athlete = {
                    "ftp": 200, "weight_kg": 70.0, "lbm_kg": 56.0,
                    "lthr": 170, "max_hr": 190,
                    "hrv_baseline_mean": None, "hrv_baseline_sd": None,
                    "rhr_baseline": None,
                }
                self._write_json(profile_dir / "athlete.json", athlete)

                # Empty .env — atomic + chmod 0600 to protect API keys.
                self._write_env_atomic(
                    profile_dir / ".env",
                    "ICU_ATHLETE_ID=\nICU_API_KEY=\n",
                )

                # Default prefs
                self._write_json(profile_dir / "user_prefs.json", {
                    "hours_per_week": 8.0,
                    "available_days": [0, 1, 2, 3, 4, 5, 6],
                    "rest_days": [0],
                })

                # v4.0.0-alpha: device_prefs seeded empty -- no trainer
                # coupling in the post-pivot app, so there's nothing to
                # initialize here. Left in place so legacy code paths that
                # read an empty dict do not trip.
                self._write_json(profile_dir / "device_prefs.json", {})

                # Add to registry
                profiles = self._registry.get("profiles", [])
                profiles.append({
                    "id": slug, "name": name, "color": color,
                    "created": datetime.now().isoformat(),
                    "last_used": datetime.now().isoformat(),
                })
                self._registry["profiles"] = profiles
                self._save_registry()
            except Exception:
                # Rollback: remove the half-created directory so _rebuild_registry
                # doesn't later resurrect it as a phantom profile.
                shutil.rmtree(str(profile_dir), ignore_errors=True)
                raise

            log.info(f"Created profile '{name}' (id={slug})")
            return slug

    def delete_profile(self, profile_id: str) -> bool:
        """Delete a profile. Cannot delete active or last profile.

        Order: validate path, rmtree first, update registry only if rmtree
        succeeded. This avoids orphaning the directory when rmtree fails.
        All registry mutations happen under `_switch_lock`.
        """
        safe_dir = _safe_profile_dir(self, profile_id)

        with self._switch_lock:
            # Active-profile check MUST be inside the lock to avoid TOCTOU
            # against a concurrent switch(). Exception: if this is ALSO the
            # last profile, we allow the delete and clear the active pointer —
            # the UI then shows a "create your first profile" empty state +
            # wizard. Otherwise blocking here traps the user when they've
            # decided to purge everything.
            profiles = self._registry.get("profiles", [])
            is_last = len(profiles) <= 1
            if profile_id == self._active_id and not is_last:
                raise ValueError("Cannot delete active profile")

            # rmtree FIRST. If it fails the registry entry is preserved so the
            # user can retry later; otherwise we'd leak the on-disk data and
            # forget about it in the registry.
            if safe_dir.exists():
                try:
                    shutil.rmtree(str(safe_dir))
                except OSError as e:
                    log.warning(
                        f"delete_profile: rmtree failed for {safe_dir} ({e}); "
                        f"keeping registry entry intact for retry"
                    )
                    return False

            # Only now remove from registry.
            self._registry["profiles"] = [
                p for p in profiles if p["id"] != profile_id
            ]
            # If that was the last profile, clear the active pointer so the
            # picker page knows to render the "create account" wizard.
            if is_last:
                self._active_id = None
                self._registry["active"] = None
            self._save_registry()

        log.info(f"Deleted profile '{profile_id}'")
        return True

    def update_profile(self, profile_id: str, name: str = None, color: str = None) -> None:
        with self._switch_lock:
            for p in self._registry.get("profiles", []):
                if p["id"] == profile_id:
                    if name is not None:
                        p["name"] = name
                    if color is not None:
                        p["color"] = color
                    break
            self._save_registry()

    def switch(self, profile_id: str) -> None:
        """Hot-swap to a different profile."""
        # Validate + resolve path OUTSIDE the lock so we fail fast without
        # holding the switch lock for path-traversal attempts.
        safe_dir = _safe_profile_dir(self, profile_id)
        if not safe_dir.exists():
            raise ValueError(f"Profile '{profile_id}' not found")

        # v4.0.0-alpha (FIX-SERVER): the former "cannot switch mid-ride"
        # guard checked ``app._training_session`` which no longer exists.
        # Live-ride runtime was removed so there is no mid-ride state to
        # protect against. Guard deleted.

        with self._switch_lock:
            if profile_id == self._active_id:
                return

            log.info(f"Switching profile: {self._active_id} → {profile_id}")

            # Save current last_used
            self._update_last_used()

            # Stop background sync (interruptible)
            try:
                import db
                db.stop_sync()
            except Exception as e:
                log.warning(f"Error stopping sync: {e}")

            # Load new profile in-memory
            self._active_id = profile_id
            self._registry["active_profile"] = profile_id
            self._load_active_profile()

            # Update os.environ (NOT setdefault — must overwrite)
            os.environ["ICU_ATHLETE_ID"] = self.icu_athlete_id
            os.environ["ICU_API_KEY"] = self.icu_api_key

            # Repoint DB. Order matters: set_db_path BEFORE close_all_connections
            # so that when worker threads re-open their connections (triggered
            # by the _db_version bump inside close_all_connections) they see
            # the new DB_PATH. See db.py: set_db_path only rebinds DB_PATH, it
            # does NOT bump the version — close_all_connections does.
            try:
                import db
                db.set_db_path(self.db_path)
                db.close_all_connections()  # bumps _db_version → threads reopen on new path
                db.init_db()
                db.restart_sync()
            except Exception as e:
                log.warning(f"Error reinitializing DB: {e}")

            # Update PLAN_DIR + WORKOUT_DIR in training_planner so freshly
            # loaded plans/workouts come from the new profile.
            # Resolution order for WORKOUT_DIR:
            #   1. user_paths.json["workout_dir"] if dir exists
            #   2. <active_dir>/workouts if dir exists
            #   3. leave the module default (bundled workouts) alone
            try:
                import training_planner
                training_planner.PLAN_DIR = self.plan_dir

                wp_dir: Optional[Path] = None
                paths = self._load_json(self.active_dir / "user_paths.json")
                custom = paths.get("workout_dir")
                if custom:
                    candidate = Path(custom)
                    if candidate.exists():
                        wp_dir = candidate
                if wp_dir is None:
                    default_per_profile = self.active_dir / "workouts"
                    if default_per_profile.exists():
                        wp_dir = default_per_profile

                if wp_dir is not None:
                    training_planner.WORKOUT_DIR = wp_dir
            except Exception as e:
                log.warning(f"Error updating training_planner on switch: {e}")

            # Save registry
            self._save_registry()

            # NOTE: trainer_connection.reload_from_profile is registered via
            # pm.on_switch in app.py lifespan and runs through the callback chain
            # below — picking up the new profile's device_prefs (H17 fix wired).
            for cb in self._on_switch_callbacks:
                try:
                    cb()
                except Exception as e:
                    log.warning(f"Switch callback error: {e}")

            log.info(f"Profile switched to '{profile_id}'")

    def on_switch(self, callback: Callable) -> None:
        """Register a callback for profile switches."""
        self._on_switch_callbacks.append(callback)

    def save_athlete(self, data: dict) -> None:
        """Save athlete settings to active profile's athlete.json.

        Clamps numeric inputs to physiological ranges; raises ValueError on
        out-of-range values so API callers can surface a 400 rather than
        silently writing garbage to disk.

        v3.6.0-fix26 §4.1: when the caller supplies an explicit `wprime_j`
        (e.g. from the settings form), route it through
        `_set_wprime(..., source="manual")` so the `wprime_source` tag is
        updated atomically and later ICU / Monod writes can't clobber it.
        """
        validators = {
            "ftp":       (50, 600),
            "weight_kg": (30, 200),
            "lbm_kg":    (20, 150),
            "lthr":      (100, 220),
            "max_hr":    (120, 240),
            "cp":        (100, 500),     # Critical Power in watts
            "wprime_j":  (5000, 40000),  # W' in joules
            "age":       (10, 100),      # years
            # sex handled separately -- string not numeric
        }
        # Work on a copy so we don't corrupt the caller's dict on failure.
        data = dict(data)
        for key, (lo, hi) in validators.items():
            if key in data and data[key] is not None:
                try:
                    v = float(data[key])
                except (TypeError, ValueError) as e:
                    raise ValueError(f"Invalid {key}: {e}")
                if not (lo <= v <= hi):
                    raise ValueError(f"{key}={v} out of range [{lo},{hi}]")
                data[key] = v

        if "sex" in data:
            v = str(data["sex"]).upper().strip()
            if v not in ("M", "F", "O", ""):
                raise ValueError(f"sex must be M, F, or O; got {data['sex']!r}")
            data["sex"] = v

        # v3.6.0-fix26 §4.1: if the caller passed wprime_j here tag it as
        # "manual" so ICU / Monod writes can't silently overwrite. Pop
        # before the generic update() + disk write so `_set_wprime`'s
        # atomic _write_json is the only path that touches the key.
        wprime_manual = data.pop("wprime_j", None)

        self._athlete.update(data)
        self._write_json(self.active_dir / "athlete.json", self._athlete)

        if wprime_manual is not None:
            self._set_wprime(int(wprime_manual), "manual")

    def _set_wprime(self, value: int | float, source: str) -> bool:
        """Shared write-path for `wprime_j` with source tracking (v3.6.0-fix26
        §4.1). Shared with IMPL-ICU §5.4.

        Source priority (higher wins):
            manual > icu > monod > fallback

        Args:
            value: W' in joules. Clamped to [5000, 40000]; anything outside
                   returns False and does NOT touch disk.
            source: One of "manual", "icu", "monod", "fallback". Anything
                    else raises ValueError.

        Returns:
            True if the value was written; False if it was rejected (out of
            range, or a higher-priority source is already set).

        Used by:
          * `save_athlete` when the user types a wprime_j in settings
            (source="manual").
          * `db._refresh_wprime_from_metrics` after ICU sync_wellness
            batches (source="icu").
          * `fitness_estimation.compute_cp_wprime` after a Monod-Scherrer
            regression fit on best-efforts (source="monod").

        Atomic write via the existing `_write_json` path (tmp+fsync+rename).
        """
        _PRIO = {"manual": 3, "icu": 2, "monod": 1, "fallback": 0}
        if source not in _PRIO:
            raise ValueError(f"unknown wprime source: {source!r}")

        try:
            v = int(float(value))
        except (TypeError, ValueError):
            log.warning("_set_wprime: invalid value %r; ignored", value)
            return False
        if not (5000 <= v <= 40000):
            log.warning(
                "_set_wprime: %d out of range [5000, 40000] "
                "(source=%s); ignored", v, source,
            )
            return False

        current_source = self._athlete.get("wprime_source")
        if current_source in _PRIO:
            if _PRIO[source] < _PRIO[current_source]:
                log.debug(
                    "_set_wprime: skipping %s write (%d J); current source "
                    "%s has higher priority", source, v, current_source,
                )
                return False

        self._athlete["wprime_j"] = v
        self._athlete["wprime_source"] = source
        self._write_json(self.active_dir / "athlete.json", self._athlete)
        log.info("_set_wprime: wprime_j=%d J source=%s", v, source)
        return True

    @property
    def wprime_source(self) -> str:
        """Source of the currently stored `wprime_j` value.

        Returns one of "manual", "icu", "monod", "fallback", or "" if no
        wprime has been written yet (callers treat empty as "fallback").
        """
        return str(self._athlete.get("wprime_source", "") or "")

    # v3.6.0-fix35e: FTP-test persistence.
    @property
    def ftp_test_history(self) -> list[dict]:
        """Return the stored FTP-test history (copy).

        Each entry: {date (ISO-8601), method (coggan_20min|ramp), ftp (int),
        source (workout name or tag)}. Returned as a list copy so external
        mutation can't corrupt the profile in memory.
        """
        return list(self._athlete.get("ftp_test_history", []))

    def record_ftp_test(self, method: str, ftp: int, source: str = "",
                        applied: bool = False) -> None:
        """Append an FTP-test entry to the active profile (atomic write).

        Does NOT mutate `ftp` — callers decide whether to apply via
        `update_ftp()`. `applied=True` is a flag stored on the entry for
        audit (did the rider accept the suggestion).
        """
        try:
            ftp = int(ftp)
        except (TypeError, ValueError):
            raise ValueError(f"ftp must be int, got {ftp!r}")
        if not (50 <= ftp <= 600):
            raise ValueError(f"ftp {ftp} out of range [50, 600]")
        if method not in ("coggan_20min", "ramp", "manual"):
            raise ValueError(f"unknown method {method!r}")
        entry = {
            "date": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "method": method,
            "ftp": ftp,
            "source": str(source or ""),
            "applied": bool(applied),
        }
        hist = list(self._athlete.get("ftp_test_history", []))
        hist.append(entry)
        self._athlete["ftp_test_history"] = hist
        self._write_json(self.active_dir / "athlete.json", self._athlete)
        log.info(
            f"record_ftp_test: method={method} ftp={ftp} "
            f"source={source!r} applied={applied}"
        )

    def update_ftp(self, ftp: int, source: str | None = None) -> None:
        """Atomically update active profile FTP (clamped [50, 600]).

        E1 (v4.1.0): optional ``source`` records the FTP provenance on the
        profile via ``ftp_source``. Allowed values: "tested_coggan_20min",
        "tested_ramp", "eftp_icu", "eftp_local", "manual". Invalid sources
        are coerced to "manual" so callers that pass free-form strings
        don't break. Callers that don't care about provenance omit the
        argument and leave the existing ftp_source untouched.
        """
        try:
            v = int(ftp)
        except (TypeError, ValueError):
            raise ValueError(f"ftp must be int, got {ftp!r}")
        if not (50 <= v <= 600):
            raise ValueError(f"ftp {v} out of range [50, 600]")
        self._athlete["ftp"] = v
        if source is not None:
            # FIX-CONTRACT C5: "eftp_auto" = auto-applied by F5's 7-day
            # sustained-drift rule. "eftp_icu" retained for any legacy
            # callers that wrote it directly (E2 accept path still uses it).
            allowed = {"tested_coggan_20min", "tested_ramp", "eftp_icu",
                       "eftp_auto", "eftp_local", "manual"}
            self._athlete["ftp_source"] = source if source in allowed else "manual"
        elif "ftp_source" not in self._athlete:
            # E1 migration: if the profile has never had ftp_source written,
            # default it to "manual" so downstream code always sees a value.
            self._athlete["ftp_source"] = "manual"
        self._write_json(self.active_dir / "athlete.json", self._athlete)
        log.info(f"update_ftp: ftp={v} source={self._athlete.get('ftp_source')}")

    @property
    def ftp_source(self) -> str:
        """E1 (v4.1.0): provenance of the currently-stored FTP.
        Returns one of tested_coggan_20min | tested_ramp | eftp_icu |
        eftp_auto | eftp_local | manual. Defaults to "manual" for
        pre-v4.1 profiles.
        """
        return str(self._athlete.get("ftp_source") or "manual")

    def get_ftp_source(self) -> str:
        """FIX-CONTRACT C1: method alias for the ``ftp_source`` property so
        callers that prefer getter semantics (``pm.get_ftp_source()``) don't
        have to know whether it's a property vs. method. Mirrors the property
        return value exactly.
        """
        return self.ftp_source

    def get_ftp_source_date(self) -> str | None:
        """FIX-CONTRACT C1: ISO date (YYYY-MM-DD) of the last ftp_source
        change. Derives from the most-recent ftp_test_history entry whose
        ``applied=True`` — that's where update_ftp writes (via the E1
        path in record_ftp_test). Returns None when the profile has never
        applied an FTP (fresh setup, pre-v4.1 migration).
        """
        for entry in reversed(self.ftp_test_history or []):
            if entry.get("applied") and entry.get("date"):
                return str(entry["date"])
        return None

    def save_env(self, icu_athlete_id: str, icu_api_key: str) -> None:
        """Save Intervals.icu credentials to active profile's .env.

        Strips leading/trailing whitespace and rejects embedded newlines
        (which would otherwise inject arbitrary KEY=VALUE lines into .env).
        Writes atomically and sets 0600 so credentials aren't world-readable.
        """
        icu_athlete_id = (icu_athlete_id or "").strip()
        icu_api_key = (icu_api_key or "").strip()
        if any(c in (icu_athlete_id + icu_api_key) for c in "\n\r"):
            raise ValueError("credentials may not contain newlines")

        self._env["ICU_ATHLETE_ID"] = icu_athlete_id
        self._env["ICU_API_KEY"] = icu_api_key
        content = f"ICU_ATHLETE_ID={icu_athlete_id}\nICU_API_KEY={icu_api_key}\n"
        self._write_env_atomic(self.active_dir / ".env", content)
        os.environ["ICU_ATHLETE_ID"] = icu_athlete_id
        os.environ["ICU_API_KEY"] = icu_api_key

    def save_prefs(self, prefs: dict) -> None:
        """Save training preferences to active profile's user_prefs.json."""
        self._prefs.update(prefs)
        self._write_json(self.active_dir / "user_prefs.json", self._prefs)

    def save_device_prefs(self, prefs: dict) -> None:
        """Save per-rider device preferences."""
        self._device_prefs.update(prefs)
        self._write_json(self.active_dir / "device_prefs.json", self._device_prefs)

    # ── Internal helpers ─────────────────────────────────────────────────

    def _load_registry(self) -> None:
        """Load profiles.json. Rebuild from directory scan if corrupt."""
        if self._registry_path.exists():
            try:
                self._registry = json.loads(
                    self._registry_path.read_text(encoding="utf-8")
                )
                self._active_id = self._registry.get("active_profile")
                self._fix_env_permissions()
                return
            except (json.JSONDecodeError, KeyError) as e:
                log.warning(f"Corrupt profiles.json, rebuilding: {e}")

        # Fallback: scan profiles directory
        self._rebuild_registry()
        self._fix_env_permissions()

    def _fix_env_permissions(self) -> None:
        """One-shot migration: any .env file created before the 0600 policy is
        tightened here on load. Legacy profiles created with umask 022 have
        world-readable credentials; this repairs them in place."""
        if not self._profiles_dir.exists():
            return
        for env_path in self._profiles_dir.glob("*/.env"):
            try:
                mode = os.stat(env_path).st_mode & 0o777
                if mode != 0o600:
                    os.chmod(env_path, 0o600)
                    log.info(f"Normalized {env_path} permissions 0o{mode:03o} -> 0o600")
            except OSError as e:
                log.debug(f"Could not chmod {env_path}: {e}")

    def _rebuild_registry(self) -> None:
        """Rebuild profiles.json from directory contents.

        If no profile directories are found we do NOT auto-create one — the
        app must detect the empty state via `has_any_profile()` and redirect
        the user to /setup. Auto-creating "Rider" here previously masked the
        first-run flow (C7)."""
        profiles = []
        if self._profiles_dir.exists():
            for d in sorted(self._profiles_dir.iterdir()):
                # Only pick up directories whose name passes our validator —
                # protects against stray dot-dirs or path-traversal names
                # ending up in the registry.
                if (
                    d.is_dir()
                    and _PROFILE_ID_RE.match(d.name)
                    and (d / "athlete.json").exists()
                ):
                    profiles.append({
                        "id": d.name,
                        "name": d.name.replace("-", " ").replace("_", " ").title(),
                        "color": PROFILE_COLORS[len(profiles) % len(PROFILE_COLORS)],
                        "created": datetime.now().isoformat(),
                        "last_used": datetime.now().isoformat(),
                    })

        if not profiles:
            # Empty state: caller must detect via has_any_profile() and route
            # the user to /setup. Do NOT create a phantom "Rider" profile here.
            self._registry = {
                "version": 1,
                "active_profile": None,
                "skip_picker": True,
                "profiles": [],
            }
            self._active_id = None
            # Persist the empty registry so subsequent boots don't re-scan.
            self._save_registry()
            return

        self._registry = {
            "version": 1,
            "active_profile": profiles[0]["id"],
            "skip_picker": len(profiles) == 1,
            "profiles": profiles,
        }
        self._active_id = profiles[0]["id"]
        self._save_registry()

    def _load_active_profile(self) -> None:
        """Load all data files for the active profile.

        When `athlete.json` exists but is empty or missing required keys we
        log a WARNING rather than crashing — the user may be mid-setup.
        Callers can detect the half-configured state via `is_fully_configured()`.
        """
        if self._active_id is None:
            # No active profile (empty-state). Leave caches empty.
            self._athlete = {}
            self._env = {}
            self._prefs = {}
            self._device_prefs = {}
            return

        d = self.active_dir
        if not d.exists():
            log.warning(f"Profile dir missing: {d}")
            self._athlete = {}
            self._env = {}
            self._prefs = {}
            self._device_prefs = {}
            return

        self._athlete = self._load_json(d / "athlete.json")
        self._env = self._load_env_file(d / ".env")
        self._prefs = self._load_json(d / "user_prefs.json")
        self._device_prefs = self._load_json(d / "device_prefs.json")

        athlete_path = d / "athlete.json"
        if athlete_path.exists():
            if not self._athlete:
                log.warning(
                    f"athlete.json for profile '{self._active_id}' is empty "
                    f"or unparsable; user may be mid-setup"
                )
            else:
                missing = [
                    k for k in _REQUIRED_ATHLETE_KEYS
                    if k not in self._athlete or self._athlete.get(k) in (None, "")
                ]
                if missing:
                    log.warning(
                        f"athlete.json for profile '{self._active_id}' is "
                        f"missing required keys: {missing}"
                    )

    # v4.0.0-alpha: trainer-difficulty accessors removed with the live
    # trainer runtime. device_prefs is kept as a dormant bag so legacy
    # callers that walked the dict do not crash.

    def _load_json(self, path: Path) -> dict:
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _write_json(self, path: Path, data: dict) -> None:
        """Atomic JSON write (tmp + fsync + os.replace)."""
        self._write_text_atomic(path, json.dumps(data, indent=2))

    def _write_text_atomic(self, path: Path, content: str, *, mode: Optional[int] = None) -> None:
        """Atomic text write: write to .tmp, fsync, os.replace onto target.

        Uses `os.replace` (atomic rename on POSIX + Windows) and fsync the tmp
        file before rename so a crash between the two leaves either the old
        content or the new content intact, never a truncated file. Optional
        `mode` applies permissions before the rename so the final file is
        never visible with laxer permissions than requested.
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode or 0o644)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
            if mode is not None:
                try:
                    os.chmod(str(tmp), mode)
                except OSError:
                    pass
            os.replace(str(tmp), str(path))
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _write_env_atomic(self, path: Path, content: str) -> None:
        """Atomic write of an .env file with 0600 permissions."""
        self._write_text_atomic(path, content, mode=0o600)
        # Belt-and-braces: if the file pre-existed with laxer perms the
        # os.replace() above would preserve the tmp file's perms, but on some
        # filesystems (NFS, FAT) that's unreliable — chmod once more.
        try:
            os.chmod(str(path), 0o600)
        except OSError:
            pass

    def _load_env_file(self, path: Path) -> dict:
        result = {}
        if path.exists():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    if "=" in line and not line.startswith("#"):
                        k, v = line.split("=", 1)
                        result[k.strip()] = v.strip()
            except OSError:
                pass
        return result

    def _save_registry(self) -> None:
        self._base.mkdir(parents=True, exist_ok=True)
        self._write_json(self._registry_path, self._registry)

    def _update_last_used(self) -> None:
        for p in self._registry.get("profiles", []):
            if p["id"] == self._active_id:
                p["last_used"] = datetime.now().isoformat()
                break
        self._save_registry()


# ── Module-private helpers (not on the class so they can't be monkey-patched
# as an instance-level bypass) ───────────────────────────────────────────────

def _safe_profile_dir(pm: "ProfileManager", profile_id: str) -> Path:
    """Validate a caller-supplied profile_id and return the absolute on-disk
    path it maps to.

    Rejects anything that:
      * doesn't match `^[a-z0-9][a-z0-9_-]{0,31}$` (blocks "..", slashes,
        uppercase, dot-files, empty string, over-long input), OR
      * resolves outside the profiles root (defence-in-depth against symlinks
        or future regex slip-ups).

    Any violation raises ValueError so API routes can return 400 instead of
    leaking a traversal.
    """
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.match(profile_id):
        raise ValueError(f"Invalid profile_id: {profile_id!r}")
    profiles_root = pm._profiles_dir.resolve()
    candidate = (pm._profiles_dir / profile_id).resolve()
    try:
        # Python 3.9+: Path.is_relative_to. Fall back to the startswith check
        # on platforms where it's not available.
        if hasattr(candidate, "is_relative_to"):
            ok = candidate.is_relative_to(profiles_root)
        else:
            ok = str(candidate).startswith(str(profiles_root) + os.sep)
    except Exception:
        ok = False
    if not ok:
        raise ValueError(f"profile_id {profile_id!r} escapes profiles root")
    return candidate
