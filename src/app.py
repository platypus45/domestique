"""
Domestique Dashboard v2 — local web interface.

Run: python3 app.py
Open: http://127.0.0.1:22400  (or $DOMESTIQUE_PORT, if set)

Pure HTML + CSS + vanilla JS. No npm, no frameworks.
"""

import clock  # the one clock every module reads (see src/clock.py)
import collections
import functools
import hashlib
import json
import os
import re
import sys
import threading
import time
import uuid
import xml.etree.ElementTree as ET
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# Load .env file if present (secrets) — check multiple locations
_env_candidates = [
    Path(__file__).parent / ".env",                                    # dev mode
    Path(__file__).parent.parent / ".env",                             # dev mode, repo root (Part-B src/ layout)
    Path.home() / ".domestique" / ".env",                              # packaged mode (user home)
    Path.home() / "Documents" / "health_tracker" / ".env",             # legacy location
]
for _env_path in _env_candidates:
    if _env_path.exists():
        for line in _env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())
        break

import asyncio
import logging
from fit_activity import fit_field as _fit_val  # v3.11.3 fit-tool compat reader

import tls_trust  # v3.11.4 — OS-native verifier on Windows, OpenSSL+certifi elsewhere



# Observability lives in obs.py. Imported (not `from`-imported into new names)
# for the module-level handles, then re-exported below so `app._log_error`,
# `app._DIAG_RING` and friends keep resolving for the test suite.
from obs import (  # noqa: F401
    log_config, error_codes,
    _log, log, log_library, log_ride_import,
    _DIAG_RING_MAX, _DIAG_RING, _DIAG_RING_LOCK,
    _log_error, _diag_ring_snapshot,
)


from fastapi import FastAPI, File, Form, Request, Query, UploadFile, HTTPException, Body
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

sys.path.insert(0, str(Path(__file__).parent))

from training import get_today_metrics, fetch_wellness, fetch_activities, TRIMP_TO_TSS_FACTOR
from sleep import get_sleep_metrics
from readiness import compute_readiness
from readiness_composite import compute_readiness_composite  # v1.1.0 IMPL-HRV-RECOVERY
import config
import db
import zones as _zones_mod
import training_planner as tp  # module-level: every plan endpoint uses tp.plan_write_lock()
import continuous_policy as cpol  # 3.4.0 W2 — continuous-mode deload + rotation policy
# v1.6.1 — register _log_error as the planner's observability hook so any
# planner-internal failure (generate_plan phase build, reforecast, match_zwo
# library scan) lands in the shared diag ring buffer. Hook indirection
# avoids the cycle that would result from training_planner importing app.
try:
    tp._set_log_error_hook(_log_error)
except Exception:
    pass

# User data directory — must be defined before any path that uses it.
# NOTE: directory creation is deferred until lifespan startup (after
# profile_manager._maybe_migrate_data_dir has had a chance to rename a
# legacy ~/.chickencycling/ dir into place). _plan_dir() below mkdirs
# on demand, so deferring this is safe.
# Filesystem locations live in paths.py. Re-exported so `app.DATA_DIR`,
# `app._plan_dir`, `app._safe_path` and friends keep resolving for the ~200
# call sites and the test files that reach them through `app.X`.
from paths import (  # noqa: F401
    domestique_home, _user_data_dir, DATA_DIR, COURSE_DIR,
    ROUTE_DATA, ROUTE_PROFILES_INDEX, ROUTE_PROFILES_DIR,
    _DEFAULT_PLAN_DIR, _plan_dir, _safe_path, _rides_fit_dir,
)


def _maybe_restore_plan_from_backup(plan_path: Path) -> str | None:
    """v1.6.2 — restore current_plan.json from the newest non-empty .bak* file.

    Triggered on app startup. If ``plan_path`` is missing or zero-bytes AND
    at least one ``current_plan.json.bak*`` snapshot exists with parseable
    JSON, atomically copies the newest valid snapshot back into place.

    Returns the source filename (e.g. ``"current_plan.json.bak"``) on
    successful restore, else ``None``. Never raises — boot must continue.
    """
    try:
        live_ok = plan_path.exists() and plan_path.stat().st_size > 0
        if live_ok:
            return None
        # Walk .bak, .bak2, ..., .bak{depth} in age order (newest first).
        candidates = [plan_path.with_suffix(plan_path.suffix + ".bak")]
        for n in range(2, tp.PLAN_BACKUP_DEPTH + 1):
            candidates.append(plan_path.with_suffix(plan_path.suffix + f".bak{n}"))
        for src in candidates:
            if not src.exists() or src.stat().st_size == 0:
                continue
            try:
                with open(src, encoding="utf-8") as f:
                    parsed = json.load(f)
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(parsed, dict) or not parsed:
                continue
            # Atomic copy: write tmp + rename. Don't go through atomic_write_plan
            # because we don't want to rotate (the .bak files we're restoring
            # FROM should be preserved as-is, not shifted).
            tmp = plan_path.with_suffix(plan_path.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(parsed, f, indent=2, default=str)
            tmp.replace(plan_path)
            return src.name
        return None
    except Exception:
        return None


def _plan_generated_stamp(plan: dict):
    """v3.1.0 (PART A) — parse a plan's generation stamp as a naive datetime.

    Reads ``generated`` (live key since v1.0.0) with legacy ``generated_at``
    fallback — the same both-keys pattern the seed anchor uses
    (training_planner.rewrite_stale_plan_classifications). Full-timestamp
    precision when the value parses as an ISO datetime; date-only values
    become midnight. Returns None when neither key parses (caller falls back
    to mtime).
    """
    for k in ("generated", "generated_at"):
        v = plan.get(k)
        if not v:
            continue
        s = str(v)
        try:
            return datetime.fromisoformat(s).replace(tzinfo=None)
        except ValueError:
            pass
        try:
            d = date.fromisoformat(s[:10])
            return datetime(d.year, d.month, d.day)
        except ValueError:
            pass
    return None


def _adopt_legacy_root_plan(root_dir: Path, prof_dir: Path,
                            multi_profile: bool) -> str:
    """v3.1.0 (IP_PLAN_CONTINUITY PART A) — one-time root→profile plan adoption.

    Since v2.1.0, single-profile users who never opened the profile picker
    kept writing plans to the legacy ROOT ``~/.domestique/plans/`` while
    ``profiles/<id>/plans/`` held a frozen migration-day snapshot; the v3.0.0
    boot repoint then served the stale snapshot. This adopts the legacy root
    ``current_plan.json`` into the ACTIVE profile's plan dir, once.

    Discriminator (STAMP-primary, A-LOCKED-1): the plan's ``generated`` /
    ``generated_at`` stamp; newer stamp wins. mtime is the fallback ONLY when
    a stamp is missing/unparseable on either side (NEVER mtime-primary — the
    boot-time stale-classification rewrite restamps the profile file every
    3.0.x boot, so the mtime rule would no-op the fix for the whole victim
    cohort). Equal stamps: single-profile home adopts (wrong-skip is
    permanent+silent, wrong-adopt is .bak-recoverable); multi-profile skips.
    mtime tie: skip (profile wins; conservative).

    Adoption = rotate profile live → .bak chain, then ``copy2`` root → profile
    (mtime/stamp preserved); root's standard ``.bak..bak{depth}`` names are
    copied only where the profile has no file of that exact name (2.1.0
    migration already copied them → expected no-ops); foreign names
    (``.bak-v206`` etc.) are never touched. The root file is retired via
    ``Path.replace`` → ``current_plan.json.pre-v3`` (the idempotency latch,
    immune to restamps) only AFTER the copy succeeded. Byte-identical
    root == profile retires WITHOUT copy/rotation — this doubles as the
    retire-failure loop guard (a re-boot after a failed retire re-lands here).

    Corrupt root JSON: skip + warn, leave the file for support. Unparseable /
    zero-byte profile plan counts as "profile missing" (else the v1.6.2
    boot restore would resurrect a stale .bak over the adopted plan).
    Never raises; returns a status string (also logged, one line).
    """
    import shutil

    root_dir = Path(root_dir)
    prof_dir = Path(prof_dir)
    root = root_dir / "current_plan.json"
    latch = root_dir / "current_plan.json.pre-v3"
    prof = prof_dir / "current_plan.json"
    try:
        if latch.exists():
            _log.debug("plan adoption: skipped — .pre-v3 latch present")
            return "latched"
        if not root.exists():
            _log.debug("plan adoption: skipped — no legacy root plan")
            return "no-root"
        try:
            if root_dir.resolve() == prof_dir.resolve():
                _log.debug("plan adoption: skipped — root dir IS the profile dir")
                return "same-dir"
        except OSError:
            pass

        try:
            root_bytes = root.read_bytes()
            root_plan = json.loads(root_bytes.decode("utf-8"))
            if not isinstance(root_plan, dict) or not root_plan:
                raise ValueError("not a non-empty JSON object")
        except Exception as e:  # noqa: BLE001 — corrupt legacy file must not brick boot
            _log.warning(
                f"plan adoption: skipped — corrupt legacy root plan left in "
                f"place for support ({e})"
            )
            return "corrupt-root"
        root_mtime = root.stat().st_mtime

        # H2 (evaluator): fallback latch on the PROFILE side for homes where
        # the root dir is unwritable (read-only volume / restored backup) and
        # the .pre-v3 retire keeps failing. Without it, the same boot's
        # stale-classification rewrite mutates the adopted plan (stamps stay
        # equal, bytes diverge) and every subsequent boot re-adopts — reverting
        # the user's edits and churning the .bak chain. The latch records the
        # adopted root stamp; as long as the root file still carries that
        # stamp there is nothing new to adopt. A genuinely different root
        # (changed stamp) still adopts normally.
        prof_latch = prof_dir / "current_plan.json.adopted-from-root"
        _r_stamp_obj = _plan_generated_stamp(root_plan)
        # Stampless roots latch on a CONTENT HASH, not a shared sentinel — a
        # different stampless root must not match an old latch (wrong-skip is
        # permanent+silent; wrong-adopt is .bak-recoverable).
        _root_stamp_str = (str(_r_stamp_obj) if _r_stamp_obj is not None
                           else "sha256:" + hashlib.sha256(root_bytes).hexdigest()[:16])
        # The latch only means "this exact root was already adopted HERE" —
        # meaningless when the profile plan is gone (support lever: delete the
        # profile plan + reboot ⇒ re-adopt the root).
        if prof_latch.exists() and (prof_dir / "current_plan.json").exists():
            try:
                if prof_latch.read_text(encoding="utf-8").strip() == _root_stamp_str:
                    _log.debug(
                        "plan adoption: skipped — profile-side latch matches "
                        "root stamp (retire previously failed)"
                    )
                    return "latched-prof"
            except OSError:
                pass  # unreadable latch → fall through to the normal decision

        prof_bytes = None
        prof_plan = None
        if prof.exists():
            try:
                prof_bytes = prof.read_bytes()
                parsed = json.loads(prof_bytes.decode("utf-8")) if prof_bytes else None
                prof_plan = parsed if (isinstance(parsed, dict) and parsed) else None
            except Exception:  # noqa: BLE001 — unparseable prof ⇒ treated as missing
                prof_plan = None

        # ── DECIDE first (pure reads), LOG the full decision, THEN act. ─────
        prof_mtime = prof.stat().st_mtime if prof.exists() else None
        r_stamp = _plan_generated_stamp(root_plan)
        p_stamp = _plan_generated_stamp(prof_plan) if prof_plan is not None else None

        if prof_bytes is not None and prof_bytes == root_bytes:
            # Byte-identical root == profile → retire only (no copy, no
            # rotation). Also the retire-failure loop guard.
            verdict, why = "retire-only", "root == profile (byte-identical)"
        elif prof_plan is None:
            verdict, why = "adopt", "profile plan missing/unparseable"
        elif r_stamp is not None and p_stamp is not None:
            if r_stamp > p_stamp:
                verdict, why = "adopt", "root stamp newer"
            elif r_stamp < p_stamp:
                verdict, why = "skip", "profile stamp newer"
            elif not multi_profile:
                verdict, why = "adopt", "equal stamps — single-profile home adopts"
            else:
                verdict, why = "skip", "equal stamps — multi-profile home skips"
        else:
            # Stamp missing/unparseable on either side → mtime fallback.
            if prof_mtime is None or root_mtime > prof_mtime:
                verdict, why = "adopt", "mtime fallback: root newer"
            elif root_mtime < prof_mtime:
                verdict, why = "skip", "mtime fallback: profile newer"
            else:
                verdict, why = "skip", "mtime fallback: tie — profile wins"

        # Log-before-act (owner requirement — adoption rewrites plan data at
        # boot): every decision input + the verdict, emitted BEFORE the first
        # filesystem mutation so the trail exists even on a crash mid-swap.
        _log.info(
            f"plan adoption decision: verdict={verdict} ({why}); "
            f"root_stamp={r_stamp} prof_stamp={p_stamp} "
            f"root_mtime={root_mtime} prof_mtime={prof_mtime} "
            f"multi_profile={multi_profile} latch_present=False "
            f"root={root} prof={prof}"
        )

        if verdict == "skip":
            return "skipped"  # GA2 DECIDED: root LEFT in place (support lever)

        if verdict == "retire-only":
            try:
                root.replace(latch)  # Path.replace — a rename, never a delete
                return "retired-identical"
            except OSError as e:
                _log.warning(
                    f"plan adoption: retire of identical root failed ({e}) — "
                    "writing profile-side latch instead"
                )
                try:
                    prof_latch.write_text(_root_stamp_str, encoding="utf-8")
                except OSError:
                    pass
                return "retire-failed"

        # verdict == "adopt". NEVER-DELETE invariant: only copy2 /
        # _rotate_plan_backups / Path.replace→.pre-v3 below — no deletion
        # calls of our own. (The standard rotation ages the OLDEST .bak
        # off the end at depth 7, same as every plan write — the live plan
        # and recent history are never destroyed.)
        with tp.plan_write_lock():
            if prof.exists():
                tp._rotate_plan_backups(prof)  # profile's current → .bak chain
            prof_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(root, prof)

        # Root's standard .bak..bak{depth} history: copy only names the
        # profile side doesn't already have; foreign .bak names untouched.
        bak_names = ["current_plan.json.bak"] + [
            f"current_plan.json.bak{n}" for n in range(2, tp.PLAN_BACKUP_DEPTH + 1)
        ]
        for name in bak_names:
            src, dst = root_dir / name, prof_dir / name
            if src.exists() and not dst.exists():
                try:
                    shutil.copy2(src, dst)
                except OSError as e:
                    _log.debug(f"plan adoption: bak copy {name} failed: {e}")

        # Retire ONLY after a successful copy (Path.replace — Windows-safe).
        try:
            root.replace(latch)
        except OSError as e:
            _log.warning(
                f"plan adoption: adopted but retire failed ({e}) — writing "
                "profile-side latch so later boots don't re-adopt after the "
                "adopted plan is modified (H2)"
            )
            try:
                prof_latch.write_text(_root_stamp_str, encoding="utf-8")
            except OSError:
                pass

        _log_error(
            error_codes.Codes.PLAN_ADOPTED_FROM_ROOT,
            root=str(root), profile_plan=str(prof), reason=why,
        )
        return "adopted"
    except Exception as e:  # noqa: BLE001 — never brick startup. Total-abort:
        # every already-completed step is safe on its own (rotation only
        # COPIES, the root→prof copy leaves root intact until retire), so no
        # partial-swap state is reachable; boot continues serving whatever
        # the profile dir holds, exactly as v3.0.2 does.
        _log.warning(f"plan adoption failed: {e}")
        return "error"


def _session_naming_lookup(
    zwo_file: str,
    classifications: dict | None,
    lib_by_file: dict | None,
) -> tuple[str, int]:
    """v1.0.4 IMPL-WIRING — resolve display_name + zwo_duration_min for a session.

    ``display_name`` comes from the ``.content_classification.json`` field
    (Layer 3, MASTER §3). When the entry is missing or the field is empty,
    returns "" — the dashboard cascade then falls back to ``zwo_name`` then
    ``session_type``.

    ``zwo_duration_min`` is the actual library duration of the matched ZWO
    (sum of segment durations, rounded), so the dashboard can render the
    *real* workout length instead of the planner's ``duration_min`` target
    when the two disagree (e.g. "title says 51min but content is 82min").

    Free-form sessions (no zwo_file) yield ("", 0) — caller decides whether
    to emit empty string or null.
    """
    if not zwo_file:
        return "", 0
    entry = (classifications or {}).get(zwo_file) or {}
    display_name = str(entry.get("display_name") or "")
    lib_meta = (lib_by_file or {}).get(zwo_file) or {}
    raw_dur = lib_meta.get("Duration(min)")
    try:
        zwo_duration_min = int(round(float(raw_dur))) if raw_dur is not None else 0
    except (TypeError, ValueError):
        zwo_duration_min = 0
    return display_name, zwo_duration_min


_BUNDLED_WORKOUT_DIR = Path(__file__).parent / "workouts"  # bundled workout files
_BUNDLED_GPX_DIR     = Path(__file__).parent / "gpx"       # bundled GPX files
WORKOUT_DIR   = _BUNDLED_WORKOUT_DIR
GPX_DIR       = _BUNDLED_GPX_DIR

# Load user path overrides — check user data dir (packaged) then project dir (dev)
# Linux-IP D3: apply an override ONLY when the dir exists (mirrors
# profile_manager) — a stale workout_dir silently emptied the library and
# every plan degenerated to unmatched Z2 with no error anywhere.
for _upf in [_user_data_dir / "user_paths.json", Path(__file__).parent / "user_paths.json"]:
    if _upf.exists():
        try:
            _up = json.loads(_upf.read_text(encoding="utf-8"))
            if _up.get("workout_dir"):
                _cand = Path(_up["workout_dir"])
                # v3.11.2: usable (holds .zwo files), not merely existing.
                if _cand.is_dir() and next(_cand.glob("*.zwo"), None) is not None:
                    WORKOUT_DIR = _cand
                else:
                    logging.getLogger("app").error(
                        "E_WORKOUT_DIR_UNUSABLE: user_paths.json workout_dir=%s "
                        "is missing or holds no .zwo files — keeping the "
                        "bundled library", _cand)
            if _up.get("gpx_dir"):
                GPX_DIR = Path(_up["gpx_dir"])
        except Exception:
            pass
        break


def active_workout_dir() -> Path:
    """The active profile's workout directory, resolved at call time.

    WORKOUT_DIR is rebound by _apply_profile_paths on every profile switch, so
    a value read earlier belongs to the previous profile. Read it late through
    this, and never `from app import WORKOUT_DIR` -- that binds the object, not
    the name, and then serves the wrong athlete's library with no error and no
    log line. tests/test_profile_path_rebinding.py enforces both halves.
    """
    return WORKOUT_DIR


def active_gpx_dir() -> Path:
    """The active profile's GPX directory, resolved at call time.

    Same contract as active_workout_dir().
    """
    return GPX_DIR


def _apply_profile_paths() -> None:
    """AC2b: resolve app.py's module-global WORKOUT_DIR / GPX_DIR from the
    ACTIVE profile's user_paths.json — one source of truth with the
    training_planner resolution profile_manager.switch() performs.

    Resolution order (mirrors profile_manager.switch):
      1. <active_dir>/user_paths.json override, when the dir exists
      2. <active_dir>/workouts, when it exists (workout dir only)
      3. bundled default (so a profile WITHOUT an override never inherits the
         previous profile's dirs — the sticky-global bug).

    Registered as a pm.on_switch callback AND run once at boot post-activation
    (same code path); setup_save calls it after writing the profile's
    user_paths.json.
    """
    global WORKOUT_DIR, GPX_DIR
    wp: Path = _BUNDLED_WORKOUT_DIR
    gp: Path = _BUNDLED_GPX_DIR
    # v3.11.3 multi-profile screen: the 60-s Diagnostics snapshot is per
    # profile (plan readable, pool_health.workout_dir) — drop it on switch so
    # the next call never reports the previous profile's state.
    try:
        _DIAG_HEALTH_CACHE["result"] = None
        _DIAG_HEALTH_CACHE["ts"] = 0.0
    except Exception:
        pass
    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        if pm.active_id is not None:
            try:
                up = json.loads((pm.active_dir / "user_paths.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                up = {}
            if not isinstance(up, dict):
                up = {}
            cand = up.get("workout_dir")
            # v3.11.2: usable (holds .zwo files), not merely existing.
            if cand and tp.workout_dir_is_usable(Path(cand)):
                wp = Path(cand)
            elif tp.workout_dir_is_usable(pm.active_dir / "workouts"):
                wp = pm.active_dir / "workouts"
            elif cand:
                log.error("E_WORKOUT_DIR_UNUSABLE: profile workout_dir=%s is "
                          "missing or holds no .zwo files — using the bundled "
                          "library", cand)
            gcand = up.get("gpx_dir")
            if gcand and Path(gcand).exists():
                gp = Path(gcand)
    except Exception as e:
        log.debug(f"_apply_profile_paths: fell back to bundled dirs: {e}")
    WORKOUT_DIR = wp
    GPX_DIR = gp
CONFIG_PATH   = Path(__file__).parent / "config.py"

# Single source of truth for the app version. VERSION file is the canonical
# spec (read by the PyInstaller builder and the /api/version route).
try:
    # Sibling first (frozen bundle: VERSION lands at _MEIPASS root next to the
    # collected modules), then one level up (dev repo root under the Part-B
    # src/ layout — VERSION stays at the root where the release tooling and
    # the README guard tests read/write it).
    _v_p = Path(__file__).parent / "VERSION"
    if not _v_p.exists():
        _v_p = Path(__file__).parent.parent / "VERSION"
    _VERSION = _v_p.read_text(encoding="utf-8-sig").strip()
except OSError:
    _VERSION = "0.0.0"

# v4.0.0-alpha pivot: the app is a planner + workout library + post-ride
# viewer. No live trainer connection, no BLE, no WebSocket. All data flows
# through plain HTTP; the heavy lifting happens in the user's preferred
# indoor app (Tacx / MyWhoosh / Golden Cheetah), and Domestique imports
# the resulting FIT afterward via POST /api/ride/import.
APP_VERSION = "4.0.0-alpha"

# v1.0.2 IMPL-MIGRATION: cached migration-check result for the dashboard.
# Populated once at lifespan startup; consumed by GET /api/migrations/last-run-result.
# Default `show_toast=False` is the defensive value before lifespan runs.
_LAST_MIGRATION_RESULT: dict = {"show_toast": False}


@asynccontextmanager
async def lifespan(app):
    # ── Profile system: migrate + init ──────────────────────────────────
    # Order matters: ProfileManager.get() runs the v3 data-dir rename
    # (~/.chickencycling → ~/.domestique). After that runs, the legacy
    # single-user migration operates against the new dir name.
    from migrate_profiles import migrate_to_profiles, migrate_to_v4, run_v102_migration_check
    from profile_manager import ProfileManager
    # v2.1.0 — migrate BEFORE the first ProfileManager.get(). On a FRESH install
    # the registry doesn't exist yet; migrate_to_profiles() must create the
    # `default` profile + profiles.json first. If get() ran first it rebuilt an
    # empty registry and migrate's `registry.exists()` guard then skipped
    # creation, leaving NO active profile → every property fell back to defaults
    # (FTP 200 / 70kg) and saves evaporated on reopen (the Windows "profile
    # resets every launch" bug). Paired with the non-destructive empty-rebuild
    # fix in profile_manager._rebuild_registry.
    migrate_to_profiles()
    pm = ProfileManager.get()

    # v1.0.2 IMPL-MIGRATION: startup version-aware self-check. Detects an
    # upgrade by comparing ~/.domestique/last_run_version.txt to _VERSION;
    # on first boot after a bump, the result dict carries `show_toast=True`
    # and the dashboard surfaces a toast naming from→to versions and
    # confirming rider data is preserved. v1.0.2 has NO schema changes —
    # this is the framework slot for future additive migrations.
    global _LAST_MIGRATION_RESULT
    try:
        _LAST_MIGRATION_RESULT = run_v102_migration_check(DATA_DIR, _VERSION)
        if _LAST_MIGRATION_RESULT.get("show_toast"):
            _log.info(
                f"v1.0.2 migration check: upgraded from "
                f"v{_LAST_MIGRATION_RESULT.get('from_version')} → "
                f"v{_LAST_MIGRATION_RESULT.get('to_version')}, "
                f"{_LAST_MIGRATION_RESULT.get('columns_added')} columns added, "
                f"rider data preserved."
            )
    except Exception as e:
        _log.warning(f"v1.0.2 migration check failed (non-fatal): {e}")
        _LAST_MIGRATION_RESULT = {"show_toast": False}

    # v4.0.0-alpha: strip removed profile fields (device-pair list,
    # trainer-difficulty scaler, bike-weight) from every existing profile.
    # Silent strip + log only per profile_manager migration contract.
    try:
        for _prof in pm.list_profiles():
            _pid = _prof.get("id") if isinstance(_prof, dict) else None
            if not _pid:
                continue
            try:
                _prof_dir = pm._profiles_dir / _pid
                migrate_to_v4(_prof_dir)
            except Exception as e:
                _log.warning(
                    f"v4 profile migration failed for profile '{_pid}': {e}"
                )
        # Reload the ACTIVE profile's in-memory caches from disk so any
        # migration that just stripped legacy keys (paired_devices,
        # trainer_effect, bike_weight_kg) actually takes effect. Without
        # this, ProfileManager.get() above has already loaded stale
        # pre-migration contents into _athlete; a later _set_wprime() at
        # boot writes _athlete back to disk and re-instates the stripped
        # keys. FIX-SERVER (Wave 4): reload BOTH _athlete and
        # _device_prefs — previously only _device_prefs was reloaded
        # while _athlete kept its paired_devices.
        if pm.active_dir.exists():
            pm._load_active_profile()
    except Exception as e:
        _log.warning(f"v4 profile migration loop failed: {e}")

    pm.on_switch(clear_cache)
    # AC2b: one-time adoption of the legacy ROOT ~/.domestique/user_paths.json
    # into the active profile (setup_save now writes per-profile only), then
    # resolve app.py's active_workout_dir()/active_gpx_dir() through the same resolver used on
    # every profile switch — one code path.
    try:
        _root_paths = _user_data_dir / "user_paths.json"
        if pm.active_id is not None and _root_paths.exists():
            _prof_paths = pm.active_dir / "user_paths.json"
            if not _prof_paths.exists():
                _prof_paths.write_text(_root_paths.read_text(encoding="utf-8"),
                                       encoding="utf-8")
                _log.info("AC2b: adopted legacy root user_paths.json into "
                          f"profile '{pm.active_id}'")
    except Exception as _e:
        _log.debug(f"legacy user_paths adoption skipped: {_e}")
    # AC2b boot side: run the SAME training-dir resolver switch() uses, once,
    # post-activation — training_planner.WORKOUT_DIR etc. point at the active
    # profile from the first request (one code path for boot + switch).
    try:
        pm.apply_training_dirs()
    except Exception as _e:
        _log.warning(f"apply_training_dirs at boot failed: {_e}")
    # v3.1.0 (IP_PLAN_CONTINUITY PART A): one-time adoption of the legacy ROOT
    # plan into the ACTIVE profile's plan dir. MUST run after
    # apply_training_dirs (PLAN_DIR points at the profile) and BEFORE
    # _apply_profile_paths / db init / the v1.6.2 .bak restore / the v4.1.1
    # stale-classification rewrite / the ICU push-hook registration — so the
    # adopted file is what those boot passes see, and booting never schedules
    # a calendar push by itself (the daily reconcile mirrors it instead).
    try:
        if pm.active_id is not None:
            _adopt_legacy_root_plan(
                _DEFAULT_PLAN_DIR,   # legacy root ~/.domestique/plans
                pm.plan_dir,         # active profile's plans dir
                multi_profile=len(pm.list_profiles()) > 1,
            )
    except Exception as _e:
        _log.warning(f"legacy root plan adoption failed: {_e}")
    _apply_profile_paths()
    pm.on_switch(_apply_profile_paths)
    # Restart the DB sync thread on every profile switch so it picks up the new
    # profile's db_path. This on_switch registration is the ONLY owner of the
    # restart (AC1 killed switch()'s inline call — the double stop/join could
    # silently kill sync after a slow pass; do not re-add it there).
    if hasattr(db, 'restart_sync'):
        pm.on_switch(db.restart_sync)
    # Point DB at active profile's database
    db.set_db_path(pm.db_path)
    db.init_db()
    db.start_background_sync()

    # v1.6.2 — boot-time plan auto-restore. If current_plan.json is missing
    # or zero-bytes but at least one .bak* snapshot exists, restore from
    # the newest valid one. Closes the failure mode where a release-time
    # mutation nuked the live file but the rotated .bak files survived.
    try:
        _restored_from = _maybe_restore_plan_from_backup(_plan_dir() / "current_plan.json")
        if _restored_from:
            _log_error(
                error_codes.Codes.PLAN_AUTO_RESTORED,
                source=_restored_from,
                plan_path=str(_plan_dir() / "current_plan.json"),
            )
            _log.warning(
                f"v1.6.2 plan auto-restore: live current_plan.json was "
                f"missing — restored from {_restored_from}."
            )
    except Exception as _e:
        _log.debug(f"plan auto-restore on boot failed: {_e}")

    # v4.1.1 FIX-PLANNER A: on boot, walk the stored plan and rewrite any
    # session whose zwo_file prefix disagrees with its session_type. Before
    # v4.1.1 the _classify_protocol heuristic was missing 6 prefix families,
    # so ~30% of existing plans have e.g. type=tempo + zwo=vo2_…zwo. The
    # fix extends the classifier but DOES NOT automatically rebuild old
    # plans — users won't regenerate manually. Best-effort: never raise,
    # log count.
    try:
        import training_planner as _tp_boot
        _plan_json = _plan_dir() / "current_plan.json"
        _rewritten = _tp_boot.rewrite_stale_plan_classifications(_plan_json)
        if _rewritten > 0:
            _log.info(
                f"v4.1.1 FIX-PLANNER A: rewrote {_rewritten} stale session "
                f"classifications in {_plan_json}"
            )
    except Exception as _e:
        _log.debug(f"stale-plan rewrite on boot failed: {_e}")

    # v3.0.1 (IP_ICU_PUSH): register the plan post-write hook AFTER the
    # boot-time restore/rewrite writes above, so booting never schedules a
    # calendar push by itself. Server-only — the CLI path never registers.
    # Boot-time writes are instead mirrored by the once-a-day reconcile
    # riding the sync loop (D3b).
    tp.post_write_callback = _icu_push_schedule_debounced
    db.post_sync_callback = _after_background_sync

    try:
        yield
    finally:
        # ── Shutdown cleanup: best-effort, don't block teardown ──────────
        try:
            _icu_push_cancel_pending()
        except Exception as e:
            log.debug(f"icu push timer cancel failed: {e}")
        try:
            if hasattr(db, 'stop_sync'):
                db.stop_sync()
        except Exception as e:
            log.debug(f"db.stop_sync failed: {e}")

app = FastAPI(title="Domestique", version=_VERSION, lifespan=lifespan)


# Global exception handler — catches unhandled errors and logs them
from starlette.requests import Request as StarletteRequest
from starlette.responses import JSONResponse as StarletteJSONResponse

@app.exception_handler(Exception)
async def generic_exception_handler(request: StarletteRequest, exc: Exception):
    log.exception(f"Unhandled exception in {request.url.path}")
    return StarletteJSONResponse(
        status_code=500,
        content={"error": "Internal server error", "detail": "see server logs"},
    )


# ── Shared JSON body helper ──────────────────────────────────────────────────
# Returns parsed JSON dict, or raises HTTPException(400) on malformed input.
# Request helpers live in http_util.py; re-exported for the ~40 call sites
# and the tests that reach them through app.X.
from http_util import (  # noqa: F401
    _icu_verify, _get_json_body, _diag_local_only,
)

# --- EXTRACTED MODULES ---
from download_lib import (  # noqa: F401
    _capacity_cap_active,
    _cap_zwo_bytes,
    _wrap_zwo_outdoor,
    _zwo_download_response,
    _cap_active_for_download,
)
from search_lib import (  # noqa: F401
    _SEARCH_TRANSLIT,
    _SEARCH_SYNONYMS,
    _SEARCH_TYPO_VOCAB,
    _SEARCH_HAY_PCT_RX,
    _SEARCH_30S15S_RX,
    _SEARCH_DUR_TOL,
    _SEARCH_QUERY_CAP,
    _search_normalize,
    _search_row_haystack,
    _get_search_haystacks,
    _search_lev1,
    _search_parse_query,
    _search_apply_typo_fix,
    _search_match_row,
    _search_score_row,
)
# Leaf modules split out of this file. Every name here is re-exported because
# 60 test files reach them through app.X. Mutable module state is NOT
# re-exported -- see routes_lib's note below.
# HR resolution lives in hr.py; re-exported for the six sections that call it.
from hr import (  # noqa: F401
    _fit_hr_mode, _prescription_hr_rows, _hr_bias, _fit_hr_params,
)

# The virtual-route library. Functions only -- routes_lib owns its own
# caches (_ROUTES_CACHE, _SURFACE_TYPES_CACHE and their mtimes/locks) and
# they are deliberately NOT re-exported: importing them here would bind the
# objects, and rebinding app.X would leave routes_lib reading its own.
from routes_lib import (  # noqa: F401
    _slugify_route_name,
    _route_key_for,
    _canonical_surface,
    _load_surface_types_db,
    _route_surface_segments,
    _build_routes_index,
    _load_routes_v2,
    _is_climb_route,
    _is_flat_route,
    _route_summary,
    _parse_csv_param,
    _apply_route_filters,
    _score_route_for_suggest,
    _top_archetypes_for_region,
    _route_profile_points,
    _load_route_index,
    _load_route_detail,
    _gradient_to_power_factor,
    _build_climb_zwo,
)


# Request logging middleware — logs all API errors
from starlette.middleware.base import BaseHTTPMiddleware

class ErrorLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        try:
            response = await call_next(request)
            if response.status_code >= 500:
                _log.error(f"HTTP {response.status_code}: {request.method} {request.url.path}")
            return response
        except Exception as e:
            _log.error(f"Middleware caught: {request.method} {request.url.path} → {type(e).__name__}: {e}")
            raise

app.add_middleware(ErrorLoggingMiddleware)


# ── Response compression ────────────────────────────────────────────────────
# Some endpoints (/api/rides/{id}, /api/workouts) can emit large JSON bodies.
# GZipMiddleware only kicks in above minimum_size so small /api/* payloads
# pass through uncompressed.
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=10_000)


# ── CSRF mitigation: Origin header check for mutating requests ───────────────
# Security model: app binds to 127.0.0.1 only (offline-by-design pywebview app).
# An Origin-header check on mutating requests blocks drive-by CSRF from other
# tabs the user has open. Token-based CSRF was deliberately not added because
# the maintenance burden (cookie/token rotation, fetch wrapper everywhere)
# outweighs the marginal benefit for a localhost-only single-user app.
# Requests with no Origin header (curl, pywebview, server-to-server) are
# allowed since CSRF requires a browser.
# v3.5.1: allow ANY port on localhost/127.0.0.1, not just :8080. The check
# exists to block cross-SITE requests (a hostile web page's Origin is its own
# https://… host); pinning the port added no CSRF protection while silently
# 403-ing every mutating POST when the app serves off-default (dev preview on
# :8090 had every button's POST rejected — the UI just looked dead).
_ALLOWED_ORIGIN_PREFIXES = (
    "http://localhost:",
    "http://127.0.0.1:",
)


@app.middleware("http")
async def origin_check(request, call_next):
    if request.method in ("POST", "DELETE", "PUT", "PATCH"):
        origin = request.headers.get("origin", "")
        if origin and not any(origin.startswith(a) for a in _ALLOWED_ORIGIN_PREFIXES):
            return StarletteJSONResponse({"error": "origin not allowed"}, status_code=403)
    return await call_next(request)


# ── Security headers (defence-in-depth) ──────────────────────────────────────
# The app binds to 127.0.0.1 only and is consumed via pywebview, so a CSP is
# belt-and-braces — but a stray dev browser tab has no in-bundle defence.
# Using ``setdefault`` so downstream handlers or stricter middleware can
# override per-response without fighting this layer. Idempotent: re-applying
# the same defaults is a no-op, so this block is safe to land alongside
# other in-flight middleware edits.
@app.middleware("http")
async def _security_headers(request, call_next):
    resp = await call_next(request)
    # NOTE: 'unsafe-eval' is required by pywebview's injected bridge
    # (webview/js/api.js:75 uses `new Function(...)` to construct the
    # window.pywebview.api proxy after Python advertises its method list).
    # Without it, the bridge silently fails to initialise — bug surfaces
    # as "window.pywebview.api.save_zwo is not a function" + a CSP
    # EvalError on the home screen. The app binds to 127.0.0.1 only and
    # serves no third-party scripts, so unsafe-eval is acceptable here.
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; connect-src 'self' ws: wss:; "
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    return resp


templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
_static_dir = Path(__file__).parent / "static"
_static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(_static_dir)), name="static")



def _setup_marker() -> Path:
    """AC4: the setup-complete marker lives in the USER DATA DIR only.

    The old module-load constant also fell back to the repo/bundle copy of
    .setup_complete — the repo bundles that file, so first-run detection was
    permanently defeated on dev checkouts and any build that shipped it.
    Function (not constant) so a test-stubbed DATA_DIR is honoured."""
    return DATA_DIR / ".setup_complete"


def _active_profile_entry(pm) -> dict:
    """Registry entry for the active profile ({} when none)."""
    try:
        for p in pm.list_profiles():
            if p.get("id") == pm.active_id:
                return p
    except Exception:
        pass
    return {}


# ═══════════════════════════════════════════════════════════════════════════════
# SETUP WIZARD
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    return templates.TemplateResponse(request=request, name="setup.html")


@app.get("/api/setup/status")
def setup_status():
    return {"complete": _setup_marker().exists()}


# ── AC5d: ONE range table. Server-side validation (setup_save + update_settings)
# and both wizards' client-side input min/max all read from here — the wizards
# fetch it via GET /api/setup/limits so client ranges can never drift again.
SETUP_LIMITS: dict[str, list[float]] = {
    "ftp": [50, 600],
    "weight": [30, 200],
    "lthr": [100, 220],
    "max_hr": [140, 220],
    "hours_per_week": [1, 40],
    "age": [10, 100],
    "cp": [100, 500],
    "wprime_j": [5000, 40000],
}


@app.get("/api/setup/limits")
def setup_limits():
    return SETUP_LIMITS


@app.get("/api/setup/defaults")
def setup_defaults():
    """Return current/detected config values to pre-fill the wizard."""
    workout_dir_default = ""

    return {
        "icu_id": config.ICU_ATHLETE_ID or "",
        "workout_dir": str(active_workout_dir()) if active_workout_dir().exists() else workout_dir_default,
        "gpx_dir": str(active_gpx_dir()) if active_gpx_dir().exists() else "",
        "weight": config.ATHLETE_WEIGHT_KG,
        "ftp": config.ATHLETE_FTP_W,
        "lthr": config.ATHLETE_LTHR,
        "max_hr": config.ATHLETE_MAX_HR,
    }


@app.post("/api/setup/test-icu")
def setup_test_icu(body: dict):
    """Test Intervals.icu credentials and return athlete info.

    v1.8.25 — athlete_id is OPTIONAL: when blank we auto-detect it from the API
    key via training.discover_athlete_id (same helper /api/setup/save uses), so
    the wizard only needs the API key. The detected id is returned so the UI can
    stash it for save.
    """
    athlete_id = body.get("athlete_id", "").strip()
    api_key = body.get("api_key", "").strip()
    if not api_key:
        return {"ok": False, "error": "API Key is required."}
    if not athlete_id:
        try:
            import training as _training
            disc = _training.discover_athlete_id(api_key)
            if disc and disc.get("id"):
                athlete_id = str(disc["id"]).strip()
        except Exception:
            pass
        if not athlete_id:
            return {"ok": False, "error": "Couldn't detect your athlete from that "
                    "API key. Double-check the key (intervals.icu → Settings → "
                    "Developer Settings → API Key)."}

    import httpx
    try:
        url = f"https://intervals.icu/api/v1/athlete/{athlete_id}"
        resp = httpx.get(url, auth=("API_KEY", api_key), timeout=10, verify=_icu_verify())
        if resp.status_code == 401:
            return {"ok": False, "error": "Invalid API key. Check your credentials."}
        if resp.status_code == 404:
            return {"ok": False, "error": f"Athlete '{athlete_id}' not found."}
        if resp.status_code != 200:
            return {"ok": False, "error": f"API returned status {resp.status_code}"}
        data = resp.json()
        # Try to get eFTP from wellness
        eftp = None
        weight = data.get("weight")
        try:
            w_url = f"https://intervals.icu/api/v1/athlete/{athlete_id}/wellness?oldest=2025-01-01&newest=2026-12-31"
            w_resp = httpx.get(w_url, auth=("API_KEY", api_key), timeout=10, verify=_icu_verify())
            if w_resp.status_code == 200:
                for w in reversed(w_resp.json()):
                    si = w.get("sportInfo", [])
                    if si and si[0].get("eftp"):
                        eftp = round(si[0]["eftp"])
                        break
                    if not weight and w.get("weight"):
                        weight = w["weight"]
        except Exception:
            pass
        return {
            "ok": True,
            "name": data.get("name", ""),
            "athlete_id": athlete_id,
            "eftp": eftp,
            "weight": round(weight, 1) if weight else None,
        }
    except httpx.TimeoutException:
        return {"ok": False, "error": "Connection timed out. Check your internet."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.post("/api/setup/check-activities")
def setup_check_activities(body: dict):
    """v1.8.25 — verify, during onboarding, that activities are actually flowing
    into Intervals.icu (i.e. the Garmin Connect sync the user just enabled is
    working). Counts ICU activities in the last 42 days and, when the activity
    `source` field is present, how many are Garmin-sourced.

    HONEST by design: we observe activities, not the connection itself, so we
    report "N recent activities on Intervals.icu" and surface the Garmin-sourced
    count separately — we never claim Garmin is linked from a manual upload.
    Creds are passed explicitly (not yet saved at this point in setup) and used
    only for this transient request, mirroring /api/setup/test-icu.
    """
    athlete_id = (body.get("athlete_id") or "").strip()
    api_key = (body.get("api_key") or "").strip()
    # AC5a: OAuth-era wizard path — no key in the browser, use the ACTIVE
    # profile's saved connection (Bearer token / stored key) via training.
    if not api_key and body.get("use_connection"):
        if not _icu_credentials_present():
            return {"ok": False, "error": "Connect Intervals.icu first."}
        try:
            import training as _training
            acts = _training.fetch_recent_activities(days=42) or []
        except Exception as e:
            return {"ok": False, "error": f"Intervals.icu request failed: {type(e).__name__}"}
        if not isinstance(acts, list):
            acts = []
        garmin = 0
        for a in acts:
            blob = " ".join(str(a.get(k) or "") for k in ("source", "device_name", "deviceName")).upper()
            if "GARMIN" in blob:
                garmin += 1
        latest = ""
        if acts:
            latest = str(acts[0].get("start_date_local") or acts[0].get("start_date") or "")[:10]
        return {"ok": True, "count": len(acts), "garmin_count": garmin, "latest_date": latest}
    if not api_key:
        return {"ok": False, "error": "Connect Intervals.icu first."}
    if not athlete_id:
        try:
            import training as _training
            disc = _training.discover_athlete_id(api_key)
            if disc and disc.get("id"):
                athlete_id = str(disc["id"]).strip()
        except Exception:
            pass
    if not athlete_id:
        return {"ok": False, "error": "Couldn't detect your athlete from that API key."}

    import httpx
    from datetime import timedelta as _td
    today = clock.today()
    oldest = (today - _td(days=42)).isoformat()
    newest = today.isoformat()
    try:
        url = (f"https://intervals.icu/api/v1/athlete/{athlete_id}/activities"
               f"?oldest={oldest}&newest={newest}")
        resp = httpx.get(url, auth=("API_KEY", api_key), timeout=15, verify=_icu_verify())
        if resp.status_code in (401, 403):
            return {"ok": False, "error": "Authentication failed — check your API key."}
        if resp.status_code != 200:
            return {"ok": False, "error": f"Intervals.icu returned status {resp.status_code}."}
        acts = resp.json() if resp.content else []
        if not isinstance(acts, list):
            acts = []
        count = len(acts)
        # Garmin-sourced detection: ICU tags the provider on `source` (e.g.
        # "GARMIN_CONNECT") and/or device fields. Count conservatively — only
        # what clearly reads as Garmin — so we never over-claim.
        garmin = 0
        for a in acts:
            blob = " ".join(str(a.get(k) or "") for k in ("source", "device_name", "deviceName")).upper()
            if "GARMIN" in blob:
                garmin += 1
        latest = ""
        if acts:
            latest = str(acts[0].get("start_date_local") or acts[0].get("start_date") or "")[:10]
        return {"ok": True, "count": count, "garmin_count": garmin, "latest_date": latest}
    except httpx.TimeoutException:
        return {"ok": False, "error": "Connection timed out. Check your internet."}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def _pick_folder_webview():
    """Native pywebview folder dialog — the packaged app runs uvicorn
    in-process with ``webview.start()`` (launcher.py), so ``webview.windows``
    is populated and the dialog parents to the app window (same
    ``create_file_dialog`` bridge launcher.JsApi uses for saves).

    Returns the picked path, ``""`` when the user cancels, or ``None`` when
    no webview window exists (bare-server/dev run) so the caller falls back
    to tkinter.
    """
    try:
        import webview
        if not getattr(webview, "windows", None):
            return None
        result = webview.windows[0].create_file_dialog(webview.FOLDER_DIALOG)
        if not result:
            return ""
        return result if isinstance(result, str) else result[0]
    except Exception:
        return None


@app.get("/api/setup/pick-folder")
def setup_pick_folder():
    """Open a native OS folder picker dialog and return the selected path.

    Prefers the pywebview dialog (native, correctly parented); tkinter is the
    fallback for runs without a webview window — the bundle keeps tkinter for
    exactly this (see domestique.spec excludes comment).
    """
    native = _pick_folder_webview()
    if native is not None:
        return {"path": native}
    import threading

    result = {"path": ""}

    def _pick():
        try:
            import tkinter as tk
            from tkinter import filedialog
            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            folder = filedialog.askdirectory(title="Select Folder")
            root.destroy()
            result["path"] = folder or ""
        except Exception:
            result["path"] = ""

    # Run in thread to avoid blocking (tkinter needs main thread on some OS)
    t = threading.Thread(target=_pick)
    t.start()
    t.join(timeout=60)
    return {"path": result["path"]}


@app.get("/api/setup/icu-hr")
def setup_icu_hr(athlete_id: str = Query(""), api_key: str = Query("")):
    """Fetch LTHR and Max HR from Intervals.icu activity data."""
    if not athlete_id or not api_key:
        return {"lthr": None, "max_hr": None}
    import httpx
    try:
        # Get recent activities to find max HR and threshold HR.
        # Rolling 12-month window (W2d): the old hardcoded 2024→2026 range was
        # both a staleness source and a Jan-2027 time bomb.
        from datetime import date as _date, timedelta as _td
        _new = (clock.today() + _td(days=1)).isoformat()
        _old = (clock.today() - _td(days=365)).isoformat()
        url = f"https://intervals.icu/api/v1/athlete/{athlete_id}/activities?oldest={_old}&newest={_new}"
        resp = httpx.get(url, auth=("API_KEY", api_key), timeout=15, verify=_icu_verify())
        if resp.status_code != 200:
            return {"lthr": None, "max_hr": None}
        activities = resp.json()
        max_hr = 0
        lthr_candidates = []
        for a in activities:
            hr = a.get("max_heartrate") or 0
            if hr > max_hr:
                max_hr = hr
            # Estimate LTHR from activities with high intensity (IF > 0.9)
            avg_hr = a.get("average_heartrate") or 0
            icu_if = a.get("icu_intensity") or 0
            if 0.85 <= icu_if <= 1.05 and avg_hr > 100:
                lthr_candidates.append(avg_hr)
        lthr = round(sum(lthr_candidates) / len(lthr_candidates)) if lthr_candidates else None
        return {"lthr": lthr, "max_hr": max_hr if max_hr > 100 else None}
    except Exception:
        return {"lthr": None, "max_hr": None}


# Guards against concurrent wizard submissions clobbering each other.
_setup_lock = threading.Lock()


def _setup_path_allowed_bases() -> "list[Path]":
    """Where a setup-supplied path may point (SEC4).

    Computed per call rather than frozen at import: the app's OWN directories
    are on this list, and on Windows they live wherever the user installed to
    — which import order cannot know.

    The guard exists to stop a hostile setup-save pointing the server at
    arbitrary filesystem locations. It was never meant to reject the
    application's own bundled workout library, but that is exactly what it did
    on Windows: the wizard auto-detects the bundled path, pre-fills it, marks
    it touched (deliberately — an auto-detected path is a proposal the user
    accepts by clicking through), and submits it. Under Program Files that
    is not below $HOME, so "Complete Setup" failed with a message about
    directories the rider had never chosen and could not act on.
    """
    bases = [Path.home(), domestique_home()]
    # The app's own directories. Trusted by definition — this is where the
    # library ships, and on a frozen build it is inside the install location.
    for own in (_BUNDLED_WORKOUT_DIR, _BUNDLED_GPX_DIR):
        try:
            bases.append(own)
            bases.append(own.parent)
        except Exception:  # noqa: BLE001 — a bad base must never break setup
            pass
    if sys.platform == "win32":
        # Windows equivalents of /tmp; %TEMP% is also where a frozen onefile
        # build unpacks itself.
        for env in ("TEMP", "TMP", "LOCALAPPDATA", "APPDATA"):
            v = os.environ.get(env)
            if v:
                bases.append(Path(v))
    else:
        bases.append(Path("/tmp"))
        bases.append(Path("/private/tmp"))  # macOS symlink target
    return bases


def _setup_path_allowed(p: Path) -> bool:
    """SEC4: restrict user-supplied setup paths to home / .domestique / /tmp.

    Resolves symlinks before comparison so `/private/var/../Users/...`-style
    escapes still fall under ``Path.home()``. Allows exact matches and any
    descendant of the allowed bases.
    """
    try:
        resolved = p.resolve()
    except (OSError, RuntimeError):
        return False
    for base in _setup_path_allowed_bases():
        try:
            rb = base.resolve()
        except (OSError, RuntimeError):
            continue
        if resolved == rb or rb in resolved.parents:
            return True
    return False


@app.post("/api/setup/save")
def setup_save(body: dict):
    """Save all wizard settings — writes to active profile's athlete.json + .env.

    v4.5.2 FIX-CREDS-HOTRELOAD: when ICU creds change we (1) rely on
    ``pm.save_env`` to refresh the in-memory ``_env`` cache (config.ICU_*
    proxies through it via ``__getattr__``, so the running process picks up
    new creds without restart), (2) reset the rides-sync throttle
    (``_write_last_sync_at(0)``) so the next /api/rides/sync isn't blocked by
    a stale 1h marker from before the user re-pasted creds, (3) clear
    ``db._auth_disabled`` so the background sync loop resumes after a 5-strike
    auth-disable from the OLD bad key, and (4) probe the new creds with a
    fast ``athlete/<id>`` GET so the response surfaces ``creds_test`` to the
    UI. The probe is best-effort — network errors are reported as failure
    but don't roll back the .env write.
    """
    from profile_manager import ProfileManager
    global WORKOUT_DIR, GPX_DIR

    creds_test: str | None = None
    athlete_id_detected: str | None = None
    athlete_name_detected: str | None = None
    athlete_id_warning: str | None = None

    with _setup_lock:
        pm = ProfileManager.get()
        if pm.active_id is None:
            # AC6a adjacency: with no active profile there is nowhere to write —
            # never fall back to the profiles-root athlete.json.
            raise HTTPException(status_code=409,
                                detail="no active profile — create a profile first")

        # ══ PHASE 1 — VALIDATE EVERYTHING (AC4b: no partial creds/athlete on 400) ══
        icu_id = body.get("icu_athlete_id", "").strip()
        icu_key = body.get("icu_api_key", "").strip()
        if any(c in (icu_id + icu_key) for c in "\n\r"):
            raise HTTPException(status_code=400,
                                detail="credentials may not contain newlines")
        # v4.5.3 FIX-AUTO-ATHLETE-ID: when user provides an API key but no
        # athlete ID (or wants to verify the one they typed), call /athlete/0
        # to discover the authenticated athlete. Read-only — safe pre-write.
        if icu_key:
            try:
                import training as _training
                discovered = _training.discover_athlete_id(icu_key)
            except Exception:
                discovered = None
            if discovered and discovered.get("id"):
                discovered_id = discovered.get("id") or ""
                athlete_name_detected = discovered.get("name") or None
                if not icu_id:
                    # User left field blank → use discovered ID.
                    icu_id = discovered_id
                    athlete_id_detected = discovered_id
                elif icu_id != discovered_id:
                    # User typed something different. Honour the override but warn.
                    athlete_id_warning = (
                        f"Submitted athlete_id differs from API key's athlete "
                        f"({discovered_id}). Using submitted value."
                    )

        # Athlete numbers — AC5d: validated against the ONE range table. Only
        # fields present in the payload are validated/written (AC5e: wizards
        # omit untouched fields; omission is the no-write mechanism).
        profile = {}
        for key in ("weight", "ftp", "lthr", "max_hr"):
            if key in body:
                profile[key] = body[key]
        for key in ("weight", "ftp", "lthr", "max_hr"):
            if key in profile:
                lo, hi = SETUP_LIMITS[key]
                try:
                    v = float(profile[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"invalid {key}")
                if not (lo <= v <= hi):
                    raise HTTPException(400, f"{key}={v} out of range [{lo},{hi}]")
                profile[key] = v
        if body.get("weight"):
            profile["lbm"] = round(float(body["weight"]) * 0.80, 1)

        # AC5b (D3): lthr < max_hr — same invariant update_settings enforces.
        # Effective values: payload wins, else the stored profile value.
        eff_lthr = profile.get("lthr", pm._athlete.get("lthr"))
        eff_max = profile.get("max_hr", pm._athlete.get("max_hr"))
        if eff_lthr is not None and eff_max is not None and float(eff_lthr) >= float(eff_max):
            raise HTTPException(
                400, f"LTHR ({int(float(eff_lthr))}) must be below max HR "
                     f"({int(float(eff_max))})")

        # AC5e (D9): target-mode question. hr requires an LTHR (typed in this
        # payload or already explicitly set on the profile).
        target_mode = None
        if "target_mode" in body:
            target_mode = str(body["target_mode"]).strip().lower()
            if target_mode not in ("power", "hr"):
                raise HTTPException(400, "target_mode must be 'power' or 'hr'")
            if target_mode == "hr":
                if eff_lthr is None:
                    raise HTTPException(400, "hr mode needs your LTHR — fill in "
                                             "the LTHR field first")
                # Mirror update_settings' gate EXACTLY (pm.max_hr property
                # fallback) — phase 2 must never 400 after creds are written.
                _hr_eff_max = profile.get("max_hr", pm.max_hr)
                if _hr_eff_max is not None and float(_hr_eff_max) <= float(eff_lthr):
                    raise HTTPException(
                        400, f"max HR ({int(float(_hr_eff_max))}) must be above "
                             f"LTHR ({int(float(eff_lthr))}) for hr mode")

        # AC5c (D4): age / sex / cp / wprime_j forwarded through save_athlete.
        extras = {}
        for key in ("age", "cp", "wprime_j"):
            if key in body and body[key] is not None and body[key] != "":
                lo, hi = SETUP_LIMITS[key]
                try:
                    v = float(body[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"invalid {key}")
                if not (lo <= v <= hi):
                    raise HTTPException(400, f"{key}={v} out of range [{lo},{hi}]")
                extras[key] = v
        if "sex" in body:
            sex = str(body["sex"]).upper().strip()
            if sex not in ("M", "F", "O", ""):
                raise HTTPException(400, "sex must be M, F, or O")
            extras["sex"] = sex

        # Preferences — AC6b: hours_per_week server bound + the two day grids
        # must reconcile (a day can't be training AND rest).
        prefs = {}
        if body.get("hours_per_week"):
            lo, hi = SETUP_LIMITS["hours_per_week"]
            try:
                hpw = float(body["hours_per_week"])
            except (TypeError, ValueError):
                raise HTTPException(400, "invalid hours_per_week")
            if not (lo <= hpw <= hi):
                raise HTTPException(400, f"hours_per_week={hpw} out of range [{lo},{hi}]")
            prefs["hours_per_week"] = hpw
        avail_days = rest_days = None
        if body.get("available_days") is not None:
            try:
                avail_days = [int(d) for d in body["available_days"]]
            except (TypeError, ValueError):
                raise HTTPException(400, "available_days must be a list of weekday numbers")
        if body.get("rest_days") is not None:
            try:
                rest_days = [int(d) for d in body["rest_days"]]
            except (TypeError, ValueError):
                raise HTTPException(400, "rest_days must be a list of weekday numbers")
        if avail_days is not None and rest_days is not None:
            overlap = sorted(set(avail_days) & set(rest_days))
            if overlap:
                _names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
                _which = ", ".join(_names[d] if 0 <= d <= 6 else str(d) for d in overlap)
                raise HTTPException(400, f"{_which}: a day can't be both a "
                                         f"training day and a rest day")
        if avail_days is not None:
            prefs["available_days"] = avail_days
        if rest_days is not None:
            prefs["rest_days"] = rest_days

        # Path overrides. SEC4: reject paths outside the allow-list
        # (home / ~/.domestique / /tmp) so a malicious setup-save cannot make
        # the server read/write arbitrary filesystem locations later.
        for _field in ("workout_dir", "gpx_dir"):
            _val = body.get(_field)
            if _val and not _setup_path_allowed(Path(_val)):
                raise HTTPException(
                    status_code=400,
                    detail=f"{_field} must be under $HOME, ~/.domestique, or /tmp",
                )
        # v3.11.2: never persist a workout folder that cannot serve as a
        # library. An existing-but-empty folder used to be accepted, emptied
        # the library on the next resolution, and every plan degenerated to
        # endurance-only with nothing telling the rider why (Linux report).
        if body.get("workout_dir") and not tp.workout_dir_is_usable(
                Path(body["workout_dir"])):
            raise HTTPException(
                status_code=400,
                detail=("workout_dir must be an existing folder containing "
                        ".zwo workout files — leave it blank to use the "
                        "built-in library"),
            )

        # ══ PHASE 2 — WRITE (all validation passed) ══════════════════════════
        creds_changed = False
        if icu_id or icu_key:
            old_id = pm.icu_athlete_id
            old_key = pm.icu_api_key
            try:
                pm.save_env(icu_id, icu_key)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            creds_changed = (icu_id != old_id) or (icu_key != old_key)

            # 1a-pre. Defensively unshadow any stale module-level
            # ``config.ICU_ATHLETE_ID`` / ``config.ICU_API_KEY`` attribute that
            # an earlier override path (e.g. ``training.fetch_recent_activities``
            # with explicit creds) may have written. Once those exist as real
            # module attributes, Python's ``getattr`` short-circuits and the
            # ``__getattr__`` proxy never runs — leaving the running process
            # blind to the freshly-saved ``pm._env``. Deleting them restores
            # the proxy so config.ICU_* re-resolve to pm._env immediately.
            for _attr in ("ICU_ATHLETE_ID", "ICU_API_KEY"):
                try:
                    delattr(config, _attr)
                except AttributeError:
                    pass

            # 1a. Reset sync throttle + auth-disabled flag so the next sync
            # actually runs immediately on the fresh creds (the user just
            # pasted new keys precisely because the old ones broke).
            if creds_changed:
                try:
                    _write_last_sync_at(0)
                except Exception:
                    pass
                try:
                    p = _icu_wellness_sync_state_path()
                    if p is not None and p.exists():
                        p.unlink()
                except Exception:
                    pass
                try:
                    import db as _db
                    _db._auth_disabled = False
                    _db._consecutive_failures = 0
                    _db._last_sync_error = None
                except Exception:
                    pass

            # 1b. Probe the new creds — quick GET athlete/<id> so the UI can
            # toast "Saved, but ICU rejected the new credentials" instead of
            # silently waiting for the next sync to fail.
            if icu_id and icu_key:
                try:
                    import training as _training
                    _training._get(f"athlete/{icu_id}")
                    creds_test = "passed"
                except _training.ICUAuthError as e:
                    creds_test = f"failed: auth_rejected ({e})"
                except _training.ICUCredentialsMissing:
                    creds_test = "failed: credentials_missing"
                except Exception as e:
                    creds_test = f"failed: {type(e).__name__}"

        # Athlete numbers via the shared settings write path (provenance
        # stamping, ledger, config mirror). AC5f: forward the wizard's
        # lthr_source_hint (set ONLY for ICU-prefilled, unedited values).
        settings_payload = dict(profile)
        if target_mode is not None:
            settings_payload["target_mode"] = target_mode
        if body.get("lthr_source_hint"):
            settings_payload["lthr_source_hint"] = body["lthr_source_hint"]
        if settings_payload:
            update_settings(settings_payload)
        if extras:
            try:
                pm.save_athlete(extras)
            except ValueError as e:
                # Pre-validated above; belt-and-braces only.
                raise HTTPException(400, str(e))

        # Path overrides. AC2b: write the PROFILE's user_paths.json (the old
        # root-level DATA_DIR/user_paths.json was a third copy nothing on the
        # switch path ever read), then refresh the module globals through the
        # same resolver boot + profile-switch use — one code path.
        paths = {}
        if body.get("workout_dir"):
            paths["workout_dir"] = body["workout_dir"]
        if body.get("gpx_dir"):
            paths["gpx_dir"] = body["gpx_dir"]
        if paths:
            paths_file = pm.active_dir / "user_paths.json"
            try:
                existing = json.loads(paths_file.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (OSError, json.JSONDecodeError):
                existing = {}
            existing.update(paths)
            paths_file.parent.mkdir(parents=True, exist_ok=True)
            paths_file.write_text(json.dumps(existing, indent=2), encoding="utf-8")
            _apply_profile_paths()

        # Training preferences (validated in phase 1).
        if prefs:
            pm.save_prefs(prefs)

        # Mark setup complete — single global marker (per-profile marker dropped to avoid drift).
        _setup_marker().parent.mkdir(parents=True, exist_ok=True)
        _setup_marker().write_text(
            json.dumps({"completed": clock.now().isoformat(), "version": "1.0.0"}, indent=2),
            encoding="utf-8",
        )

        # AC4: wizard completion clears the migrate-created "bootstrapped"
        # flag so / stops redirecting to /setup for this profile.
        if _active_profile_entry(pm).get("bootstrapped"):
            pm.clear_bootstrapped()

        clear_cache()
    resp: dict = {"ok": True}
    if creds_test is not None:
        resp["creds_test"] = creds_test
    if athlete_id_detected is not None:
        resp["athlete_id_detected"] = athlete_id_detected
    if athlete_name_detected is not None:
        resp["athlete_name"] = athlete_name_detected
    if athlete_id_warning is not None:
        resp["warning"] = athlete_id_warning
    return resp


# ── Cache ─────────────────────────────────────────────────────────────────────
# Lives in cache.py. Imported by name and re-exported so `app.cached`,
# `app.clear_cache` and the four sections that mutate `_cache` directly keep
# working -- the dicts are the same objects, so in-place mutation is shared.
from cache import (  # noqa: F401
    _cache, _cache_ts, cached, clear_cache, register_clearer,
)

# W2B-G5 fix: per-key lock map for the fatigue-resistance endpoint so
# concurrent same-key requests don't both compute and don't race the
# lazy-GC pass against the cache-write.
import threading as _threading_for_fr
_fatigue_resistance_locks: dict[str, _threading_for_fr.Lock] = {}
_fatigue_resistance_locks_master = _threading_for_fr.Lock()


def _fatigue_resistance_cache_lock(cache_key: str):
    """Return (and lazily create) a per-cache-key Lock for FR compute."""
    with _fatigue_resistance_locks_master:
        lk = _fatigue_resistance_locks.get(cache_key)
        if lk is None:
            lk = _threading_for_fr.Lock()
            _fatigue_resistance_locks[cache_key] = lk
        return lk


# v1.8.9 Bug 4 (master §4/§10) — memoise the heavy fatigue-resistance
# compute keyed on (latest_ride_id, current_ftp, window_days, kj_threshold).
# Power streams don't change once written, so caching the result by these
# inputs is safe. lru_cache(maxsize=4) covers the realistic combinations
# (one window × two thresholds × maybe two profiles).
@functools.lru_cache(maxsize=4)
def _fatigue_resistance_memoised(latest_ride_id: str, current_ftp: int,
                                  window_days: int, kj_threshold: int) -> dict:
    import power_curve as _pc
    return _pc.compute_fatigue_resistance(
        _active_profile_id_or_default(),
        window_days=int(window_days),
        kj_threshold=int(kj_threshold),
    )


# The FR memo is keyed on (ride_id, ftp, window, kj) -- NOT profile -- so a
# profile switch must drop it or profile B serves A's curve. It cannot live in
# cache.py because it resolves the active profile, so it registers instead.
register_clearer(_fatigue_resistance_memoised.cache_clear)



# Serialises FIRST-EVER ProfileManager construction from concurrent request
# threads: ProfileManager.get() assigns cls._instance BEFORE loading the
# registry/active profile, so a racing thread can observe a half-built
# singleton whose active_id is still None (and silently fall back to
# "default" — two cache keys, double computes). Boot normally constructs it
# in the single-threaded lifespan; this covers lifespan-less contexts (tests).
_pm_get_lock = threading.Lock()


def _active_profile_id_or_default() -> str:
    """AC2a (grill): power_curve call sites must stop hardcoding "default" —
    pass the ACTIVE profile id so per-profile archive resolution attributes
    work to the right rider. "default" only as the no-profile fallback."""
    try:
        from profile_manager import ProfileManager
        with _pm_get_lock:
            pm = ProfileManager.get()
        return pm.active_id or "default"
    except Exception:
        return "default"


# ═══════════════════════════════════════════════════════════════════════════════
# PROFILE APIs
# ═══════════════════════════════════════════════════════════════════════════════

# Safe profile-id shape — defends path traversal / arbitrary file probes.
_PROFILE_ID_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,31}$')


def _validate_profile_id(profile_id: str) -> None:
    """Raise HTTPException(400) if profile_id doesn't match the strict slug regex."""
    if not profile_id or not _PROFILE_ID_RE.match(profile_id):
        raise HTTPException(status_code=400, detail="invalid profile id")


@app.get("/api/profiles")
def api_profiles():
    """List profiles. Each entry is enriched with ftp/weight/color from athlete.json
    so the UI doesn't have to make N extra requests. Returns 500 on failure.
    """
    from profile_manager import ProfileManager
    try:
        pm = ProfileManager.get()
        entries = pm.list_profiles()
        enriched = []
        for p in entries:
            pid = p.get("id", "")
            entry = dict(p)
            # Load per-profile athlete.json for ftp + weight
            try:
                athlete_path = pm._profiles_dir / pid / "athlete.json" if hasattr(pm, '_profiles_dir') else None
                if not athlete_path or not athlete_path.exists():
                    # Fallback to best-known home path if internal attribute missing
                    athlete_path = domestique_home() / "profiles" / pid / "athlete.json"
                if athlete_path.exists():
                    data = json.loads(athlete_path.read_text(encoding="utf-8"))
                    if "ftp" in data:
                        entry["ftp"] = data.get("ftp")
                    if "weight_kg" in data or "weight" in data:
                        entry["weight_kg"] = data.get("weight_kg") or data.get("weight")
                    if "color" in data and "color" not in entry:
                        entry["color"] = data.get("color")
            except Exception as e:
                log.debug(f"Failed to enrich profile {pid}: {e}")
            enriched.append(entry)
        return {
            "active": pm.active_id,
            "active_name": pm.profile_name,
            "active_color": pm.profile_color,
            "profiles": enriched,
        }
    except Exception as e:
        log.exception(f"api_profiles failed: {e}")
        return JSONResponse({"error": "Failed to list profiles"}, 500)


@app.post("/api/profiles/create")
def api_profiles_create(body: dict):
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    name = body.get("name", "").strip()
    color = body.get("color", "").strip()
    if not name:
        return JSONResponse({"error": "Name is required"}, status_code=400)
    # AC6b: duplicate display-name warning (non-blocking — ids stay unique via
    # the slug counter, but two "Anna" profiles are a foot-gun in the picker).
    dup = any((p.get("name") or "").strip().lower() == name.lower()
              for p in pm.list_profiles())
    new_id = pm.create_profile(name, color)
    resp = {"ok": True, "id": new_id}
    if dup:
        resp["warning"] = (f"A profile named \"{name}\" already exists — "
                           f"both will show in the picker. Rename one in "
                           f"Settings if that's not intended.")
    return resp


@app.post("/api/profiles/switch")
async def api_profiles_switch(request: Request):
    from profile_manager import ProfileManager
    body = await _get_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected object with 'id' key")
    pm = ProfileManager.get()
    profile_id = str(body.get("id", "")).strip()
    _validate_profile_id(profile_id)
    # v4.0.0-alpha: no live training to guard — the pivot removed the
    # live session global. Profile switch is always safe.
    # Optionally update skip_picker preference
    if "skip_picker" in body:
        pm._registry["skip_picker"] = bool(body["skip_picker"])
        pm._save_registry()
    try:
        pm.switch(profile_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except db.SyncBusy:
        # AC1: switch could not quiesce an in-flight sync write within the
        # bounded wait — NEVER proceed unlocked. Client retries.
        return JSONResponse({"error": "sync busy — try again in a moment"},
                            status_code=503)
    return {"ok": True, "active": pm.active_id}


@app.put("/api/profiles/{profile_id}")
def api_profiles_update(profile_id: str, body: dict):
    from profile_manager import ProfileManager
    _validate_profile_id(profile_id)
    pm = ProfileManager.get()
    pm.update_profile(profile_id, name=body.get("name"), color=body.get("color"))
    return {"ok": True}


@app.delete("/api/profiles/{profile_id}")
def api_profiles_delete(profile_id: str):
    from profile_manager import ProfileManager
    _validate_profile_id(profile_id)
    pm = ProfileManager.get()
    try:
        pm.delete_profile(profile_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


# v3.6.0-fix35e: post-FTP-test FTP adoption endpoint.
# Body: {"ftp": int, "method": str, "source": str, "applied": bool}.
# Always appends a history entry; only mutates `ftp` when applied=True.
@app.get("/api/profile/ftp-history")
def api_profile_ftp_history():
    """T4 (v4.1.0) — expose the ftp_test_history ledger for the UI.

    FIX-CONTRACT C7: emits ALL field-name aliases the UI's chart + tooltip
    might read so U2 never silently drops rows. Canonical field is ``ftp``;
    ``suggested_ftp`` aliases it for callers that expect the pre-accept
    semantic. ``applied`` is the boolean the chart filter gates on;
    ``accepted`` aliases it. ``source`` is the canonical provenance enum,
    with ``type`` (legacy) + ``method`` (test-kind) both populated.

    Provenance enum values:
      * ``tested_coggan_20min`` — user ran + accepted a Coggan 20-min test
      * ``tested_ramp`` — user ran + accepted a ramp test
      * ``eftp_auto`` — F5 auto-applied after 7d sustained drift
      * ``eftp_icu`` — user accepted eFTP via the settings panel
      * ``eftp_local`` — local best-efforts eFTP (suggestion only, rare
        in history)
      * ``manual`` — user edited the FTP field directly
    """
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    history = []
    for entry in pm.ftp_test_history:
        method = entry.get("method") or "manual"
        src = entry.get("source") or ""
        # Derive the canonical "type" from source if populated (catches
        # eftp_auto / eftp_icu / tested_* enums), else fall back to method.
        if src in ("eftp_auto", "eftp_icu", "eftp_local",
                   "tested_coggan_20min", "tested_ramp"):
            row_type = src
        elif method in ("coggan_20min", "ramp"):
            row_type = f"tested_{method}"
        else:
            row_type = "manual"
        ftp_val = entry.get("ftp")
        applied = bool(entry.get("applied", False))
        history.append({
            "date": entry.get("date"),
            # Canonical:
            "ftp": ftp_val,
            "type": row_type,
            "source": src or row_type,
            "applied": applied,
            "method": method,
            # Aliases so UI chart + tooltip never silently drop rows:
            "suggested_ftp": ftp_val,
            "accepted": applied,
        })
    return {
        "ftp_current": pm.ftp,
        "ftp_source": pm._athlete.get("ftp_source", "manual"),
        "history": history,
    }


@app.post("/api/profile/update-ftp")
def api_profile_update_ftp(body: dict):
    from profile_manager import ProfileManager
    try:
        new_ftp = int(body.get("ftp"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "ftp (int) required"}, status_code=400)
    method = str(body.get("method") or "manual")
    source = str(body.get("source") or "")
    applied = bool(body.get("applied", True))
    # E1 (v4.1.0): map the test method to an ftp_source enum so provenance
    # lands on the profile. "coggan_20min" → "tested_coggan_20min", etc.
    source_map = {
        "coggan_20min": "tested_coggan_20min",
        "ramp": "tested_ramp",
        "manual": "manual",
    }
    ftp_source_tag = source_map.get(method, "manual")
    pm = ProfileManager.get()
    try:
        pm.record_ftp_test(method=method, ftp=new_ftp, source=source, applied=applied)
        if applied:
            pm.update_ftp(new_ftp, source=ftp_source_tag)
            # v2.2.9 FIX — mirror onto the LIVE config so the topbar, settings
            # field and readiness reflect the new FTP immediately. update_ftp
            # writes athlete.json, but the running process keeps the
            # ATHLETE_FTP_W it cached at startup; without this refresh the saved
            # FTP appears to "revert" to the old value (e.g. an eFTP) in the UI
            # until the app restarts — even though athlete.json + the graph are
            # correct. Matches what the general /api/settings save already does.
            config.ATHLETE_FTP_W = new_ftp
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, "ftp": pm.ftp,
            "ftp_source": pm.ftp_source,
            "ftp_test_history": pm.ftp_test_history}


@app.get("/api/profile/tau-fits")
def api_profile_tau_fits(refresh: int = Query(0)):
    """v1.0.7 IMPL-TAU-FIT-WIRING — return current per-athlete τ fit + CIs.

    Drives the dashboard's <details class="tau-fit-results"> panel under
    the CTL/ATL/TSB chart. The dict mirrors ``fit_tau_per_athlete``'s
    return shape (PATCH §1) plus the conventional defaults for the UI's
    "vs. default" comparison columns.

    Calls ``tau_fitting.fit_tau_per_athlete(persist=False)`` so this
    endpoint never writes nls_fit rows on its own — the planner-side
    integration in ``training.get_today_metrics`` is the only writer.
    """
    try:
        import tau_fitting
        result = tau_fitting.fit_tau_per_athlete("default", persist=False)
    except Exception as e:
        _log.warning("api_profile_tau_fits failed: %s", e)
        result = {
            "ctl_tau_fit": None, "atl_tau_fit": None,
            "ctl_tau_ci_low": None, "ctl_tau_ci_high": None,
            "atl_tau_ci_low": None, "atl_tau_ci_high": None,
            "cp_tau1_fit": None, "cp_tau2_fit": None,
            "wprime_tau1_fit": None, "wprime_tau2_fit": None,
            "pmax_tau1_fit": None, "pmax_tau2_fit": None,
            "fit_residual_r2": None,
            "n_markers": 0, "weighted_n": 0.0,
            "fit_horizon_days": 365,
            "fit_status": "insufficient_data",
        }
    # Conventional τ defaults for the dashboard "vs. default" column.
    # Mirror training.py's CTL_TAU/ATL_TAU + the v1.0.6 3D defaults.
    result["conventional"] = {
        "ctl_tau": 42.0, "atl_tau": 7.0,
        "cp_tau1": 52.0, "cp_tau2": 10.0,
        "wprime_tau1": 5.0, "wprime_tau2": 5.0,
        "pmax_tau1": 10.0, "pmax_tau2": 4.0,
    }
    return result


@app.get("/api/profile/banister-validation")
def api_profile_banister_validation(refresh: int = Query(0)):
    """v1.2.0 IMPL-OOS-VALIDATION — out-of-sample Banister model accuracy.

    Calls ``oos_validation.validate_banister_oos()`` which fits τ on weeks
    1..N-4 and predicts the trailing 4-week holdout. Returns MAE per
    metric + bootstrap CIs + Hellard 2006 literature comparison. Cached
    for 24 hours via the standard ``_cache``/``_cache_ts`` mechanism;
    pass ``?refresh=1`` to force recompute.

    PATCH G4: ``validate_banister_oos`` calls ``fit_tau_per_athlete``
    with ``persist=False`` so this endpoint never pollutes the live
    nls_fit τ values. PATCH G12: 24h lazy cache, no scheduled task.
    """
    cache_key = "banister_oos_default"
    now = time.time()
    ttl = 24 * 3600  # 24h
    if not refresh and cache_key in _cache and now - _cache_ts.get(cache_key, 0) < ttl:
        return _cache[cache_key]
    try:
        import oos_validation
        result = oos_validation.validate_banister_oos("default")
    except Exception as e:
        _log.warning("api_profile_banister_validation failed: %s", e)
        result = {
            "fit_status": "insufficient_data",
            "horizon_weeks": 0, "holdout_weeks": 4,
            "n_markers_in_holdout": 0, "predictions": [],
            "ftp_mae_w": None, "ftp_mae_pct": None,
            "ftp_mae_pct_ci_low": None, "ftp_mae_pct_ci_high": None,
            "ctl_mae_tss": None, "ctl_mae_tss_ci_low": None,
            "ctl_mae_tss_ci_high": None,
            "hellard_2006_baseline_pct": "5-8",
            "comparison": "insufficient_data",
            "cp_fitness_mae_pct": None,
            "wprime_fitness_mae_pct": None,
            "pmax_fitness_mae_pct": None,
            "tau_fits_used": {},
        }
    _cache[cache_key] = result
    _cache_ts[cache_key] = now
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# v1.3.0 POWER CURVE + BACKFILL endpoints
# ═══════════════════════════════════════════════════════════════════════════════

# Module-level state for the async backfill task. Single-flight semantics
# are enforced by ``power_curve.acquire_backfill_lock`` (G3); the in-memory
# dict is just for /status polling within the running process.
_backfill_tasks: dict[str, dict] = {}
_backfill_thread_lock = threading.Lock()


@app.get("/api/profile/power-curve")
def api_profile_power_curve(window_days: int = Query(90, ge=1, le=3650),
                              refresh: int = Query(0)):
    """v1.3.0 — aggregated rider mean-max curve + P&G 2011 baseline.

    Cached for 24h via the standard ``_cache``/``_cache_ts`` mechanism.
    Per PATCH G4 the cache key includes ``latest_ride_id_in_window`` so
    that a freshly-imported ride invalidates automatically (no manual TTL
    bust needed). Pass ``?refresh=1`` to force recompute.
    """
    import power_curve
    _pid = _active_profile_id_or_default()
    try:
        latest = power_curve.latest_ride_id_in_window(_pid, int(window_days))
    except Exception as e:
        _log.debug(f"power_curve latest_ride_id failed: {e}")
        latest = ""
    cache_key = f"power_curve_{_pid}_{int(window_days)}_{latest}"
    now = time.time()
    ttl = 24 * 3600
    if not refresh and cache_key in _cache and now - _cache_ts.get(cache_key, 0) < ttl:
        return _cache[cache_key]
    try:
        result = power_curve.aggregate_power_curve(_pid,
                                                    window_days=int(window_days))
    except Exception as e:
        _log.warning(f"api_profile_power_curve failed: {e}")
        result = {
            "window_days": int(window_days),
            "n_rides": 0,
            "weight_kg": 70.0,
            "current_ftp": 200,
            "rider_curve": [],
            "pg_2011_baseline": [],
            "cp_w": None,
            "wprime_j": None,
            "pmax_w": None,
        }
    # Self-healing backfill: the curve comes back empty whenever the cached
    # envelopes in the window have never been hydrated with `efforts`
    # (summary-only ICU records carry efforts==[]). In that state n_rides
    # can be large (e.g. 51) yet rider_curve is []. The async toast-driven
    # backfill never fired here because it keyed on `icu_list_count`, a field
    # the server never emitted — so the curve stayed blank forever. Detect
    # the "rides present but none have efforts" case and run the one-shot
    # ICU-history backfill inline (it holds the single-flight lock G3,
    # fetches streams, derives STANDARD_DURATIONS efforts, and persists each
    # envelope), then recompute ONCE. Bounded to a single pass; if the
    # backfill can't acquire the lock (one already running) or hydrates
    # nothing, we fall through with the empty curve + needs_backfill=True.
    try:
        n_rides = int(result.get("n_rides") or 0)
    except (TypeError, ValueError):
        n_rides = 0
    rider_curve_empty = not (result.get("rider_curve") or [])
    # v2.1.0 (C1): self-heal whenever in-window rides are MISSING EFFORTS — not
    # only when the curve is empty. A few stale rides at the window edge produce a
    # NON-empty but wrong/low curve (short-duration peaks ~half, CP/W' starved
    # of points), and the old `rider_curve_empty` gate then skipped the backfill
    # forever — so the ~24 real in-window rides were never hydrated with efforts/
    # streams. Trigger on n_missing>0 so those rides get backfilled and the curve
    # recomputes with the true peaks. (`n_missing>0` below still guards the work.)
    if n_rides > 0:
        try:
            _n_win, _n_missing = power_curve.count_rides_missing_efforts(int(window_days))
        except Exception as e:
            _log.debug(f"power_curve count_rides_missing_efforts failed: {e}")
            _n_missing = 0
        if _n_missing > 0:
            # v3.2.1 BUG-FIX: NEVER backfill inline. It fetches ICU streams
            # rate-limited to 1/s, so ~24 missing rides blocked THIS request for
            # 20-30s — the power curve (and fatigue resistance, which shares the
            # streams) timed out and never rendered. Kick it in the background
            # and return the (possibly empty) curve immediately with
            # needs_backfill=True + backfill_progress; the frontend polls and
            # re-fetches when the streams are hydrated.
            result["backfill_progress"] = _kick_power_curve_backfill(
                _pid, int(window_days), _n_missing)
    # v1.8.8 Bug 2/11 — surface `needs_backfill` so the dashboard can swap
    # the "Loading…" placeholder for a "Run backfill" button when streams
    # haven't been cached yet. The legacy gate (n_rides==0) silently missed
    # the common case where rides ARE cached but none carry efforts yet, so
    # it's now True whenever the rider curve is still empty after the
    # self-heal pass above. ADD-only field — callers ignore it when absent.
    result["needs_backfill"] = bool(not (result.get("rider_curve") or []))
    # Lazy GC — prune stale power_curve_* entries with old latest_ride_ids.
    for k in list(_cache.keys()):
        if k.startswith(f"power_curve_{_pid}_{int(window_days)}_") and k != cache_key:
            _cache.pop(k, None)
            _cache_ts.pop(k, None)
    # Only cache a populated curve for 24h. When the curve is still empty
    # (self-heal couldn't acquire the lock, or the backfill hydrated nothing
    # this pass), skip the cache so the next request retries instead of
    # serving a stale empty curve for a full day.
    if not result.get("needs_backfill"):
        _cache[cache_key] = result
        _cache_ts[cache_key] = now
    return result


_pc_backfill_running: set = set()
_pc_backfill_running_lock = threading.Lock()


def _kick_power_curve_backfill(pid, window_days: int, n_missing: int) -> dict:
    """v3.2.1 — fire-and-forget ICU power-stream backfill so the power-curve /
    fatigue-resistance endpoints never block on it. Returns the LIVE cached-%
    (cached rides / in-window rides) for the frontend progress bar; polling the
    endpoint climbs it as rides hydrate. Single-flight per process; the on-disk
    G3 lock guards cross-process."""
    import power_curve
    key = pid or "_default"
    with _pc_backfill_running_lock:
        starting = key not in _pc_backfill_running
        if starting:
            _pc_backfill_running.add(key)
    if starting:
        # AC1 (chip closed): snapshot the sync identity BEFORE the thread
        # starts; every envelope write inside backfill_icu_history runs under
        # db.sync_write_gate(snap), so a profile switch mid-backfill aborts
        # the loop instead of mis-filing envelopes into the new profile.
        _snap = db.snapshot_sync_identity()
        def _work():
            try:
                acquired, _lk = power_curve.acquire_backfill_lock()
                if acquired:
                    try:
                        power_curve.backfill_icu_history(
                            pid, max_per_second=2, _skip_lock=True,
                            sync_snapshot=_snap)
                    finally:
                        power_curve.release_backfill_lock()
            except Exception as e:
                _log.warning(f"background power-curve backfill failed: {e}")
            finally:
                with _pc_backfill_running_lock:
                    _pc_backfill_running.discard(key)
        threading.Thread(target=_work, daemon=True, name="pc-backfill").start()
    try:
        n_win, n_miss = power_curve.count_rides_missing_efforts(int(window_days))
        pct = round(100 * (n_win - n_miss) / n_win) if n_win else 0
    except Exception:
        pct = max(0, 100 - round(100 * n_missing / max(n_missing + 1, 1)))
    return {"running": True, "pct": max(0, min(99, pct))}


def _sync_task_snapshot() -> tuple:
    """AC1: sync-identity snapshot taken at TASK START by app.py's write
    pipelines (lazy icu_sync thread, wellness sync, backfill worker). Passed
    to db.sync_write_gate(snapshot) around every write section so a mid-task
    profile switch (or purge) aborts the writes instead of landing them in
    the wrong profile's DB/dirs. Must be the exact 3-tuple the gate checks —
    delegate to db so the shape can never drift."""
    return db.snapshot_sync_identity()


def _run_backfill_job(task_id: str) -> None:
    """Worker thread — runs the one-shot backfill and updates the task entry.

    GRILL-WAVE2A W2A-G2 fix: the lock acquired by the endpoint is held
    until this worker finishes (success or crash). Released exactly once,
    here, in the `finally`. Closes the TOCTOU window where two POSTs
    between endpoint-release and worker-acquire could spawn duplicate
    ICU workers.

    AC1 (chip closed, post-3.2.2): the snapshot below IS enforced now —
    passed as ``sync_snapshot`` into ``backfill_icu_history``, which wraps
    every per-ride envelope write in ``db.sync_write_gate(snap)``. A profile
    switch/purge mid-backfill raises SyncAborted on the next write and the
    loop stops with status "aborted" (surfaced in the task entry) instead of
    mis-filing envelopes into the newly-active profile's dir. The GET-side
    background kick (_kick_power_curve_backfill) carries the same gate.
    """
    import power_curve

    snap = _sync_task_snapshot()

    def _progress(done, total):
        # Live "xx of yy" for the UI poll (backfill-history/status).
        with _backfill_thread_lock:
            e = _backfill_tasks.get(task_id) or {"task_id": task_id}
            e.update({"state": "running", "done": int(done), "total": int(total)})
            _backfill_tasks[task_id] = e

    try:
        result = power_curve.backfill_icu_history(
            snap[0] or "default", max_per_second=1, _skip_lock=True,
            progress_cb=_progress, sync_snapshot=snap,
        )
        with _backfill_thread_lock:
            _backfill_tasks[task_id] = {
                "task_id": task_id,
                "state": "done",
                "status": "complete",  # the UI poll checks this
                **result,
            }
    except Exception as e:
        _log.warning(f"backfill task {task_id} failed: {e}")
        with _backfill_thread_lock:
            _backfill_tasks[task_id] = {
                "task_id": task_id,
                "state": "error",
                "error": str(e),
            }
    finally:
        # Always release — endpoint held it, worker owns it now.
        try:
            power_curve.release_backfill_lock()
        except Exception:
            pass


@app.post("/api/profile/backfill-history")
def api_profile_backfill_history():
    """v1.3.0 — kicks off a one-shot ICU history backfill job.

    Single-flight per PATCH G3 — when a backfill is already running, returns
    ``{"status": "already_running", "task_id": <existing>}``. On accept,
    spawns a worker thread and returns ``{"status": "started", "task_id":
    <new>}`` immediately. Poll ``/api/profile/backfill-history/status?task_id=X``
    for progress.
    """
    import power_curve
    acquired, lock = power_curve.acquire_backfill_lock()
    if not acquired:
        return {
            "status": "already_running",
            "task_id": lock.get("task_id"),
        }
    task_id = lock.get("task_id") or uuid.uuid4().hex
    # GRILL-WAVE2A W2A-G2 fix: do NOT release the lock here. The worker
    # holds it for the full job and releases in its `finally` clause. Earlier
    # implementation released between endpoint and thread-start, opening a
    # TOCTOU window where two concurrent POSTs could each see the lock as
    # free and spawn duplicate ICU workers. Lock now released exactly once,
    # by the worker thread, when the job ends or crashes.
    with _backfill_thread_lock:
        _backfill_tasks[task_id] = {
            "task_id": task_id,
            "state": "running",
            "backfilled": 0,
            "already_cached": 0,
            "failed": 0,
        }
    t = threading.Thread(target=_run_backfill_job, args=(task_id,), daemon=True)
    t.start()
    return {"status": "started", "task_id": task_id}


@app.get("/api/profile/backfill-history/status")
def api_profile_backfill_history_status(task_id: str = Query(...)):
    """v1.3.0 — poll the in-memory state of a backfill job.

    Returns the most recent task entry (``state ∈ {running, done, error}``,
    plus counters). 404 when the task_id is unknown.
    """
    if not isinstance(task_id, str) or not task_id:
        return JSONResponse({"error": "task_id required"}, 400)
    with _backfill_thread_lock:
        entry = _backfill_tasks.get(task_id)
    if entry is None:
        return JSONResponse({"error": "unknown task_id"}, 404)
    return entry


@app.get("/api/sync/progress")
def api_sync_progress():
    """v2.2.9 — live progress of the ICU activity sync for the homepage banner.

    Returns ``{state, total, done, added, updated, ts}``. ``state`` is
    ``running`` while ``_sync_icu_activities`` is processing the ICU feed,
    ``done`` when it finished, ``idle`` before any sync this session. A
    ``running`` entry whose ``ts`` is older than 120 s is reported as ``stale``
    so a crashed/killed sync can't pin the banner open forever.
    """
    with _sync_progress_lock:
        snap = dict(_icu_sync_progress)
    if snap.get("state") == "running" and (time.time() - float(snap.get("ts") or 0)) > 120:
        snap["state"] = "stale"
    return snap


# ───────────────────────────────────────────────────────────────────────────
# v1.8.10 — DFA α1 backfill (retry pass)
# ───────────────────────────────────────────────────────────────────────────
# The ICU sync runs ``_augment_icu_record_with_dfa`` once per newly-added
# activity. Augment can silently NOT run for an activity (sync error mid-
# loop) or land in a non-sticky failure state (``fetch_failed`` /
# ``timeout`` / missing). A separate retry pass walks every ICU ride and
# attempts augment for any record whose status is not in the sticky set.
#
# Lazy compute path means even rides whose .fit ICU 404s now usually
# succeed — ICU's /streams endpoint exposes an ``hrv`` channel that
# contains the same RR data as the FIT's HrvMessage records.

_dfa_backfill_tasks: dict[str, dict] = {}
_dfa_backfill_thread_lock = threading.Lock()
_dfa_backfill_lock = threading.Lock()  # single-flight gate
_dfa_backfill_cancel: set[str] = set()  # task IDs the user asked to cancel


def _run_dfa_backfill_job(task_id: str, force: bool = False,
                          migrate: bool = False) -> None:
    """Background worker — iterate ICU rides + augment records.

    Three candidate-selection modes:
      - default: non-sticky records only (fetch_failed / timeout / missing).
      - ``force``: every record (used after a manual re-upload).
      - ``migrate`` (v1.8.14 grill B1): records that ARE ``computed`` but were
        computed by an OLDER ``dfa_algo_version`` than the current one. This
        heals the flagship case where the algorithm changed (e.g. the v3 HRVT
        fields were added) but the rides are sticky-``computed`` so neither
        sync (90-day window only) nor the default backfill would touch them.
        Only ``computed`` rides qualify — they have HRV data, so recompute is
        meaningful; ``no_rr_data`` (runs, no RR) + ``icu_deleted`` are skipped
        (recompute is wasteful and the outcome won't change). Migrate calls
        augment with ``force=True`` per record so the per-record sticky gate
        doesn't re-block it.

    Updates ``_dfa_backfill_tasks[task_id]`` in place so the polling
    endpoint can report progress without holding the worker lock.
    Respects cancellation via ``_dfa_backfill_cancel``.
    """
    import json as _json
    from datetime import datetime as _dt
    started = time.time()
    paths: list[Path] = []
    try:
        # 3.4.2 — same v3.0.0 AC2a miss as _iter_icu_dfa_rides: scan the
        # ACTIVE profile's archive, not the legacy global dir (empty since
        # the migration), which made every backfill a 0-candidate no-op —
        # and killed the progress strip on both surfaces (terminal state +
        # candidates===0 renders nothing). Resolved INSIDE the try so a
        # no-active-profile raise degrades to an honest empty pass instead
        # of crashing the worker before the lock-releasing finally below.
        from ride_storage import _icu_rides_dir as _rs_icu_rides_dir
        icu_dir = _rs_icu_rides_dir()
        if icu_dir.exists():
            paths = sorted(icu_dir.glob("*.json"))
    except Exception:
        paths = []
    paths = [p for p in paths if not p.name.startswith(".")]
    total = len(paths)
    # v2.1.2 DFA-FIX — `no_rr_data` is NO LONGER sticky. ICU's /streams `hrv`
    # channel often lags the initial sync (it's added after ICU finishes
    # processing the upload), and the per-ride augment can also time out — both
    # transiently mark a ride `no_rr_data` even though the RR-intervals ARE there
    # (confirmed: rides flagged no_rr_data return 29k RR-intervals on a later
    # fetch). Keeping it sticky meant those rides NEVER recomputed, so DFA
    # silently went blank. Now the retry pass re-augments them; a genuinely
    # RR-less ride (optical-HR run) just re-fails on one fast streams GET.
    sticky = {"computed", "sanity_rejected", "icu_deleted"}
    candidates: list[tuple[Path, str]] = []
    for p in paths:
        try:
            rec = _json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(rec, dict):
            continue
        ext = rec.get("external_id") or rec.get("id") or p.stem
        if not isinstance(ext, str) or not ext:
            continue
        status = rec.get("dfa_alpha1_status")
        if migrate:
            # Only stale-version COMPUTED rides (they have HRV → recompute
            # meaningfully adds the v3 HRVT/zone fields or fixes a v1 α1).
            if status != "computed":
                continue
            if int(rec.get("dfa_algo_version") or 0) >= _DFA_ALGO_VERSION:
                continue
        elif not force and status in sticky:
            continue
        candidates.append((p, ext))

    with _dfa_backfill_thread_lock:
        entry = _dfa_backfill_tasks.get(task_id) or {}
        entry.update({
            "task_id": task_id,
            "state": "running",
            "total": total,
            "candidates": len(candidates),
            "augmented": 0,
            "computed": 0,
            "no_rr_data": 0,
            "icu_deleted": 0,
            "failed": 0,
            "current": "",
            "started_at": _dt.now().isoformat(),
        })
        _dfa_backfill_tasks[task_id] = entry

    computed = 0
    no_rr = 0
    deleted = 0
    failed = 0
    augmented = 0
    try:
        for p, ext in candidates:
            if task_id in _dfa_backfill_cancel:
                with _dfa_backfill_thread_lock:
                    e = dict(_dfa_backfill_tasks.get(task_id) or {})
                    e["state"] = "cancelled"
                    e["finished_at"] = _dt.now().isoformat()
                    _dfa_backfill_tasks[task_id] = e
                _dfa_backfill_cancel.discard(task_id)
                return
            with _dfa_backfill_thread_lock:
                e = dict(_dfa_backfill_tasks.get(task_id) or {})
                e["current"] = ext
                _dfa_backfill_tasks[task_id] = e
            try:
                # Migrate mode forces the per-record recompute (the record is
                # sticky-``computed`` but stale-version, so the gate would
                # otherwise skip it).
                _augment_icu_record_with_dfa(p, ext, force=(force or migrate))
                # Re-read to count outcome.
                try:
                    rec2 = _json.loads(p.read_text(encoding="utf-8"))
                    new_status = (rec2 or {}).get("dfa_alpha1_status")
                except Exception:
                    new_status = None
                augmented += 1
                if new_status == "computed":
                    computed += 1
                elif new_status == "no_rr_data":
                    no_rr += 1
                elif new_status == "icu_deleted":
                    deleted += 1
                else:
                    failed += 1
            except Exception as e:
                _log.debug(f"_run_dfa_backfill_job augment({ext}) raised: {e}")
                failed += 1
            with _dfa_backfill_thread_lock:
                e = dict(_dfa_backfill_tasks.get(task_id) or {})
                e["augmented"] = augmented
                e["computed"] = computed
                e["no_rr_data"] = no_rr
                e["icu_deleted"] = deleted
                e["failed"] = failed
                _dfa_backfill_tasks[task_id] = e
    finally:
        with _dfa_backfill_thread_lock:
            e = dict(_dfa_backfill_tasks.get(task_id) or {})
            e["state"] = e.get("state") or "done"
            if e["state"] == "running":
                e["state"] = "done"
            e["finished_at"] = _dt.now().isoformat()
            e["elapsed_s"] = round(time.time() - started, 2)
            e["current"] = ""
            _dfa_backfill_tasks[task_id] = e
        # Release single-flight gate so the next backfill can start.
        try:
            _dfa_backfill_lock.release()
        except RuntimeError:
            pass


@app.post("/api/profile/dfa-backfill")
def api_profile_dfa_backfill(force: int = Query(0), migrate: int = Query(0)):
    """v1.8.10 — kick off a DFA α1 retry pass over all ICU rides.

    Single-flight: a second concurrent POST returns
    ``{"status": "already_running", "task_id": <existing>}``. With
    ``?force=1`` even sticky statuses are re-attempted (used when the
    user manually re-uploaded an activity that was previously
    ``icu_deleted``). With ``?migrate=1`` (v1.8.14) only ``computed`` rides
    stamped with an older ``dfa_algo_version`` are recomputed — the one-time
    upgrade pass that backfills the v3 HRVT/zone fields onto existing rides.

    Returns ``{"status": "started", "task_id": <uuid>}`` on accept.
    Poll progress via ``/api/profile/dfa-backfill/status?task_id=X``.
    """
    acquired = _dfa_backfill_lock.acquire(blocking=False)
    if not acquired:
        with _dfa_backfill_thread_lock:
            existing = None
            for tid, t in _dfa_backfill_tasks.items():
                if (t or {}).get("state") == "running":
                    existing = tid
                    break
        return {"status": "already_running", "task_id": existing}
    task_id = uuid.uuid4().hex
    with _dfa_backfill_thread_lock:
        _dfa_backfill_tasks[task_id] = {
            "task_id": task_id,
            "state": "running",
            "augmented": 0, "computed": 0, "no_rr_data": 0,
            "icu_deleted": 0, "failed": 0,
        }
    t = threading.Thread(
        target=_run_dfa_backfill_job,
        args=(task_id, bool(int(force or 0)), bool(int(migrate or 0))),
        daemon=True,
    )
    t.start()
    return {"status": "started", "task_id": task_id}


@app.get("/api/profile/dfa-backfill/status")
def api_profile_dfa_backfill_status(task_id: str = Query(...)):
    """v1.8.10 — poll DFA backfill task progress."""
    if not isinstance(task_id, str) or not task_id:
        return JSONResponse({"error": "task_id required"}, 400)
    with _dfa_backfill_thread_lock:
        entry = _dfa_backfill_tasks.get(task_id)
    if entry is None:
        return JSONResponse({"error": "unknown task_id"}, 404)
    return entry


@app.post("/api/profile/dfa-backfill/cancel")
def api_profile_dfa_backfill_cancel(task_id: str = Query(...)):
    """v1.8.10 — request cancellation of a running DFA backfill.

    Cooperative: the worker checks this set between rides. The currently-
    in-flight augment for one ride finishes (small window: ~1-3 s), then
    the worker exits and reports ``state: "cancelled"``.
    """
    if not isinstance(task_id, str) or not task_id:
        return JSONResponse({"error": "task_id required"}, 400)
    with _dfa_backfill_thread_lock:
        entry = _dfa_backfill_tasks.get(task_id)
    if entry is None:
        return JSONResponse({"error": "unknown task_id"}, 404)
    _dfa_backfill_cancel.add(task_id)
    return {"status": "cancel_requested", "task_id": task_id}


@app.get("/api/profile/fatigue-resistance")
def api_profile_fatigue_resistance(window_days: int = Query(365, ge=1, le=3650),
                                     kj_threshold: int = Query(1500),
                                     refresh: int = Query(0)):
    """v1.3.0 IMPL-FATIGUE — Pinot 2014 robustness index.

    Compares peak power on fresh legs (kJ-at-start < 500) versus tired legs
    (kJ-at-start ≥ ``kj_threshold``) across all cached rides in window.
    Per PATCH G5 the threshold is a 2-button toggle: ``{1500, 2000}`` only.

    Cached for 24h via the standard ``_cache``/``_cache_ts`` mechanism. Per
    PATCH G4 the cache key includes ``kj_threshold`` AND
    ``latest_ride_id_in_window`` so threshold flips invalidate AND a freshly
    imported ride invalidates automatically. Pass ``?refresh=1`` to force
    recompute.

    Falls back to ``insufficient_data`` on any exception so the dashboard
    never gets a 500.
    """
    import power_curve
    from fastapi import HTTPException
    # PATCH G5 — only 1500 and 2000 are valid thresholds. W2B-G6: prior
    # implementation silently coerced other values to 1500 → response
    # said kj_threshold:1500 but the request asked for something else.
    # Reject explicitly so the caller sees the bug instead of being lied to.
    if kj_threshold not in (1500, 2000):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "invalid_kj_threshold",
                "valid": [1500, 2000],
                "received": int(kj_threshold),
            },
        )
    _pid = _active_profile_id_or_default()
    try:
        latest = power_curve.latest_ride_id_in_window(_pid,
                                                      int(window_days))
    except Exception as e:
        _log.debug(f"fatigue_resistance latest_ride_id failed: {e}")
        latest = ""
    # v1.8.9 Bug 4 — pull current FTP for the lru_cache key. Power streams
    # don't change once written, so (latest_ride_id, current_ftp) is a safe
    # identity for the compute output.
    try:
        from profile_manager import ProfileManager as _PM
        _current_ftp = int(_PM.get().ftp or 0)
    except Exception:
        _current_ftp = 0
    cache_key = (f"fatigue_resistance_{_pid}_{int(window_days)}_"
                 f"{int(kj_threshold)}_{latest}")
    now = time.time()
    ttl = 24 * 3600
    # v1.8.9 Bug 4 — bust the lru_cache + module cache on refresh=1.
    if refresh:
        try:
            _fatigue_resistance_memoised.cache_clear()
        except Exception:
            pass
    # W2B-G5 fix: serialise concurrent same-key compute via a per-key lock
    # so two simultaneous dashboard polls don't both compute, and so
    # lazy-GC + cache-write are atomic.
    with _fatigue_resistance_cache_lock(cache_key):
        if not refresh and cache_key in _cache and \
           now - _cache_ts.get(cache_key, 0) < ttl:
            cached_result = dict(_cache[cache_key])
            # v1.8.9 Bug 4 — surface a warm `compute_ms` so the UI can
            # show "Loaded from cache" speed. Warm path is effectively 0ms.
            cached_result["compute_ms"] = 0
            return cached_result
        compute_t0 = time.time()
        try:
            # v1.8.9 Bug 4 — route through lru_cache wrapper. Repeated
            # identical (latest, ftp, window, threshold) tuples return
            # the memoised value in microseconds.
            result = _fatigue_resistance_memoised(
                latest, int(_current_ftp),
                int(window_days), int(kj_threshold),
            )
            # The lru_cache may return the same dict instance across calls,
            # so we deepcopy before mutating with per-request fields below.
            import copy as _copy_for_fr
            result = _copy_for_fr.deepcopy(result)
        except Exception as e:
            _log.warning(f"api_profile_fatigue_resistance failed: {e}")
            result = {
                "window_days": int(window_days),
                "n_long_rides": 0,
                "n_long_rides_with_streams": 0,
                "fit_status": "insufficient_data",
                "reason": "compute_failed",
                "kj_threshold": int(kj_threshold),
                "robustness_score": None,
                "by_duration": [],
                "scatter": [],
            }
        compute_ms = int(round((time.time() - compute_t0) * 1000))
        result["compute_ms"] = compute_ms
        # v1.8.8 Bug 5 — when long rides exist but no power streams have
        # been cached, kick off the background backfill once and surface
        # the state to the UI so it can show a "Backfilling…" placeholder
        # and poll. Adds ADD-only fields:
        #   - power_streams_cached_pct: int    coverage %, 0-100
        #   - auto_backfill_triggered: bool    true when this call started one
        try:
            n_long = int(result.get("n_long_rides") or 0)
        except (TypeError, ValueError):
            n_long = 0
        try:
            n_with_streams = int(result.get("n_long_rides_with_streams") or 0)
        except (TypeError, ValueError):
            n_with_streams = 0
        try:
            n_unfetchable = int(result.get("n_long_rides_unfetchable") or 0)
        except (TypeError, ValueError):
            n_unfetchable = 0
        # 3.4.1 ② — terminally-unfetchable rides (no power data on
        # intervals.icu, marker persisted by the backfill) count toward
        # DONE: the pct now reaches an honest 100 instead of asymptoting
        # at e.g. 93 forever while the frontend re-polled + re-kicked the
        # backfill for rides that can never hydrate. `n_unfetchable` is
        # ADD-only for the UI's "N rides have no power data" note.
        pct = (int(round(100.0 * min(n_long, n_with_streams + n_unfetchable)
                         / n_long))
               if n_long > 0 else 0)
        result["power_streams_cached_pct"] = pct
        result["n_unfetchable"] = n_unfetchable
        auto_triggered = False
        if n_with_streams == 0 and n_long >= 1 and pct < 100:
            # Fire-and-forget: try acquiring the single-flight backfill lock
            # and spawn the worker. Already-running case is fine — we just
            # report the existing task implicitly via the lock.
            try:
                acquired, lock = power_curve.acquire_backfill_lock()
                if acquired:
                    task_id = lock.get("task_id") or uuid.uuid4().hex
                    with _backfill_thread_lock:
                        _backfill_tasks[task_id] = {
                            "task_id": task_id,
                            "state": "running",
                            "backfilled": 0,
                            "already_cached": 0,
                            "failed": 0,
                        }
                    t = threading.Thread(
                        target=_run_backfill_job,
                        args=(task_id,),
                        daemon=True,
                    )
                    t.start()
                    auto_triggered = True
                    _log.info(
                        "fatigue_resistance: auto-triggered backfill "
                        "n_long=%d streams=0 task_id=%s",
                        n_long, task_id,
                    )
            except Exception as e:  # noqa: BLE001
                _log.debug(f"fatigue_resistance auto-backfill kick failed: {e}")
        result["auto_backfill_triggered"] = auto_triggered
        # Lazy GC — prune stale fatigue_resistance_* entries with old keys
        # for the same (window_days, kj_threshold) pair. W2B-G5: skip GC
        # when latest=="" so a transient latest-ride-lookup failure doesn't
        # nuke a perfectly good cached result.
        if latest:
            prefix = (f"fatigue_resistance_{_pid}_{int(window_days)}_"
                      f"{int(kj_threshold)}_")
            for k in list(_cache.keys()):
                if k.startswith(prefix) and k != cache_key:
                    _cache.pop(k, None)
                    _cache_ts.pop(k, None)
        _cache[cache_key] = result
        _cache_ts[cache_key] = now
        return result


@app.get("/api/profile/dfa-alpha1")
def api_profile_dfa_alpha1():
    """v1.8.9 Bug 7 (master §7) — DFA α1 snapshot for the homepage.

    Surfaces the most recent rides' ``summary.dfa_alpha1_avg`` value(s)
    so the homepage snapshot card can render a readable line without
    a 404 dance.

    v1.8.10 Bug B — also scans the profile's ``rides/icu/*.json`` so ICU
    rides with computed DFA aren't ignored (the previous version read
    only ``~/.domestique/profiles/<id>/rides/`` via ride_storage.list_rides,
    which misses every ICU-synced ride and therefore the entire DFA
    history for any user with intervals.icu sync enabled).

    Returns 200 always:

      Has HRV data (≥1 ride with ``dfa_alpha1_avg``):
        ``{"value": float, "n_rides": int, "last_computed_at": str,
           "n_recent_total": int, "n_no_rr_data": int,
           "n_fetch_failed": int,
           "message": "DFA α1 X.XX — last computed YYYY-MM-DD"}``

      No HRV-tagged rides yet (some rides exist but none computed):
        ``{"value": null, "n_rides": 0, "last_computed_at": null,
           "n_recent_total": int, "n_no_rr_data": int,
           "n_fetch_failed": int,
           "message": "No recent HRV rides — <diagnostic>"}``

    Locked field names per master_v189 §10 + v1810 §2 extension.
    """
    try:
        info = _recent_dfa_diagnostic()
    except Exception as e:
        _log.debug(f"api_profile_dfa_alpha1 lookup failed: {e}")
        info = {
            "values": [], "last_computed_at": None,
            "n_recent_total": 0, "n_no_rr_data": 0,
            "n_fetch_failed": 0,
        }
    dfa_vals = info.get("values") or []
    n = len(dfa_vals)
    n_recent_total = int(info.get("n_recent_total") or 0)
    n_no_rr_data = int(info.get("n_no_rr_data") or 0)
    n_fetch_failed = int(info.get("n_fetch_failed") or 0)
    last_computed_at = info.get("last_computed_at")
    if n == 0:
        # Build a diagnostic message so the UI can show users WHY DFA is
        # empty instead of the same flat "no HRV rides" string forever.
        if n_recent_total == 0:
            diag = "no rides indexed yet"
        else:
            parts = []
            if n_no_rr_data > 0:
                parts.append(
                    f"{n_no_rr_data} had no RR data (head unit didn't record HRV)"
                )
            if n_fetch_failed > 0:
                parts.append(
                    f"{n_fetch_failed} ICU couldn't deliver the FIT"
                )
            diag = "; ".join(parts) if parts else "no DFA computed yet"
        return {
            "value": None,
            "n_rides": 0,
            "last_computed_at": None,
            "n_recent_total": n_recent_total,
            "n_no_rr_data": n_no_rr_data,
            "n_fetch_failed": n_fetch_failed,
            "message": f"No recent HRV rides — {diag}",
        }
    avg = sum(dfa_vals) / float(n)
    if last_computed_at:
        msg = (f"DFA α1 {avg:.2f} — last computed "
               f"{str(last_computed_at)[:10]}")
    else:
        msg = (f"DFA α1 {avg:.2f} over last {n} ride"
               f"{'s' if n != 1 else ''}")
    return {
        "value": round(float(avg), 2),
        "n_rides": int(n),
        "last_computed_at": last_computed_at,
        "n_recent_total": n_recent_total,
        "n_no_rr_data": n_no_rr_data,
        "n_fetch_failed": n_fetch_failed,
        "message": msg,
    }


@app.get("/api/profile/dfa-rides")
def api_profile_dfa_rides():
    """v1.8.14 — all HRV-bearing rides for the DFA α1 tab, plus a recent
    median HRVT1/HRVT2 aggregate. Scan-on-read over stored envelopes; NO
    stream re-fetch (grill D3). Per-ride rows carry the stored HRVT dicts
    UNCHANGED (grill B2). Aggregate is per-channel, r²-gated, null-safe.

    Locked shape (MASTER_DECISIONS_v1814 §4 + §10):
      { rides:[{id,date,name,duration_min,avg_hr,alpha1_avg,lt1_minutes,
                status,zone_minutes,hrvt1,hrvt2,algo_version}],
        aggregate:{hrvt1:{hr,n_hr,r2_hr_median,power,n_power,r2_power_median,
                          date_span}|null, hrvt2:{...}|null, n_window},
        n_total, n_computed, n_stale_version }
    """
    import statistics as _stats
    from datetime import datetime as _dt, timedelta as _td
    try:
        from analytics import DFA_AGG_MIN_R2 as _AGG_R2
    except Exception:
        _AGG_R2 = 0.50
    # v2.4.1 — exclude clearly-drifting rides from the HRVT aggregate. DFA α1 is
    # a fatigue/durability marker (Rogers 2022 PMID 35615679; 2025 PMID 39904800):
    # a ride with high aerobic decoupling has a non-stationary α1, so its HRVT is
    # biased low. Well-coupled is <5% (Friel); 10% is a conservative "drifting" cut
    # (only applied when decoupling_pct is present — missing data is kept).
    _AGG_MAX_DECOUPLING = 10.0

    try:
        raw = _iter_icu_dfa_rides()
    except Exception as e:
        _log.debug(f"api_profile_dfa_rides scan failed: {e}")
        raw = []

    rides = []
    n_computed = 0
    n_stale = 0
    for d in raw:
        status = d.get("dfa_alpha1_status")
        a1 = (d.get("summary") or {}).get("dfa_alpha1_avg")
        if status == "computed":
            n_computed += 1
            if int(d.get("dfa_algo_version") or 0) < _DFA_ALGO_VERSION:
                n_stale += 1
        # A ride with no α1 still belongs in the table: 15 of one tester's 46
        # rides carried no beat-to-beat data at all (head unit not recording
        # RR), and omitting them read as "many rides not indexed" — an error
        # where none existed. Surface them with the reason instead.
        if a1 is None and status not in ("no_rr_data", "sanity_rejected"):
            continue
        # "not detected" with no reason reads as failure. The reason is
        # computable from what is already stored: a threshold only resolves
        # when the ride's α1 series actually CROSSES the target (no
        # extrapolation, r²-gated) — so say which of those failed.
        def _hrvt_note(target_alpha):
            series = d.get("dfa_alpha1_series")
            if not isinstance(series, list) or not series:
                return None
            alphas = [w.get("alpha1") for w in series
                      if isinstance(w, dict) and w.get("alpha1") is not None]
            if len(alphas) < 8:
                return "too few clean windows"
            if min(alphas) > target_alpha:
                return "no crossing — ride stayed easier than this threshold"
            if max(alphas) < target_alpha:
                return "no crossing — whole ride harder than this threshold"
            return "crossing too noisy to fit (r² gate)"
        moving = d.get("moving_s") or d.get("duration_s") or 0
        rides.append({
            "id": d.get("id"),
            "date": str(d.get("started_at") or "")[:10],  # naive ISO, no shift
            "name": d.get("name"),
            "duration_min": round(float(moving) / 60.0) if moving else None,
            "avg_hr": d.get("avg_hr"),
            "alpha1_avg": a1,
            "lt1_minutes": d.get("dfa_alpha1_lt1_minutes"),
            "status": status,
            "zone_minutes": d.get("dfa_zone_minutes"),
            "hrvt1": d.get("dfa_hrvt1"),   # stored dict, passed through unchanged
            "hrvt2": d.get("dfa_hrvt2"),
            "hrvt1_note": ("no beat-to-beat data from the recording device"
                           if status == "no_rr_data" else
                           None if d.get("dfa_hrvt1") else _hrvt_note(0.75)),
            "hrvt2_note": ("no beat-to-beat data from the recording device"
                           if status == "no_rr_data" else
                           None if d.get("dfa_hrvt2") else _hrvt_note(0.50)),
            "no_rr": status in ("no_rr_data", "sanity_rejected"),
            "algo_version": d.get("dfa_algo_version"),
            # v2.4.1 — whole-ride aerobic decoupling; high drift means a
            # non-stationary α1, so such rides are dropped from the HRVT
            # aggregate (Rogers 2025 durability, PMID 39904800).
            "decoupling_pct": d.get("decoupling_pct"),
        })

    # Newest first (date string sorts lexically for ISO).
    rides.sort(key=lambda r: r.get("date") or "", reverse=True)

    # ── Aggregate: rides in the last 42 days that resolved a threshold;
    #    fall back to the last 8 resolved if <3 in the window. r²-gated.
    def _resolved(row, key, channel):
        dc = row.get("decoupling_pct")
        if dc is not None and dc > _AGG_MAX_DECOUPLING:
            return None  # drifting ride — α1 non-stationary, HRVT biased low
        t = row.get(key)
        if not isinstance(t, dict):
            return None
        load = t.get(channel)
        r2 = t.get("r2_hr" if channel == "hr" else "r2_power")
        if load is None or r2 is None or r2 < _AGG_R2:
            return None
        return (load, r2, row.get("date") or "")

    def _weighted_median(pairs):
        """Median weighted by fit confidence (r²): higher-r² rides pull more,
        while staying robust to outliers at small n. pairs = [(value, weight)]."""
        pairs = sorted((p for p in pairs if p[1] and p[1] > 0), key=lambda p: p[0])
        if not pairs:
            return None
        half = sum(w for _, w in pairs) / 2.0
        acc = 0.0
        for v, w in pairs:
            acc += w
            if acc >= half:
                return v
        return pairs[-1][0]

    def _aggregate(key):
        # Collect resolved per channel from the date-windowed set.
        try:
            cutoff = (_dt.now() - _td(days=42)).date().isoformat()
        except Exception:
            cutoff = ""
        recent = [r for r in rides if (r.get("date") or "") >= cutoff]
        # Need ≥3 resolved (either channel) in window, else last-8 resolved.
        def _has_any(row):
            return _resolved(row, key, "hr") or _resolved(row, key, "power")
        windowed = [r for r in recent if _has_any(r)]
        if len(windowed) < 3:
            windowed = [r for r in rides if _has_any(r)][:8]
        if not windowed:
            return None
        out = {}
        dates = []
        for channel, n_k, med_k, r2_k in (
            ("hr", "n_hr", "hr", "r2_hr_median"),
            ("power", "n_power", "power", "r2_power_median"),
        ):
            vals, r2s = [], []
            for row in windowed:
                res = _resolved(row, key, channel)
                if res:
                    vals.append(res[0]); r2s.append(res[1]); dates.append(res[2])
            if vals:
                wm = _weighted_median(list(zip(vals, r2s)))
                out[med_k] = round(wm if wm is not None else _stats.median(vals), 1)
                out[n_k] = len(vals)
                out[("r2_hr_median" if channel == "hr" else "r2_power_median")] = round(_stats.median(r2s), 3)
            else:
                out[med_k] = None
                out[n_k] = 0
                out[("r2_hr_median" if channel == "hr" else "r2_power_median")] = None
        if out.get("n_hr", 0) == 0 and out.get("n_power", 0) == 0:
            return None
        ds = sorted(d for d in dates if d)
        out["date_span"] = [ds[0], ds[-1]] if ds else None
        return out

    n_excl_dc = sum(
        1 for r in rides
        if (r.get("decoupling_pct") is not None
            and r["decoupling_pct"] > _AGG_MAX_DECOUPLING)
        and (isinstance(r.get("hrvt1"), dict) or isinstance(r.get("hrvt2"), dict))
    )
    return {
        "rides": rides,
        "aggregate": {
            "hrvt1": _aggregate("hrvt1"),
            "hrvt2": _aggregate("hrvt2"),
            "n_window": 8,
            "agg_min_r2": _AGG_R2,
            "agg_max_decoupling": _AGG_MAX_DECOUPLING,
            "n_excluded_decoupling": n_excl_dc,
        },
        "n_total": len(raw),
        "n_computed": n_computed,
        "n_stale_version": n_stale,
    }


# ── P2.4 (v3.0.0, G14-G20) — Rider Profile stats card ───────────────────────
# ONE aggregation endpoint over EXISTING sources. Every field is a provenance
# TRIPLE {value, source: icu|manual|derived|fallback, source_date|null} —
# never a bare "est." tag (G-locked). Fallback-valued fields (wprime/pmax
# source ∈ {"", fallback}; cp with no persisted key AND power-curve cp_w None;
# lbm default) emit value=None → the card renders an empty-state, never a
# fabricated number. The ONLY new computations are the EF 42d trend + the
# season-totals loop (G14). Each subsystem fan-out is wrapped in try/except so
# a failing subsystem yields a PARTIAL payload, never a 500 (get_today_metrics
# does live ICU calls).

def _prov(value, source: str, source_date=None) -> dict:
    """Provenance triple. source ∈ {icu, manual, derived, fallback}."""
    return {"value": value, "source": source, "source_date": source_date}


_PROV_EMPTY = {"value": None, "source": "fallback", "source_date": None}


def _prov_source(raw) -> str:
    """Map internal provenance enums onto the locked 4-value vocabulary."""
    s = str(raw or "").strip().lower()
    if s in ("icu", "eftp_icu", "eftp_auto", "intervals.icu"):
        return "icu"
    if s in ("eftp_local", "monod", "computed", "derived"):
        return "derived"
    if s in ("", "fallback", "unknown"):
        return "fallback"
    return "manual"  # manual / tested_* enums


def _rider_stats_season_block(rides: list, start_iso: str, end_iso: str) -> dict:
    """Season totals over the ride archive (G14 sanctioned computation #2).

    Distance sums ICU rides only (the FIT sidecar carries no distance) —
    labeled as such in the payload. hrTSS-sourced loads count toward TSS and
    are surfaced via ``hr_loads`` so the card can label them.
    """
    hours = 0.0
    tss = 0.0
    dist = 0.0
    count = 0
    hr_loads = 0
    for r in rides:
        if not isinstance(r, dict):
            continue
        d = (r.get("date") or str(r.get("started_at") or ""))[:10]
        if not d or not (start_iso <= d <= end_iso):
            continue
        count += 1
        try:
            hours += float(r.get("duration_s") or 0) / 3600.0
        except (TypeError, ValueError):
            pass
        try:
            tss += float(r.get("tss") or 0)
        except (TypeError, ValueError):
            pass
        if r.get("load_source") == "hr_icu":
            hr_loads += 1
        if r.get("source") == "icu":
            try:
                dist += float(r.get("distance_km") or 0)
            except (TypeError, ValueError):
                pass
    return {"hours": round(hours, 1), "tss": round(tss), "rides": count,
            "distance_km": round(dist), "hr_loads": hr_loads}


@app.get("/api/rider-stats")
def api_rider_stats():
    """P2.4 — read-only stats grid for the top of the Analysis tab."""
    out: dict = {"errors": []}

    def _fail(section: str, e: Exception):
        _log.debug(f"/api/rider-stats {section} failed: {e}")
        out["errors"].append(section)

    athlete: dict = {}
    pm = None
    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        athlete = getattr(pm, "_athlete", {}) or {}
    except Exception as e:
        _fail("profile", e)

    # ── power-curve subsystem (peaks + Monod CP fallback) ───────────────────
    curve: dict = {}
    try:
        import power_curve
        curve = power_curve.aggregate_power_curve(
            _active_profile_id_or_default(), 90) or {}
    except Exception as e:
        _fail("power_curve", e)

    # ── power section ────────────────────────────────────────────────────────
    power: dict = {}
    try:
        ftp_set = bool(athlete.get("ftp"))
        ftp_source_raw = None
        ftp_date = None
        if pm is not None:
            try:
                ftp_source_raw = pm.get_ftp_source()
            except Exception:
                ftp_source_raw = athlete.get("ftp_source")
            try:
                ftp_date = pm.get_ftp_source_date()
            except Exception:
                ftp_date = None
        power["ftp"] = (
            _prov(int(athlete["ftp"]), _prov_source(ftp_source_raw or "manual"),
                  ftp_date)
            if ftp_set else dict(_PROV_EMPTY)
        )

        # eFTP — latest ICU wellness sportInfo[0].eftp (synced store).
        eftp_triple = dict(_PROV_EMPTY)
        try:
            import ride_storage as _rs
            wl = _rs.load_recent_wellness(days=90) or []
            for w in sorted(wl, key=lambda x: str(x.get("id") or ""),
                            reverse=True):
                si = w.get("sportInfo") or []
                ef = si[0].get("eftp") if si and isinstance(si[0], dict) else None
                if ef:
                    eftp_triple = _prov(round(float(ef)), "icu",
                                        str(w.get("id") or "")[:10] or None)
                    break
        except Exception as e:
            _fail("eftp", e)
        power["eftp"] = eftp_triple

        weight_set = bool(athlete.get("weight_kg"))
        if ftp_set and weight_set:
            try:
                wkg = round(float(athlete["ftp"]) / float(athlete["weight_kg"]), 2)
                power["w_per_kg"] = _prov(wkg, "derived", ftp_date)
            except (TypeError, ValueError, ZeroDivisionError):
                power["w_per_kg"] = dict(_PROV_EMPTY)
        else:
            power["w_per_kg"] = dict(_PROV_EMPTY)

        # CP: persisted athlete key (manual) → power-curve Monod fit
        # (derived) → empty-state (keys on curve cp_w None, per the grill).
        if athlete.get("cp"):
            power["cp"] = _prov(int(athlete["cp"]), "manual", None)
        elif curve.get("cp_w") is not None:
            power["cp"] = _prov(int(curve["cp_w"]), "derived", None)
        else:
            power["cp"] = dict(_PROV_EMPTY)

        # W' (kJ) / Pmax: fallback-source values render EMPTY (never the
        # ftp×80 / ftp×1.30 property fallbacks).
        wp_src = str(athlete.get("wprime_source") or "")
        if athlete.get("wprime_j") and wp_src not in ("", "fallback"):
            power["wprime_kj"] = _prov(
                round(float(athlete["wprime_j"]) / 1000.0, 1),
                _prov_source(wp_src), None)
        else:
            power["wprime_kj"] = dict(_PROV_EMPTY)
        pm_src = str(athlete.get("pmax_source") or "")
        if athlete.get("pmax_w") and pm_src not in ("", "fallback"):
            power["pmax"] = _prov(int(athlete["pmax_w"]), _prov_source(pm_src),
                                  None)
        else:
            power["pmax"] = dict(_PROV_EMPTY)
    except Exception as e:
        _fail("power", e)
    out["power"] = power

    # ── peak efforts 5s/1m/5m/20m (90d window, from the existing curve) ─────
    try:
        by_dur = {pt.get("duration_s"): pt
                  for pt in (curve.get("rider_curve") or [])
                  if isinstance(pt, dict)}
        peaks = {"window_days": 90}
        for label, secs in (("5s", 5), ("1m", 60), ("5m", 300), ("20m", 1200)):
            pt = by_dur.get(secs)
            peaks[label] = (
                {"watts": pt.get("watts"), "watts_per_kg": pt.get("watts_per_kg"),
                 "date": pt.get("date"), "source": "derived"}
                if pt else None
            )
        out["peaks"] = peaks
    except Exception as e:
        _fail("peaks", e)

    # ── VO2max — ICU-ONLY (athlete_metrics metric=vo2max); omit if absent ───
    try:
        rows = db.query_metric_history("vo2max", days=3650)
        if rows:
            last = rows[-1]
            out["vo2max"] = {
                **_prov(last.get("value"), "icu", last.get("date")),
                "label": "via Intervals.icu",
            }
    except Exception as e:
        _fail("vo2max", e)

    # ── heart section ────────────────────────────────────────────────────────
    heart: dict = {}
    try:
        lthr_ok = False
        try:
            v = athlete.get("lthr")
            lthr_ok = v is not None and 100 <= float(v) <= 220
        except (TypeError, ValueError):
            lthr_ok = False
        heart["lthr"] = (
            _prov(int(athlete["lthr"]),
                  _prov_source(athlete.get("lthr_source") or "manual"),
                  athlete.get("lthr_source_date"))
            if lthr_ok else dict(_PROV_EMPTY)
        )
        if athlete.get("max_hr"):
            heart["max_hr"] = _prov(
                int(athlete["max_hr"]),
                _prov_source(athlete.get("max_hr_source") or "manual"), None)
        elif athlete.get("age") and pm is not None:
            heart["max_hr"] = _prov(int(pm.max_hr), "derived", None)
        else:
            heart["max_hr"] = dict(_PROV_EMPTY)
        # RHR: manual baseline → latest wellness restingHR (fallback chain).
        if athlete.get("rhr_baseline"):
            heart["rhr"] = _prov(int(athlete["rhr_baseline"]), "manual", None)
        else:
            heart["rhr"] = dict(_PROV_EMPTY)
            try:
                import ride_storage as _rs
                wl = _rs.load_recent_wellness(days=28) or []
                for w in sorted(wl, key=lambda x: str(x.get("id") or ""),
                                reverse=True):
                    if w.get("restingHR"):
                        heart["rhr"] = _prov(int(w["restingHR"]), "icu",
                                             str(w.get("id") or "")[:10] or None)
                        break
            except Exception as e:
                _fail("rhr_wellness", e)
    except Exception as e:
        _fail("heart", e)

    # DFA-AeT — the dfa-rides 42d hrvt1 aggregate (existing endpoint helper).
    try:
        dfa = api_profile_dfa_rides()
        agg = ((dfa or {}).get("aggregate") or {}).get("hrvt1")
        if isinstance(agg, dict) and (agg.get("hr") or agg.get("power")):
            span = agg.get("date_span") or [None, None]
            heart["dfa_aet"] = {
                **_prov(agg.get("hr"), "derived",
                        span[1] if len(span) > 1 else None),
                "watts": agg.get("power"),
                "n": (agg.get("n_hr") or 0) + (agg.get("n_power") or 0),
                "label": "DFA α1 HRVT1, 42d aggregate",
            }
        else:
            heart["dfa_aet"] = dict(_PROV_EMPTY)
    except Exception as e:
        _fail("dfa_aet", e)
        heart["dfa_aet"] = dict(_PROV_EMPTY)
    out["heart"] = heart

    # ── efficiency: EF (NP/avgHR) latest + 42d trend (G14 computation #1),
    #    decoupling latest ──────────────────────────────────────────────────
    efficiency: dict = {}
    rides_all: list = []
    try:
        rides_all = _load_all_rides_safe()
    except Exception as e:
        _fail("rides", e)
    try:
        today = clock.today()
        ef_rows = []  # (date_iso, ef)
        for r in rides_all:
            if not isinstance(r, dict):
                continue
            np_w, avg_hr = r.get("np_w"), r.get("avg_hr")
            if not np_w or not avg_hr:
                continue
            d = (r.get("date") or str(r.get("started_at") or ""))[:10]
            if not d:
                continue
            try:
                ef_rows.append((d, round(float(np_w) / float(avg_hr), 2)))
            except (TypeError, ValueError, ZeroDivisionError):
                continue
        ef_rows.sort()
        if ef_rows:
            last_d, last_ef = ef_rows[-1]
            cut_recent = (today - timedelta(days=21)).isoformat()
            cut_old = (today - timedelta(days=42)).isoformat()
            recent = [ef for d, ef in ef_rows if d >= cut_recent]
            prior = [ef for d, ef in ef_rows if cut_old <= d < cut_recent]
            trend = None
            trend_pct = None
            if len(recent) >= 2 and len(prior) >= 2:
                mr = sum(recent) / len(recent)
                mp = sum(prior) / len(prior)
                if mp > 0:
                    trend_pct = round((mr - mp) / mp * 100, 1)
                    trend = ("up" if trend_pct > 2.0
                             else "down" if trend_pct < -2.0 else "flat")
            efficiency["ef"] = {**_prov(last_ef, "derived", last_d),
                                "trend": trend, "trend_pct": trend_pct,
                                "window_days": 42}
        else:
            efficiency["ef"] = dict(_PROV_EMPTY)
        dec = None
        for r in rides_all:  # newest-first from load_all_rides
            if isinstance(r, dict) and r.get("decoupling_pct") is not None:
                d = (r.get("date") or str(r.get("started_at") or ""))[:10]
                dec = _prov(round(float(r["decoupling_pct"]), 1), "icu", d or None)
                break
        efficiency["decoupling"] = dec if dec else dict(_PROV_EMPTY)
    except Exception as e:
        _fail("efficiency", e)
    out["efficiency"] = efficiency

    # ── training load: CTL/ATL/TSB via get_today_metrics + local fallback ───
    load: dict = {}
    try:
        merged = _merge_training_load(cached("training", get_today_metrics))
        src = "icu" if merged.get("source") in ("icu", "icu_cached") else "fallback"
        as_of = merged.get("as_of") or clock.today().isoformat()
        for k in ("ctl", "atl", "tsb"):
            v = merged.get(k)
            load[k] = (_prov(round(float(v), 1), src, as_of)
                       if v is not None else dict(_PROV_EMPTY))
    except Exception as e:
        _fail("load", e)
    out["load"] = load

    # ── body section ─────────────────────────────────────────────────────────
    body: dict = {}
    try:
        body["weight_kg"] = (
            _prov(round(float(athlete["weight_kg"]), 1), "manual", None)
            if athlete.get("weight_kg") else dict(_PROV_EMPTY)
        )
        # LBM: default-valued profiles render empty-state (locked).
        body["lbm_kg"] = (
            _prov(round(float(athlete["lbm_kg"]), 1), "manual", None)
            if athlete.get("lbm_kg") and float(athlete["lbm_kg"]) != 56.0
            else dict(_PROV_EMPTY)
        )
    except Exception as e:
        _fail("body", e)
    out["body"] = body

    # ── season totals (G14 computation #2) ──────────────────────────────────
    try:
        today = clock.today()
        out["season"] = {
            "year": _rider_stats_season_block(
                rides_all, date(today.year, 1, 1).isoformat(),
                today.isoformat()),
            "rolling_365": _rider_stats_season_block(
                rides_all, (today - timedelta(days=365)).isoformat(),
                today.isoformat()),
            "distance_label": "(ICU rides only)",
        }
    except Exception as e:
        _fail("season", e)

    return out


@app.post("/api/activity/{activity_id}/race")
def api_activity_set_race(activity_id: str, body: dict):
    """v1.0.7 IMPL-TAU-FIT-WIRING — toggle the is_race flag on an activity.

    Body: ``{"is_race": bool}``. Returns 204 on success, 404 when the
    activity doesn't exist in the local SQLite ``activities`` table.

    The is_race column is added by ``db.init_db()`` (PATCH G11). Race-tagged
    activities are weighted higher in ``tau_fitting.count_weighted_markers``
    and the τ-fit recomputes on the next ``GET /api/profile/tau-fits`` hit.
    """
    if not isinstance(activity_id, str) or not activity_id or len(activity_id) > 80:
        return JSONResponse({"error": "bad activity_id"}, status_code=400)
    if not re.match(r"^[\w\-]+$", activity_id):
        return JSONResponse({"error": "bad activity_id"}, status_code=400)
    is_race = bool(body.get("is_race", False))
    try:
        existed = db._set_is_race(activity_id, is_race)
    except Exception as e:
        _log.warning("api_activity_set_race failed: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)
    if not existed:
        return JSONResponse({"error": "activity not found"}, status_code=404)
    return Response(status_code=204)


@app.get("/profile-picker", response_class=HTMLResponse)
def profile_picker_page(request: Request):
    return templates.TemplateResponse(request=request, name="profile_picker.html")


@app.get("/profile-setup", response_class=HTMLResponse)
def profile_setup_page(request: Request):
    return templates.TemplateResponse(request=request, name="profile_setup.html")


# ═══════════════════════════════════════════════════════════════════════════════
# CORE APIs
# ═══════════════════════════════════════════════════════════════════════════════

def _iter_icu_dfa_rides() -> list[dict]:
    """v1.8.10 Bug B — yield wrapped ICU ride records for DFA scanning.

    ICU sync writes flat envelopes to ``<profile>/rides/icu/*.json``
    (dfa_alpha1_avg, dfa_alpha1_status, rr_intervals_count at the top
    level — no ``summary`` nesting). ``ride_storage.list_rides()`` reads
    the FIT-import dir only, so any DFA scan based on it misses ICU
    rides entirely. We wrap each ICU envelope to mimic the FIT shape
    expected by callers: ``{id, started_at, summary{...}, ...}``.
    """
    import json as _json
    out: list[dict] = []
    try:
        # 3.4.2 — the v3.0.0 AC2a migration moved the ICU envelope archive to
        # <profile>/rides/icu/, but this reader kept scanning the legacy
        # global ~/.domestique/rides/icu/ (empty post-migration), so every
        # DFA surface (home card, DFA tab, readiness DFA cap) went silently
        # blank. Resolve through ride_storage._icu_rides_dir — the single
        # source of truth the sync WRITER already uses. A no-active-profile
        # RuntimeError is swallowed by the enclosing except → empty list,
        # same degraded shape as before.
        from ride_storage import _icu_rides_dir as _rs_icu_rides_dir
        icu_dir = _rs_icu_rides_dir()
        if not icu_dir.exists():
            return out
        for f in sorted(icu_dir.glob("*.json")):
            if f.name.startswith("."):
                continue
            try:
                d = _json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(d, dict):
                continue
            out.append({
                "id": d.get("id") or f.stem,
                "started_at": (d.get("started_at")
                               or d.get("local_start_date")
                               or ""),
                "summary": {
                    "dfa_alpha1_avg": d.get("dfa_alpha1_avg"),
                    "decoupling_pct": d.get("decoupling_pct"),
                },
                "dfa_alpha1_status": d.get("dfa_alpha1_status"),
                "rr_intervals_count": d.get("rr_intervals_count"),
                # v1.8.14 — fields for the DFA α1 tab (None on older records
                # until the migrate backfill recomputes them under algo v3).
                "name": d.get("name"),
                "moving_s": d.get("moving_s"),
                "duration_s": d.get("duration_s"),
                "avg_hr": d.get("avg_hr"),
                "dfa_alpha1_lt1_minutes": d.get("dfa_alpha1_lt1_minutes"),
                "dfa_hrvt1": d.get("dfa_hrvt1"),
                "dfa_hrvt2": d.get("dfa_hrvt2"),
                # The per-ride table derives its "why not detected" note from
                # the window series (crossed? stayed easy? too noisy?); without
                # this the note silently degrades to the bare "not detected"
                # this projection was starving it into.
                "dfa_alpha1_series": d.get("dfa_alpha1_series"),
                "dfa_zone_minutes": d.get("dfa_zone_minutes"),
                "dfa_algo_version": d.get("dfa_algo_version"),
            })
    except Exception:
        pass
    return out


def _recent_dfa_and_decoupling() -> tuple[list[float], float | None, str | None, str | None]:
    """v2.2.6 perf — memoised wrapper around the archive scan below.

    The scan walks the FIT + ICU ride archives (~500ms on a large data dir) and
    runs on EVERY /api/readiness AND /api/today-session. Cache it in the shared
    5-min store (cleared by clear_cache() on sync / FIT import) so a warm
    home-page load doesn't pay it once per endpoint. The inner fn never raises
    (returns the empty 4-tuple), so cached()'s dict error-path can't fire.
    """
    return cached("recent_dfa_decoupling", _recent_dfa_and_decoupling_uncached)


def _recent_dfa_and_decoupling_uncached() -> tuple[list[float], float | None, str | None, str | None]:
    """F1/F2 (v4.1.0) — pull last 3 rides' DFA α1 + most recent decoupling %
    from the local ride archive. Returns ([], None) gracefully when empty.

    Reads ``summary.dfa_alpha1_avg`` + ``summary.decoupling_pct`` written by
    ride_storage._build_summary_dict. Newest first. Missing/None values
    skipped; a ride with no HRM/HR stream just doesn't contribute a sample.

    v1.8.10 Bug B — also pulls ICU-side records (flat envelope) so the
    homepage card sees DFA from ICU-synced rides, not just FIT imports.
    """
    rides: list[dict] = []
    try:
        import ride_storage
        rides.extend(ride_storage.list_rides())
    except Exception:
        pass
    try:
        rides.extend(_iter_icu_dfa_rides())
    except Exception:
        pass
    # Dedup by id, prefer the entry with non-null dfa_alpha1_avg.
    by_id: dict[str, dict] = {}
    for r in rides:
        rid = r.get("id") or ""
        if not rid:
            continue
        existing = by_id.get(rid)
        if existing is None:
            by_id[rid] = r
            continue
        cur_alpha = (existing.get("summary") or {}).get("dfa_alpha1_avg")
        new_alpha = (r.get("summary") or {}).get("dfa_alpha1_avg")
        if not isinstance(cur_alpha, (int, float)) and \
           isinstance(new_alpha, (int, float)):
            by_id[rid] = r
    merged = sorted(
        by_id.values(),
        key=lambda r: r.get("started_at") or "",
        reverse=True,
    )
    dfa_vals: list[float] = []
    last_dec: float | None = None
    last_dec_date: str | None = None    # started_at[:10] of the decoupling source ride
    newest_dfa_date: str | None = None  # started_at[:10] of the newest DFA-contributing ride
    for r in merged:
        summary = r.get("summary") or {}
        if last_dec is None:
            dec = summary.get("decoupling_pct")
            if isinstance(dec, (int, float)):
                last_dec = float(dec)
                # v1.8.15 — capture WHICH ride supplied the advisory so the
                # readiness banner names the real day instead of hardcoding
                # "Yesterday" (the source ride is often 2+ days old when the
                # most recent day was a rest day / unindexed ride).
                sa = r.get("started_at") or ""
                last_dec_date = str(sa)[:10] if sa else None
        alpha = summary.get("dfa_alpha1_avg")
        if isinstance(alpha, (int, float)):
            # v1.8.16 (grill B1) — the DFA-cap recency gate needs the newest
            # DFA-CONTRIBUTING ride's date, which is a DIFFERENT ride from the
            # decoupling source. Capture it on the FIRST appended α1 (merged is
            # newest-first), never reuse last_dec_date for the cap.
            if newest_dfa_date is None:
                sa = r.get("started_at") or ""
                newest_dfa_date = str(sa)[:10] if sa else None
            dfa_vals.append(float(alpha))
        if len(dfa_vals) >= 3 and last_dec is not None:
            break
    return dfa_vals[:3], last_dec, last_dec_date, newest_dfa_date


def _age_days_from_iso(d: "str | None") -> "int | None":
    """v1.8.16 — whole-day age of a naive ``YYYY-MM-DD[...]`` date string vs
    today, by integer DATE subtraction (grill: never datetime — a 23:00 ride
    vs 01:00 'now' must not round to a different day). None when unparseable so
    callers fail-safe per the recency policy."""
    if not d:
        return None
    try:
        from datetime import date as _date
        return (clock.today() - _date.fromisoformat(str(d)[:10])).days
    except (ValueError, TypeError):
        return None


def _recent_dfa_diagnostic() -> dict:
    """v1.8.10 Bug B — richer-than-list_floats view for the homepage DFA
    card so the UI can show *why* there's no DFA value when the rider's
    recent rides genuinely had no RR data or ICU 404'd the FIT.

    Examines the top 5 newest rides (FIT + ICU merged, deduped) and
    returns:

      {
        "values": list[float],         # dfa_alpha1_avg samples, newest first
        "last_computed_at": str|None,  # started_at of newest contributing ride
        "n_recent_total": int,         # rides considered (cap=5)
        "n_no_rr_data": int,           # subset whose status == no_rr_data
        "n_fetch_failed": int,         # subset whose status in
                                       # {fetch_failed, timeout}
      }
    """
    rides: list[dict] = []
    try:
        import ride_storage
        rides.extend(ride_storage.list_rides())
    except Exception:
        pass
    rides.extend(_iter_icu_dfa_rides())
    by_id: dict[str, dict] = {}
    for r in rides:
        rid = r.get("id") or ""
        if not rid:
            continue
        existing = by_id.get(rid)
        if existing is None:
            by_id[rid] = r
            continue
        cur_alpha = (existing.get("summary") or {}).get("dfa_alpha1_avg")
        new_alpha = (r.get("summary") or {}).get("dfa_alpha1_avg")
        if not isinstance(cur_alpha, (int, float)) and \
           isinstance(new_alpha, (int, float)):
            by_id[rid] = r
    merged = sorted(
        by_id.values(),
        key=lambda r: r.get("started_at") or "",
        reverse=True,
    )
    recent = merged[:5]
    values: list[float] = []
    last_computed_at: "str | None" = None
    n_no_rr = 0
    n_fetch_failed = 0
    for r in recent:
        alpha = (r.get("summary") or {}).get("dfa_alpha1_avg")
        if isinstance(alpha, (int, float)):
            values.append(float(alpha))
            if last_computed_at is None:
                last_computed_at = r.get("started_at") or None
            continue
        status = r.get("dfa_alpha1_status")
        if status == "no_rr_data":
            n_no_rr += 1
        elif status in ("fetch_failed", "timeout"):
            n_fetch_failed += 1
    return {
        "values": values[:3],
        "last_computed_at": last_computed_at,
        "n_recent_total": len(recent),
        "n_no_rr_data": n_no_rr,
        "n_fetch_failed": n_fetch_failed,
    }


def _readiness_revert_flag_path():
    """FIX-CONTRACT C6: per-day flag file that suppresses the dfa_cap /
    decoupling_advisory booleans on /api/readiness once the rider clicks
    Revert. Keyed by ISO date so it auto-clears at midnight (next day's
    read finds no file).
    """
    return DATA_DIR / "readiness_cap_reverted.json"


def _is_readiness_cap_reverted_today() -> bool:
    try:
        from datetime import date as _d
        p = _readiness_revert_flag_path()
        if not p.exists():
            return False
        data = json.loads(p.read_text(encoding="utf-8"))
        stored = str(data.get("date") or "")
        return stored == _d.today().isoformat()
    except Exception:
        return False


def _mark_readiness_cap_reverted_today() -> None:
    try:
        from datetime import date as _d
        p = _readiness_revert_flag_path()
        p.write_text(
            json.dumps({"date": _d.today().isoformat(),
                        "at": clock.now().isoformat()}),
            encoding="utf-8",
        )
    except Exception as e:
        _log.warning(f"readiness revert flag write failed: {e}")


def _fitness_state(today: "date | None" = None) -> dict:
    """CTL/ATL/TSB from their one owner (src/fitness.py): intervals.icu live,
    else its last stored values, else unknown."""
    import fitness
    return fitness.state(cached("training", get_today_metrics), today)


# The CTL a planning or projection call starts from when the rider's is
# unknown (intervals.icu never answered and nothing is stored): the planner's
# own start (training_planner.generate_plan). One number where handlers used
# 30, 37 and 50.
_PLANNING_CTL_UNKNOWN = 37.0


def _planning_ctl() -> float:
    ctl = _fitness_state()["ctl"]
    return _PLANNING_CTL_UNKNOWN if ctl is None else ctl


import readiness_composite as _readiness_composite_mod  # noqa: E402
# The composite readiness reads today's TSB from the same owner (the S-3 split).
_readiness_composite_mod.today_load_provider = lambda: _fitness_state()


def _merge_training_load(icu_t: dict | None) -> dict:
    """The training-load block the readiness surfaces serve: CTL/ATL/TSB from
    the fitness owner (intervals.icu, the owner's decision of 2026-09-14), with
    ICU's acwr/ramp/monotony/strain beside them.

    Until then it merged live ICU values with a local EWMA over the FIT-only
    ride list, per field, and could answer "mixed" (the audit's S-2).
    ``source`` is "icu", "icu_cached" (its last stored values, dated by
    ``as_of``) or "none".
    """
    import fitness
    icu = icu_t or {}
    out = fitness.state(icu)
    out.update({k: icu.get(k) for k in ("acwr", "ramp_rate", "monotony", "strain")})
    return out


def _readiness_with_data_status(r: dict, has_local_load: bool) -> dict:
    """v4.4.2 §B6 — unify the readiness default shape across endpoints.

    Adds a top-level ``data_status`` field to the readiness dict
    ("ok" | "insufficient_data" | "no_data") and replaces the score-None
    with a numeric neutral default (50) when local rides exist (so the
    frontpage gauges aren't blank).

    Mutates ``r`` in place AND returns it for chaining.
    """
    status = r.get("status") or "OK"
    if status == "INSUFFICIENT_DATA":
        r["data_status"] = "insufficient_data"
        if r.get("score") is None:
            # If local rides exist (so frontpage has something to base a
            # neutral default on), fall back to 50. Otherwise leave None
            # so callers know there's truly no data.
            r["score"] = 50 if has_local_load else None
    else:
        r["data_status"] = "ok"
    return r


def _local_sleep_metrics(days: int = 14) -> dict:
    """v4.5.0 — derive sleep/HRV/RHR metrics from the local wellness store.

    Used by /api/readiness as a fallback when the ICU live wellness path
    returns {} (rate-limited, 403'd, or genuinely empty). Defers to
    sleep.compute_sleep_metrics_from_wellness so the exact same HRV-baseline
    + RHR-delta logic applies.
    """
    try:
        import ride_storage as _rs
        records = _rs.load_recent_wellness(days=days)
    except Exception:
        return {}
    if not records:
        return {}
    # load_recent_wellness returns newest-first; sleep helper expects oldest-first.
    from sleep import compute_sleep_metrics_from_wellness
    return compute_sleep_metrics_from_wellness(list(reversed(records)))


@app.get("/api/readiness")
def api_readiness(subjective: float = Query(None)):
    training = cached("training", get_today_metrics)
    sleep = cached("sleep", get_sleep_metrics)
    # v4.5.0: when ICU live wellness returns {}, fall back to local wellness
    # store (populated by lazy sync) so HRV/Sleep/RHR component bars aren't
    # blanks when ~/.domestique/wellness/ has data.
    data_status_local_wellness = False
    if not sleep:
        sleep = _local_sleep_metrics(days=14)
        if sleep:
            data_status_local_wellness = True
    # Auto-feed soreness/Hooper from morning log into subjective score.
    # v4.6.6 IMPL-B: route through `_get_soreness_subjective()` so we read
    # all 4 Hooper fields (sleep/fatigue/stress/soreness) once IMPL-C lands
    # the expanded form. Wrapped because some test fixtures use mocked DBs
    # that don't have the daily_log table.
    if subjective is None:
        try:
            subjective = _get_soreness_subjective()
        except Exception:  # noqa: BLE001
            subjective = None
    dfa_vals, last_dec, last_dec_date, newest_dfa_date = _recent_dfa_and_decoupling()
    r = compute_readiness(
        ln_rmssd_7d=sleep.get("ln_rmssd_7d"),
        swc_lower=sleep.get("swc_lower"), swc_upper=sleep.get("swc_upper"),
        tsb=training.get("tsb"), sleep_h=sleep.get("sleep_h"),
        rhr_delta=sleep.get("rhr_delta"), subjective=subjective,
        recent_dfa_alpha1=dfa_vals, last_decoupling_pct=last_dec,
        last_decoupling_age_days=_age_days_from_iso(last_dec_date),
        newest_dfa_age_days=_age_days_from_iso(newest_dfa_date),
    )
    # FIX-CONTRACT C2: flatten DFA cap + decoupling advisory as top-level
    # booleans so U6's banner can gate on them without knowing the nested
    # readiness.dfa_cap.cap_applied path. Dict truthiness ≠ boolean so the
    # nested form would fire spuriously; exposing a real bool matches the
    # UI contract. C6: suppress booleans for today if the rider clicked
    # Revert via /api/readiness/revert-cap (flag auto-clears at midnight).
    dfa_cap = r.get("dfa_cap") or {}
    dec_adv = r.get("decoupling_advisory") or {}
    reverted = _is_readiness_cap_reverted_today()
    dfa_cap_applied = bool(dfa_cap.get("cap_applied")) and not reverted
    decoupling_advisory = bool(dec_adv.get("advisory")) and not reverted
    # v4.4.2 §B3: when ICU wellness path returns no CTL/ATL/TSB, fall back
    # to local-archive EWMA so the frontpage gauges aren't dashes when
    # 51 ICU + 12 FIT rides sit on disk.
    merged_load = _merge_training_load(training)
    has_local_load = merged_load.get("ctl") is not None or merged_load.get("atl") is not None
    # v4.4.2 §B6: unify default response shape so /api/today-session and
    # /api/readiness agree on score/data_status semantics.
    r = _readiness_with_data_status(r, has_local_load=has_local_load)
    # v4.5.0: surface "local_wellness" data_status when sleep/HRV came from the
    # local file store (instead of ICU live) so the UI can show a hint.
    # We override "ok" AND "insufficient_data" both — the wellness is real
    # data, just from the local cache; INSUFFICIENT_DATA only signals "not
    # enough components to compute readiness composite", not "no source".
    if data_status_local_wellness:
        r["data_status"] = "local_wellness"
    # v1.8.8 Bug 8 — unify the two readiness scales so the homepage card
    # and the scroll-down card stop disagreeing. `score_0_100` is the
    # canonical compute_readiness output (legacy field, unchanged).
    # `score_0_10` is the same metric expressed on the 0-10 scale used by
    # the composite readiness card. ADD-only; existing `r.score` stays.
    raw_score = r.get("score")
    try:
        s100 = float(raw_score) if raw_score is not None else None
    except (TypeError, ValueError):
        s100 = None
    s10 = round(s100 / 10.0, 1) if s100 is not None else None
    # v2.2.5 issue #3 R2 — chain the Hooper/composite severity onto the
    # canonical readiness payload so the unified "Today" card reads ONE
    # (non-deprecated) endpoint for both the 0-100 number AND the action.
    # Same tolerant pattern as /api/readiness/composite; absence = normal.
    severity = source = None
    severity_reasons: list = []
    try:
        from datetime import date as _date_cls
        from readiness_composite import compute_training_severity as _cts
        sev = _cts("default", _date_cls.today().isoformat()) or {}
        if isinstance(sev, dict):
            severity = sev.get("severity")
            source = sev.get("source")
            severity_reasons = sev.get("reasons") or []
    except Exception:
        pass
    return {
        "readiness": r,
        # v1.8.8 Bug 8 — top-level unified score fields.
        "score_0_10": s10,
        "score_0_100": s100,
        # v2.2.5 issue #3 R2 — severity/source/reasons for the unified card.
        "severity": severity,
        "source": source,
        "severity_reasons": severity_reasons,
        "training": merged_load,
        "sleep": {
            "sleep_h": sleep.get("sleep_h"), "sleep_score": sleep.get("sleep_score"),
            "sleep_asof": sleep.get("sleep_asof"), "hrv_asof": sleep.get("hrv_asof"),
            "rhr_asof": sleep.get("rhr_asof"), "sleep_status": sleep.get("sleep_status"),
            "hrv_ms": sleep.get("hrv_ms"), "ln_rmssd_7d": sleep.get("ln_rmssd_7d"),
            "hrv_status": sleep.get("hrv_status"),
            "swc_lower": sleep.get("swc_lower"), "swc_upper": sleep.get("swc_upper"),
            "rhr_today": sleep.get("rhr_today"), "rhr_delta": sleep.get("rhr_delta"),
            "rhr_status": sleep.get("rhr_status"),
            "red_hrv_streak": sleep.get("red_hrv_streak", 0),
        },
        "ftp": config.ATHLETE_FTP_W,
        "weight": config.ATHLETE_WEIGHT_KG,
        # FIX-CONTRACT C2: flattened booleans for U6 banner gating.
        "dfa_cap_applied": dfa_cap_applied,
        "decoupling_advisory": decoupling_advisory,
        # v1.8.15 — date (YYYY-MM-DD) of the ride that triggered the advisory,
        # so the banner can name the real day instead of hardcoding "Yesterday".
        "decoupling_advisory_date": last_dec_date,
        # Keep the richer nested dicts available for detail tooltips.
        "dfa_cap": dfa_cap,
        "decoupling_advisory_detail": dec_adv,
        "cap_reverted_today": reverted,
    }


@app.post("/api/readiness/revert-cap")
async def api_readiness_revert_cap(request: Request):
    """FIX-CONTRACT C6 — rider-opt-out for today's DFA/decoupling cap.

    Accepts ``{signal: "dfa"|"decoupling", date?: "today"|YYYY-MM-DD}``.
    Writes a one-day flag to DATA_DIR so the next ``/api/readiness`` call
    returns ``dfa_cap_applied=False`` + ``decoupling_advisory=False``
    regardless of the underlying signals. The flag auto-clears at midnight
    (keyed by today's ISO date).

    The flag is coarse (not per-signal) because the rider's intent when
    clicking Revert is "I want to do the planned hard session today" — both
    guards come down together. If either fires again tomorrow, the banner
    re-shows.
    """
    try:
        body = await _get_json_body(request)
    except Exception:
        body = {}
    # Body is optional; accept empty POST as "revert whatever fired today".
    _ = str(body.get("signal") or "any")
    _mark_readiness_cap_reverted_today()
    clear_cache()  # training metrics cache may have gated on the flag
    # The flag only suppresses the LIVE adjustment. When the adaptation was
    # also persisted into the plan (api_today_session_persist), the stored day
    # still carries the replacement — restore it from the pre_adapt stash, or
    # the button closes the modal and visibly changes nothing.
    restored = None
    try:
        json_path = _plan_dir() / "current_plan.json"
        if json_path.exists():
            with tp.plan_write_lock():
                with open(json_path, encoding="utf-8") as f:
                    plan = json.load(f)
                today_iso = clock.today().isoformat()
                for w in plan.get("weeks", []):
                    for s in w.get("sessions", []):
                        if (s.get("day") == today_iso and s.get("adapted")
                                and isinstance(s.get("pre_adapt"), dict)):
                            pre = s.pop("pre_adapt")
                            for k, v in pre.items():
                                s[k] = v
                            s["adapted"] = False
                            s.pop("adapted_reason", None)
                            restored = {"session_type": s.get("session_type"),
                                        "duration_min": s.get("duration_min"),
                                        "zwo_file": s.get("zwo_file"),
                                        "zwo_name": s.get("zwo_name")}
                            tp.atomic_write_plan(json_path, plan)
                            break
                    if restored:
                        break
    except Exception:
        _log.exception("revert-cap: plan restore skipped")
    _log.info(f"EVENT=readiness_cap_reverted signal={_} restored={bool(restored)}")
    return {
        "ok": True,
        "reverted": True,
        "restored": restored,
        "cleared_at_midnight": True,
        "flag_file": str(_readiness_revert_flag_path()),
    }


# ─── v1.1.0 — Bayesian HRV-readiness composite ─────────────────────────────
# Lives alongside /api/readiness (legacy 0-100). The composite is a 0-10
# score that requires ≥30 days of wellness data; it surfaces on the home
# page as a separate card and is consumed by the planner advisory only when
# status='dynamic_weights'. See readiness_composite.py for the contract.

@app.get("/api/readiness/composite")
def api_readiness_composite(date: str = Query(None)):
    """v1.1.0 IMPL-HRV-RECOVERY — Bayesian HRV-readiness composite (0-10).

    Cached for 5 min (key includes the date so per-day re-fetch works).
    Returns the dict shape from compute_readiness_composite() unchanged.

    v1.8.0 §F1 — chains compute_training_severity to merge severity, source,
    and severity_reasons fields onto the returned dict. Legacy fields preserved.
    """
    target_iso = date or clock.today().isoformat()
    profile_id = "default"  # single-rider scope; profile_manager is a separate concern
    cache_key = f"readiness_composite_{profile_id}_{target_iso}"
    result = cached(cache_key, lambda: compute_readiness_composite(profile_id, target_iso))
    # v1.8.0 — chain severity helper (A-BACKEND owns the impl). Tolerate
    # absence: helper may not be exported yet during cross-agent rollout.
    try:
        from readiness_composite import compute_training_severity as _cts
        sev = _cts(profile_id, target_iso) or {}
        if isinstance(result, dict) and isinstance(sev, dict):
            result["severity"] = sev.get("severity")
            result["source"] = sev.get("source")
            result["severity_reasons"] = sev.get("reasons") or []
    except Exception:
        pass
    # v1.8.8 Bug 8 — mark this endpoint deprecated; callers should use
    # /api/readiness (which now returns BOTH score_0_10 and score_0_100).
    if isinstance(result, dict):
        result["deprecated"] = True
    return result


@app.post("/api/readiness/apply-tier-down")
async def api_readiness_apply_tier_down(request: Request):
    """v1.7.5 — apply a one-tier intensity drop to today's planned session.

    Trigger context: the readiness composite card surfaces "Soft tier-down
    recommended. Drop today's hard session by one tier." when the score
    lands in [3.0, 5.0). Pre-v1.7.5 that was advice-only — the user had
    to manually open the Plan tab and use Rematch to make the change
    stick. This endpoint executes the drop in one click:

      1. Find target session (defaults to today).
      2. Walk one step down ``tp._INTENSITY_LADDER`` via ``_drop_intensity``.
         Rest / recovery / unknown types short-circuit (no_change / already_easy).
      3. Keep duration_min, recompute tss_estimate from
         ``TSS_PER_HOUR[new_type]``.
      4. Re-match ZWO so the loaded workout matches the new bucket
         (the prior ZWO is added to ``used_names`` to force a different
         pick). NoCandidateWorkoutError clears the ZWO fields.
      5. Persist ``last_tier_down`` breadcrumb + run a full
         ``tp.reforecast_dict`` so downstream sessions catch up to the
         new actual TSS / availability flow (mirrors accept-redraw).

    Body: ``{"date": "YYYY-MM-DD"}`` (date optional → defaults to today).
    Returns: ``{ok, day, old_type, new_type, old_tss, new_tss,
    zwo_file, zwo_name}``.
    """
    body = await _get_json_body(request)
    day_iso = str(body.get("date") or clock.today().isoformat()).strip()
    try:
        date.fromisoformat(day_iso)
    except ValueError:
        return JSONResponse({"error": "Invalid date format (use YYYY-MM-DD)"}, 400)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
        target = None
        target_week = None
        for w in plan.get("weeks", []):
            for s in w.get("sessions", []):
                if s.get("day") == day_iso:
                    target = s
                    target_week = w
                    break
            if target:
                break
        if not target or not target_week:
            return JSONResponse({"error": f"No session at {day_iso}"}, 404)
        old_type = target.get("session_type", "") or ""
        if old_type in ("rest", "recovery"):
            return {"ok": False, "action": "already_easy", "day": day_iso,
                    "session_type": old_type}
        # v1.8.3 BUG-D — distinguish error reasons so the user gets a
        # specific toast instead of the generic "could not apply: no_change":
        #   1. session_type unknown to the Seiler ladder → unknown_type
        #   2. session_type is already at the practical intensity bottom
        #      (z2 / long_z2 — these ARE in the ladder but dropping them
        #      yields a longer/lower-volume z2, not a real intensity tier-down)
        #      → already_at_bottom
        #   3. _drop_intensity returns same value defensively → no_change (kept)
        if old_type not in tp._INTENSITY_LADDER:
            return {"ok": False, "action": "unknown_type", "day": day_iso,
                    "session_type": old_type}
        if old_type in ("z2", "long_z2"):
            return {"ok": False, "action": "already_at_bottom", "day": day_iso,
                    "session_type": old_type}
        new_type = tp._drop_intensity(old_type)
        if not new_type or new_type == old_type:
            return {"ok": False, "action": "no_change", "day": day_iso,
                    "session_type": old_type}

        old_duration = int(target.get("duration_min", 0) or 0)
        old_tss = float(target.get("tss_estimate", 0) or 0)
        # issue #3: a tier-DOWN must never INCREASE load. The rule now lives in
        # tp._deescalated_load so every gate that walks the ladder shares it.
        new_duration, new_tss = tp._deescalated_load(
            old_duration, new_type, old_tss)
        target["session_type"] = new_type
        target["duration_min"] = new_duration
        target["tss_estimate"] = new_tss
        target["adapted"] = True
        target["adapted_reason"] = f"Tier-down: {old_type} → {new_type} (readiness)"
        target["status"] = "pending"

        # Re-match ZWO to the new session-type bucket.
        try:
            library = tp.load_workout_library()
            planned = tp.PlannedSession(
                day=date.fromisoformat(day_iso),
                day_name=target.get("day_name", ""),
                session_type=new_type,
                duration_min=new_duration,
                tss_estimate=new_tss,
                description=target.get("description", ""),
            )
            week_num = target_week.get("week_num", 0)
            day_idx = (date.fromisoformat(day_iso)
                       - date.fromisoformat(target_week["start"])).days
            old_zwo_name = target.get("zwo_name", "")
            excluded = {old_zwo_name} if old_zwo_name else set()
            tp.match_zwo(
                planned, library,
                week_num=week_num, day_idx=day_idx,
                used_names=excluded, raise_on_empty=True,
                hr_bias=_hr_bias(),
             micro_only=bool(((plan or {}).get("goal") or {}).get("vo2_microintervals_only", False)),)
            target["zwo_file"] = planned.zwo_file
            target["zwo_name"] = planned.zwo_name
        except tp.NoCandidateWorkoutError:
            # Library has nothing in the new bucket — clear so the UI
            # shows unmatched state instead of the wrong workout.
            target["zwo_file"] = ""
            target["zwo_name"] = ""
        except Exception:
            _log.exception("apply-tier-down re-match swallowed")

        plan["last_tier_down"] = {
            "date": day_iso,
            "at": clock.now().isoformat(),
            "old_type": old_type,
            "new_type": new_type,
        }

        # Downstream reforecast (mirrors accept-redraw + save-availability).
        try:
            try:
                activities = db.query_activities(days=120)
            except Exception:
                activities = []
            try:
                training_snap = cached("training", get_today_metrics)
                current_tsb = training_snap.get("tsb")
            except Exception:
                current_tsb = None
            tsb_series: "dict | None" = None
            if current_tsb is not None:
                tsb_series = {}
                for w in plan.get("weeks", []) or []:
                    try:
                        ws = date.fromisoformat(w["start"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    for i in range(7):
                        tsb_series[ws + timedelta(days=i)] = current_tsb
            avail = {
                day_iso2: float(entry["hours"])
                for day_iso2, entry in plan.get("availability", {}).items()
                if isinstance(entry, dict) and "hours" in entry
            }
            plan, _modified, _ri = tp.reforecast_dict(
                plan,
                today_iso=clock.today().isoformat(),
                tsb_series=tsb_series,
                recent_activities=activities,
                availability_overrides=avail,
            )
        except Exception:
            _log.exception("apply-tier-down reforecast skipped")

        tp.atomic_write_plan(json_path, plan)
        return {
            "ok": True,
            "day": day_iso,
            "old_type": old_type,
            "new_type": new_type,
            "old_tss": old_tss,
            "new_tss": new_tss,
            # The stored duration, not the one we started from — a tier-down
            # trims duration to hold the load, so reporting the old value made
            # the response disagree with the plan it had just written.
            "duration_min": new_duration,
            "zwo_file": target.get("zwo_file", ""),
            "zwo_name": target.get("zwo_name", ""),
        }
    except Exception:
        _log.exception("apply-tier-down failed")
        return JSONResponse({"detail": "tier-down failed"}, 500)


@app.post("/api/plan/auto-adjust")
async def api_plan_auto_adjust(request: Request):
    """v1.8.0 §F1 — Automated TSB+HRV → planner with Hooper override.

    Computes today's training severity (Hooper if logged, else TSB+HRV
    composite) and adjusts the plan accordingly:

      - severity=normal → no change (returns actions=[], note="no adjustment
        needed").
      - severity=rest → today's session set to rest (clear ZWO, tss=0).
        scope=week is collapsed to today-only with an explanatory note.
      - severity=tier_down + scope=today → drop today's hard session one tier.
      - severity=tier_down + scope=week → walk all remaining hard sessions
        Mon-Sun and tier-down each via training_planner.apply_week_tier_down.

    Body: ``{"scope": "today"|"week", "dry_run": bool}``.
    Response: ``{ok, severity, source, scope, dry_run, actions, sessions_modified, note}``.

    dry_run=True works against a deepcopy of the plan; never writes disk
    and never calls reforecast.
    """
    body = await _get_json_body(request)
    scope = str(body.get("scope") or "today").strip().lower()
    # v1.8.8 Bug 7 — accept `scope='day'` for "apply rest to tomorrow".
    # 'today' / 'week' continue to mean what they always did. 'day' targets
    # tomorrow's session so the "Apply rest day" button on the readiness
    # card stops a planned hard ride before it happens.
    if scope not in ("today", "week", "day"):
        return JSONResponse(
            {"error": "scope must be 'today', 'day', or 'week'"}, 400,
        )
    dry_run = bool(body.get("dry_run", False))
    # v1.8.8 Bug 7 — allow explicit severity override so the rest-day
    # button can ask for "rest" regardless of computed severity (the user
    # is asserting it). When provided it bypasses the computed-severity
    # path. Backward-compat: when omitted, computed severity is used.
    severity_override = body.get("severity")
    if severity_override is not None:
        severity_override = str(severity_override).strip().lower() or None

    today_iso = clock.today().isoformat()
    # v1.8.8 Bug 7 — `scope='day'` targets tomorrow's session.
    target_iso = (
        (clock.today() + timedelta(days=1)).isoformat()
        if scope == "day" else today_iso
    )
    profile_id = "default"

    # 1. Resolve severity (A-BACKEND helper). Tolerate absence.
    # v1.8.8 Bug 7 — caller may override (severity in body) so the
    # explicit "Apply rest day" button always lands rest.
    if severity_override:
        severity = severity_override
        source = "user_override"
    else:
        try:
            from readiness_composite import compute_training_severity as _cts
        except Exception:
            return JSONResponse({"error": "severity unavailable"}, 503)
        try:
            sev = _cts(profile_id, today_iso) or {}
        except Exception:
            _log.exception("auto-adjust: compute_training_severity failed")
            return JSONResponse({"error": "severity unavailable"}, 503)
        severity = sev.get("severity") or "normal"
        source = sev.get("source") or "insufficient"

    # 2. severity=normal → no change.
    if severity == "normal":
        return {
            "ok": True,
            "severity": severity,
            "source": source,
            "scope": scope,
            "dry_run": dry_run,
            "actions": [],
            # v1.8.9 Bug 9 (master §9/§10): `applied` is a bool — true iff
            # the persistence step actually changed the plan. severity=normal
            # never writes, so always false.
            "applied": False,
            "applied_sessions": [],
            "sessions_modified": 0,
            "note": "no adjustment needed",
        }

    # 3. Load plan.
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
    except Exception:
        _log.exception("auto-adjust: plan load failed")
        return JSONResponse({"error": "plan load failed"}, 500)

    # 4. Build working copy for dry-run.
    import copy as _copy
    working = _copy.deepcopy(plan) if dry_run else plan

    actions: list[dict] = []
    note = ""

    # 5. severity=rest → single-day rest. `scope='day'` targets tomorrow,
    # everything else (including 'week' collapse) targets today.
    if severity == "rest":
        target = None
        for w in working.get("weeks", []) or []:
            for s in w.get("sessions", []) or []:
                if s.get("day") == target_iso:
                    target = s
                    break
            if target:
                break
        # FC3 (v2.5.0, L3-7): severity=rest must never wipe the race day — the
        # readiness card's "Apply rest day" button on race eve converted the
        # race to rest/0min/no-zwo (the race chip floating over a rest cell).
        if target is not None and target.get("is_race"):
            note = "race day is fixed — edit the race instead"
            target = None
        if target is not None:
            old_type = target.get("session_type", "") or ""
            old_duration = int(target.get("duration_min", 0) or 0)
            old_tss = float(target.get("tss_estimate", 0) or 0)
            target["session_type"] = "rest"
            target["tss_estimate"] = 0
            target["duration_min"] = 0
            target["zwo_file"] = ""
            target["zwo_name"] = ""
            target["adapted"] = True
            target["adapted_reason"] = "Auto-adjust: rest (severity=rest)"
            target["status"] = "pending"
            actions.append({
                "day": target_iso,
                "before": {"type": old_type, "duration_min": old_duration, "tss": old_tss},
                "after": {"type": "rest", "duration_min": 0, "tss": 0.0},
                "rematched": False,
                "zwo_cleared": True,
            })
        if scope == "week":
            note = "week scope ignored for severity=rest; applies to today only"

    # 6. severity=tier_down.
    elif severity == "tier_down":
        if scope == "today":
            # Walk today only — mirror apply-tier-down body inline so we
            # share the dry-run path.
            target = None
            target_week = None
            for w in working.get("weeks", []) or []:
                for s in w.get("sessions", []) or []:
                    if s.get("day") == today_iso:
                        target = s
                        target_week = w
                        break
                if target:
                    break
            if target is not None and target_week is not None:
                old_type = target.get("session_type", "") or ""
                if old_type in tp._HARD_SESSION_TYPES:
                    new_type = tp._drop_intensity(old_type)
                    old_duration = int(target.get("duration_min", 0) or 0)
                    old_tss = float(target.get("tss_estimate", 0) or 0)
                    new_duration, new_tss = tp._deescalated_load(
                        old_duration, new_type, old_tss)
                    target["session_type"] = new_type
                    target["duration_min"] = new_duration
                    target["tss_estimate"] = new_tss
                    target["adapted"] = True
                    target["adapted_reason"] = (
                        f"Auto-adjust: {old_type} → {new_type} (severity=tier_down)"
                    )
                    target["status"] = "pending"
                    rematched = False
                    zwo_cleared = False
                    try:
                        library = tp.load_workout_library()
                        planned = tp.PlannedSession(
                            day=date.fromisoformat(today_iso),
                            day_name=target.get("day_name", ""),
                            session_type=new_type,
                            # v3.6.0: match on the NEW duration. The tier-down
                            # now trims duration to hold the load, so matching
                            # on the old one attached a workout longer than the
                            # session it was attached to.
                            duration_min=new_duration,
                            tss_estimate=new_tss,
                            description=target.get("description", ""),
                        )
                        week_num = target_week.get("week_num", 0)
                        try:
                            day_idx = (date.fromisoformat(today_iso)
                                       - date.fromisoformat(target_week["start"])).days
                        except (KeyError, ValueError, TypeError):
                            day_idx = 0
                        old_zwo_name = target.get("zwo_name", "")
                        excluded = {old_zwo_name} if old_zwo_name else set()
                        tp.match_zwo(
                            planned, library,
                            week_num=week_num, day_idx=day_idx,
                            used_names=excluded, raise_on_empty=True,
                            hr_bias=_hr_bias(),
                         micro_only=bool(((plan or {}).get("goal") or {}).get("vo2_microintervals_only", False)),)
                        target["zwo_file"] = planned.zwo_file
                        target["zwo_name"] = planned.zwo_name
                        rematched = True
                    except tp.NoCandidateWorkoutError:
                        target["zwo_file"] = ""
                        target["zwo_name"] = ""
                        zwo_cleared = True
                    except Exception:
                        _log.exception("auto-adjust: today rematch swallowed")
                    actions.append({
                        "day": today_iso,
                        "before": {"type": old_type, "duration_min": old_duration, "tss": old_tss},
                        "after": {"type": new_type, "duration_min": new_duration, "tss": new_tss},
                        "rematched": rematched,
                        "zwo_cleared": zwo_cleared,
                    })
        else:  # scope == "week"
            try:
                # issue #3: on a SEVERE day (severity "rest" — e.g. high soreness
                # forcing recovery) drop hard sessions ALL the way to easy in one
                # pass, instead of one ladder step the rider has to repeat. A soft
                # "tier_down" day still drops a single tier.
                _to_floor = (severity == "rest")
                wk_result = tp.apply_week_tier_down(
                    working, today_iso, dry_run=dry_run, to_floor=_to_floor)
                actions = wk_result.get("actions", [])
                if wk_result.get("note"):
                    note = wk_result["note"]
            except Exception:
                _log.exception("auto-adjust: apply_week_tier_down failed")
                return JSONResponse({"error": "tier-down walk failed"}, 500)

    # 7. Persist + reforecast on real (non-dry-run) paths only.
    sessions_modified = 0 if dry_run else len(actions)
    if not dry_run and actions:
        try:
            try:
                activities = db.query_activities(days=120)
            except Exception:
                activities = []
            try:
                training_snap = cached("training", get_today_metrics)
                current_tsb = training_snap.get("tsb")
            except Exception:
                current_tsb = None
            tsb_series: "dict | None" = None
            if current_tsb is not None:
                tsb_series = {}
                for w in working.get("weeks", []) or []:
                    try:
                        ws = date.fromisoformat(w["start"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    for i in range(7):
                        tsb_series[ws + timedelta(days=i)] = current_tsb
            avail = {
                day_iso2: float(entry["hours"])
                for day_iso2, entry in working.get("availability", {}).items()
                if isinstance(entry, dict) and "hours" in entry
            }
            working, _modified, _ri = tp.reforecast_dict(
                working,
                today_iso=today_iso,
                tsb_series=tsb_series,
                recent_activities=activities,
                availability_overrides=avail,
            )
        except Exception:
            _log.exception("auto-adjust: reforecast skipped")
        try:
            tp.atomic_write_plan(json_path, working)
        except Exception:
            _log.exception("auto-adjust: atomic_write_plan failed")
            return JSONResponse({"error": "plan write failed"}, 500)

    # v1.8.9 Bug 9 (master §9/§10): `applied` is a bool — true iff the
    # persistence step actually changed the plan (i.e. not a dry-run AND
    # at least one action was applied). `applied_sessions` carries the
    # `[{date, session_type}]` summary that the v1.8.8 `applied` list
    # provided — preserved for the dashboard's toast wording.
    applied_sessions = [
        {"date": a.get("day"), "session_type": a.get("after", {}).get("type")}
        for a in actions
        if not dry_run and a.get("day")
    ]
    applied_bool = bool(not dry_run and applied_sessions)
    return {
        "ok": True,
        "severity": severity,
        "source": source,
        "scope": scope,
        "dry_run": dry_run,
        "actions": actions,
        "applied": applied_bool,
        "applied_sessions": applied_sessions,
        "sessions_modified": sessions_modified,
        "note": note,
    }


@app.post("/api/wellness/import-hrv4training")
async def api_wellness_import_hrv4training(file: UploadFile = File(...)):
    """v1.1.0 IMPL-HRV-RECOVERY — HRV4Training CSV import (PATCH G16).

    Locked columns: date, rmssd, hrv_baseline, recovery_points.
    Skip rows missing date or rmssd. Optional columns ignored.
    Each row upserts wellness.hrv (= rmssd value); other wellness columns
    are left untouched if the row already exists.

    Returns {"imported": int, "skipped": int}.
    """
    import csv as _csv
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")  # tolerates BOM
    except UnicodeDecodeError:
        text = raw.decode("utf-8", errors="replace")
    reader = _csv.DictReader(text.splitlines())
    imported = 0
    skipped = 0
    conn = db.get_db()
    for row in reader:
        d = (row.get("date") or "").strip()
        rmssd_raw = (row.get("rmssd") or "").strip()
        if not d or not rmssd_raw:
            skipped += 1
            continue
        try:
            rmssd = float(rmssd_raw)
        except (TypeError, ValueError):
            skipped += 1
            continue
        # Upsert: if a wellness row exists, only update hrv; else insert minimal row.
        existing = conn.execute("SELECT date FROM wellness WHERE date = ?", (d,)).fetchone()
        if existing:
            conn.execute("UPDATE wellness SET hrv = ? WHERE date = ?", (rmssd, d))
        else:
            conn.execute(
                "INSERT INTO wellness (date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json) "
                "VALUES (?, NULL, NULL, ?, NULL, NULL, NULL, NULL, '{}')",
                (d, rmssd),
            )
        imported += 1
    conn.commit()
    # invalidate composite-readiness caches that span this date range
    for k in list(_cache.keys()):
        if k.startswith("readiness_composite_"):
            _cache.pop(k, None)
            _cache_ts.pop(k, None)
    return {"imported": imported, "skipped": skipped}


@app.post("/api/wellness/manual-hrv")
async def api_wellness_manual_hrv(request: Request):
    """v1.1.0 IMPL-HRV-RECOVERY — manual HRV entry (rMSSD ms).

    Body: {"date": "YYYY-MM-DD" (default today), "rmssd": float}.
    Writes to wellness.hrv via UPSERT. The field name `hrv_manual_rmssd`
    distinguishes this path from ICU-pulled `hrv` in audit logs.
    """
    body = await _get_json_body(request)
    d = (body.get("date") or clock.today().isoformat()).strip()
    rmssd_raw = body.get("hrv_manual_rmssd", body.get("rmssd"))
    try:
        rmssd = float(rmssd_raw)
    except (TypeError, ValueError):
        return JSONResponse({"error": "hrv_manual_rmssd must be numeric"}, 400)
    if rmssd <= 0 or rmssd > 250:
        return JSONResponse({"error": "rmssd out of range (0, 250]"}, 400)
    conn = db.get_db()
    existing = conn.execute("SELECT date FROM wellness WHERE date = ?", (d,)).fetchone()
    if existing:
        conn.execute("UPDATE wellness SET hrv = ? WHERE date = ?", (rmssd, d))
    else:
        conn.execute(
            "INSERT INTO wellness (date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json) "
            "VALUES (?, NULL, NULL, ?, NULL, NULL, NULL, NULL, '{}')",
            (d, rmssd),
        )
    conn.commit()
    for k in list(_cache.keys()):
        if k.startswith("readiness_composite_"):
            _cache.pop(k, None)
            _cache_ts.pop(k, None)
    return {"ok": True, "date": d, "hrv_manual_rmssd": rmssd}


@app.get("/api/activities/blocks")
def api_activities_blocks(days: int = Query(42)):
    """Per-date actual-ride lap blocks for the plan grid's completed-day
    sprites. A separate endpoint on purpose: /api/activities is the homepage
    hot path built from the ICU wellness summaries, which carry no laps —
    attaching a ride-store join there would tax every frontpage load for a
    grid-only need. One request per grid render, straight off the local ride
    store, shaped for miniPowerBlockSVG."""
    try:
        days = max(1, min(int(days or 42), 120))
        cutoff = (clock.today() - timedelta(days=days)).isoformat()
        try:
            from profile_manager import ProfileManager
            _pm_ftp = ProfileManager.get().ftp or None
        except Exception:
            _pm_ftp = None
        out: dict = {}
        for r in _load_all_rides_safe():
            d = (r.get("started_at") or "")[:10]
            if not d or d < cutoff:
                continue
            blocks = _actual_blocks_from_laps(r, _pm_ftp)
            if blocks:
                out.setdefault(d, []).append(
                    {"sport": r.get("sport"), "blocks": blocks})
        return out
    except Exception:
        _log.exception("activities/blocks failed")
        return {}


@app.get("/api/activities")
def api_activities():
    # v1.6.1: outer-fn try; surfaces E_ACTIVITIES_LIST_FAILED on any uncaught
    # exception so the diag ring carries evidence when the homepage list is
    # mysteriously empty.
    try:
        # v4.4.2 §B1+B2: wire lazy ICU sync here so the frontpage triggers
        # syncs (loadHome calls /api/activities, not /api/calendar). Also
        # force-resync if today's date isn't cached locally.
        # v1.6.3: fire-and-forget so the request thread never blocks on the
        # per-ride FIT-download/DFA augment loop (was the root cause of the
        # frontpage-hangs-30s bug).
        try:
            _kick_lazy_icu_sync(force_if_today_missing=True)
        except Exception as _e:
            _log.debug(f"/api/activities: lazy ICU sync kick swallowed: {_e}")
        training = cached("training", get_today_metrics)
        activities = training.get("recent_activities", [])
        if activities:
            return activities
        # v4.4.2 §B1: when ICU wellness path returns empty, fall back to the
        # local rides store (FIT + ICU-synced) so today's ride still surfaces
        # on the frontpage.
        try:
            rides = _load_all_rides_safe()
        except Exception:
            rides = []
        if rides:
            out = []
            cutoff = (clock.today() - timedelta(days=7)).isoformat()
            for r in rides:
                d = _ride_started_local_iso_date(r) or ""
                if not d or d < cutoff:
                    continue
                summary = r.get("summary") or {}
                out.append({
                    "date": d,
                    "name": r.get("name") or summary.get("name") or "",
                    "sport": r.get("sport") or "Ride",
                    "duration_min": round((r.get("duration_s") or summary.get("duration_s") or 0) / 60),
                    "tss": r.get("tss") if r.get("tss") is not None else summary.get("tss"),
                    "avg_power": r.get("avg_power_w") if r.get("avg_power_w") is not None else summary.get("avg_power"),
                    "avg_hr": r.get("avg_hr") if r.get("avg_hr") is not None else summary.get("avg_hr"),
                    "id": r.get("ride_id") or r.get("id") or "",
                })
            if out:
                out.sort(key=lambda a: a.get("date") or "", reverse=True)
                return out
        # Final fallback to SQLite
        rows = db.query_activities(days=7)
        return [
            {
                "date": r["date"], "name": r.get("name", ""),
                "sport": r.get("sport", ""), "duration_min": round((r.get("duration_sec") or 0) / 60),
                "tss": r.get("tss"), "avg_power": r.get("avg_power"), "avg_hr": r.get("avg_hr"),
                "id": r.get("id"),
            }
            for r in rows
        ]
    except Exception as _e:
        _log_error(error_codes.Codes.ACTIVITIES_LIST_FAILED, exc=_e)
        raise


# v1.3.3 — Banister τ defaults for the three Kontro 2026 components.
# CP slow / W' fast / Pmax mid — Fig. S2 single-athlete illustrative values.
# These are NOT population-fit; they are the same defaults the README and
# strain_score module document (line 60-62), so the energy-system breakdown
# chart can render *something* before tau_fitting per-component lands.
_V133_TAU_DEFAULTS_DAYS = {
    "cp":      (52.0, 10.0),
    "w_prime": ( 5.0,  5.0),
    "pmax":    (10.0,  4.0),
}


def _augment_wellness_with_3d_fitness(records: list[dict]) -> None:
    """v1.3.3 — populate cp_fitness / cp_fatigue / w_prime_* / pmax_* in-place.

    The v1.0.6 IMPL-3D-MODEL wired strain_score.compute_xss_components into
    every ride import — so per-day SS_x sums are written to athlete_metrics
    (rows ss_cp_daily / ss_w_prime_daily / ss_pmax_daily). What was missing
    was the per-component Banister convolution that turns those daily
    impulses into the fitness/fatigue curves the dashboard chart consumes.
    This helper closes that gap on read: it walks athlete_metrics once,
    builds an oldest-first contiguous per-day load series for each
    component, runs strain_score.banister() per record date, and writes
    the six keys onto each wellness dict in-place. Idempotent — keys that
    are already populated (e.g. by an upstream writer in a future patch)
    are not overwritten.
    """
    if not records:
        return
    dates = [r.get("id") or r.get("date") for r in records if isinstance(r, dict)]
    dates = [d for d in dates if isinstance(d, str) and len(d) >= 10]
    if not dates:
        return
    try:
        import strain_score as _ss
    except Exception as e:
        _log.debug(f"_augment_wellness_with_3d_fitness: strain_score import failed: {e}")
        return
    # Pull a window large enough to span the longest fitness τ × ~3 plus the
    # span of the requested wellness records. 365 days covers > 7×τ_CP=52d,
    # which is well past the Banister equilibrium tail.
    span_days = 365
    histories: dict[str, dict[str, float]] = {}
    for component, metric in (("cp", "ss_cp_daily"),
                              ("w_prime", "ss_w_prime_daily"),
                              ("pmax", "ss_pmax_daily")):
        try:
            rows = db.query_metric_history(metric, days=span_days)
        except Exception as e:
            _log.debug(f"_augment_wellness_with_3d_fitness: query {metric}: {e}")
            rows = []
        per_day: dict[str, float] = {}
        for r in rows:
            d = r.get("date")
            v = r.get("value")
            if not d or v is None:
                continue
            try:
                per_day[d] = per_day.get(d, 0.0) + float(v)
            except (TypeError, ValueError):
                continue
        histories[component] = per_day
    # If nothing in athlete_metrics yet, leave records alone — keys will be
    # absent and the chart's "not-yet-computed" placeholder still applies.
    if not any(histories.values()):
        return
    import datetime as _dt
    earliest = min(dates)
    try:
        start = _dt.date.fromisoformat(earliest) - _dt.timedelta(days=span_days)
    except Exception:
        return
    for rec in records:
        if not isinstance(rec, dict):
            continue
        rid = rec.get("id") or rec.get("date")
        if not isinstance(rid, str) or len(rid) < 10:
            continue
        try:
            today = _dt.date.fromisoformat(rid[:10])
        except Exception:
            continue
        for component, (tau_fit, tau_fat) in _V133_TAU_DEFAULTS_DAYS.items():
            per_day = histories.get(component) or {}
            # Build oldest-first contiguous series ending on `today`.
            series: list[float] = []
            d = start
            while d <= today:
                series.append(per_day.get(d.isoformat(), 0.0))
                d += _dt.timedelta(days=1)
            fit_v, fat_v, _form = _ss.banister(series, tau_fit=tau_fit, tau_fat=tau_fat)
            fit_key = f"{component}_fitness"
            fat_key = f"{component}_fatigue"
            # Idempotent: don't clobber an upstream-written value.
            if rec.get(fit_key) is None:
                rec[fit_key] = round(float(fit_v), 1)
            if rec.get(fat_key) is None:
                rec[fat_key] = round(float(fat_v), 1)


def _wellness_record_to_api_dict(w: dict) -> dict:
    """v4.5.0 — common ICU/local wellness record → /api/wellness response shape.

    v1.0.6 IMPL-3D-DASHBOARD: additionally surface per-day 3D fitness/fatigue
    components (cp_fitness, cp_fatigue, w_prime_fitness, w_prime_fatigue,
    pmax_fitness, pmax_fatigue) when present in the record. These mirror the
    Banister curves IMPL-3D-MODEL writes to athlete_metrics. Gracefully empty
    when the 3D model has not yet computed values for this date — preserves
    TSS-primary contract (CTL/ATL/TSB always present).

    v1.3.3: callers are expected to run _augment_wellness_with_3d_fitness on
    the record list BEFORE invoking this serializer, which writes the six
    Banister-curve keys in-place. This function still gracefully reads None
    for any key the augmenter declined to populate.
    """
    ctl = w.get("ctl")
    atl = w.get("atl")
    sport_info = w.get("sportInfo") or []
    eftp = (sport_info[0].get("eftp") if sport_info and isinstance(sport_info[0], dict) else None)
    return {
        "date": w.get("id"),
        "ctl": ctl,
        "atl": atl,
        "tsb": round(ctl - atl, 1) if (ctl is not None and atl is not None) else None,
        "hrv": w.get("hrv"),
        "rhr": w.get("restingHR"),
        "sleep_h": round(w.get("sleepSecs", 0) / 3600, 2) if w.get("sleepSecs") else None,
        "sleep_score": w.get("sleepScore"),
        "eftp": eftp,
        # v1.0.6 — 3D Banister components (None when not yet computed)
        "cp_fitness": w.get("cp_fitness"),
        "cp_fatigue": w.get("cp_fatigue"),
        "w_prime_fitness": w.get("w_prime_fitness"),
        "w_prime_fatigue": w.get("w_prime_fatigue"),
        "pmax_fitness": w.get("pmax_fitness"),
        "pmax_fatigue": w.get("pmax_fatigue"),
    }


@app.get("/api/wellness")
def api_wellness(days: int = Query(28),
                 date_from: str | None = Query(None, alias="from"),
                 date_to: str | None = Query(None, alias="to")):
    # 3.4.4: optional inclusive ISO date-range params (`from`/`to`) for the
    # Analysis Fitness & Form filter. When present, `days` is widened so the
    # upstream fetch reaches back to `from`, then the serialized records are
    # sliced to [from, to]. Records carry pre-computed per-day CTL/ATL, so
    # slicing never changes values — only which days are returned. Legacy
    # days-only callers (home boot ?days=90, default 28) are untouched.
    f_iso = t_iso = None
    if date_from or date_to:
        try:
            f = date.fromisoformat(date_from) if date_from else None
            t = date.fromisoformat(date_to) if date_to else None
        except ValueError:
            raise HTTPException(status_code=422,
                                detail="from/to must be ISO dates (YYYY-MM-DD)")
        if f and t and f > t:
            raise HTTPException(status_code=422, detail="from must be <= to")
        if f:
            days = max(days, (clock.today() - f).days + 1)
        f_iso = f.isoformat() if f else None
        t_iso = t.isoformat() if t else None

    def _window(out: list[dict]) -> list[dict]:
        if f_iso is None and t_iso is None:
            return out
        return [r for r in out
                if (f_iso is None or (r.get("date") or "") >= f_iso)
                and (t_iso is None or (r.get("date") or "") <= t_iso)]

    # v1.6.1: outer-fn try wraps the whole compute path so any unexpected
    # throw lands in the diag ring as E_WELLNESS_FETCH_FAILED before being
    # re-raised. Inner branches keep their own narrower fallbacks (cached
    # already logs E_CACHE_WELLNESS; ride_storage debug-logs).
    try:
        # Try live API first, fall back to local file store, then SQLite.
        data = cached(f"wellness_{days}", lambda: fetch_wellness(days))
        if data:
            # v1.3.3: convolve per-day SS_x series (athlete_metrics) into per-day
            # CP / W' / Pmax fitness curves and stamp them onto the records the
            # dashboard energy-system chart reads.
            _augment_wellness_with_3d_fitness(data)
            return _window([_wellness_record_to_api_dict(w) for w in data])
        # v4.5.0: local file store fallback. If empty AND ICU creds available,
        # do a one-shot fetch + persist before returning.
        try:
            import ride_storage as _rs
            local = _rs.load_recent_wellness(days=days)
        except Exception as e:
            _log.debug(f"/api/wellness local load failed: {e}")
            local = []
        if not local and _icu_credentials_present():
            try:
                _sync_icu_wellness(force=True, days=days)
                local = _rs.load_recent_wellness(days=days)
            except Exception as e:
                _log.debug(f"/api/wellness one-shot sync failed: {e}")
        if local:
            # local records are newest-first; API contract is unspecified order so
            # leave as-is.
            _augment_wellness_with_3d_fitness(local)
            return _window([_wellness_record_to_api_dict(w) for w in local])
        # Fallback to SQLite — reshape into the ICU-record shape the augmenter
        # expects (id-keyed) before convolving, then project the API dict.
        rows = db.query_wellness(days)
        shaped = [
            {
                "id": r["date"],
                "ctl": r.get("ctl"),
                "atl": r.get("atl"),
                "hrv": r.get("hrv"),
                "restingHR": r.get("rhr"),
                "sleepSecs": r.get("sleep_secs"),
                "sleepScore": r.get("sleep_score"),
                "sportInfo": [{"eftp": r.get("eftp")}] if r.get("eftp") is not None else [],
            }
            for r in rows
        ]
        _augment_wellness_with_3d_fitness(shaped)
        return _window([_wellness_record_to_api_dict(w) for w in shaped])
    except Exception as _e:
        _log_error(error_codes.Codes.WELLNESS_FETCH_FAILED, exc=_e, days=days)
        raise


# ═══════════════════════════════════════════════════════════════════════════════
# v1.3.6 — 3D-fitness backfill UX (status + on-demand backfill)
# ═══════════════════════════════════════════════════════════════════════════════
# IMPL-3D-INGEST (v1.0.6) wired ride_storage.compute_ride_xss but never landed
# a production callsite — so ss_*_daily rows in athlete_metrics stayed empty
# and the energy-system chart's placeholder fired forever. v1.3.6 adds the
# missing FIT-import hook (see _parse_fit_stats above) plus this two-endpoint
# pair so users can audit existing rides and run a one-shot backfill from
# the dashboard. The cutoff date is the v1.0.6 release — older rides predate
# the SS partition contract.
_V136_SS_CUTOFF_DATE = "2026-05-04"  # v1.0.6 release


def _v136_status_counts() -> dict:
    """Return {'rides_with_ss': K, 'rides_total_post_v106': M}.

    M counts rides whose started_at-day is on or after the v1.0.6 release
    date. K counts the subset of those rides whose ride-day already has an
    ss_cp_daily row in athlete_metrics. The dashboard placeholder consumes
    these to render an actionable line ("K of M rides have SS data").
    """
    try:
        import ride_storage as _rs136
        rides = _rs136.load_all_rides()
    except Exception:
        rides = []
    eligible = [
        r for r in rides
        if (r.get("started_at") or "")[:10] >= _V136_SS_CUTOFF_DATE
    ]
    M = len(eligible)
    try:
        rows = db.query_metric_history("ss_cp_daily", days=730)
        ss_dates = {r.get("date") for r in rows if r.get("date")}
    except Exception:
        ss_dates = set()
    K = sum(
        1 for r in eligible
        if (r.get("started_at") or "")[:10] in ss_dates
    )
    return {"rides_with_ss": K, "rides_total_post_v106": M}


@app.get("/api/wellness/3d-fitness-status")
def api_3d_fitness_status():
    """v1.3.6 — count K of M rides that have SS data."""
    return _v136_status_counts()


def _v136_extract_fit_power_series(fit_path: Path) -> list:
    """Lightweight FIT → power_series helper for the backfill loop.

    Mirrors the RecordMessage scan in `_parse_fit_stats` but returns ONLY
    the watts list — used so the backfill doesn't redo FTP-test detection
    or eFTP estimation on every old ride.
    """
    try:
        from fit_tool.fit_file import FitFile
    except Exception:
        return []
    try:
        ff = FitFile.from_file(str(fit_path))
    except Exception:
        return []
    out: list = []
    try:
        for rec in ff.records:
            msg = rec.message
            if type(msg).__name__ != "RecordMessage":
                continue
            try:
                pw = _fit_val(msg, "power")
                out.append(int(pw) if pw is not None else 0)
            except Exception:
                out.append(0)
    except Exception:
        return out
    return out


@app.post("/api/wellness/backfill-3d-fitness")
def api_backfill_3d_fitness(auto: int = Query(0)):
    """v1.3.6 — one-shot SS backfill for post-v1.0.6 rides.

    For each ride whose day has no ss_cp_daily row in athlete_metrics:
      * FIT rides → re-parse to recover power_series, call compute_ride_xss
      * ICU rides → fetch streams via training.fetch_activity_streams,
        pass watts to compute_ride_xss. When ICU streams come back empty,
        v1.8.8 Bug 9 also tries the local FIT file (if any) so rides
        captured on a Garmin AND synced to ICU still backfill.

    Returns ``{'backfilled': N, 'skipped': K, 'failed': F,
    'results': [{ride_id, skipped_reason}, ...],
    'aggregate_summary': {with_power, without_power, pre_cutoff,
    successfully_backfilled}}`` and busts the server cache so the next
    /api/wellness call re-augments with the fresh SS rows.

    v1.8.9 Bug 3 (master §3): the aggregate_summary buckets the per-ride
    results into the four categories the dashboard needs for its
    user-facing message — `with_power` (had power streams),
    `without_power` (HR-only rides — common indoor/Zwift no-meter),
    `pre_cutoff` (predate the SS feature), `successfully_backfilled`
    (skipped_reason None). The existing per-ride `results` list stays.

    `?auto=1` query: server logs auto vs manual trigger so we can tell
    user-initiated runs apart from the dashboard's auto-kick on load.
    No behavioral difference.

    `skipped_reason` is one of:
      ``"no_power_stream"`` — fetch returned nothing
      ``"all_zero"``        — stream present but every sample == 0
      ``"fit_missing"``     — FIT-sourced ride but the .fit file is gone
      ``"cutoff"``          — ride pre-dates the SS feature cutoff
      ``"already_cached"``  — day already has an ss_cp_daily row
      ``None``              — successful backfill or fall-through
    """
    if auto:
        _log.info("backfill-3d-fitness: auto-triggered run (?auto=1)")
    import ride_storage as _rs136
    try:
        rides = _rs136.load_all_rides()
    except Exception as e:
        _log.warning(f"backfill-3d-fitness load_all_rides failed: {e}")
        return {"backfilled": 0, "skipped": 0, "failed": 0, "results": []}

    try:
        rows = db.query_metric_history("ss_cp_daily", days=730)
        already = {r.get("date") for r in rows if r.get("date")}
    except Exception:
        already = set()

    backfilled = 0
    skipped = 0
    failed = 0
    results: list[dict] = []
    for r in rides:
        ride_id = r.get("ride_id") or r.get("external_id") or ""
        day = (r.get("started_at") or "")[:10]
        if not day or day < _V136_SS_CUTOFF_DATE:
            skipped += 1
            results.append({"ride_id": ride_id, "skipped_reason": "cutoff"})
            continue
        if day in already:
            skipped += 1
            results.append({"ride_id": ride_id, "skipped_reason": "already_cached"})
            continue
        try:
            ps: list = []
            fit_path_attr = r.get("_fit_path")
            if r.get("source") == "fit":
                if fit_path_attr:
                    fp = Path(fit_path_attr)
                    if fp.exists():
                        ps = _v136_extract_fit_power_series(fp)
                    else:
                        skipped += 1
                        results.append({
                            "ride_id": ride_id,
                            "skipped_reason": "fit_missing",
                        })
                        continue
                else:
                    skipped += 1
                    results.append({
                        "ride_id": ride_id,
                        "skipped_reason": "fit_missing",
                    })
                    continue
            elif r.get("source") == "icu" and r.get("external_id"):
                try:
                    import training as _training
                    streams = _training.fetch_activity_streams(
                        str(r["external_id"])
                    ) or {}
                except Exception:
                    streams = {}
                ps = streams.get("watts") or streams.get("power") or []
                # v1.8.8 Bug 9 — ICU streams unavailable? Try the local
                # FIT file if one was synced alongside the ICU record.
                if not ps and fit_path_attr:
                    fp = Path(fit_path_attr)
                    if fp.exists():
                        ps = _v136_extract_fit_power_series(fp)
            if not ps:
                skipped += 1
                results.append({
                    "ride_id": ride_id,
                    "skipped_reason": "no_power_stream",
                })
                continue
            if not any(int(p or 0) > 0 for p in ps):
                skipped += 1
                results.append({
                    "ride_id": ride_id,
                    "skipped_reason": "all_zero",
                })
                continue
            comp = _rs136.compute_ride_xss(
                ps, started_at=r.get("started_at"),
            )
            if comp:
                backfilled += 1
                already.add(day)
                results.append({
                    "ride_id": ride_id,
                    "skipped_reason": None,
                })
            else:
                skipped += 1
                results.append({
                    "ride_id": ride_id,
                    "skipped_reason": "no_power_stream",
                })
        except Exception as e:
            _log.debug(f"backfill-3d-fitness ride {r.get('ride_id')}: {e}")
            failed += 1
            results.append({
                "ride_id": ride_id,
                "skipped_reason": "no_power_stream",
            })

    # Bust cache so /api/wellness re-augments next read.
    try:
        clear_cache()
    except Exception:
        pass

    # v1.8.9 Bug 3 (master §3/§10) — bucket per-ride results into the four
    # categories the dashboard renders in its user-facing summary.
    #   with_power: rides that had a usable power stream (incl. successful)
    #   without_power: HR-only rides skipped for lack of watts
    #   pre_cutoff: rides predating the SS feature
    #   successfully_backfilled: subset that wrote a row (skipped_reason None)
    pre_cutoff_n = sum(1 for r in results
                       if r.get("skipped_reason") == "cutoff")
    without_power_n = sum(1 for r in results
                          if r.get("skipped_reason")
                          in ("no_power_stream", "all_zero", "fit_missing"))
    success_n = backfilled
    with_power_n = success_n + sum(1 for r in results
                                    if r.get("skipped_reason")
                                    == "already_cached")
    aggregate_summary = {
        "with_power": int(with_power_n),
        "without_power": int(without_power_n),
        "pre_cutoff": int(pre_cutoff_n),
        "successfully_backfilled": int(success_n),
    }
    return {
        "backfilled": backfilled,
        "skipped": skipped,
        "failed": failed,
        "results": results,
        "aggregate_summary": aggregate_summary,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# v1.0.7 IMPL-HRV-PROMPT — HRV-recording prompt endpoints
# ═══════════════════════════════════════════════════════════════════════════════

def _hrv_prompt_state_path() -> Path:
    """Per-profile JSON file holding HRV-prompt dismissal flags.

    Layout::
        {"version_dismissed": {"<version>": True, ...},
         "permanent_dismissed": True | False}

    Stored under DATA_DIR (same pattern as ``_readiness_revert_flag_path``)
    so it survives across runs. Tests can stub by monkeypatching DATA_DIR
    or this helper directly.
    """
    return DATA_DIR / "hrv_prompt_state.json"


def _read_hrv_prompt_state() -> dict:
    """Load the dismissal-flag JSON; return an empty-default shape on error."""
    p = _hrv_prompt_state_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError) as e:
        _log.debug(f"_read_hrv_prompt_state read failed: {e}")
    return {"version_dismissed": {}, "permanent_dismissed": False}


def _write_hrv_prompt_state(state: dict) -> None:
    """Persist the dismissal-flag JSON. Failure is logged-only."""
    p = _hrv_prompt_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(state, indent=2), encoding="utf-8")
    except OSError as e:
        _log.debug(f"_write_hrv_prompt_state write failed: {e}")


def _last_ride_for_hrv_prompt() -> dict | None:
    """Return the most-recent saved ride (ICU-or-FIT), normalized into a dict.

    Used by ``/api/wellness/hrv-recording-status`` to inspect
    ``dfa_alpha1_status`` + device fields. Falls back to None when no rides
    exist or the loader raises.
    """
    try:
        merged = _load_all_rides_safe()
    except Exception as e:
        _log.debug(f"_last_ride_for_hrv_prompt: load failed: {e}")
        return None
    if not merged:
        return None
    # merged is sorted newest-first per ride_storage.load_all_rides contract.
    most_recent = merged[0]
    if not isinstance(most_recent, dict):
        return None
    rid = most_recent.get("ride_id") or ""
    # ICU records carry dfa_alpha1_status inline (written by
    # _augment_icu_record_with_dfa). FIT bare entries do not — we need to
    # build the normalized record to read the status.
    if isinstance(rid, str) and rid.startswith("fit_"):
        try:
            stem = rid[4:]
            fit_path = _rides_fit_dir() / f"{stem}.fit"
            if fit_path.exists():
                return _build_fit_normalized(fit_path, rid)
        except Exception as e:
            _log.debug(f"_last_ride_for_hrv_prompt: FIT normalize failed: {e}")
    return most_recent


def _recent_rides_have_dfa_computed(n: int = 3) -> bool:
    """v1.8.3 BUG-B: any of the last ``n`` rides has dfa_alpha1_status='computed'?

    Walks the newest-first ride list and normalizes FIT-only entries on the
    fly (same pattern as :func:`_last_ride_for_hrv_prompt`). Returns True as
    soon as one ride reports ``dfa_alpha1_status == 'computed'``.

    Used by ``/api/wellness/hrv-recording-status`` to suppress the
    educational toast when the rider is already capturing HRV on most
    rides — a single misfire (strap dropout, no HR connected) shouldn't
    trigger "enable HRV recording on your Garmin".
    """
    try:
        merged = _load_all_rides_safe()
    except Exception as e:
        _log.debug(f"_recent_rides_have_dfa_computed: load failed: {e}")
        return False
    if not merged:
        return False
    for ride in merged[: max(1, int(n))]:
        if not isinstance(ride, dict):
            continue
        status = ride.get("dfa_alpha1_status")
        if status == "computed":
            return True
        # FIT-only entries don't carry dfa_alpha1_status inline; normalize.
        if status is None:
            rid = ride.get("ride_id") or ""
            if isinstance(rid, str) and rid.startswith("fit_"):
                try:
                    stem = rid[4:]
                    fit_path = _rides_fit_dir() / f"{stem}.fit"
                    if fit_path.exists():
                        rec = _build_fit_normalized(fit_path, rid)
                        if isinstance(rec, dict) and rec.get("dfa_alpha1_status") == "computed":
                            return True
                except Exception as e:
                    _log.debug(
                        f"_recent_rides_have_dfa_computed: FIT normalize failed: {e}"
                    )
    return False


@app.get("/api/wellness/hrv-recording-status")
def api_hrv_recording_status():
    """v1.0.7 IMPL-HRV-PROMPT — does the home-page toast fire?

    Checks the most-recent saved ride. Returns
    ``should_show_prompt=True`` when:
      * the ride's ``dfa_alpha1_status == 'no_rr_data'`` (FIT had HR
        records but no HrvMessage records — rider didn't enable HRV
        recording on their Garmin), AND
      * neither ``hrv_prompt_dismissed_<VERSION>`` nor
        ``hrv_prompt_never_show`` flag is set.

    The response also names the device when known so the dashboard modal
    can pre-highlight the rider's row in the device-by-device path table.
    """
    state = _read_hrv_prompt_state()
    if state.get("permanent_dismissed"):
        return {
            "should_show_prompt": False,
            "device_product_name": None,
            "last_ride_id": None,
            "dismissal_state": "permanent",
            "version": _VERSION,
        }
    version_flag = bool((state.get("version_dismissed") or {}).get(_VERSION))
    if version_flag:
        return {
            "should_show_prompt": False,
            "device_product_name": None,
            "last_ride_id": None,
            "dismissal_state": "version",
            "version": _VERSION,
        }

    ride = _last_ride_for_hrv_prompt()
    if not isinstance(ride, dict):
        return {
            "should_show_prompt": False,
            "device_product_name": None,
            "last_ride_id": None,
            "dismissal_state": "none",
            "version": _VERSION,
        }
    status = ride.get("dfa_alpha1_status")
    should_show = status == "no_rr_data"
    # v1.8.3 BUG-B: suppress the toast when ANY of the last 3 rides has
    # dfa_alpha1_status='computed'. A single misfire (strap dropout, HR
    # not connected) shouldn't trigger "enable HRV recording" when the
    # rider is already capturing HRV on most rides.
    if should_show and _recent_rides_have_dfa_computed(n=3):
        should_show = False
    return {
        "should_show_prompt": bool(should_show),
        "device_product_name": ride.get("device_product_name"),
        "last_ride_id": ride.get("ride_id"),
        "dismissal_state": "none",
        "version": _VERSION,
    }


@app.post("/api/wellness/hrv-recording-dismiss")
def api_hrv_recording_dismiss(payload: dict = Body(...)):
    """v1.0.7 IMPL-HRV-PROMPT — record toast dismissal.

    ``level='version'`` suppresses the toast for the current ``_VERSION``
    (re-fires on the next release). ``level='permanent'`` suppresses it
    forever. Anything else returns 400.
    """
    if not isinstance(payload, dict):
        return JSONResponse({"error": "bad payload"}, 400)
    level = payload.get("level")
    if level not in ("version", "permanent"):
        return JSONResponse({"error": "level must be 'version' or 'permanent'"}, 400)
    state = _read_hrv_prompt_state()
    if not isinstance(state, dict):
        state = {"version_dismissed": {}, "permanent_dismissed": False}
    state.setdefault("version_dismissed", {})
    if level == "permanent":
        state["permanent_dismissed"] = True
    else:
        state["version_dismissed"][_VERSION] = True
    _write_hrv_prompt_state(state)
    return {"ok": True, "level": level, "version": _VERSION}


# ═══════════════════════════════════════════════════════════════════════════════
# WORKOUT APIs
# ═══════════════════════════════════════════════════════════════════════════════

# v4.2.0 IMPL-LIBRARY: helper to scan a single ZWO into the structural
# metrics the score helper + filters need. Extracted so /api/workouts and
# /api/workouts/tags can share the parse without duplicating XML walking.
def _np_fraction_from_samples(samples: "list[float]") -> float:
    """Coggan NP as an FTP fraction over a 1 Hz planned-power series.

    30-s simple rolling mean, full windows only (first value covers samples
    [0, 30)), 4th-power mean, 4th root. Byte-identical to the planner's
    training_planner._np_fraction_from_samples — the two scanners must stay
    in lockstep (v4.1.1 Bug C / score_sync contract).
    """
    n = len(samples)
    if n < 30:
        return 0.0
    s = sum(samples[:30])
    p4 = (s / 30.0) ** 4
    count = 1
    for i in range(30, n):
        s += samples[i] - samples[i - 30]
        p4 += (s / 30.0) ** 4
        count += 1
    return (p4 / count) ** 0.25


def _scan_zwo_for_library(zwo_path: Path) -> dict | None:
    """Parse one ZWO file → metrics dict, or None on parse error / empty.

    Returns a dict with: name, description, total_sec, tss, if_val,
    z1_sec..z6_sec, distinct_high_targets (set), has_vo2_intensity (bool),
    zwo_tags (list).  Mirrors the planner's parse so structural metrics
    are identical between code paths (closes v4.1.1 Bug C PARTIAL).
    """
    try:
        tree = ET.parse(zwo_path)
    except (ET.ParseError, OSError):
        return None
    root = tree.getroot()
    name = (root.findtext("name") or zwo_path.stem).strip()
    description = (root.findtext("description") or "").strip()
    workout_el = root.find("workout")
    if workout_el is None:
        return None
    zwo_tags: list[str] = []
    tags_el = root.find("tags")
    if tags_el is not None:
        for tag_el in tags_el.findall("tag"):
            nm = tag_el.get("name")
            if nm:
                zwo_tags.append(nm.strip())

    total_sec = 0
    z1_sec = z2_sec = z3_sec = z4_sec = z5_sec = z6_sec = 0
    distinct_high_targets: set = set()
    has_vo2_intensity = False
    # v3.5.0: 1 Hz planned-power series (FTP fractions) for Coggan NP —
    # replaces the per-segment RMS accumulation (mirrors the planner).
    samples: list = []

    def _acc_zone(power_pct: float, dur_s: int):
        nonlocal z1_sec, z2_sec, z3_sec, z4_sec, z5_sec, z6_sec
        if power_pct < 56: z1_sec += dur_s
        elif power_pct < 76: z2_sec += dur_s
        elif power_pct < 91: z3_sec += dur_s
        elif power_pct < 106: z4_sec += dur_s
        elif power_pct < 121: z5_sec += dur_s
        else: z6_sec += dur_s

    def _acc_structure(power_pct: float):
        nonlocal has_vo2_intensity
        if power_pct > 75:
            distinct_high_targets.add(round(power_pct))
        if power_pct > 105:
            has_vo2_intensity = True

    for seg in workout_el:
        tag = seg.tag
        if tag in ("Warmup", "Cooldown", "Ramp"):
            dur = int(float(seg.get("Duration", 0)))
            plo = float(seg.get("PowerLow", 0.5))
            phi = float(seg.get("PowerHigh", 0.7))
            total_sec += dur
            samples.extend(plo + (phi - plo) * (t + 0.5) / dur for t in range(dur))
            # v2.1.0: slice the ramp across the zones it SWEEPS, mirroring the
            # planner's load_workout_library parse (training_planner.py, v2.0.6).
            # Binning the whole duration at mean power here (while the planner
            # sliced) drifted the two scanners' zone seconds → score_sync mismatch
            # (e.g. neuromuscular_9x30s-2min_175pct_52min.zwo scored 5 vs 6).
            # 1-second slices — see the note on the planner's copy of this
            # scanner. The two must stay in step: they are compared file by
            # file in tests/test_zone_binning.py.
            _RAMP_SLICES = max(20, int(dur))
            for _i in range(_RAMP_SLICES):
                _acc_zone((plo + (phi - plo) * (_i + 0.5) / _RAMP_SLICES) * 100, dur / _RAMP_SLICES)
            _acc_structure(phi * 100)
        elif tag == "SteadyState":
            dur = int(float(seg.get("Duration", 0)))
            p = float(seg.get("Power", 0.65))
            total_sec += dur
            samples.extend([p] * dur)
            _acc_zone(p * 100, dur)
            _acc_structure(p * 100)
        elif tag == "IntervalsT":
            reps = int(seg.get("Repeat", 1))
            on_s = int(float(seg.get("OnDuration", 0)))
            off_s = int(float(seg.get("OffDuration", 0)))
            on_p = float(seg.get("OnPower", 1.0))
            off_p = float(seg.get("OffPower", 0.5))
            total_sec += reps * (on_s + off_s)
            samples.extend(([on_p] * on_s + [off_p] * off_s) * reps)
            _acc_zone(on_p * 100, reps * on_s)
            _acc_zone(off_p * 100, reps * off_s)
            _acc_structure(on_p * 100)
            _acc_structure(off_p * 100)
        elif tag == "FreeRide":
            dur = int(float(seg.get("Duration", 0)))
            total_sec += dur
            samples.extend([0.65] * dur)
            _acc_zone(65, dur)

    if total_sec < 30:
        # NP is undefined below one 30-s window (mirrors the planner's skip).
        return None
    if_val = _np_fraction_from_samples(samples)
    tss_np = (total_sec / 3600) * (if_val ** 2) * 100
    return {
        "name": name, "description": description,
        "total_sec": total_sec, "tss": tss_np, "if_val": if_val,
        "z1_sec": z1_sec, "z2_sec": z2_sec, "z3_sec": z3_sec,
        "z4_sec": z4_sec, "z5_sec": z5_sec, "z6_sec": z6_sec,
        "distinct_high_targets": distinct_high_targets,
        "has_vo2_intensity": has_vo2_intensity,
        "zwo_tags": zwo_tags,
    }


# v4.2.0 IMPL-LIBRARY: tag cache for /api/workouts/tags. Built lazily on
# first request and invalidated when the workout-dir mtime drifts. The
# library has ~3000 files so a 100ms scan is acceptable on cold cache;
# subsequent requests hit memory.
_LIBRARY_TAGS_CACHE: tuple[float, list[str]] | None = None
_LIBRARY_TAGS_LOCK = threading.Lock()


def _compute_library_tags() -> list[str]:
    """Scan all ZWO files for distinct tags. Returns sorted unique list."""
    if not active_workout_dir().exists():
        return []
    seen: set[str] = set()
    for zwo_path in active_workout_dir().glob("*.zwo"):
        try:
            tree = ET.parse(zwo_path)
        except (ET.ParseError, OSError):
            continue
        tags_el = tree.getroot().find("tags")
        if tags_el is None:
            continue
        for tag_el in tags_el.findall("tag"):
            nm = tag_el.get("name")
            if nm:
                seen.add(nm.strip())
    return sorted(seen)


def _get_library_tags_cached() -> list[str]:
    """Return cached distinct tag list, refreshing on dir mtime drift."""
    global _LIBRARY_TAGS_CACHE
    try:
        mtimes = [p.stat().st_mtime for p in active_workout_dir().glob("*.zwo")]
    except OSError:
        mtimes = []
    sig = max(mtimes) if mtimes else 0.0
    with _LIBRARY_TAGS_LOCK:
        if _LIBRARY_TAGS_CACHE and abs(_LIBRARY_TAGS_CACHE[0] - sig) < 0.5:
            return _LIBRARY_TAGS_CACHE[1]
        tags_list = _compute_library_tags()
        _LIBRARY_TAGS_CACHE = (sig, tags_list)
        return tags_list


@app.get("/api/workouts/tags")
def api_workouts_tags():
    """Return the sorted distinct list of tags across the library.

    v4.2.0 IMPL-LIBRARY: powers the tag multi-select chip-list in the
    library browser. Cached in-memory; refreshes when active_workout_dir() mtimes
    drift.
    """
    return {"tags": _get_library_tags_cached()}


# R2 (2026-07-07): S1 — mtime-keyed cache of the fully-parsed library rows.
# /api/workouts previously re-parsed all ~4,250 ZWO files on EVERY request
# (0.9-1.5s measured in-process), which is what made library search feel dead
# (each keystroke pause = full rescan) and armed the stale-response race in
# the dashboard's loadWorkouts. We cache the PARSE, never per-query results:
# every filter/sort/limit param still runs per-request over the cached rows.
_LIBRARY_ROWS_CACHE: tuple[tuple, list[dict]] | None = None
_LIBRARY_ROWS_LOCK = threading.Lock()


def _build_library_rows() -> list[dict]:
    """Parse every ZWO in active_workout_dir() into its full, filter-ready library row.

    R2 (2026-07-07): S1 — extracted verbatim from the api_workouts per-file
    loop so the parse can be cached. Everything computed here is
    query-INDEPENDENT (name, score, protocol, zones, classification fields);
    api_workouts applies all query params (min_score / duration / type /
    tags / flags / search / sort / limit) on top of the cached output.
    """
    rows: list[dict] = []
    if not active_workout_dir().exists():
        return rows

    # v4.1.2 IMPL-CLASSIFIER: load the content-classification cache once per
    # rebuild. Same source as training_planner so the library browser and
    # planner agree on protocol categorisation. Empty {} fallback when the
    # cache is missing — the dominant-zone heuristic kicks back in.
    _content_classifications = tp._load_content_classifications()
    _CONTENT_TO_PROTOCOL = tp._CONTENT_TO_PROTOCOL

    for zwo_path in sorted(active_workout_dir().glob("*.zwo")):
        scan = _scan_zwo_for_library(zwo_path)
        if not scan:
            continue
        name = scan["name"]
        description = scan["description"]
        total_sec = scan["total_sec"]
        tss_accum = scan["tss"]
        if_val = scan["if_val"]
        z1_sec, z2_sec, z3_sec = scan["z1_sec"], scan["z2_sec"], scan["z3_sec"]
        z4_sec, z5_sec, z6_sec = scan["z4_sec"], scan["z5_sec"], scan["z6_sec"]
        zwo_tags = scan["zwo_tags"]
        dur_min = total_sec / 60

        # Zone percentages.
        zp = lambda s: round(s / total_sec * 100, 1) if total_sec else 0

        # Session type classification (for Protocol field).
        # v4.1.2 IMPL-CLASSIFIER: prefer the content-based 12-rule cascade
        # output (workouts/.content_classification.json) over the
        # dominant-zone heuristic. Falls back to dominant zone when the
        # cache is missing or this file isn't classified.
        content_entry = (_content_classifications or {}).get(zwo_path.name) or {}
        content_class_val = content_entry.get("primary") or ""
        content_confidence = content_entry.get("confidence") or 0.0
        secondary_flags = content_entry.get("secondary_flags") or {}
        # R2 pass-2 (2026-07-07): union the classifier's per-file tags
        # (is_ronnestad, ftp_test_*) into the row Tags. They live ONLY in
        # .content_classification.json — the ZWO <tags> blocks don't carry
        # them — and both the tags= filter and the search haystack read row
        # Tags. (/api/workouts/tags chip-list still scans ZWO XML only.)
        content_tags = content_entry.get("tags") or []
        if content_tags:
            zwo_tags = zwo_tags + [t for t in content_tags if t not in zwo_tags]
        if content_class_val and content_confidence >= 0.6:
            protocol = _CONTENT_TO_PROTOCOL.get(content_class_val, "Mixed")
        else:
            zones_sec = [z1_sec, z2_sec, z3_sec, z4_sec, z5_sec, z6_sec]
            dom_idx = zones_sec.index(max(zones_sec)) if any(zones_sec) else 1
            protocol_map = {
                0: "Recovery", 1: "Endurance", 2: "Tempo",
                3: "Sweet Spot / Threshold", 4: "VO2max", 5: "Anaerobic/Sprint"
            }
            protocol = protocol_map.get(dom_idx, "Mixed")

        # v4.2.0 IMPL-LIBRARY: route through shared training_planner.score_workout
        # so /api/workouts and the planner produce identical scores per file
        # (closes v4.1.1 Bug C PARTIAL — distinct_high_targets vs zone-count
        # drift).
        score = max(1, min(10, int(round(tp.score_workout({
            "tss": tss_accum,
            "total_sec": total_sec,
            "z1_sec": z1_sec, "z2_sec": z2_sec, "z3_sec": z3_sec,
            "z4_sec": z4_sec, "z5_sec": z5_sec, "z6_sec": z6_sec,
            "distinct_high_targets": scan["distinct_high_targets"],
            "has_vo2_intensity": scan["has_vo2_intensity"],
        })))))

        # Title validator: strip misleading prefixes when IF contradicts name
        orig_name = name
        name_lower = name.lower()
        if if_val > 0.75 and 'recovery' in name_lower:
            name = re.sub(r'^recovery\s+', '', name, flags=re.I)
            logging.getLogger(__name__).warning(f"Stripped 'Recovery' from {orig_name} (IF={if_val:.2f})")
        if if_val < 0.55 and any(w in name_lower for w in ('vo2', 'threshold', 'supra')):
            name = f"Easy {name}"

        rows.append({
            "Name": name,
            "Category": "Workout",
            "File": zwo_path.name,
            "Duration(min)": round(dur_min, 1),
            "TSS": round(tss_accum, 1),
            "IF": round(if_val, 3),
            "Score": score,
            "Protocol": protocol,
            "Notes": description[:200],
            "Z1%": zp(z1_sec), "Z2%": zp(z2_sec), "Z3%": zp(z3_sec),
            "Z4%": zp(z4_sec), "Z5%": zp(z5_sec), "Z6%": zp(z6_sec),
            # T5 (v4.1.0): surface ZWO <tags> so UI + planner can filter.
            "Tags": zwo_tags,
            # v4.1.2 IMPL-CLASSIFIER: content-classification fields lifted
            # from workouts/.content_classification.json. content_class is
            # the 12-rule cascade primary type; secondary_flags carry the
            # hybrid markers (has_threshold_work / has_vo2_work / has_sprints
            # / has_sweet_spot_work / pattern_over_under / pattern_microinterval
            # / polarized_consistent / pyramidal_consistent).
            "content_class": content_class_val,
            "secondary_flags": secondary_flags,
            "confidence": content_confidence,
            # v1.8.20 — canonical human title from the content classification.
            # The library/picker UIs render this instead of the ZWO <name> tag,
            # which disagrees with the real content type for ~50% of files.
            "display_name": content_entry.get("display_name") or "",
        })
    return rows


def _get_library_rows_cached() -> list[dict]:
    """Return the parsed library rows, re-scanning only when the dir drifts.

    R2 (2026-07-07): S1 — key = (dir, max zwo mtime, zwo count). mtime
    catches edits/additions, count catches deletions (which can only lower
    the max mtime), dir catches a profile switching workout_dir at runtime
    (_apply_profile_paths). Parse runs inside the lock — mirrors
    _get_library_tags_cached — so concurrent cold requests don't duplicate
    the 1.5s scan. Rows are shared across requests: treat them as read-only.
    """
    global _LIBRARY_ROWS_CACHE
    # os.scandir instead of glob()+Path.stat(): the signature sweep runs on
    # EVERY request, and pathlib costs ~60ms over 4,250 files vs ~20ms for
    # scandir (DirEntry.stat avoids re-resolving each path). The dot-file
    # exclusion mirrors glob("*.zwo") semantics exactly.
    try:
        with os.scandir(active_workout_dir()) as it:
            mtimes = [e.stat().st_mtime for e in it
                      if e.name.endswith(".zwo") and not e.name.startswith(".")]
    except OSError:
        mtimes = []
    sig = (str(active_workout_dir()), max(mtimes) if mtimes else 0.0, len(mtimes))
    with _LIBRARY_ROWS_LOCK:
        if _LIBRARY_ROWS_CACHE and _LIBRARY_ROWS_CACHE[0] == sig:
            return _LIBRARY_ROWS_CACHE[1]
        rows = _build_library_rows()
        _LIBRARY_ROWS_CACHE = (sig, rows)
        return rows


# ═══ Library search pass 2 (R2, 2026-07-07) ═════════════════════════════════
# A cyclist-query parser for /api/workouts?search=. Pass 1 gave the row cache,
# the race guard and whole-query family aliases; pass 2 makes the box
# understand real training queries:
#
#     "threshold 3x16"   → class family AND structure token
#     "ss 90min"         → sweet-spot family, duration 90min ±12%
#     ">120 vo2"         → vo2 family, strictly longer than 120min
#     "30/15" / "30-15"  → the library's 30s15s microinterval token
#     "@105" / "105%"    → rows whose names carry a percent within ±2 pts
#     "treshold"         → typo-rescued to threshold (class vocab only)
#     "rønnestad"        → ø→o, is_ronnestad tag OR 30s15s-pattern rows
#
# Shape: three small importable steps + two shared normalizers, orchestrated
# inline by api_workouts (tests import these directly):
#     _search_parse_query(q)   → intents + residual AND-tokens  (pure)
#     _search_match_row(...)   → bool, AND semantics over one cached row
#     _search_score_row(...)   → relevance points (sort=relevance)
# Pass-1's whole-query _SEARCH_TYPE_ALIASES dict is absorbed into
# _SEARCH_SYNONYMS below (same family values, now applied per-token).


























@app.get("/api/workouts")
def api_workouts(
    # R2 (2026-07-07): injected by FastAPI on HTTP requests; None when the
    # picker (see ~L6000) calls this function directly from python. The
    # None default keeps those direct calls working — their except-Exception
    # wrappers would otherwise swallow a TypeError SILENTLY and the picker
    # would degrade to empty suggestion lists.
    response: Response = None,
    min_score: int = Query(0), session_type: str = Query(None),
    max_duration: int = Query(300), min_duration: int = Query(0),
    duration_min: int | None = None,
    duration_max: int | None = None,
    limit: int = Query(3000), sort: str = Query("score_desc"),
    tags: str = Query(None),
    content_class: str | None = None,
    has_flag: str | None = None,
    search: str | None = None,
):
    """Scan flat workout library (workouts/*.zwo). Extract metadata from each ZWO.

    T5 (v4.1.0): the ``<tags><tag name="…"/></tags>`` block is now parsed
    and surfaced on each row. Pass ``?tags=ftp_test`` (or any comma-separated
    list) to filter the library to just tagged workouts — used by the
    "FTP Tests" tab in the library UI and by the planner to avoid
    accidentally scheduling a test on a random Tuesday.

    v4.2.0 IMPL-LIBRARY query params:
      - ``content_class=vo2max`` — exact-match against the 12-rule cascade
        primary type (recovery, endurance, tempo, sweet_spot, threshold,
        over_under, vo2max, vo2_short, anaerobic, neuromuscular, ftp_test,
        mixed).
      - ``tags=ftp_test,polarized_consistent`` — OR-match (any tag matches).
      - ``duration_min=30&duration_max=60`` — minutes; either bound optional.
      - ``has_flag=pattern_over_under`` — secondary-flag boolean filter.
      - ``search=…`` — R2 pass-2 (2026-07-07): tokenized cyclist-query
        search over the cached rows. Multi-word queries AND their tokens
        across Name/display_name/File/Protocol/content_class/Tags
        (lowercased, [-_/]→space, ø→o/×→x). Intent tokens are parsed out
        first: durations ("90min"/"1h30"/"1.5h" = ±12%; "<60"/">120"/
        "60-90" literal, intersected with the slider), structures
        ("3x16", "30/15"→30s15s), intensity ("@105" = ±2 pts vs the
        "@ 91%" embedded in names), synonyms (ss/ou/vo2/ronnestad/micro/
        strides/ladder/test — see _SEARCH_SYNONYMS), and a bounded
        Levenshtein-1 typo rescue over the class vocabulary. Family
        tokens keep pass-1 semantics: class-name PREFIX match
        (threshold → threshold + threshold_ladder), reverting to plain
        substring when an explicit ``content_class`` filter is active.
        Parsed intents echo back via ``X-Search-Intents`` and the
        highlightable tokens via ``X-Search-Tokens`` response headers.
      - ``sort=relevance|score_desc|score_asc|duration_desc|duration_asc|
        name_asc|name_desc`` — default ``score_desc``; ``relevance`` ranks
        by search score (no active search → duration_asc fallback).

    R2 (2026-07-07): S1 — filters run over the mtime-keyed row cache
    (_get_library_rows_cached) instead of re-parsing ~4,250 XML files per
    request (0.9-1.5s → warm requests in the tens of ms). The pre-filter
    library size is exposed as the ``X-Library-Total`` header so the UI can
    render "Showing N of M" and surface ``limit`` truncation (D2) instead
    of silently dropping the tail.
    """
    workouts = []
    if not active_workout_dir().exists():
        return []

    filter_tags: set[str] = set()
    if tags:
        filter_tags = {t.strip().lower() for t in tags.split(",") if t.strip()}

    # v4.2.0: resolve duration window. The new ``duration_min/max`` params
    # take precedence over the legacy ``min_duration/max_duration`` aliases
    # so callers using either spelling get the same behaviour.
    eff_min_dur = duration_min if duration_min is not None else min_duration
    eff_max_dur = duration_max if duration_max is not None else max_duration

    search_lower = (search or "").strip().lower()
    content_class_lower = (content_class or "").strip().lower()
    has_flag_key = (has_flag or "").strip()

    rows = _get_library_rows_cached()
    total = len(rows)  # pre-filter library size → X-Library-Total header

    # R2 pass-2 (2026-07-07): tokenized cyclist-query search. The whole-query
    # alias hijack is superseded by _search_parse_query (family aliases now
    # apply per-token; the Type-filter substring guard lives in
    # _search_match_row). Haystacks are cached per rows-generation; the typo
    # rescue needs them, so it runs here rather than inside the pure parser.
    parsed = _search_parse_query(search_lower) if search_lower else None
    hays = _get_search_haystacks(rows) if parsed is not None else None
    if parsed is not None:
        _search_apply_typo_fix(parsed, hays)
    matched_hays: list[str] = []   # aligned with `workouts` for sort=relevance

    for _row_idx, row in enumerate(rows):
        if row["Score"] < min_score:
            continue
        if not (eff_min_dur <= row["Duration(min)"] <= eff_max_dur):
            continue
        if session_type and session_type.lower() not in row["Protocol"].lower():
            continue
        if filter_tags:
            row_tags_lower = {t.lower() for t in row["Tags"]}
            if not (filter_tags & row_tags_lower):
                continue
        # v4.2.0: content_class exact-match. Empty/missing classification
        # never matches a non-empty filter, so the user can't accidentally
        # surface uncategorised files via the dropdown.
        if content_class_lower:
            if (row["content_class"] or "").lower() != content_class_lower:
                continue
        # v4.2.0: secondary-flag boolean filter.
        if has_flag_key:
            if not bool((row["secondary_flags"] or {}).get(has_flag_key)):
                continue
        # R2 pass-2 (2026-07-07): tokenized AND-match over the normalized
        # haystack (Name/display_name/File/Protocol/content_class/Tags) plus
        # the parsed duration/structure/percent/family/semantic intents.
        # Duration intents intersect with the slider window checked above.
        if parsed is not None:
            if not _search_match_row(row, hays[_row_idx], parsed,
                                     bool(content_class_lower)):
                continue
            matched_hays.append(hays[_row_idx])
        workouts.append(row)

    # v4.2.0 IMPL-LIBRARY: explicit sort enum. Legacy ``score`` / ``duration``
    # / ``tss`` aliases stay supported (callers from older clients) so we
    # don't break the planner's score-ranked iteration.
    # R2 pass-2: ``relevance`` ranks by _search_score_row when a search is
    # active (ties → duration asc, then name); without a search it falls back
    # to duration_asc, the library's no-search default order.
    if sort == "relevance":
        if parsed is not None:
            order = sorted(
                range(len(workouts)),
                key=lambda i: (-_search_score_row(workouts[i], matched_hays[i], parsed),
                               workouts[i]["Duration(min)"], workouts[i]["Name"].lower()))
            workouts = [workouts[i] for i in order]
        else:
            workouts.sort(key=lambda w: w["Duration(min)"])
    elif sort in ("score_desc", "score"):
        workouts.sort(key=lambda w: (-w["Score"], -w["Duration(min)"]))
    elif sort == "score_asc":
        workouts.sort(key=lambda w: (w["Score"], w["Duration(min)"]))
    elif sort in ("duration_desc",):
        workouts.sort(key=lambda w: -w["Duration(min)"])
    elif sort in ("duration_asc", "duration"):
        workouts.sort(key=lambda w: w["Duration(min)"])
    elif sort == "name_asc":
        workouts.sort(key=lambda w: w["Name"].lower())
    elif sort == "name_desc":
        workouts.sort(key=lambda w: w["Name"].lower(), reverse=True)
    elif sort == "tss":
        workouts.sort(key=lambda w: -w["TSS"])

    result = workouts[:limit]
    # R2 (2026-07-07): dual return. Direct python callers (the picker,
    # ~L6000) get the plain list they always got. HTTP requests (FastAPI
    # injected `response`) get a pre-serialized JSONResponse instead:
    # FastAPI's jsonable_encoder walk over ~4,250 cached rows costs ~360ms
    # per request — 7x the raw json.dumps (~50ms) — and would eat the whole
    # S1 cache win. The X-Library-Total header must ride on the returned
    # response object; FastAPI ignores the injected one when a Response is
    # returned directly.
    if response is None:
        return result
    headers = {"X-Library-Total": str(total)}
    # R2 pass-2: echo what the parser UNDERSTOOD so the count line can render
    # "understood: threshold · 3×16 · ~90min", and which tokens the Name cell
    # should <mark>. json.dumps ensure_ascii keeps the header values latin-1
    # safe (the "→" in typo-rescue labels arrives as a \u escape).
    if parsed is not None:
        if parsed["intents"]:
            headers["X-Search-Intents"] = json.dumps(parsed["intents"])
        if parsed["highlight"]:
            headers["X-Search-Tokens"] = json.dumps(parsed["highlight"])
    return JSONResponse(result, headers=headers)


@app.get("/api/workout/download/{filename}")
def download_workout_by_id(filename: str, cap: int = Query(0)):
    """Serve a ZWO workout file as a download attachment.

    v4.0.0-alpha: IMPL-B's dashboard calls this URL pattern to let the user
    grab the raw ZWO for external trainer apps (MyWhoosh, Tacx, Zwift) now
    that the live-ride runtime is gone.

    FIX-SERVER: path disambiguated from ``/api/workout/{filename}/download``
    AND registered BEFORE ``/api/workout/{category}/{filename}`` below so
    FastAPI's declaration-order match resolves ``download`` as the literal
    path segment, not as ``category=download``. Both guarantees must hold
    for the route to resolve correctly.

    task #24: ``cap=1`` (PROMPT APPROVE) or the profile "on" toggle caps short
    reps to the rider's measured-power envelope. A no-op cap keeps the plain
    FileResponse (byte-identical to disk).
    """
    path = _safe_path(active_workout_dir(), filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    # v1.6.4: media_type "application/octet-stream" (was "application/xml")
    # so WKWebView always treats it as a download.
    if _cap_active_for_download(cap):
        from profile_manager import ProfileManager
        try:
            raw = path.read_bytes()
            capped = _cap_zwo_bytes(raw, filename, ProfileManager.get())
            if capped is not raw and capped != raw:
                return Response(
                    capped, media_type="application/octet-stream",
                    headers={"Content-Disposition":
                             f'attachment; filename="{filename}"'})
        except Exception:
            pass
    return FileResponse(
        path,
        filename=filename,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/workout/{category}/{filename}")
def api_workout_detail(category: str, filename: str, view: str | None = Query(None)):
    """Parse a ZWO file and return segment data for visualization.

    ``view`` overrides the athlete's target_mode for THIS response only —
    'hr' shows the HR conversion to a power-mode user (and 'power' the watt
    view to an hr-mode user), powering the modal's ⚡/❤ peek toggle. The hr
    view is only honoured when the profile has a sane LTHR (same invariant
    as the settings gate); downloads follow the same param so what you see
    is what the head unit gets."""
    # Flat layout first, legacy category/file fallback
    path = _safe_path(active_workout_dir(), filename)
    if not path or not path.exists():
        path = _safe_path(active_workout_dir(), category, filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return JSONResponse({"error": "parse error"}, 400)

    root = tree.getroot()
    name = (root.findtext("name") or filename).strip()
    desc = (root.findtext("description") or "").strip()
    workout_el = root.find("workout")
    if workout_el is None:
        return {"name": name, "description": desc, "segments": []}

    segments = []
    cursor = 0  # seconds
    ftp = config.ATHLETE_FTP_W

    # hr target_mode (IP_HR_ONLY): attach per-segment HR/RPE guidance from the
    # single-source converter so the detail chart shows exactly what the FIT
    # builder will put on the head unit. Power mode payload is unchanged
    # (unless view='hr' explicitly asks for the peek).
    from profile_manager import ProfileManager
    _pm = ProfileManager.get()
    if view == "hr":
        _hr_mode = _pm.lthr_is_set and _pm.max_hr > _pm.lthr
    elif view == "power":
        _hr_mode = False
    else:
        _hr_mode = _pm.target_mode == "hr"
    if _hr_mode:
        from functools import partial
        from hr_targets import power_target_to_hr as _pt2hr
        # int() — save_athlete's range validator stores numerics as float;
        # bpm maths rounds anyway but the UI shows this value verbatim.
        _lthr, _maxhr = int(_pm.lthr), int(_pm.max_hr)
        # W1: athlete-tuned prescription rows (or None → Coggan defaults),
        # threaded into every conversion below via partial.
        _rows_ovr = _prescription_hr_rows(_pm)
        power_target_to_hr = partial(_pt2hr, hr_rows_override=_rows_ovr)

    for el in workout_el:
        tag = el.tag
        # ZWO files occasionally store Duration as a float (e.g. "600.5").
        # `int()` on a numeric float is fine, but `int("600.5")` raises
        # ValueError and 500s the whole route list. Float-first, then int.
        dur = int(float(el.get("Duration", 0) or 0))
        if tag in ("Warmup", "Cooldown", "Ramp"):
            plo = float(el.get("PowerLow", 0.5))
            phi = float(el.get("PowerHigh", 0.7))
            seg = {
                "type": tag, "start": cursor, "duration": dur,
                "power_low": round(plo * ftp), "power_high": round(phi * ftp),
                "pct_low": round(plo * 100), "pct_high": round(phi * 100),
            }
            if _hr_mode:
                # Time-order direction mirrors the chart's v2.0.6 rule:
                # Warmup ramps up, Cooldown down, generic Ramp as authored.
                lo, hi = min(plo, phi) * 100, max(plo, phi) * 100
                if tag == "Cooldown":
                    p_start, p_end = hi, lo
                elif tag == "Warmup":
                    p_start, p_end = lo, hi
                else:
                    p_start, p_end = plo * 100, phi * 100
                seg["hr"] = power_target_to_hr(p_start, p_end, dur, _lthr, _maxhr)
            segments.append(seg)
            cursor += dur
        elif tag == "SteadyState":
            pw = float(el.get("Power", 0.65))
            seg = {
                "type": tag, "start": cursor, "duration": dur,
                "power": round(pw * ftp), "pct": round(pw * 100),
            }
            if _hr_mode:
                seg["hr"] = power_target_to_hr(pw * 100, pw * 100, dur, _lthr, _maxhr)
            segments.append(seg)
            cursor += dur
        elif tag == "IntervalsT":
            reps = int(el.get("Repeat", 1))
            on_d = int(el.get("OnDuration", 0))
            off_d = int(el.get("OffDuration", 0))
            on_p = float(el.get("OnPower", 0.95))
            off_p = float(el.get("OffPower", 0.50))
            seg = {
                "type": tag, "start": cursor,
                "duration": reps * (on_d + off_d),
                "repeats": reps,
                "on_duration": on_d, "off_duration": off_d,
                "on_power": round(on_p * ftp), "off_power": round(off_p * ftp),
                "on_pct": round(on_p * 100), "off_pct": round(off_p * 100),
            }
            if _hr_mode:
                # On/off legs convert independently — each is its own step.
                seg["hr_on"] = power_target_to_hr(on_p * 100, on_p * 100, on_d, _lthr, _maxhr)
                seg["hr_off"] = power_target_to_hr(off_p * 100, off_p * 100, off_d, _lthr, _maxhr)
            segments.append(seg)
            cursor += reps * (on_d + off_d)
        elif tag == "FreeRide":
            segments.append({
                "type": tag, "start": cursor, "duration": dur,
            })
            cursor += dur

    # v1.8.20 — canonical title for the library detail modal.
    try:
        _cc = tp._load_content_classifications() or {}
        _display_name = (_cc.get(path.name) or {}).get("display_name") or ""
    except Exception:
        _display_name = ""

    out = {
        "name": name, "description": desc,
        "display_name": _display_name,
        "total_seconds": cursor,
        "segments": segments,
        "ftp": ftp,
        # Whether the ⚡/❤ peek toggle can offer an HR view at all (needs a
        # sane LTHR — same invariant as the settings gate).
        "hr_available": bool(_pm.lthr_is_set and _pm.max_hr > _pm.lthr),
    }

    # task #24: measured-capacity short-rep advisory for the modal. Built from a
    # LIVE parse (cap_zwo_text details) -- NOT facts aggregates. Only ever
    # populated when a trustworthy MEASURED Pmax exists (pmax_is_set) and the
    # view is power; the client renders it per the toggle (OFF=info,
    # PROMPT=APPROVE/DENY, ON=capped). pmax_w is exposed only when set so the
    # UI never shows the ftp*1.30 fallback as a "ceiling".
    if _pm.pmax_is_set and not _hr_mode:
        try:
            import capacity_cap as _cc
            _txt = path.read_text(encoding="utf-8")
            _, _n, _det = _cc.cap_zwo_text(
                _txt, float(ftp), float(_pm.cp), float(_pm.pmax_w),
                filename=path.name)
            if _n > 0:
                # Worst (highest-demand) qualifying rep drives the headline.
                _worst = max(_det, key=lambda d: d["orig_w"])
                out["capacity_cap"] = {
                    "pmax_is_set": True,
                    "toggle": _pm.cap_short_intervals,
                    "n_reps": _n,
                    "rep_seconds": _worst["t"],
                    "demand_w": _worst["orig_w"],
                    "ceiling_w": int(round(_pm.pmax_w)),
                    "cap_w": _worst["cap_w"],
                }
        except Exception:
            pass
    if _hr_mode:
        out["target_mode"] = "hr"
        out["lthr"] = _lthr
        out["max_hr"] = _maxhr
        # Chart axis labels — computed HERE with the same Python rounding the
        # converter uses. Client-side Math.round(frac*lthr) disagreed with
        # Python's banker's rounding at e.g. LTHR 150 (125 vs 124), breaking
        # the "one number everywhere" promise (red-team D2). Keyed by the %FTP
        # gridline they sit on; values are the zone-top bpm from the SAME
        # resolved rows the converter uses (W1: custom rows flow through too).
        from hr_targets import _resolve_rows_bpm as _rrb
        _rows = _rrb(_lthr, _maxhr, _rows_ovr)
        out["hr_axis"] = {
            "55": _rows[1][1], "75": _rows[2][1],
            "90": _rows[3][1], "105": _rows[4][1],
        }
    return out


@app.post("/api/workouts/bulk-segments")
async def api_bulk_segments(request: Request):
    """Fetch segments for multiple workouts in one request.
    Body: {"files": ["cat/file.zwo", ...]}
    Returns: {"results": {"cat/file.zwo": {segments: [...]}, ...}}
    """
    body = await _get_json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail="expected object with 'files' key")
    files = body.get("files", [])
    if not isinstance(files, list):
        raise HTTPException(status_code=400, detail="'files' must be an array")
    results = {}
    ftp = config.ATHLETE_FTP_W
    # Cap at 200 per request to bound response size. Warn when truncation
    # happens so a caller that silently relied on an unbounded list gets a
    # log signal instead of a mystery "missing file" downstream.
    if len(files) > 200:
        log.warning(
            "bulk-segments: truncating %d extra files (received %d, cap 200)",
            len(files) - 200, len(files),
        )
    for f in files[:200]:
        # Accept both flat ("file.zwo") and legacy ("category/file.zwo") forms
        if "/" in f:
            parts = f.split("/", 1)
            cat, fname = parts
        else:
            cat, fname = "", f
        # Flat lookup first; fall back to legacy category subdir
        path = _safe_path(active_workout_dir(), fname) if fname else None
        if (not path or not path.exists()) and cat:
            path = _safe_path(active_workout_dir(), cat, fname)
        if not path or not path.exists():
            continue
        try:
            tree = ET.parse(path)
            root = tree.getroot()
            workout_el = root.find("workout")
            if workout_el is None:
                continue
            segs = []
            cursor = 0
            for el in workout_el:
                tag = el.tag
                # Float-first int conversion — same ZWO "Duration can be
                # a float string" defensive read as the single-workout path.
                dur = int(float(el.get("Duration", 0) or 0))
                if tag in ("Warmup", "Cooldown", "Ramp"):
                    plo = float(el.get("PowerLow", 0.5))
                    phi = float(el.get("PowerHigh", 0.7))
                    segs.append({"type": tag, "start": cursor, "duration": dur,
                                 "power_low": round(plo * ftp), "power_high": round(phi * ftp)})
                    cursor += dur
                elif tag == "SteadyState":
                    pw = float(el.get("Power", 0.65))
                    segs.append({"type": tag, "start": cursor, "duration": dur, "power": round(pw * ftp)})
                    cursor += dur
                elif tag == "IntervalsT":
                    reps = int(el.get("Repeat", 1))
                    on_d = int(el.get("OnDuration", 0))
                    off_d = int(el.get("OffDuration", 0))
                    on_p = float(el.get("OnPower", 0.95))
                    off_p = float(el.get("OffPower", 0.50))
                    segs.append({"type": tag, "start": cursor, "duration": reps * (on_d + off_d),
                                 "repeats": reps, "on_duration": on_d, "off_duration": off_d,
                                 "on_power": round(on_p * ftp), "off_power": round(off_p * ftp)})
                    cursor += reps * (on_d + off_d)
                elif tag == "FreeRide":
                    segs.append({"type": tag, "start": cursor, "duration": dur})
                    cursor += dur
            results[f] = {"segments": segs}
        except Exception:
            continue
    return {"results": results, "ftp": ftp}


@app.get("/api/picker")
def api_picker(subjective: float = Query(7), duration: int = Query(75)):
    """Workout-of-the-day picker.

    v4.1.1 FIX-PICKER-MOVE (Bug D): two fixes land here.

    1. T5 (v4.1.0) added a ``tags: str = Query(None)`` parameter to
       ``api_workouts``. This endpoint calls ``api_workouts`` as a plain
       Python function — bypassing FastAPI's dependency injection — so
       unpassed params keep their ``Query(None)`` *default objects* as
       values. Inside ``api_workouts`` line 1015 then does
       ``if tags: tags.split(",")`` — the Query() object is truthy and
       has no ``.split`` → the entire call raised AttributeError and the
       endpoint 500'd, surfacing in the UI as "readiness ?/100; no
       workouts". We now pass ``tags=None`` explicitly.

    2. When the user is on a fresh install with no FIT/HRV data yet,
       ``compute_readiness`` returns ``score=None``. The old branch then
       returned an empty workouts list — not useful for a
       workout-picker UI. We now fall back to a neutral 70/100 baseline
       ("no HRV data yet") and proceed to surface z2/endurance
       suggestions so the user sees a sensible default set rather than
       a blank card on day one.
    """
    try:
        training = cached("training", get_today_metrics)
    except Exception as _e:
        _log.debug(f"picker training metrics unavailable: {_e}")
        training = {}
    try:
        sleep = cached("sleep", get_sleep_metrics)
    except Exception as _e:
        _log.debug(f"picker sleep metrics unavailable: {_e}")
        sleep = {}

    try:
        r = compute_readiness(
            ln_rmssd_7d=sleep.get("ln_rmssd_7d"),
            swc_lower=sleep.get("swc_lower"), swc_upper=sleep.get("swc_upper"),
            tsb=training.get("tsb"), sleep_h=sleep.get("sleep_h"),
            rhr_delta=sleep.get("rhr_delta"), subjective=subjective,
        )
    except Exception:
        _log.exception("picker readiness compute failed — falling back to baseline")
        r = {"score": None, "status": "INSUFFICIENT_DATA", "advice": ""}

    score = r.get("score")
    status = r.get("status", "?")
    advice = r.get("advice", "")

    # Fix D.2: baseline when no HRV data yet. Don't return an empty card —
    # surface a neutral z2/endurance starter set instead so the picker is
    # usable on day one of a fresh install.
    baseline_used = False
    if score is None:
        _log.info("picker: no HRV data yet, using 70/100 baseline")
        score = 70
        status = status or "BASELINE"
        advice = advice or "No HRV data yet — using neutral baseline"
        baseline_used = True

    # Map readiness to session type + fallback types. Grill
    # PICKER-DEAD-TYPE-KEYWORDS: session_type is substring-matched against
    # the library's Protocol values — "z2"/"helgerud"/"rønnestad"/"seiler"
    # match ZERO of the 17 Protocol strings, so the ≥80 pool was vo2-only
    # and the 60-69 pool endurance-only. Every keyword below matches a real
    # Protocol family (the Helgerud/Rønnestad-style sessions live inside
    # VO2max/Over-Unders protocols anyway).
    if score >= 80:
        types = ["vo2", "over-under", "anaerobic"]
    elif score >= 70:
        types = ["sweet spot", "threshold", "endurance"]
    elif score >= 60:
        types = ["tempo", "endurance"]
    elif score >= 40:
        types = ["endurance", "recovery"]
    else:
        # REST-level readiness: still show 3 light suggestions rather
        # than an empty card (Bug D.2).
        types = ["recovery", "endurance"]

    # Search with multiple type keywords for better matches. Fix D.1:
    # pass tags=None explicitly — api_workouts' Query() default object
    # is truthy and crashes .split().
    #
    # v4.1.1 FIX-PICKER-MOVE: the previous ``min_score=7`` floor was
    # pathological: the library's score rubric (tss*0.6 + structure*0.4)
    # produces only 3 out of 3000 workouts at score≥7 — the picker was
    # effectively asking for needles in a haystack. We now use 3 as the
    # primary floor and widen further on misses, so the picker returns
    # real suggestions instead of silently collapsing to an empty list.
    # Forum bug (2026-08-30): at duration=30 the old flat ±30 window spanned
    # [0, 60] and sort="score" favours high-TSS files — which are the LONG
    # ones — so a rider asking for 30 minutes was handed 60-minute workouts.
    # The requested duration is a HARD CAP (owner decision): the rider said
    # how much time they have, so nothing longer is ever served. The search
    # flexes DOWNWARD only (25% below the request, floor 10 min), and the
    # final list is ordered by closeness to the requested duration (score
    # breaks ties). The fallbacks below widen further down, never up.
    _dur_pad = max(10, int(duration * 0.25))
    workouts: list[dict] = []
    for stype in types:
        try:
            matches = api_workouts(
                min_score=3, session_type=stype,
                max_duration=duration,
                min_duration=max(0, duration - _dur_pad),
                limit=10, sort="score", tags=None,
            )
        except Exception as _e:
            _log.warning(f"picker api_workouts({stype!r}) failed: {_e}")
            matches = []
        workouts.extend(matches)
        if len(workouts) >= 5:
            break

    # Grill PICKER-FTP-TEST-LEAK: the typeless fallbacks below have no
    # protocol filter, and with sort="score" an FTP-test ramp ranks FIRST in
    # exactly these windows — a maximal to-failure protocol as the casual
    # workout-of-the-day card. Tests are ridden deliberately, never picked.
    def _not_a_test(w: dict) -> bool:
        if (w.get("Protocol") or "").lower() == "ftp test":
            return False
        return "ftp_test" not in {t.lower() for t in (w.get("Tags") or [])}

    # If still empty, try without session type filter (still capped).
    # Grill PICKER-FALLBACK-QUERY-CRASH: session_type=None must be passed
    # explicitly — calling api_workouts as a plain function leaves FastAPI's
    # Query(None) default OBJECT in place, which is truthy and crashes at
    # session_type.lower(); the except swallowed it, so both fallbacks were
    # dead code and underfilled pickers silently returned 0-2 cards.
    if not workouts:
        try:
            workouts = [w for w in api_workouts(
                min_score=3, session_type=None, max_duration=duration,
                min_duration=max(0, duration - 30),
                limit=10, sort="score", tags=None,
            ) if _not_a_test(w)][:5]
        except Exception as _e:
            _log.warning(f"picker api_workouts fallback failed: {_e}")
            workouts = []

    # Last-resort widening: drop the score floor + widen the window all the
    # way DOWN (never above the cap) so fresh installs with a smaller
    # library still see ≥3 cards.
    if len(workouts) < 3:
        try:
            extra = [w for w in api_workouts(
                min_score=1, session_type=None, max_duration=duration,
                min_duration=0, limit=15, sort="score", tags=None,
            ) if _not_a_test(w)][:10]
            workouts.extend(extra)
        except Exception as _e:
            _log.warning(f"picker last-resort widen failed: {_e}")

    # Deduplicate by name, preserve first-seen order
    seen: set[str] = set()
    unique: list[dict] = []
    for w in workouts:
        nm = w.get("Name") or ""
        if nm and nm not in seen:
            seen.add(nm)
            unique.append(w)

    # Closest-to-requested-duration first; score breaks ties. Without this,
    # score ordering hands a 30-min request the longest file in the window.
    def _dur_rank(w: dict):
        try:
            d = float(w.get("Duration(min)") or 0)
        except (TypeError, ValueError):
            d = 0.0
        try:
            sc = float(w.get("Score") or 0)
        except (TypeError, ValueError):
            sc = 0.0
        return (abs(d - duration), -sc)

    unique.sort(key=_dur_rank)

    return {
        "type": types[0] if types else "z2",
        "readiness": score, "status": status, "advice": advice,
        "baseline_used": baseline_used,
        "workouts": unique[:5],
    }


# ═══════════════════════════════════════════════════════════════════════════════
# COURSE APIs
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/courses")
def api_courses(region: str = Query(None)):
    courses = []
    for crs in sorted(COURSE_DIR.rglob("*.crs")):
        r = crs.parent.name
        if region and region != r:
            continue
        desc = ""
        try:
            with open(crs, encoding="utf-8") as f_:
                for line in f_:
                    if line.startswith("DESCRIPTION"):
                        desc = line.split("=", 1)[1].strip()
                        break
        except Exception:
            pass

        # Always compute km + climb from the CRS data itself — NEVER trust the
        # DESCRIPTION header text as a source of truth. Loop-extended routes
        # keep the original "5km" text in their header while the actual
        # CRS data is 15km long, so reading from desc gives the wrong km
        # and a wildly inflated avg_grad.
        km = 0.0
        climb = 0
        max_grade = 0.0
        try:
            in_data = False
            cum_elev = 0.0
            cum_km = 0.0
            max_g = 0.0
            with open(crs, encoding="utf-8") as f2:
                for line2 in f2:
                    s = line2.strip()
                    if s == "[COURSE DATA]": in_data = True; continue
                    if s == "[END COURSE DATA]": break
                    if in_data and s and not s.startswith("DIST"):
                        parts = s.split()
                        if len(parts) >= 2:
                            try:
                                delta_km = float(parts[0])
                                grade = float(parts[1])
                            except ValueError:
                                continue
                            cum_km += delta_km
                            elev_change = delta_km * 10.0 * grade
                            if elev_change > 0:
                                cum_elev += elev_change
                            if abs(grade) > max_g:
                                max_g = abs(grade)
            km = round(cum_km, 1)
            climb = round(cum_elev)
            max_grade = round(max_g, 1)
        except Exception:
            pass

        # Fallback to description-parse only if CRS had zero data (corrupt file)
        if km == 0:
            m = re.search(r'([\d.]+)km', desc)
            if m:
                km = float(m.group(1))

        avg_grad = round(climb / (km * 10.0), 1) if (km > 0 and climb > 0) else 0.0

        # Difficulty category
        if climb >= 1500 or (km >= 15 and avg_grad >= 7):
            difficulty = "HC"
        elif climb >= 800 or (km >= 10 and avg_grad >= 6):
            difficulty = "Cat 1"
        elif climb >= 400:
            difficulty = "Cat 2"
        elif climb >= 200:
            difficulty = "Cat 3"
        elif climb >= 100:
            difficulty = "Cat 4"
        else:
            difficulty = "Flat"

        # Check if matching GPX exists
        gpx_name = crs.stem + ".gpx"
        has_gpx = (active_gpx_dir() / r / gpx_name).exists()

        courses.append({
            "name": crs.stem, "region": r, "description": desc,
            "km": km, "climb": climb, "avg_grad": avg_grad,
            "difficulty": difficulty,
            "filename": crs.name,
            "has_gpx": has_gpx,
        })
    return courses


@app.get("/api/surface-types")
def api_surface_types():
    """Return surface_types.json content for client-side surface filtering."""
    path = Path(__file__).parent / "surface_types.json"
    if not path.exists():
        return {"routes": {}, "gravel_workouts": []}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@app.get("/api/course/{region}/{filename}")
def api_course_profile(region: str, filename: str):
    """Return elevation profile data from a CRS file for charting.

    v1.8.8 Bug 4 — cached routes.json from before the v1.8.6 ASCII rename
    may carry non-ASCII filenames (``pavé``, ``Mür``). On 404, retry with
    Unicode NFC and NFD normalization. If still 404, log the disk path
    attempted so the next bug-report tells us exactly which name was
    missing.
    """
    import unicodedata as _ud

    def _resolve_course(fname: str) -> Path | None:
        p = _safe_path(COURSE_DIR, region, fname)
        if p and p.exists():
            return p
        # Virtual routes live under courses/virtual/<region>/<filename>
        p = _safe_path(COURSE_DIR, "virtual", region, fname)
        if p and p.exists():
            return p
        return None

    attempts: list[str] = [filename]
    path = _resolve_course(filename)
    if path is None and any(ord(c) > 127 for c in filename):
        # Try both Unicode normalization forms — file may be on disk in
        # the opposite form from what the cached routes.json carries.
        for form in ("NFC", "NFD"):
            try:
                candidate = _ud.normalize(form, filename)
            except (TypeError, ValueError):
                continue
            if candidate in attempts:
                continue
            attempts.append(candidate)
            path = _resolve_course(candidate)
            if path is not None:
                _log.info(
                    "course profile resolved via %s normalization "
                    "region=%s filename=%r resolved=%r",
                    form, region, filename, candidate,
                )
                break
    if path is None:
        _log.warning(
            "course profile 404 region=%s filename=%r attempts=%s",
            region, filename, attempts,
        )
        return JSONResponse({"error": "not found"}, 404)
    points = []
    in_data = False
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line == "[COURSE DATA]":
                in_data = True; continue
            if line == "[END COURSE DATA]":
                break
            if in_data and line:
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        dist = float(parts[0])
                        grad = float(parts[1])
                        points.append({"d": round(dist, 3), "g": round(grad, 1)})
                    except ValueError:
                        pass
    # CRS distances are delta segment lengths — reconstruct cumulative for chart
    cum_dist = [0.0]
    elevation = [0.0]
    for i in range(1, len(points)):
        seg_km = points[i]["d"]  # delta segment length
        cum_dist.append(round(cum_dist[-1] + seg_km, 3))
        ele_change = seg_km * 10 * points[i]["g"]  # gradient% * distance in 100m units
        elevation.append(round(elevation[-1] + ele_change, 1))

    # Downsample for chart (max 200 points)
    step = max(1, len(points) // 200)
    sampled = [{"d": cum_dist[i], "g": points[i]["g"], "e": elevation[i]}
               for i in range(0, len(points), step)]

    # Compute header stats so the detail modal shows correct numbers.
    # Previously the endpoint returned only `points` and the JS fell back
    # to 0 for elevation / avg_grade / max_grade on every route.
    total_km = round(cum_dist[-1], 2) if cum_dist else 0.0
    elev_gain = 0.0
    max_g = 0.0
    for i in range(1, len(points)):
        seg_km = points[i]["d"]
        grade = points[i]["g"]
        e_change = seg_km * 10.0 * grade
        if e_change > 0:
            elev_gain += e_change
        if abs(grade) > max_g:
            max_g = abs(grade)
    elev_gain = round(elev_gain)
    avg_grade = round(elev_gain / (total_km * 10.0), 2) if total_km > 0 else 0.0

    return {
        "points": sampled,
        "total_points": len(points),
        "stats": {
            "distance_km": total_km,
            "elev_gain_m": elev_gain,
            "climb_m": elev_gain,  # alias for the older client fallback
            "avg_grade": avg_grade,
            "max_grade": round(max_g, 1),
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# VIRTUAL ROUTES API
# ═══════════════════════════════════════════════════════════════════════════════





# ─── v2 routes.json schema helpers ───────────────────────────────────────────
# routes.json is now a FLAT LIST of route entries (see route_schema_contract.md).
# Load once per process with mtime-based invalidation and pre-build inverted
# indexes for hot filter fields.

REGION_TITLES = {
    "alps": "Alps", "pyrenees": "Pyrenees", "dolomites": "Dolomites",
    "mallorca": "Mallorca", "tenerife": "Tenerife",
    "costa_blanca": "Costa Blanca", "costa_daurada": "Costa Daurada",
    "basque": "Basque Country", "andorra": "Andorra", "girona": "Girona",
    "lanzarote": "Lanzarote", "gravel": "Gravel Classics",
    "other": "Other Climbs",
    "richmond": "Richmond (UCI 2015)", "austria": "Austria / Tirol",
    "london": "London", "scotland": "Scotland",
    "flanders": "Flanders (Ronde)",
    "netherlands_gravel": "Netherlands Gravel",
    "italy_gravel": "Italy Gravel", "germany_gravel": "Germany Gravel",
    "france_gravel": "France Gravel", "usa_gravel": "USA Gravel",
    "blue_ridge": "Blue Ridge", "iron_pass": "Iron Pass",
    "desert_loop": "Desert Loop",
}

WORLD_CHARACTER = {
    "blue_ridge":  {"emoji": "🏞", "title": "Blue Ridge",
                    "blurb": "Rolling hills, punchy kickers, classic cobble walls, occasional gravel",
                    "tags": ["rolling", "cat4", "cobble", "gravel", "lap"]},
    "iron_pass":   {"emoji": "⛰", "title": "Iron Pass",
                    "blurb": "Long alpine climbs, HC monsters, gravel summits, Flemish walls",
                    "tags": ["hc", "cat1", "wall", "gravel_climb"]},
    "desert_loop": {"emoji": "🌵", "title": "Desert Loop",
                    "blurb": "Flat TT circuits, Strade-Bianche gravel, pan-flat time-trial laps",
                    "tags": ["flat_tt", "gravel", "lap_criterium"]},
    "__real_world__": {"emoji": "🌍", "title": "Real World",
                    "blurb": "Famous climbs and iconic race routes: Ventoux, Stelvio, Richmond, Innsbruck, Box Hill, Bealach na Bà, Ronde van Vlaanderen",
                    "tags": ["hc", "classic", "wall", "cobble", "flanders"]},
}





























@app.get("/api/routes")
def api_routes(
    source: str = Query("any"),
    region: str | None = Query(None),
    surface: str | None = Query(None),
    terrain: str | None = Query(None),
    category: str | None = Query(None),
    finish: str | None = Query(None),
    min_km: float | None = Query(None),
    max_km: float | None = Query(None),
    loop_only: bool = Query(False),
    lap_only: bool = Query(False),
    max_difficulty: float | None = Query(None),
    search: str | None = Query(None),
    limit: int = Query(200),
    offset: int = Query(0),
):
    """Unified route list API (v2 schema). Replaces /api/virtual-routes."""
    routes, _ = _load_routes_v2()
    limit = max(1, min(int(limit or 200), 1000))
    offset = max(0, int(offset or 0))

    filtered = _apply_route_filters(
        routes,
        source=(source or "any").lower(),
        regions=_parse_csv_param(region) or None,
        surfaces=_parse_csv_param(surface) or None,
        terrains=_parse_csv_param(terrain) or None,
        categories=_parse_csv_param(category) or None,
        finishes=_parse_csv_param(finish) or None,
        min_km=min_km,
        max_km=max_km,
        loop_only=bool(loop_only),
        lap_only=bool(lap_only),
        max_difficulty=max_difficulty,
        search=search,
    )
    total = len(filtered)
    page = filtered[offset:offset + limit]
    return {
        "total_matching": total,
        "returned": len(page),
        "routes": [_route_summary(r) for r in page],
    }






@app.post("/api/routes/suggest")
async def api_routes_suggest(request: Request):
    """Rank + return top N routes matching user picker input."""
    body = await _get_json_body(request)

    d = body.get("distance_km") or {}
    d_min = float(d.get("min", 0)) if isinstance(d, dict) else 0.0
    d_max = float(d.get("max", 0)) if isinstance(d, dict) else 0.0
    if d_max <= 0:
        d_max = max(d_min, 200.0)  # default wide upper bound
    d_mid = (d_min + d_max) / 2.0
    d_half = max(0.0, (d_max - d_min) / 2.0)
    # Relax ±1 km for point targets so the hard filter at _apply_route_filters
    # doesn't kill everything before the graceful distance decay runs.
    _hard_min = (d_min - 1.0) if (d_min == d_max and d_min > 0) else (d_min if d_min > 0 else None)
    _hard_max = (d_max + 1.0) if (d_min == d_max and d_max > 0) else (d_max if d_max > 0 else None)

    # Climb-meters range — new in v1.5 (replaces "climbs mode" tri-chip +
    # "max difficulty" slider with a direct elevation-gain window).
    cm = body.get("climb_m") or {}
    climb_min: float | None = None
    climb_max: float | None = None
    if isinstance(cm, dict):
        try:
            climb_min = float(cm["min"]) if cm.get("min") is not None else None
        except (TypeError, ValueError):
            climb_min = None
        try:
            climb_max = float(cm["max"]) if cm.get("max") is not None else None
        except (TypeError, ValueError):
            climb_max = None

    climbs_mode = (body.get("climbs") or "include_any_climb").lower()
    difficulty_max = body.get("difficulty_max")
    try:
        difficulty_max = float(difficulty_max) if difficulty_max is not None else None
    except (TypeError, ValueError):
        difficulty_max = None

    surface_list = body.get("surface")
    surface_filter: set[str] | None = None
    if isinstance(surface_list, list) and surface_list:
        surface_filter = {str(s).lower() for s in surface_list}

    finish_list = body.get("finish")
    finish_filter: set[str] | None = None
    if isinstance(finish_list, list) and finish_list:
        finish_filter = {str(s).lower() for s in finish_list}

    regions_list = body.get("regions")
    region_filter: list[str] | None = None
    if isinstance(regions_list, list) and regions_list:
        region_filter = [str(r).lower() for r in regions_list]

    loop_only = bool(body.get("loop_only", False))
    max_results = int(body.get("max_results") or 5)
    max_results = max(1, min(max_results, 50))

    routes, _ = _load_routes_v2()

    # Stage 1: hard filters
    candidates = _apply_route_filters(
        routes,
        regions=region_filter,
        surfaces=list(surface_filter) if surface_filter else None,
        finishes=list(finish_filter) if finish_filter else None,
        min_km=_hard_min,
        max_km=_hard_max,
        loop_only=loop_only,
    )

    # Stage 2: climbs mode — delegated to module-level predicates so the
    # empty-state `_count(drop)` helper stays in lockstep (grill caught drift).
    if climbs_mode == "exclude":
        candidates = [r for r in candidates if _is_flat_route(r)]
    elif climbs_mode == "climb_required":
        candidates = [r for r in candidates if _is_climb_route(r)]
    # else include_any_climb → no extra constraint

    # Stage 2b: climb-meters range (v1.5 primary climb filter)
    if climb_min is not None:
        candidates = [r for r in candidates if (r.get("climb_m") or 0) >= climb_min]
    if climb_max is not None:
        candidates = [r for r in candidates if (r.get("climb_m") or 0) <= climb_max]

    # Stage 3: difficulty_max (legacy; UI no longer emits this as of v1.5
    # but the scorer still honours it when sent by older clients or tests)
    if difficulty_max is not None:
        candidates = [r for r in candidates
                      if (r.get("difficulty_score") or 0) <= difficulty_max]

    # Stage 4: rank
    target_diff_mid = (difficulty_max / 2.0) if difficulty_max else 5.0
    scored = []
    for r in candidates:
        sc, rat = _score_route_for_suggest(
            r,
            d_mid=d_mid,
            d_half=d_half,
            target_diff_mid=target_diff_mid,
            diff_max=difficulty_max,
            surface_filter=surface_filter,
            finish_filter=finish_filter,
        )
        scored.append((sc, rat, r))
    # Compound sort: primary = score descending, tiebreaker = distance
    # proximity to user midpoint ascending, final tiebreaker = route id.
    # Without this, wide-band queries (10-200 km, no other filters) tie
    # 4-8 routes at 1.0 and ordering collapses to file-insertion order.
    def _rank_key(triple):
        sc, _rat, r = triple
        km = r.get("distance_km") or 0
        rid = r.get("id") or ""
        return (-sc, abs(km - d_mid), rid)
    scored.sort(key=_rank_key)

    # Stage 5: regional diversity pass. If more than `max_per_region` routes
    # from the same region would sit in the accepted window, demote extras by
    # -0.08 so top-N doesn't get dominated by whichever region has the most
    # routes. Re-sort with the SAME compound key so tiebreakers stay stable.
    max_per_region = 2
    region_seen: dict[str, int] = {}
    reranked: list[tuple[float, str, dict]] = []
    for sc, rat, r in scored:
        reg = (r.get("region") or "").lower()
        count = region_seen.get(reg, 0)
        if count >= max_per_region:
            sc = max(0.0, round(sc - 0.08, 3))
        reranked.append((sc, rat, r))
        region_seen[reg] = count + 1
    reranked.sort(key=_rank_key)
    top = reranked[:max_results]

    # Empty-state hint (grill C #6): if we have zero matches, figure out which
    # single filter, when dropped, would yield the most results. Gives the UI a
    # "Drop the summit finish to see 14 routes" one-click recovery.
    empty_hint: dict | None = None
    if not top:
        def _count(drop: str) -> int:
            rr = routes
            rr = _apply_route_filters(
                rr,
                regions=None if drop == "regions" else region_filter,
                surfaces=None if drop == "surface" else (list(surface_filter) if surface_filter else None),
                finishes=None if drop == "finish" else (list(finish_filter) if finish_filter else None),
                min_km=None if drop == "distance" else _hard_min,
                max_km=None if drop == "distance" else _hard_max,
                loop_only=False if drop == "loop" else loop_only,
            )
            # Re-apply climb stage unless dropping it. Shares the helpers with
            # the main pipeline so the "would_match" count never drifts.
            if drop != "climbs":
                if climbs_mode == "exclude":
                    rr = [r for r in rr if _is_flat_route(r)]
                elif climbs_mode == "climb_required":
                    rr = [r for r in rr if _is_climb_route(r)]
            # Climb-meters range unless dropping it
            if drop != "climb":
                if climb_min is not None:
                    rr = [r for r in rr if (r.get("climb_m") or 0) >= climb_min]
                if climb_max is not None:
                    rr = [r for r in rr if (r.get("climb_m") or 0) <= climb_max]
            # Difficulty stage unless dropping it
            if drop != "difficulty" and difficulty_max is not None:
                rr = [r for r in rr if (r.get("difficulty_score") or 0) <= difficulty_max]
            return len(rr)

        candidates_to_drop = []
        if d_min > 0 or d_max > 0:
            candidates_to_drop.append(("distance", "the distance range"))
        # Climb range counts as "set" if it's tighter than the widget defaults
        # (0–4000 m). A full-width request means the user didn't constrain it.
        climb_set = (climb_min is not None and climb_min > 0) or (climb_max is not None and climb_max < 4000)
        if climb_set:
            candidates_to_drop.append(("climb", "the climb-meters range"))
        if surface_filter and len(surface_filter) < 3:
            candidates_to_drop.append(("surface", "the surface filter"))
        if finish_filter and len(finish_filter) < 4:
            candidates_to_drop.append(("finish", "the finish-type filter"))
        if climbs_mode != "include_any_climb":
            candidates_to_drop.append(("climbs", "the climbs filter"))
        if difficulty_max is not None and difficulty_max < 9:
            candidates_to_drop.append(("difficulty", "the difficulty cap"))
        if region_filter:
            candidates_to_drop.append(("regions", "the region filter"))
        if loop_only:
            candidates_to_drop.append(("loop", "the loop-only filter"))
        if candidates_to_drop:
            options = [(name, label, _count(name)) for name, label in candidates_to_drop]
            options.sort(key=lambda x: -x[2])
            best = options[0]
            if best[2] > 0:
                empty_hint = {
                    "filter": best[0],
                    "filter_label": best[1],
                    "would_match": best[2],
                }

    resp: dict = {
        "count": len(top),
        "filters_applied": {
            "distance_km": {"min": d_min, "max": d_max},
            "climb_m": {"min": climb_min, "max": climb_max},
            "climbs": climbs_mode,
            "difficulty_max": difficulty_max,
            "surface": sorted(surface_filter) if surface_filter else None,
            "finish": sorted(finish_filter) if finish_filter else None,
            "regions": region_filter,
            "loop_only": loop_only,
            "max_results": max_results,
        },
        "results": [
            {"route": _route_summary(r), "match_score": sc, "rationale": rat}
            for sc, rat, r in top
        ],
    }
    if empty_hint:
        resp["empty_hint"] = empty_hint
    return resp




@app.get("/api/routes/regions")
def api_routes_regions():
    """Region cards (virtual worlds) + real_world_meta with per-region counts."""
    routes, index = _load_routes_v2()

    worlds: list[dict] = []
    for virt_region in ("blue_ridge", "iron_pass", "desert_loop"):
        idxs = index["by_region"].get(virt_region, [])
        virt_routes = [routes[i] for i in idxs]
        char = WORLD_CHARACTER.get(virt_region, {})
        worlds.append({
            "region": virt_region,
            "source": "virtual",
            "count": len(virt_routes),
            "emoji": char.get("emoji", ""),
            "title": char.get("title", REGION_TITLES.get(virt_region, virt_region)),
            "blurb": char.get("blurb", ""),
            # Use curated WORLD_CHARACTER tags (rolling/cobble/gravel/cat4/lap) —
            # data-derived tags surface "flat"/"none"/"asphalt" which is noisy.
            "sample_tags": char.get("tags", []) or _top_archetypes_for_region(virt_routes, 5),
        })

    # Real-world meta
    rw_routes = [r for r in routes if r.get("source") == "real_world"]
    rw_region_counts: dict[str, int] = {}
    for r in rw_routes:
        reg = r.get("region", "other")
        rw_region_counts[reg] = rw_region_counts.get(reg, 0) + 1

    rw_char = WORLD_CHARACTER["__real_world__"]
    rw_regions_list = sorted(
        [
            {"region": reg, "count": cnt, "title": REGION_TITLES.get(reg, reg)}
            for reg, cnt in rw_region_counts.items()
        ],
        key=lambda x: -x["count"],
    )

    real_world_meta = {
        "region": "__real_world__",
        "emoji": rw_char["emoji"],
        "title": rw_char["title"],
        "blurb": rw_char["blurb"],
        "sample_tags": rw_char.get("tags", []) or _top_archetypes_for_region(rw_routes, 5),
        "count": len(rw_routes),
        "regions": rw_regions_list,
    }

    return {"worlds": worlds, "real_world_meta": real_world_meta}


@app.get("/api/routes/surfaces")
def api_routes_surfaces():
    """Return surface filter counts across the full route list."""
    routes, _ = _load_routes_v2()
    counts = {"asphalt": 0, "gravel": 0, "cobble": 0, "has_gravel": 0, "has_cobble": 0}
    for r in routes:
        ps = r.get("primary_surface") or ""
        if ps in counts:
            counts[ps] += 1
        if r.get("has_gravel"):
            counts["has_gravel"] += 1
        if r.get("has_cobble"):
            counts["has_cobble"] += 1
    return counts


@app.get("/api/routes/{route_id:path}")
def api_route_detail(route_id: str):
    """Return the full v2 entry for a route (including crs_path)."""
    from urllib.parse import unquote
    rid = unquote(route_id or "")
    _, index = _load_routes_v2()
    r = index.get("by_id", {}).get(rid)
    if not r:
        raise HTTPException(status_code=404, detail=f"Route not found: {rid}")
    return r


@app.get("/api/virtual-routes")
def api_virtual_routes(world: str = Query(None)):
    """Stable alias for the virtual-route slice of /api/routes.

    v3.6.0-fix33d-log-hygiene: the previous "deprecated" warning was
    removed because the frontend still depends on this endpoint's
    flat-list shape (dashboard.html loadVirtualRoutes / loadAllRoutes)
    and there is no scheduled cutover date — the warning was pure noise
    firing on every cold start. The endpoint remains a thin adapter
    over ``_load_routes_v2`` + the virtual-source filter, so the v2
    route store stays the single source of truth.
    """
    routes, index = _load_routes_v2()
    virt = [r for r in routes if r.get("source") == "virtual"]
    if world:
        w_lower = world.lower()
        virt = [r for r in virt if (r.get("region") or "").lower() == w_lower
                or (REGION_TITLES.get(r.get("region", ""), "")).lower() == w_lower]

    world_names = []
    seen = set()
    for r in virt:
        nm = REGION_TITLES.get(r.get("region", ""), r.get("region", ""))
        if nm and nm not in seen:
            seen.add(nm)
            world_names.append(nm)

    shaped = []
    for r in virt:
        reg = r.get("region", "")
        _crs = r.get("crs_path") or ""
        shaped.append({
            **r,
            "world": REGION_TITLES.get(reg, reg),
            "world_slug": reg,
            "crs_region": (os.path.basename(os.path.dirname(_crs)) if _crs else None),
            "file": os.path.basename(_crs) or None,
            "slug": (r.get("id", "").split("/", 1)[1] if "/" in r.get("id", "") else r.get("id", "")),
            "url": r.get("id", ""),
            "km": r.get("distance_km", 0),
            "climb": r.get("climb_m", 0),
            "avg_grad": r.get("avg_grade_signed", 0),
            "difficulty": str(r.get("difficulty_score", "")),
        })
    return {"worlds": world_names, "routes": shaped, "total": len(shaped)}








@app.get("/api/profiles-bulk")
def api_profiles_bulk():
    """Return ALL pre-indexed elevation profiles (~1MB). Frontend caches this once."""
    return _load_route_index()


@app.get("/api/route-profile")
def api_route_profile(url: str = Query(...)):
    """Return full route detail (elevation profile + coordinates) from individual file."""
    route_data = _load_route_detail(url)
    if not route_data:
        return JSONResponse({"error": "Route not scraped yet"}, 404)

    # Preferred: profile files written by generate_route_profiles.py carry a
    # ready-to-render `profile` list of {d, e, g}. Fall back to legacy shapes
    # (lat_lon_grade / profile_points) for any older scraped data.
    profile = route_data.get("profile") or []
    lat_lon_grade = route_data.get("lat_lon_grade", [])
    lat_lon = route_data.get("lat_lon", [])
    profile_points = route_data.get("profile_points", [])

    if profile:
        points = profile
    elif lat_lon_grade:
        points = _route_profile_points(lat_lon_grade)
    elif profile_points:
        # Climb portal: profile_points have {e, g} — need to add distance
        # Use the indexed profile which already has distance computed
        idx = _load_route_index()
        indexed = idx.get(url)
        points = indexed.get("points", []) if indexed else []
    else:
        points = []

    if not points:
        return JSONResponse({"error": "No profile data"}, 404)

    total_climb = sum(max(0, points[i]["e"] - points[i-1]["e"]) for i in range(1, len(points)))
    total_descent = sum(max(0, points[i-1]["e"] - points[i]["e"]) for i in range(1, len(points)))
    max_grad = max((abs(p["g"]) for p in points), default=0)

    # Include route_shape SVG data if available (climb portals)
    route_shape = route_data.get("route_shape")

    return {
        "points": points,
        "lat_lon": lat_lon,
        "route_shape": route_shape,
        "total_km": round(points[-1]["d"], 1) if points else 0,
        "total_climb": round(total_climb),
        "total_descent": round(total_descent),
        "max_gradient": round(max_grad, 1),
        "min_ele": round(min(p["e"] for p in points), 1),
        "max_ele": round(max(p["e"] for p in points), 1),
    }






@app.get("/api/climb-workout")
def api_climb_workout(url: str = Query(...), warmup: int = Query(10)):
    """Generate a ZWO workout from a virtual route profile."""
    route_data = _load_route_detail(url)
    if not route_data:
        return JSONResponse({"error": "Route not scraped yet"}, 404)

    points = _route_profile_points(route_data.get("lat_lon_grade", []))
    if len(points) < 2:
        return JSONResponse({"error": "Not enough profile data"}, 400)

    # Extract route name from URL
    route_name = url.rstrip("/").split("/")[-1].replace("-", " ").title()
    zwo_xml = _build_climb_zwo(points, route_name, warmup)

    from fastapi.responses import Response
    safe_name = route_name.replace('"', '_')
    return Response(
        content=zwo_xml,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.zwo"'},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# DOWNLOAD APIs
# ═══════════════════════════════════════════════════════════════════════════════











@app.get("/api/download/zwo/{filename}")
def download_zwo_flat(filename: str, outdoor: int = Query(0),
                      transit_min: int = Query(10), spin_min: int = Query(20),
                      cap: int = Query(0)):
    """Single-segment path used by the dashboard's planner-modal "Download ZWO"
    button. v1.0.0 fix: pre-fix the dashboard called /api/download/zwo/<file>
    (one segment) but the only registered route was /api/download/zwo/{category}
    /{filename} (two segments) — FastAPI returned 404 with body {error: 'not
    found'} on every click. Added this single-arg variant to match the call site.

    v1.6.4: media_type changed from "application/xml" to "application/octet-stream"
    and Content-Disposition explicitly stamped. WKWebView (packaged DMG) honors
    the Disposition header for octet-stream but renders application/xml inline,
    which was the "white screen with Times New Roman text" the user reported when
    clicking Download ZWO in the Library tab.

    G1 (v2.1): ``outdoor=1`` wraps the block with an off-plan transit warmup +
    spin-home cooldown (transit_min / spin_min minutes).
    task #24: ``cap=1`` (PROMPT APPROVE) or the profile "on" toggle caps short
    reps to the rider's measured-power envelope for THIS download only.
    """
    path = _safe_path(active_workout_dir(), filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    return _zwo_download_response(path, filename, outdoor, transit_min, spin_min,
                                 cap_active=_cap_active_for_download(cap))


@app.get("/api/download/zwo/{category}/{filename}")
def download_zwo(category: str, filename: str, outdoor: int = Query(0),
                 transit_min: int = Query(10), spin_min: int = Query(20),
                 cap: int = Query(0)):
    """v1.6.4: see download_zwo_flat docstring for media-type + Disposition change.
    G1 (v2.1): ``outdoor=1`` adds an off-plan transit warmup + spin-home cooldown.
    task #24: ``cap=1``/on toggle caps short reps to measured power."""
    # Flat layout first, legacy category/file fallback
    path = _safe_path(active_workout_dir(), filename)
    if not path or not path.exists():
        path = _safe_path(active_workout_dir(), category, filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    return _zwo_download_response(path, filename, outdoor, transit_min, spin_min,
                                 cap_active=_cap_active_for_download(cap))


@app.get("/api/climb-zwo/{region}/{filename}")
def api_climb_zwo(region: str, filename: str, warmup: int = Query(10)):
    """Generate a ZWO workout from a climb profile, optionally with warmup."""
    # Get course profile
    profile = api_course_profile(region, filename)
    if isinstance(profile, dict) and profile.get("error"):
        return JSONResponse({"error": profile["error"]}, 404)
    points = profile.get("points", [])
    if len(points) < 2:
        return JSONResponse({"error": "Not enough profile data"}, 400)

    ftp = config.ATHLETE_FTP_W
    course_name = filename.rsplit(".", 1)[0]
    total_dist = points[-1]["d"]
    total_climb = sum(
        max(0, points[i]["e"] - points[i - 1]["e"])
        for i in range(1, len(points))
    )

    # Build ZWO segments from gradient profile
    # Group into ~500m segments and map gradient to power
    segments_xml = ""
    if warmup > 0:
        warmup_sec = warmup * 60
        segments_xml += f'    <Warmup Duration="{warmup_sec}" PowerLow="0.45" PowerHigh="0.65"/>\n'

    # Sample profile into ~20-40 segments for the ZWO
    num_segs = min(40, max(10, len(points) // 5))
    seg_step = max(1, len(points) // num_segs)

    for i in range(0, len(points) - seg_step, seg_step):
        j = min(i + seg_step, len(points) - 1)
        dist_km = points[j]["d"] - points[i]["d"]
        if dist_km <= 0:
            continue
        avg_grad = sum(points[k]["g"] for k in range(i, j + 1)) / (j - i + 1)

        # Map gradient to power (% FTP)
        power_pct = _gradient_to_power_factor(avg_grad)

        # Estimate duration: assume ~15km/h uphill, ~30km/h flat, ~40km/h downhill
        if avg_grad >= 5:
            speed_kmh = max(8, 20 - avg_grad * 1.2)
        elif avg_grad >= 0:
            speed_kmh = 25
        else:
            speed_kmh = min(45, 25 - avg_grad * 2)

        duration_sec = max(30, int(dist_km / speed_kmh * 3600))
        segments_xml += f'    <SteadyState Duration="{duration_sec}" Power="{power_pct:.2f}"/>\n'

    # Cooldown
    # v3.7.0 — a Cooldown ramps PowerLow -> PowerHigh, so 0.40 -> 0.60 was an
    # ascending "cooldown": it finished the rider at 60 % FTP. Same defect the
    # library carried; invisible to the library test because this is generated
    # into an HTTP response rather than written to workouts/.
    segments_xml += '    <Cooldown Duration="300" PowerLow="0.60" PowerHigh="0.45"/>\n'

    from xml.sax.saxutils import escape as xml_escape
    desc = f"Climb simulation: {course_name}. {total_dist:.1f}km, {total_climb:.0f}m elevation."
    if warmup > 0:
        desc = f"{warmup}min warmup + {desc}"

    zwo_xml = f"""<?xml version='1.0' encoding='utf-8'?>
<workout_file>
  <author>Domestique</author>
  <name>{xml_escape(course_name)}</name>
  <description>{xml_escape(desc)}</description>
  <sportType>bike</sportType>
  <workout>
{segments_xml}  </workout>
</workout_file>"""

    from fastapi.responses import Response
    return Response(
        content=zwo_xml,
        media_type="application/xml",
        headers={"Content-Disposition": f'attachment; filename="{course_name.replace(chr(34), "_")}.zwo"'},
    )


@app.get("/api/download/crs/{region}/{filename}")
def download_crs(region: str, filename: str):
    path = _safe_path(COURSE_DIR, region, filename)
    if not path or not path.exists():
        # Virtual routes live under courses/virtual/<region>/<filename>
        path = _safe_path(COURSE_DIR, "virtual", region, filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(path, filename=filename, media_type="text/plain")


@app.get("/api/course/{region}/{filename}/download")
def download_course_by_id(region: str, filename: str):
    """Serve a CRS course file as a download attachment.

    v4.0.0-alpha: IMPL-B's route-picker calls this URL pattern. The earlier
    /api/download/crs/<region>/<filename> route is kept for backward compat.
    """
    path = _safe_path(COURSE_DIR, region, filename)
    if not path or not path.exists():
        path = _safe_path(COURSE_DIR, "virtual", region, filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(
        path,
        filename=filename,
        media_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/download/gpx/{region}/{filename}")
def download_gpx(region: str, filename: str):
    path = _safe_path(active_gpx_dir(), region, filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    return FileResponse(path, filename=filename, media_type="application/gpx+xml")


# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS & FTP
# ═══════════════════════════════════════════════════════════════════════════════

def _tail_lines(p: Path, n: int = 2000) -> list[str]:
    """Return the last *n* lines of file *p* without loading it entirely.

    Seeks backwards in 4 KB blocks so a multi-megabyte log doesn't pin its
    full content in RAM (the previous `read_text().splitlines()[-n:]` on
    a noisy long-running session could easily exceed 50 MB). The final
    decode tolerates partial multi-byte sequences via `errors="replace"`.
    """
    with p.open("rb") as f:
        f.seek(0, 2)
        size = f.tell()
        if size == 0:
            return []
        block = 4096
        data = b""
        while f.tell() > 0 and data.count(b"\n") <= n:
            back = min(block, f.tell())
            f.seek(-back, 1)
            data = f.read(back) + data
            f.seek(-back, 1)
        return data.decode("utf-8", "replace").splitlines()[-n:]


@app.get("/api/logs")
def api_logs(lines: int = Query(100)):
    """Return last N lines of the log file for debugging.

    Hard-capped at 2000 lines via seek-from-end tailing so that a bloated
    log file (many MB) can't OOM the process when the debug viewer opens.
    """
    log_path = domestique_home() / "logs" / "domestique.log"
    if not log_path.exists():
        return {"lines": [], "path": str(log_path), "exists": False}
    try:
        cap = min(max(1, lines), 2000)
        tail = _tail_lines(log_path, n=cap)
        return {
            "lines": tail,
            "total": len(tail),
            "path": str(log_path),
            "size_kb": round(log_path.stat().st_size / 1024, 1),
            "exists": True,
        }
    except Exception as e:
        log.exception(f"Failed to read log file at {log_path}: {e}")
        return JSONResponse({"error": "Failed to read log file", "path": str(log_path)}, 500)


@app.get("/api/version")
def api_version():
    """Return app version and system info for bug reports.

    `platform.platform()` is intentionally omitted: it leaks the kernel
    build string with no diagnostic value for local-only bug reports.
    """
    import platform
    return {
        # Identity marker, not decoration: the launcher's single-instance probe
        # uses it to tell a running Domestique from any other server that
        # happens to hold :8080. Do not rename or remove.
        "app": "domestique",
        "version": _VERSION,
        "python": platform.python_version(),
        "frozen": getattr(sys, "frozen", False),
        "data_dir": str(domestique_home()),
    }


@app.get("/api/migrations/last-run-result")
def api_migrations_last_run_result():
    """Return the v1.0.2 startup migration-check result.

    Populated once during `lifespan` startup by ``run_v102_migration_check``.
    The dashboard fetches this on home-page boot; if ``show_toast`` is true
    AND the per-version localStorage flag isn't set, it surfaces a toast
    confirming rider data is preserved across the upgrade. Returns the
    defensive ``{"show_toast": False}`` shape if lifespan hasn't populated
    the cache yet (shouldn't happen in production).
    """
    return _LAST_MIGRATION_RESULT


# ─── v1.0.2 IMPL-BANNER: update-check endpoint ──────────────────────────────
# Hits the public GitHub Releases API to discover newer versions, caches the
# response at DATA_DIR/update_check_cache.json with a 6h TTL, filters the
# release assets by `sys.platform` so macOS users see the .dmg and Windows
# users see the .exe (or .zip fallback). On any httpx error, returns the
# last-good cached response with `error` populated — never raises to the UI.
#
# Response contract (locked by MASTER_DECISIONS_v102.md §1 + v187 expand):
#   { current, latest, update_available, release_url, download_url,
#     asset_name, platform, checked_at, cached, error, release_body }
_UPDATE_CHECK_CACHE_TTL_S = 6 * 60 * 60  # 6 hours
_GITHUB_RELEASES_LATEST_URL = "https://api.github.com/repos/platypus45/domestique/releases/latest"
_RELEASE_BODY_MAX_CHARS = 8192
_RELEASE_BODY_TRUNCATION_SUFFIX = "\n\n… (full release notes on GitHub)"


def _update_check_cache_path() -> Path:
    """Indirection so tests can monkeypatch the cache file location cleanly."""
    return DATA_DIR / "update_check_cache.json"


def _select_platform_asset(assets, plat):
    """Pick (download_url, asset_name) from a GitHub release `assets` list
    based on `plat` (sys.platform string).

    macOS (`darwin`) → `.dmg`, prefer plain `Domestique.dmg` over decorated
    variants like `Domestique-1.0.3.dmg` so the canonical asset wins.
    Windows (`win32`) → prefer `.exe`, fall back to `.zip`.
    Linux → `.AppImage` (released as `Domestique-v<VERSION>-x86_64.AppImage`).
    Anything else (BSD) → no asset; banner falls back to release_url.
    """
    if not isinstance(assets, list):
        return None, None

    def _name(a):
        return (a.get("name") or "") if isinstance(a, dict) else ""

    def _url(a):
        return (a.get("browser_download_url") or "") if isinstance(a, dict) else ""

    if plat == "darwin":
        dmgs = [a for a in assets if _name(a).lower().endswith(".dmg")]
        if not dmgs:
            return None, None
        canonical = [a for a in dmgs if _name(a) == "Domestique.dmg"]
        chosen = canonical[0] if canonical else dmgs[0]
        return _url(chosen) or None, _name(chosen) or None

    if plat == "win32":
        exes = [a for a in assets if _name(a).lower().endswith(".exe")]
        if exes:
            return _url(exes[0]) or None, _name(exes[0]) or None
        zips = [a for a in assets if _name(a).lower().endswith(".zip")]
        if zips:
            return _url(zips[0]) or None, _name(zips[0]) or None
        return None, None

    if plat == "linux":
        # One AppImage per release, and it always carries the version token —
        # unlike the DMG, whose bare name caused the v1.8.8 mix-up. No
        # canonical-name preference is needed because there is only ever one.
        imgs = [a for a in assets if _name(a).lower().endswith(".appimage")]
        if not imgs:
            return None, None
        return _url(imgs[0]) or None, _name(imgs[0]) or None

    return None, None


def _read_update_check_cache():
    p = _update_check_cache_path()
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_update_check_cache(payload):
    p = _update_check_cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        body = dict(payload)
        body["cache_written_at"] = clock.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        p.write_text(json.dumps(body, indent=2), encoding="utf-8")
    except Exception:
        pass


def _cache_is_fresh(cache, now):
    if not cache:
        return False
    # v1.8.15 — version-mismatch invalidation. The cache stores `current` =
    # the app version that WROTE it. If the live bundled version differs, the
    # app was updated/reinstalled since the cache was written, so the WHOLE
    # cached payload (latest, release_url, release_body, the stale `current`)
    # is from a prior install — treat it as not-fresh and force a full
    # refetch. Without this, only the `current`/`update_available` overlay
    # would be corrected while `release_body` etc. stayed stale for up to 6h
    # after an update. This is the "cache updates after new installation" fix.
    if cache.get("current") != _VERSION:
        return False
    written = cache.get("cache_written_at")
    if not written:
        return False
    try:
        ts = datetime.strptime(written, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return False
    return (now - ts).total_seconds() < _UPDATE_CHECK_CACHE_TTL_S


# ───────────────────────────────────────────────────────────────────────────
# intervals.icu OAuth — per-profile "Connect" (see IP_ICU_OAUTH.md). Additive:
# the only change to the existing data path is training._auth_header() preferring
# a Bearer token. client_id/secret are blank until ICU provisions them → /start
# bounces with ?icu=unavailable and nothing about the API-key path changes.
# ───────────────────────────────────────────────────────────────────────────
_icu_oauth_states: dict = {}        # state -> {"profile_id": str, "ts": float}
_ICU_OAUTH_STATE_TTL_S = 600        # 10 min (ICU's auth CODE itself expires in 2)


def _icu_oauth_prune(now: float) -> None:
    for _s, _v in list(_icu_oauth_states.items()):
        if now - _v.get("ts", 0) > _ICU_OAUTH_STATE_TTL_S:
            _icu_oauth_states.pop(_s, None)


def _icu_oauth_reset_throttle() -> None:
    """Mirror the API-key save: let the next sync run immediately on fresh creds."""
    try:
        _write_last_sync_at(0)
    except Exception:
        pass
    try:
        import db as _db
        _db._auth_disabled = False
        _db._consecutive_failures = 0
        _db._last_sync_error = None
    except Exception:
        pass


def _icu_oauth_safe_return(return_to: str) -> str:
    """Allow only same-app local paths as the post-OAuth return target (no open
    redirect). Anything that isn't a single-slash-rooted path falls back to '/'."""
    rt = (return_to or "/").strip()
    if not rt.startswith("/") or rt.startswith("//") or "\\" in rt:
        return "/"
    return rt


def _icu_profile_exists(pm, profile_id: str) -> bool:
    """AC3a: does the OAuth-state's profile still exist in the registry?"""
    return bool(profile_id) and any(
        p.get("id") == profile_id for p in pm.list_profiles())


def _icu_profile_stored_athlete_id(pm, profile_id: str) -> str:
    """Stored ICU_ATHLETE_ID for ``profile_id`` (may be non-active). Reads the
    profile's own .env — never the active profile's in-memory _env."""
    try:
        if profile_id == pm.active_id:
            return pm.icu_athlete_id or ""
        env = pm._load_env_file(pm._profiles_dir / profile_id / ".env")
        return (env.get("ICU_ATHLETE_ID") or "").strip()
    except Exception:
        return ""


def _save_icu_token_for_profile(pm, profile_id: str, access_token: str,
                                athlete_id: "str | None",
                                athlete_name: "str | None",
                                refresh_token: "str | None" = None,
                                expires_in: "int | float | None" = None,
                                granted_scopes: "str | None" = None) -> None:
    """AC3a: persist an OAuth token to the profile that STARTED the flow —
    not whichever profile happens to be active when ICU redirects back.

    Active profile → the normal pm.save_icu_token path (in-memory _env +
    os.environ refresh). Non-active → path-based write into that profile's
    .env (atomic, 0600) + athlete.json, leaving the active profile's
    in-memory state untouched.

    AC3c (capture only, no refresh flow): refresh_token / expires_in are
    stored when ICU sends them — mirror of pm.save_icu_token's fields.

    v3.0.1 (IP_ICU_PUSH): ``granted_scopes`` — the token response's "scope"
    field — is stamped as ICU_GRANTED_SCOPES so the calendar push engine
    knows whether CALENDAR:WRITE was granted.
    """
    if profile_id == pm.active_id:
        pm.save_icu_token(access_token, athlete_id, athlete_name,
                          refresh_token, expires_in,
                          granted_scopes=granted_scopes)
        return
    prof_dir = pm._profiles_dir / profile_id
    env = {}
    try:
        env = pm._load_env_file(prof_dir / ".env")
    except Exception:
        env = {}
    new_id = (athlete_id if athlete_id is not None
              else (env.get("ICU_ATHLETE_ID") or "")).strip()
    api_key = (env.get("ICU_API_KEY") or "").strip()
    token = (access_token or "").strip()
    lines = [f"ICU_ATHLETE_ID={new_id}",
             f"ICU_API_KEY={api_key}",
             f"ICU_ACCESS_TOKEN={token}"]
    if token:
        # AC3c parity with pm.save_icu_token: keep/refresh the capture fields;
        # a disconnect (empty token) drops them.
        rt = (str(refresh_token).strip() if refresh_token
              else (env.get("ICU_REFRESH_TOKEN") or "").strip())
        if rt:
            lines.append(f"ICU_REFRESH_TOKEN={rt}")
        gs = (str(granted_scopes).strip() if granted_scopes
              else (env.get("ICU_GRANTED_SCOPES") or "").strip())
        if gs:
            lines.append(f"ICU_GRANTED_SCOPES={gs}")
        exp = (env.get("ICU_TOKEN_EXPIRES_AT") or "").strip()
        try:
            if expires_in is not None and float(expires_in) > 0:
                exp = str(int(time.time() + float(expires_in)))
        except (TypeError, ValueError):
            _log.warning("icu token save: ignoring bad expires_in %r", expires_in)
        if exp:
            lines.append(f"ICU_TOKEN_EXPIRES_AT={exp}")
    if any(c in "".join(lines) for c in "\n\r"):
        raise ValueError("credentials may not contain newlines")
    pm._write_env_atomic(prof_dir / ".env", "\n".join(lines) + "\n")
    if athlete_name is not None:
        athlete = pm._load_json(prof_dir / "athlete.json")
        if not isinstance(athlete, dict):
            athlete = {}
        athlete["icu_athlete_name"] = athlete_name
        pm._write_json(prof_dir / "athlete.json", athlete)


@app.get("/oauth/icu/start")
def api_oauth_icu_start(return_to: str = Query("/")):
    """Begin the ICU OAuth flow: mint a CSRF state, 302 to ICU's authorize page.
    Blank client_id (not yet provisioned) → bounce back with ?icu=unavailable.
    ``return_to`` (validated local path) is where the callback bounces afterward
    so the setup wizard can resume on its own step and show the linked account."""
    from fastapi.responses import RedirectResponse
    from profile_manager import ProfileManager
    import secrets as _secrets
    from urllib.parse import urlencode
    if not getattr(config, "ICU_OAUTH_CLIENT_ID", ""):
        return RedirectResponse(url="/?icu=unavailable")
    now = time.time()
    _icu_oauth_prune(now)
    try:
        profile_id = ProfileManager.get().active_id or ""
    except Exception:
        profile_id = ""
    state = _secrets.token_urlsafe(32)
    _icu_oauth_states[state] = {"profile_id": profile_id, "ts": now,
                                "return_to": _icu_oauth_safe_return(return_to)}
    _log.info("EVENT=icu_oauth_start profile=%s tls=%s", profile_id or "?",
              tls_trust.backend_name())
    params = urlencode({
        "client_id": config.ICU_OAUTH_CLIENT_ID,
        "redirect_uri": config.ICU_OAUTH_REDIRECT_URI,
        "scope": config.ICU_OAUTH_SCOPES,
        "state": state,
    })
    return RedirectResponse(url=f"{config.ICU_OAUTH_AUTHORIZE_URL}?{params}")


@app.get("/oauth/icu/callback")
def api_oauth_icu_callback(code: str = Query(""), state: str = Query(""),
                           error: str = Query("")):
    """ICU redirects here after the user authorizes. Verify the state (CSRF,
    single-use, 10-min TTL), exchange the code for a bearer token within ICU's
    2-min window, persist it to the active profile, then bounce to the app.
    Never echoes the code/secret into a redirect."""
    from fastapi.responses import RedirectResponse
    from profile_manager import ProfileManager
    now = time.time()
    _icu_oauth_prune(now)
    st = _icu_oauth_states.pop(state, None)        # single-use
    return_to = _icu_oauth_safe_return((st or {}).get("return_to") or "/")
    _sep = "&" if "?" in return_to else "?"

    def _back(suffix: str):
        return RedirectResponse(url=f"{return_to}{_sep}{suffix}")

    if error:
        _log.info("EVENT=icu_oauth_denied error=%s", error)
        return _back("icu=error&reason=denied")
    if not st or not code:
        _log.warning("EVENT=icu_oauth_bad_state has_state=%s has_code=%s",
                     bool(st), bool(code))
        return _back("icu=error&reason=state")
    # AC3a: the token belongs to the profile that STARTED this flow. If it was
    # deleted while the user was on intervals.icu, error out — never bind the
    # token to whichever profile is active now.
    target_pid = (st.get("profile_id") or "").strip()
    pm = ProfileManager.get()
    if not _icu_profile_exists(pm, target_pid):
        _log.warning("EVENT=icu_oauth_profile_gone profile=%s", target_pid or "?")
        return _back("icu=error&reason=profile_gone")
    try:
        import httpx
        # Diagnostic: a build that didn't bundle .oauth.env ships an empty secret
        # → ICU rejects the exchange. Log it explicitly (the symptom on Windows CI
        # builds before the secret was wired into release.yml).
        if not getattr(config, "ICU_OAUTH_CLIENT_SECRET", ""):
            _log.error("EVENT=icu_oauth_no_secret — client_secret missing from this "
                       "build (.oauth.env not bundled); token exchange will fail")
        # v3.11.3: PHASE-SPECIFIC failure codes. Every failure below used to
        # collapse into one "token exchange failed" — including a token that
        # exchanged fine but could not be SAVED (profile-folder permissions)
        # and an athlete-change purge that raised. A rider's screenshot then
        # said nothing about which step broke. Each phase now names itself.
        try:
            resp = httpx.post(config.ICU_OAUTH_TOKEN_URL, data={
                "client_id": config.ICU_OAUTH_CLIENT_ID,
                "client_secret": config.ICU_OAUTH_CLIENT_SECRET,
                "code": code,
            }, timeout=15, verify=_icu_verify())
        except httpx.TransportError as e:
            # DNS / TLS / proxy / connection refused — the request never got an
            # answer. Certificate stores and corporate proxies live here.
            _log.warning("EVENT=icu_oauth_network error=%s", str(e)[:200])
            # v3.11.4: name the interceptor. Two Windows logs in a row said
            # "certificate verify failed" without saying WHOSE certificate.
            # Unverified fetch of the presented chain, diagnostics only.
            try:
                from urllib.parse import urlparse as _up
                _host = _up(config.ICU_OAUTH_TOKEN_URL).hostname or "intervals.icu"
                _log.warning("EVENT=icu_oauth_peer_chain host=%s tls=%s chain=%s", _host,
                             tls_trust.backend_name(),
                             " | ".join(tls_trust.peer_chain_summary(_host))[:600])
            except Exception as e2:
                _log.warning("EVENT=icu_oauth_peer_chain unavailable=%s", str(e2)[:120])
            return _back("icu=error&reason=network")
        if resp.status_code != 200:
            _log.warning("EVENT=icu_oauth_exchange_http status=%s body=%s",
                         resp.status_code, (resp.text or "")[:200])
            return _back(f"icu=error&reason=exchange&status={int(resp.status_code)}")
        try:
            tok = resp.json()
        except ValueError:
            _log.warning("EVENT=icu_oauth_bad_json body=%s", (resp.text or "")[:200])
            return _back("icu=error&reason=token")
        access_token = tok.get("access_token")
        athlete = tok.get("athlete") or {}
        athlete_id = str(athlete.get("id") or "")
        athlete_name = (athlete.get("name") or "").strip()
        # v3.0.1 (IP_ICU_PUSH): ICU's token response carries the GRANTED
        # scope set (forum #2759). Stamp it on the profile so the calendar
        # push engine can tell a write-capable connection from a legacy
        # read-only one (missing stamp ⇒ reconnect prompt, no silent 403s).
        granted_scopes = tok.get("scope") or ""
        if isinstance(granted_scopes, (list, tuple)):
            granted_scopes = ",".join(str(s) for s in granted_scopes)
        granted_scopes = str(granted_scopes).strip()
        if not access_token:
            raise RuntimeError("no access_token in token response")
        # Capture the athlete's display name for the "Linked as <name>" UI. The
        # token response usually carries it; if not, one lightweight /athlete/0
        # fetch with the fresh token fills it (also backstops a missing id).
        if not athlete_name or not athlete_id:
            try:
                r2 = httpx.get("https://intervals.icu/api/v1/athlete/0",
                               headers={"Authorization": f"Bearer {access_token}"},
                               timeout=10, verify=_icu_verify())
                if r2.status_code == 200:
                    a2 = r2.json() or {}
                    athlete_name = athlete_name or (a2.get("name") or "").strip()
                    athlete_id = athlete_id or str(a2.get("id") or "")
            except Exception:
                pass
        # AC3d: no athlete id → hard error surface (the connection card shows a
        # retry hint) instead of persisting a token that yields silent
        # sync_skipped-forever. Nothing is saved — retry is clean.
        if not athlete_id:
            _log.warning("EVENT=icu_oauth_no_athlete_id profile=%s", target_pid)
            return _back("icu=error&reason=no_athlete_id")
        # AC3b: the SAME profile reconnected as a DIFFERENT ICU athlete →
        # its local rows/mirrors belong to the old athlete; purge before save.
        prev_athlete = _icu_profile_stored_athlete_id(pm, target_pid)
        if prev_athlete and prev_athlete != athlete_id:
            _log.info("EVENT=icu_oauth_athlete_changed profile=%s old=%s new=%s "
                      "— purging the profile's synced data",
                      target_pid, prev_athlete, athlete_id)
            try:
                db.purge_profile_data(target_pid)
            except db.SyncBusy:
                # Nothing saved yet — a clean retry re-runs the whole flow.
                _log.warning("EVENT=icu_oauth_purge_busy profile=%s", target_pid)
                return _back("icu=error&reason=busy")
            except Exception:
                _log.exception("EVENT=icu_oauth_purge_failed profile=%s", target_pid)
                return _back("icu=error&reason=purge")
        try:
            _save_icu_token_for_profile(
                pm, target_pid, access_token, athlete_id or None, athlete_name or None,
                refresh_token=tok.get("refresh_token"),
                expires_in=tok.get("expires_in"),
                granted_scopes=granted_scopes or None)
        except Exception:
            # The exchange SUCCEEDED — the token just could not be persisted
            # (profile folder permissions / disk). The one-time code is spent,
            # so the rider must authorise again after fixing the folder.
            _log.exception("EVENT=icu_oauth_save_failed profile=%s", target_pid)
            return _back("icu=error&reason=save")
        if target_pid == pm.active_id:
            # Unshadow stale module-level config.ICU_* so the proxy re-resolves
            # to the freshly-saved token immediately (same fix as the API-key
            # save path). Only meaningful when the ACTIVE profile changed.
            for _attr in ("ICU_ATHLETE_ID", "ICU_API_KEY", "ICU_ACCESS_TOKEN"):
                try:
                    delattr(config, _attr)
                except AttributeError:
                    pass
            _icu_oauth_reset_throttle()
        _log.info("EVENT=icu_oauth_connected profile=%s athlete_id=%s name=%r",
                  target_pid, athlete_id or "?", athlete_name or "")
        return _back("icu=connected")
    except Exception:
        # Anything not caught by a phase above (e.g. a raise from inside a
        # mocked/unknown step) keeps the historical catch-all code.
        _log.exception("ICU OAuth token exchange failed")
        return _back("icu=error&reason=exchange")


@app.post("/api/icu/disconnect")
def api_icu_disconnect():
    """AC3b: disconnect = clear the token AND purge the profile's synced data
    (activities/wellness/sync_log rows + icu-sourced athlete mirrors). The next
    account connected to this profile must start clean — no residual rows from
    the previous athlete polluting CTL/ATL."""
    from profile_manager import ProfileManager
    try:
        pm = ProfileManager.get()
        pid = pm.active_id
        if pid is None:
            return JSONResponse({"ok": False, "error": "no active profile"},
                                status_code=409)
        # v3.0.1 (IP_ICU_PUSH G-A): best-effort removal of OUR pushed calendar
        # events BEFORE the token purge — afterwards there are no credentials
        # left to delete with. Failures never block the disconnect.
        try:
            import icu_calendar_push as _icp
            _icp.sweep_all()
        except Exception:
            _log.debug("icu disconnect calendar sweep failed (best-effort)",
                       exc_info=True)
        try:
            db.purge_profile_data(pid)
        except db.SyncBusy:
            return JSONResponse(
                {"ok": False, "error": "sync busy — try again in a moment"},
                status_code=503)
        pm.save_icu_token("", None)
        try:
            delattr(config, "ICU_ACCESS_TOKEN")
        except AttributeError:
            pass
        _log.info("EVENT=icu_disconnect profile=%s (token cleared + data purged)", pid)
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/icu/connection")
def api_icu_connection():
    """Connection state for the UI: method = oauth / apikey / none.

    ``name`` is the linked intervals.icu athlete's display name (OAuth) so the
    UI can show 'Linked as <name>'. ``needs_oauth_migration`` flags a profile
    still on a legacy API key (no OAuth token) so the dashboard can prompt the
    one-click sign-in upgrade."""
    token = getattr(config, "ICU_ACCESS_TOKEN", "") or ""
    key = getattr(config, "ICU_API_KEY", "") or ""
    aid = getattr(config, "ICU_ATHLETE_ID", "") or ""
    method = "oauth" if token else ("apikey" if key else "none")
    name = ""
    try:
        from profile_manager import ProfileManager
        name = ProfileManager.get().icu_name or ""
    except Exception:
        name = ""
    # v3.0.1 (IP_ICU_PUSH): can this connection write the ICU calendar?
    # apikey → always (G-F); oauth → needs the CALENDAR:WRITE stamp (legacy
    # connections lack it → the UI offers a one-click reconnect).
    _wok = False
    try:
        import icu_calendar_push as _icp
        _wok = _icp.write_ok()
    except Exception:
        _wok = False
    return {
        "connected": bool(token or key),
        "method": method,
        "athlete_id": aid,
        "name": name,
        "write_ok": _wok,
        "needs_oauth_migration": bool(key and not token),
        "oauth_available": bool(getattr(config, "ICU_OAUTH_CLIENT_ID", "")),
    }


# ── v3.0.1 (IP_ICU_PUSH) — calendar push: endpoints + debounced plan hook ────
# One engine (icu_calendar_push.reconcile), two triggers: the plan-tab button
# (POST below) and the atomic_write_plan post-write callback registered at
# boot, debounced ~30s so a mutation burst coalesces into ONE bulk call.

ICU_PUSH_DEBOUNCE_S = 30.0
_icu_push_timer_lock = threading.Lock()
_icu_push_timer: "threading.Timer | None" = None

# 3.3.1 hotfix (B3b): last reconcile outcome, whatever the trigger (button /
# debounced / daily). Background runs previously failed SILENTLY — the tester's
# swap-triggered push 422'd four times and the UI had nothing to show. GET
# /api/icu/push now carries this so the sync status can surface error +
# error_detail without waiting for the user to press the button.
_icu_push_last_result: "dict | None" = None


def _icu_push_note_result(res: dict) -> None:
    """Record a reconcile result (+timestamp) for the status endpoint."""
    global _icu_push_last_result
    try:
        _icu_push_last_result = {"at": clock.now().isoformat(), **res}
    except Exception:
        pass


def _icu_push_sync_enabled() -> bool:
    """The Settings toggle 'Keep intervals.icu calendar in sync' (default OFF)."""
    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        return pm.active_id is not None and bool(
            pm.prefs.get("icu_calendar_sync"))
    except Exception:
        return False


def _icu_push_debounced_run(expected_pid: str) -> None:
    global _icu_push_timer
    with _icu_push_timer_lock:
        _icu_push_timer = None
    try:
        if not _icu_push_sync_enabled():   # toggled off while pending
            return
        # TOCTOU guard: the profile that scheduled this push must still be
        # active at fire time — a switch during the debounce window would
        # push profile A's plan under A's prefix onto B's calendar, where
        # B's sweeps treat it as foreign (G-B: warn, never delete) and it
        # can never be cleaned up. (A switch during the reconcile's own
        # network span is the same race class the sync loop handles with
        # SyncAborted — seconds wide, accepted.)
        from profile_manager import ProfileManager
        if ProfileManager.get().active_id != expected_pid:
            _log.info("EVENT=icu_push_debounced_abort reason=profile_switch")
            return
        import icu_calendar_push
        res = icu_calendar_push.reconcile()
        _icu_push_note_result(res)
        # 3.3.1 hotfix (B3b): this line omitted `error` entirely — the
        # tester's swap-triggered 422 failed with a clean-looking log line.
        _log.info("EVENT=icu_push_debounced pushed=%s updated=%s deleted=%s "
                  "needs_reconnect=%s error=%s error_detail=%s",
                  res.get("pushed"), res.get("updated"), res.get("deleted"),
                  res.get("needs_reconnect"), res.get("error"),
                  res.get("error_detail"))
    except Exception:
        _log.warning("debounced ICU calendar push failed", exc_info=True)


def _icu_push_schedule_debounced(json_path) -> None:
    """training_planner.atomic_write_plan post-write callback (boot-registered;
    the CLI path never registers it). Coalesces: every plan write within the
    window cancels and re-arms one shared ~30s timer. Only the ACTIVE
    profile's live current_plan.json schedules — never a stray path."""
    global _icu_push_timer
    try:
        if not _icu_push_sync_enabled():
            return
        from profile_manager import ProfileManager
        pid = ProfileManager.get().active_id
        if pid is None:
            return
        if Path(json_path).name != "current_plan.json":
            return
        with _icu_push_timer_lock:
            if _icu_push_timer is not None:
                _icu_push_timer.cancel()
            t = threading.Timer(ICU_PUSH_DEBOUNCE_S, _icu_push_debounced_run,
                                args=(pid,))
            t.daemon = True
            _icu_push_timer = t
            t.start()
    except Exception:
        _log.debug("icu push debounce scheduling failed", exc_info=True)


def _icu_push_cancel_pending() -> None:
    """Cancel any armed debounce timer (toggle-OFF and shutdown paths)."""
    global _icu_push_timer
    with _icu_push_timer_lock:
        if _icu_push_timer is not None:
            _icu_push_timer.cancel()
            _icu_push_timer = None


_icu_push_last_daily: "str | None" = None


def _after_background_sync() -> None:
    """db.post_sync_callback: what follows each pass of the 30-min sync loop.
    The daily calendar reconcile, and the F5 eFTP auto-apply (opt-in, behind
    the plausibility guard), which /api/weekly-plan used to run on a read."""
    _icu_push_daily_from_sync()
    try:
        from profile_manager import ProfileManager
        if not ProfileManager.get().prefs.get("eftp_auto_apply", False):
            return      # opt-in; don't call intervals.icu every pass for nothing
        # 14 days, so a 7-day streak ending yesterday stays in the window.
        auto = _guarded_check_and_auto_apply_eftp(fetch_wellness(14))
        if auto:
            _log.info(f"EVENT=eftp_auto_applied {auto}")
            clear_cache()
    except Exception as _e:  # noqa: BLE001 - never break the sync loop
        _log.debug(f"eftp auto-apply skipped: {_e}")


def _icu_push_daily_from_sync() -> None:
    """db.post_sync_callback (boot-registered): ONE reconcile per calendar
    day, riding the 30-min sync loop (D3b). This is what rolls the 14-day
    horizon forward on mutation-free days and mirrors boot-time plan writes
    — the atomic_write_plan hook is deliberately registered AFTER those."""
    global _icu_push_last_daily
    try:
        if not _icu_push_sync_enabled():
            return
        today = clock.today().isoformat()
        if _icu_push_last_daily == today:
            return
        _icu_push_last_daily = today
        import icu_calendar_push
        res = icu_calendar_push.reconcile()
        _icu_push_note_result(res)
        if res.get("error"):
            _icu_push_last_daily = None   # transient — retry next sync pass
        # 3.3.1 hotfix (B3b): + error_detail (the ICU body excerpt) so a
        # rejected batch is a one-log diagnosis instead of a bare http_422.
        _log.info("EVENT=icu_push_daily pushed=%s updated=%s deleted=%s "
                  "needs_reconnect=%s error=%s error_detail=%s",
                  res.get("pushed"), res.get("updated"), res.get("deleted"),
                  res.get("needs_reconnect"), res.get("error"),
                  res.get("error_detail"))
    except Exception:
        _icu_push_last_daily = None
        _log.warning("daily ICU calendar push failed", exc_info=True)


@app.get("/api/icu/push")
def api_icu_push_status():
    """Push-button + sync-toggle state for the UI. Never raises."""
    import icu_calendar_push as _icp
    from profile_manager import ProfileManager
    token = getattr(config, "ICU_ACCESS_TOKEN", "") or ""
    key = getattr(config, "ICU_API_KEY", "") or ""
    _wok = False
    try:
        _wok = _icp.write_ok()
    except Exception:
        _wok = False
    sync_enabled = False
    try:
        sync_enabled = bool(ProfileManager.get().prefs.get("icu_calendar_sync"))
    except Exception:
        pass
    return {
        "connected": bool(token or key),
        "method": "oauth" if token else ("apikey" if key else "none"),
        "write_ok": _wok,
        "sync_enabled": sync_enabled,
        "horizon_days": 14,
        # 3.3.1 hotfix (B3b): outcome of the most recent reconcile (any
        # trigger), incl. error + error_detail — background pushes are no
        # longer silent to the UI.
        "last_result": _icu_push_last_result,
    }


@app.post("/api/icu/push")
def api_icu_push(body: "dict | None" = None):
    """No body / {} → run the reconcile NOW (the plan-tab button).
    {"sync": true|false} → flip the keep-in-sync toggle; OFF runs the final
    forward sweep (G-A) so our events leave the athlete's calendar.
    Always 200 with a result dict — needs_reconnect/error are data, not 500s
    (G4)."""
    import icu_calendar_push as _icp
    from profile_manager import ProfileManager
    body = body or {}
    try:
        if "sync" in body:
            enable = bool(body.get("sync"))
            pm = ProfileManager.get()
            if pm.active_id is None:
                return {"ok": False, "error": "no_active_profile"}
            if enable:
                if not _icp.write_ok(pm):
                    # Don't arm a toggle that can only 403 — reconnect first.
                    return {"ok": False, "needs_reconnect": True,
                            "sync_enabled": False}
                pm.save_prefs({"icu_calendar_sync": True})
                return {"ok": True, "sync_enabled": True}
            pm.save_prefs({"icu_calendar_sync": False})
            _icu_push_cancel_pending()      # a pending debounce would re-push
            res = _icp.sweep_all()          # G-A: final forward sweep
            return {"ok": True, "sync_enabled": False, **res}
        res = _icp.reconcile()
        _icu_push_note_result(res)          # 3.3.1 hotfix (B3b): status parity
        return {"ok": not res.get("error") and not res.get("needs_reconnect"),
                **res}
    except Exception as e:
        _log.exception("icu push endpoint failed")
        return {"ok": False, "error": f"internal:{type(e).__name__}"}


@app.post("/api/calendar/push-workout")
def api_calendar_push_workout(body: "dict | None" = None):
    """calendar-push (2026-07-07): push ONE workout to the athlete's ICU calendar
    from a workout-detail modal — the "Send to intervals.icu calendar" button.

    Body: {source:"planner"|"library", zwo_file, date (ISO — REQUIRED for both),
    name?}. Two shapes:
      library — a dateless library workout on a user-picked date → a PERMANENT
        manual entry under the domestique-manual: root the plan-mirror sweep
        never deletes (distinct prefix; _ours_in_window skips it). Repeated
        pushes are distinct (uuid8) and coexist by design with a planned session
        on the same day (different id roots, O4).
      planner — a planned session on its own date → REUSE _desired_events and
        push the ONE matching event, inheriting the EXACT domestique:<pid>:<day>:<n>
        auto-sync would produce (byte-identical → merges, no duplicate). Never a
        client index or a hand-rolled <n> (grill amendment 2). O1: the horizon is
        extended to cover a target day beyond the 14-day auto-sync window.

    ALWAYS HTTP 200 with a data dict ({ok,pushed,event_date} / {needs_reconnect}
    / {needs_lthr} / {error}) — never a 4xx/500, so the shared toast handler reads
    the fields (amendment 7; mirrors api_icu_push). Guard chain mirrors reconcile
    (active profile → connection → write_ok); the icu_calendar_sync PREF is
    deliberately NOT gated — a manual push needs only CALENDAR:WRITE, granted once
    (D2). HR-mode + broken LTHR → needs_lthr, never a silently-degraded power FIT
    (amendment 3 / O2)."""
    import icu_calendar_push as _icp
    from profile_manager import ProfileManager
    body = body or {}
    try:
        source = str(body.get("source") or "")
        zwo_file = str(body.get("zwo_file") or "").strip()
        date_str = str(body.get("date") or "").strip()
        if source not in ("planner", "library"):
            return {"error": "bad_source"}
        if not zwo_file:
            return {"error": "no_workout_file"}

        # Guard chain, mirroring reconcile (icu_calendar_push.py active-profile →
        # _connection → write_ok). Order matters: an apikey with no athlete_id is
        # write_ok yet not usable, so _connection gates FIRST.
        pm = ProfileManager.get()
        profile_id = pm.active_id
        if not profile_id:
            return {"error": "no_active_profile"}
        athlete_id, err = _icp._connection(pm)
        if err:
            return {"error": err}
        if not _icp.write_ok(pm):
            return {"needs_reconnect": True}

        # amendment 3 / O2: raw target_mode (NOT pm.target_mode, which degrades
        # hr→power on a broken invariant). HR rider + broken/missing LTHR must be
        # blocked BEFORE any build — never push a power FIT to an HR rider.
        raw_mode = (pm._athlete.get("target_mode") or "power")
        hr_mode = raw_mode == "hr"
        lthr_ok = bool(pm.lthr_is_set and pm.max_hr > pm.lthr)
        if hr_mode and not lthr_ok:
            return {"needs_lthr": True}

        # date REQUIRED for both sources (amendment 1): planner needs it to locate
        # the day; library is the user-picked target. today..+365 — past/garbage
        # rejected so nothing stale or malformed reaches the calendar.
        today = clock.today()
        try:
            target = date.fromisoformat(date_str)
        except (ValueError, TypeError):
            return {"error": "bad_date"}
        if target < today or target > today + timedelta(days=365):
            return {"error": "date_out_of_range"}
        date_iso = target.isoformat()

        workout_dir = Path(active_workout_dir())
        if source == "library":
            # amendment 5: zwo_file is CLIENT-supplied → reject traversal BEFORE
            # any read (_safe_path returns None when the resolved path escapes
            # active_workout_dir(), e.g. "../../etc/passwd"). _build_event re-validates too,
            # but failing fast here gives the clear message.
            if not _safe_path(workout_dir, zwo_file):
                return {"error": "invalid_workout_file"}
            classifications = _icp._load_classifications(workout_dir)
            # session_type from the content classifier (nested under
            # "classifications"; flat fallback) — display-only, the attachment is
            # driven by zwo_file. The frontend's display name is authoritative for
            # the event title (via zwo_name in the _display_name cascade).
            entry = (classifications.get("classifications") or {}).get(zwo_file) \
                or classifications.get(zwo_file) or {}
            ext_id = f"{_icp._MANUAL_ID_ROOT}{profile_id}:{uuid.uuid4().hex[:8]}"
            synth = {"zwo_file": zwo_file, "day": date_iso,
                     "session_type": str(entry.get("primary") or ""),
                     "zwo_name": str(body.get("name") or "")}
            event, reason = _icp._build_event(
                synth, ext_id, pm, classifications, workout_dir,
                hr_mode, lthr_ok)
            if event is None:
                return {"error": reason}
            events = [event]
        else:  # planner
            plan = _icp._load_plan()
            if plan is None:
                return {"error": "no_plan"}
            # O1: extend the horizon so a day beyond the 14-day auto-sync window
            # is still buildable. _desired_events assigns the exact <n>, so the
            # selected event is byte-identical to what auto-sync would push.
            horizon = max(_icp.HORIZON_DAYS, (target - today).days)
            desired, skipped, _ = _icp._desired_events(
                pm, plan, today, horizon, profile_id)
            # Match on (day, filename): filename is the ZWO name (power) or
            # stem+".fit" (hr) that _build_event emits. Same-zwo-twice-on-a-day →
            # identical events → the first is correct (harmless).
            want = (Path(zwo_file).stem + ".fit") if hr_mode \
                else Path(zwo_file).name
            event = next(
                (ev for ev in desired
                 if ev["start_date_local"][:10] == date_iso
                 and ev["filename"] == want), None)
            if event is None:
                # Defense (the frontend hides the button per O3): a slot with no
                # ZWO / dismissed / skipped isn't pushable. Surface needs_lthr if
                # that's why (can't happen here — guarded above — but explicit).
                reason = next((s["reason"] for s in skipped
                               if s["day"] == date_iso), None)
                if reason == "needs_lthr":
                    return {"needs_lthr": True}
                return {"error": "not a pushable planned session"}
            events = [event]

        # ONE bulk upsert (idempotent on external_id) via the engine's own seam —
        # the SAME transport reconcile uses, so 403-scope maps to needs_reconnect.
        status, resp = _icp._http(
            "POST", f"athlete/{athlete_id}/events/bulk?upsert=true",
            payload=events)
        if _icp._scope_403(status, resp):
            return {"needs_reconnect": True}
        if status == 0:
            return {"error": "network"}
        if not (200 <= status < 300):
            return {"error": f"http_{status}"}
        _log.info("EVENT=icu_calendar_push_workout source=%s date=%s pushed=1",
                  source, date_iso)
        return {"ok": True, "pushed": 1, "event_date": date_iso}
    except Exception as e:                      # amendment 7: zero 500s, ever
        _log.exception("calendar push-workout endpoint failed")
        return {"error": f"internal:{type(e).__name__}"}


@app.get("/api/icu/athlete-numbers")
def api_icu_athlete_numbers():
    """Pull FTP / weight / LTHR / max-HR from the linked intervals.icu athlete so
    the setup wizard can prefill 'Athlete Numbers'. Best-effort: any field ICU
    doesn't have comes back null. Uses the active profile's OAuth token (or legacy
    API key) via training._get / _auth_header. eFTP (from wellness) backstops a
    missing configured FTP."""
    out = {"ok": False, "ftp": None, "weight": None, "lthr": None, "max_hr": None}
    token = getattr(config, "ICU_ACCESS_TOKEN", "") or ""
    key = getattr(config, "ICU_API_KEY", "") or ""
    if not (token or key):
        return out
    try:
        import training as _training
        a = _training._get("athlete/0")
        if isinstance(a, dict):
            out["weight"] = a.get("weight")
            out["max_hr"] = a.get("max_hr")
            # The cycling sport's FTP/LTHR/maxHR live in sportSettings[]; pick the
            # Ride-typed entry (fall back to the first).
            ss = a.get("sportSettings") or []
            ride = None
            for s in ss:
                if not isinstance(s, dict):
                    continue
                types = s.get("types") or []
                if any(t in ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide")
                       for t in types):
                    ride = s
                    break
            ride = ride or (ss[0] if ss and isinstance(ss[0], dict) else {})
            out["ftp"] = ride.get("ftp") or out["ftp"]
            out["lthr"] = ride.get("lthr") or out["lthr"]
            out["max_hr"] = ride.get("max_hr") or out["max_hr"]
            out["ok"] = True
    except Exception:
        _log.debug("athlete-numbers: /athlete/0 fetch failed", exc_info=True)
    # eFTP fallback when no configured FTP on the account.
    if not out["ftp"]:
        try:
            wellness = cached("wellness_7", lambda: fetch_wellness(7))
            if wellness:
                for w in reversed(wellness):
                    si = w.get("sportInfo", [])
                    if si and si[0].get("eftp"):
                        _cand = round(si[0]["eftp"])
                        # 3.3.1 hotfix (B5): sportInfo[0] is sport-UNFILTERED
                        # (the first entry may be a non-Ride sport) and a
                        # sparse-data eFTP can be nonsense — this prefill is
                        # the one path that INVENTS an FTP the rider never
                        # typed (prime suspect for the tester's ftp=122).
                        # Implausible → don't prefill; the wizard says
                        # "enter manually" and manual entry stays ungated.
                        if not _ftp_auto_ingest_ok(
                                _cand, getattr(config, "ATHLETE_FTP_W", 0)):
                            _log.warning(
                                "EVENT=ftp_auto_ingest_rejected "
                                "source=athlete_numbers_eftp candidate=%s "
                                "current=%s", _cand,
                                getattr(config, "ATHLETE_FTP_W", 0))
                            break
                        out["ftp"] = _cand
                        out["ok"] = True
                        break
        except Exception:
            pass
    return out


@app.get("/api/update/check")
def api_update_check(force: int = Query(0)):
    """Live GitHub-Releases poll with 6h cache + platform-specific asset.

    Always returns 200; on upstream failure, populates `error` and returns
    the last-good cache so the dashboard banner can still render.

    ``force=1`` bypasses the 6h cache and re-polls GitHub. The dashboard sends
    it once on startup so a release published while the app was closed surfaces
    on the very next launch instead of waiting up to 6h for the cache to expire
    (the cache held a stale ``latest`` and the banner never appeared).
    """
    import httpx
    from packaging.version import parse as _vparse, InvalidVersion

    plat = sys.platform
    now = clock.now(timezone.utc)
    cache = _read_update_check_cache()

    def _overlay_live_current(out):
        # v1.8.15 BUGFIX — ``current`` is the RUNNING app's own bundled
        # version; it is known instantly from ``_VERSION`` and must NEVER be
        # served from cache. Caching it meant a just-updated app (e.g. 1.8.9
        # → 1.8.14) kept showing the OLD ``current`` for up to 6h, so the
        # update banner read a phantom "v1.8.9 → v1.8.14" even though 1.8.14
        # was already running. Always recompute current + update_available
        # from the live version against the cached ``latest``.
        out["current"] = _VERSION
        try:
            out["update_available"] = bool(
                _vparse(_VERSION) < _vparse(out.get("latest") or "0")
            )
        except InvalidVersion:
            out["update_available"] = False
        return out

    if not force and _cache_is_fresh(cache, now):
        out = {k: cache.get(k) for k in (
            "current", "latest", "update_available", "release_url",
            "download_url", "asset_name", "platform", "checked_at", "error",
            "release_body",
        )}
        out["cached"] = True
        return _overlay_live_current(out)

    try:
        resp = httpx.get(_GITHUB_RELEASES_LATEST_URL, timeout=10, verify=_icu_verify())
        if resp.status_code != 200:
            raise RuntimeError("GitHub returned status " + str(resp.status_code))
        rel = resp.json()
        tag = (rel.get("tag_name") or "").lstrip("v").strip()
        if not tag:
            raise RuntimeError("release missing tag_name")
        try:
            update_available = _vparse(_VERSION) < _vparse(tag)
        except InvalidVersion:
            update_available = False
        download_url, asset_name = _select_platform_asset(rel.get("assets") or [], plat)
        raw_body = rel.get("body")
        if raw_body is None:
            release_body = ""
        elif len(raw_body) > _RELEASE_BODY_MAX_CHARS:
            release_body = raw_body[:_RELEASE_BODY_MAX_CHARS] + _RELEASE_BODY_TRUNCATION_SUFFIX
        else:
            release_body = raw_body
        payload = {
            "current": _VERSION,
            "latest": tag,
            "update_available": bool(update_available),
            "release_url": rel.get("html_url") or ("https://github.com/platypus45/domestique/releases/tag/v" + tag),
            "download_url": download_url,
            "asset_name": asset_name,
            "platform": plat,
            "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cached": False,
            "error": None,
            "release_body": release_body,
        }
        _write_update_check_cache(payload)
        return payload
    except Exception as e:
        if cache:
            out = {k: cache.get(k) for k in (
                "current", "latest", "update_available", "release_url",
                "download_url", "asset_name", "platform", "checked_at",
                "release_body",
            )}
            out["cached"] = True
            out["error"] = str(e)
            return _overlay_live_current(out)
        return {
            "current": _VERSION,
            "latest": None,
            "update_available": False,
            "release_url": None,
            "download_url": None,
            "asset_name": None,
            "platform": plat,
            "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "cached": False,
            "error": str(e),
            "release_body": None,
        }


@app.get("/api/settings")
def api_settings():
    from profile_manager import ProfileManager
    pm = ProfileManager.get()

    # Get eFTP from intervals.icu
    wellness = cached("wellness_7", lambda: fetch_wellness(7))
    eftp = None
    if wellness:
        for w in reversed(wellness):
            si = w.get("sportInfo", [])
            if si and si[0].get("eftp"):
                eftp = round(si[0]["eftp"])
                break

    p_zones = _power_zones(pm.ftp)
    h_zones = _hr_zones(pm.lthr, pm.max_hr, pm=pm)
    _hz_model, _hz_anchor, _hz_why = _hr_zone_model(pm)

    return {
        "weight": config.ATHLETE_WEIGHT_KG,
        "lbm": config.ATHLETE_LBM_KG,
        "ftp": config.ATHLETE_FTP_W,
        "eftp": eftp,
        "lthr": config.ATHLETE_LTHR,
        "max_hr": config.ATHLETE_MAX_HR,
        # hr target_mode (IP_HR_ONLY): lthr_is_set drives the toggle gate —
        # the bare lthr value above is a 170 default when unset.
        "target_mode": pm.target_mode,
        "lthr_is_set": pm.lthr_is_set,
        # HRR zone model (issue #10): what is ACTIVE, what the rider CHOSE,
        # and — when the choice cannot take effect — the plain-language why,
        # so the UI greys the option instead of silently ignoring it.
        "hr_zone_model": _hz_model,
        "hr_zone_model_choice": pm._athlete.get("hr_zone_model") or "lthr",
        "hrr_unavailable_reason": _hz_why,
        "hrr_anchor_bpm": _hz_anchor,
        "hrr_rhr_mode": pm._athlete.get("hrr_rhr_mode") or "rolling",
        "hrr_rhr_window_days": pm._athlete.get("hrr_rhr_window_days") or 7,
        # W2d: LTHR provenance — hr-mode accuracy hangs on this one number.
        "lthr_source": pm._athlete.get("lthr_source"),
        "lthr_source_date": pm._athlete.get("lthr_source_date"),
        # Prescription rows, computed HERE so no client surface ever re-derives
        # bpm with JS rounding (red-team D2/F3). [low, high] bpm. W1: resolved
        # through the same helper the converter uses (custom rows included).
        "hr_rows": (lambda rows: {
            "z1": [0, rows[1][1]], "z2": list(rows[2]),
            "z3": list(rows[3]), "z4": list(rows[4]),
        })(__import__("hr_targets")._resolve_rows_bpm(
            int(pm.lthr), int(pm.max_hr), _prescription_hr_rows(pm))),
        "hr_rows_custom": _prescription_hr_rows(pm) is not None,
        "power_zones": p_zones,
        "hr_zones": h_zones,
        "w_per_kg": round(config.ATHLETE_FTP_W / max(config.ATHLETE_WEIGHT_KG, 1), 2),
        # FIX-CONTRACT C1: expose ftp_source + ftp_source_date so U3 badge,
        # U4 manual-edit confirm, and U5 eFTP auto-banner have the provenance
        # enum they gate on. pre-v4.1 profiles default to "manual" via the
        # ProfileManager property. ftp_source_date is None when the profile
        # has never applied an FTP (fresh migration) — UI treats as
        # "unknown" and falls back to last-modified mtime.
        "ftp_source": pm.get_ftp_source() or "manual",
        "ftp_source_date": pm.get_ftp_source_date(),
        # task #24: measured-capacity short-rep cap. The select is only usable
        # when a trustworthy MEASURED Pmax exists (pmax_is_set); otherwise the
        # UI disables the row and shows a hint. pmax_w is exposed for the modal
        # comparison but MUST NOT be shown as a "ceiling" when unset (it is the
        # ftp*1.30 fallback then) -- pmax_is_set gates that.
        "cap_short_intervals": pm.cap_short_intervals,
        "pmax_is_set": pm.pmax_is_set,
        "pmax_w": int(pm.pmax_w) if pm.pmax_is_set else None,
        "pmax_source": pm.pmax_source,
    }


@app.post("/api/settings")
def update_settings(request_body: dict):
    """Update athlete settings — writes to active profile's athlete.json.

    E2 (v4.1.0): when the FTP change originates from an eFTP accept
    (distinguished by the current eFTP matching the new value within 2W),
    append an ``ftp_test_history`` row with ``source="eftp_icu"`` so the
    ledger captures the change instead of silently overwriting the tested
    FTP. Also stamp ``ftp_source`` on the profile for E1 provenance.
    """
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    updates = {}
    # Map API field names to athlete.json keys and config attribute names
    field_map = {
        "weight": ("weight_kg", "ATHLETE_WEIGHT_KG", float),
        "lbm": ("lbm_kg", "ATHLETE_LBM_KG", float),
        "ftp": ("ftp", "ATHLETE_FTP_W", int),
        "lthr": ("lthr", "ATHLETE_LTHR", int),
        "max_hr": ("max_hr", "ATHLETE_MAX_HR", int),
        # v1.0.6 IMPL-3D-INGEST: manual pmax_w override. The athlete_key
        # "pmax_w" is recognised by save_athlete() which routes through
        # _set_pmax(..., "manual") so later ICU writes can't clobber.
        "pmax": ("pmax_w", "ATHLETE_PMAX_W", int),
    }
    athlete_updates = {}
    # HRR zone model (issue #10): free-form athlete keys, validated here and
    # only here. The model choice persists even while ineligible — the
    # settings payload carries the why, and _hr_zone_model refuses to
    # activate it until the anchors are real.
    if "hr_zone_model" in request_body:
        v = str(request_body["hr_zone_model"] or "lthr")
        if v not in ("lthr", "hrr"):
            raise HTTPException(400, "hr_zone_model must be lthr or hrr")
        athlete_updates["hr_zone_model"] = v
    if "hrr_rhr_mode" in request_body:
        v = str(request_body["hrr_rhr_mode"] or "rolling")
        if v not in ("rolling", "manual"):
            raise HTTPException(400, "hrr_rhr_mode must be rolling or manual")
        athlete_updates["hrr_rhr_mode"] = v
    if "hrr_rhr_window_days" in request_body:
        try:
            w = int(request_body["hrr_rhr_window_days"])
        except (TypeError, ValueError):
            raise HTTPException(400, "hrr_rhr_window_days must be an integer")
        if not 2 <= w <= 60:
            raise HTTPException(400, "hrr_rhr_window_days must be 2-60")
        athlete_updates["hrr_rhr_window_days"] = w
    old_ftp = pm.ftp
    new_ftp = None
    for key, (athlete_key, config_key, cast) in field_map.items():
        if key in request_body:
            try:
                val = cast(request_body[key])
            except (ValueError, TypeError):
                continue
            athlete_updates[athlete_key] = val
            if key == "ftp":
                new_ftp = val
            # v3.6.0-fix28 M-3: allow athlete-only keys (no config mirror).
            if config_key is not None:
                updates[config_key] = val

    # W2d provenance: a settings-form LTHR write is MANUAL — it blocks the ICU
    # mirror (W3) from clobbering a tested value, and the UI shows source+date.
    # AC5f: EXCEPT when the caller passes lthr_source_hint="icu" — the wizards
    # send that ONLY for an ICU-prefilled value the user did not edit, so the
    # provenance honestly reads "icu" (a typed value still stamps manual).
    if "lthr" in athlete_updates:
        from datetime import date as _date
        _hint = str(request_body.get("lthr_source_hint") or "").strip().lower()
        athlete_updates["lthr_source"] = "icu" if _hint == "icu" else "manual"
        athlete_updates["lthr_source_date"] = clock.today().isoformat()

    # Red-team S1/S2: validate lthr/max_hr HERE with the ranges that actually
    # PERSIST. save_athlete accepts max_hr [120,240] but the _set_max_hr write
    # path silently clamps to [140,220] — so a 240 got a 200 OK, never landed,
    # and could leave lthr > max_hr on disk with hr mode silently degraded.
    # Out-of-range now 400s (was an unhandled ValueError → 500).
    # AC5d: bounds come from the ONE range table (SETUP_LIMITS).
    _lthr_lo, _lthr_hi = SETUP_LIMITS["lthr"]
    _mhr_lo, _mhr_hi = SETUP_LIMITS["max_hr"]
    if "lthr" in athlete_updates and not (_lthr_lo <= athlete_updates["lthr"] <= _lthr_hi):
        raise HTTPException(400, f"LTHR {athlete_updates['lthr']} out of range [{_lthr_lo}, {_lthr_hi}]")
    if "max_hr" in athlete_updates and not (_mhr_lo <= athlete_updates["max_hr"] <= _mhr_hi):
        raise HTTPException(400, f"max HR {athlete_updates['max_hr']} out of range [{_mhr_lo}, {_mhr_hi}]")

    # hr target_mode (IP_HR_ONLY C15/C16). Gate on the PROSPECTIVE lthr/max_hr
    # (a same-request lthr write counts), and on lthr being EXPLICITLY set —
    # pm.lthr's bare 170 default must never satisfy the gate.
    if "target_mode" in request_body:
        mode = str(request_body["target_mode"]).strip().lower()
        if mode not in ("power", "hr"):
            raise HTTPException(400, "target_mode must be 'power' or 'hr'")
        if mode == "hr":
            eff_lthr = athlete_updates.get("lthr", pm._athlete.get("lthr"))
            eff_max = athlete_updates.get("max_hr", pm.max_hr)
            if eff_lthr is None:
                raise HTTPException(400, "hr mode needs your LTHR — set it first "
                                          "(20-min field test or intervals.icu estimate)")
            if eff_max <= eff_lthr:
                raise HTTPException(400, f"max HR ({eff_max}) must be above LTHR "
                                          f"({eff_lthr}) for hr mode")
        athlete_updates["target_mode"] = mode
    elif pm._athlete.get("target_mode") == "hr":
        # Red-team S4: an lthr/max_hr edit can break the hr invariant while
        # target_mode isn't in the body. Reads already degrade to power, but
        # self-heal the stored value too so no stale raw "hr" lingers for a
        # future direct-dict reader.
        eff_lthr = athlete_updates.get("lthr", pm._athlete.get("lthr"))
        eff_max = athlete_updates.get("max_hr", pm.max_hr)
        if eff_lthr is None or eff_max <= eff_lthr:
            athlete_updates["target_mode"] = "power"

    # W1: custom HR prescription rows. null resets to Coggan defaults; a dict
    # must be monotone, each low<high, all within [80, prospective max_hr].
    if "hr_rows_custom" in request_body:
        rows_in = request_body["hr_rows_custom"]
        if rows_in is None:
            athlete_updates["hr_prescription_rows_custom"] = None
        else:
            eff_max = athlete_updates.get("max_hr", pm.max_hr)
            try:
                z1h = int(rows_in["z1_high"])
                z2 = [int(rows_in["z2"][0]), int(rows_in["z2"][1])]
                z3 = [int(rows_in["z3"][0]), int(rows_in["z3"][1])]
                z4 = [int(rows_in["z4"][0]), int(rows_in["z4"][1])]
            except (KeyError, TypeError, ValueError, IndexError):
                raise HTTPException(400, "hr_rows_custom must carry z1_high + z2/z3/z4 [low,high] bpm")
            vals = [z1h, *z2, *z3, *z4]
            if any(not (80 <= v <= eff_max) for v in vals):
                raise HTTPException(400, f"HR target rows must be within [80, {eff_max}] bpm")
            if not (z2[0] < z2[1] and z3[0] < z3[1] and z4[0] < z4[1]):
                raise HTTPException(400, "each HR row needs low < high")
            if not (z1h <= z2[1] <= z3[1] <= z4[1]):
                raise HTTPException(400, "HR rows must not descend across zones (Z1 top ≤ Z2 ≤ Z3 ≤ Z4)")
            athlete_updates["hr_prescription_rows_custom"] = {
                "z1_high": z1h, "z2": z2, "z3": z3, "z4": z4}

    # task #24: measured-capacity short-rep cap posture. Enum-validated so a
    # bad value 400s instead of persisting an unknown state. Storing "on"/
    # "prompt" without a trustworthy Pmax is harmless -- the serve gate
    # (_capacity_cap_active) also requires pmax_is_set, so it stays inert; the
    # UI additionally disables the row until a measured Pmax exists.
    if "cap_short_intervals" in request_body:
        cap_mode = str(request_body["cap_short_intervals"]).strip().lower()
        if cap_mode not in ("off", "prompt", "on"):
            raise HTTPException(400, "cap_short_intervals must be 'off', 'prompt', or 'on'")
        athlete_updates["cap_short_intervals"] = cap_mode

    # E2 detect: did the FTP change to exactly-match the currently-cached
    # eFTP? If so this is almost certainly an "Accept eFTP" action.
    ftp_source_for_history = None
    if new_ftp is not None and new_ftp != old_ftp:
        try:
            wellness = cached("wellness_7", lambda: fetch_wellness(7))
            for w in reversed(wellness or []):
                si = w.get("sportInfo", []) or []
                e = si[0].get("eftp") if si else None
                if e and abs(round(e) - new_ftp) <= 2:
                    ftp_source_for_history = "eftp_icu"
                    break
        except Exception:
            pass
        if ftp_source_for_history is None:
            ftp_source_for_history = "manual"

    if athlete_updates:
        try:
            pm.save_athlete(athlete_updates)
        except ValueError as e:
            # Out-of-range athlete input is a client error, not a 500
            # (red-team S2 — e.g. ftp 9999 during the hr-mode onboarding save).
            raise HTTPException(400, str(e))
        # E1 stamp provenance on profile when an FTP write happened.
        if new_ftp is not None and new_ftp != old_ftp:
            try:
                pm._athlete["ftp_source"] = ftp_source_for_history or "manual"
                pm._write_json(pm.active_dir / "athlete.json", pm._athlete)
            except Exception:
                pass
            # E2 ledger row so acceptEftp doesn't silently overwrite.
            try:
                pm.record_ftp_test(
                    method="manual",
                    ftp=new_ftp,
                    source=ftp_source_for_history or "manual",
                    applied=True,
                )
            except Exception:
                pass
        clear_cache()
        # Auto-log metrics to history
        db.log_metrics_from_settings(updates)

    return {"ok": True, "updated": updates, "settings": api_settings()}


def _custom_zones(athlete_key: str):
    """Return the active profile's CUSTOM zones for ``athlete_key``
    ('power_zones_custom' / 'hr_zones_custom') when the rider edited them, else
    None. Shape: list of {zone,name,low,high}. This is the single chokepoint —
    honoring custom zones here means display AND ride time-in-zone analysis use
    them automatically. Stored as None to mean 'reset to auto'."""
    try:
        from profile_manager import ProfileManager
        z = ProfileManager.get()._athlete.get(athlete_key)
        if isinstance(z, list) and z and all(
            isinstance(x, dict) and x.get("low") is not None and x.get("high") is not None
            for x in z
        ):
            return [
                {"zone": x.get("zone") or f"Z{i + 1}", "name": x.get("name") or "",
                 "low": int(x["low"]), "high": int(x["high"])}
                for i, x in enumerate(z)
            ]
    except Exception:
        pass
    return None


def _power_zones(ftp):
    # Custom (rider-edited) zones win; else the Coggan/Allen 7-zone model.
    # Adapts Zone(low, high, name) → dict shape required by clients
    # (dashboard.html / training.html expect zone, name, low, high).
    custom = _custom_zones("power_zones_custom")
    if custom:
        return custom
    return [
        {"zone": f"Z{i + 1}", "name": z.name, "low": z.low, "high": z.high}
        for i, z in enumerate(_zones_mod.power_zones(ftp))
    ]

def _hrr_rhr_anchor(pm) -> "tuple[int | None, str]":
    """(resting bpm, source) for the HRR zone anchor, or (None, reason).

    Two modes, rider-chosen (docs/SCIENCE.md "Heart-rate zones"): a fixed
    manual value (rhr_baseline — the field that already backs the profile's
    RHR display), or a rolling MEDIAN of the synced daily wellness RHR.
    The literature's anchor is morning resting HR — which the wellness RHR
    is — but no published standard exists for an averaging window, so the
    7-day default is practice extrapolation, labeled as such, and editable.

    Eligibility is a SAMPLE-COUNT rule, not a calendar cutoff: >=4 RHR
    samples in the last 14 days. A calendar rule let one stale reading from
    three weeks ago enable the model while the rolling window sat empty.
    """
    a = pm._athlete
    if (a.get("hrr_rhr_mode") or "rolling") == "manual":
        if a.get("rhr_baseline"):
            return int(a["rhr_baseline"]), "manual"
        return None, "manual mode selected but no resting HR set"
    try:
        window = max(2, min(60, int(a.get("hrr_rhr_window_days") or 7)))
    except (TypeError, ValueError):
        window = 7
    try:
        import ride_storage as _rs
        wl = _rs.load_recent_wellness(days=max(window, 14)) or []
    except Exception:
        wl = []
    def _day(w):
        return str(w.get("id") or "")[:10]
    dated = sorted((( _day(w), int(w["restingHR"]) ) for w in wl
                    if w.get("restingHR")), reverse=True)
    recent14 = [v for d, v in dated[:60] if d >= (clock.today() - timedelta(days=14)).isoformat()]
    if len(recent14) < 4:
        return None, "needs at least 4 resting-HR samples in the last 14 days"
    in_window = [v for d, v in dated if d >= (clock.today() - timedelta(days=window)).isoformat()]
    src = in_window if in_window else recent14
    src = sorted(src)
    mid = len(src) // 2
    med = src[mid] if len(src) % 2 else round((src[mid - 1] + src[mid]) / 2)
    return int(med), f"rolling {window}d median"


def _max_hr_is_set(pm) -> bool:
    """True only for a MEASURED/entered max HR. pm.max_hr silently falls
    back to Tanaka 208-0.7xage — fine for display, not for anchoring a zone
    model whose top band is 90-100% of it."""
    raw = pm._athlete.get("max_hr")
    try:
        return raw is not None and 140 <= int(raw) <= 220
    except (TypeError, ValueError):
        return False


def _hr_zone_model(pm) -> "tuple[str, int | None, str]":
    """(active model, hrr anchor bpm | None, reason-if-unavailable)."""
    want = (pm._athlete.get("hr_zone_model") or "lthr")
    if want != "hrr":
        return "lthr", None, ""
    if not _max_hr_is_set(pm):
        return "lthr", None, "requires a measured max HR (no age formula)"
    anchor, why = _hrr_rhr_anchor(pm)
    if anchor is None:
        return "lthr", None, why
    if anchor >= int(pm._athlete["max_hr"]):
        return "lthr", None, "resting HR is not below max HR"
    return "hrr", anchor, ""


def _hr_zones(lthr, max_hr, pm=None):
    # Custom (rider-edited) zones win; else the rider's zone model: the
    # LTHR-anchored hybrid (default), or heart-rate reserve when selected
    # AND eligible — never silently, never by fallback guesswork. HRR here
    # covers the ZONE TABLE and displays; workout prescription stays
    # LTHR-anchored (no validated %FTP->%HRR mapping exists, and Z5+ keeps
    # RPE per the standing contract) — docs/SCIENCE.md carries the verdict.
    custom = _custom_zones("hr_zones_custom")
    if custom:
        return custom
    if pm is not None:
        model, anchor, _why = _hr_zone_model(pm)
        if model == "hrr":
            return [
                {"zone": f"Z{i + 1}", "name": z.name, "low": z.low,
                 "high": z.high}
                for i, z in enumerate(
                    _zones_mod.hrr_zones(anchor, int(pm._athlete["max_hr"])))
            ]
    return [
        {"zone": f"Z{i + 1}", "name": z.name, "low": z.low, "high": z.high}
        for i, z in enumerate(_zones_mod.hr_zones(lthr, max_hr))
    ]


@app.post("/api/settings/zones")
def update_custom_zones(body: dict):
    """Save or reset CUSTOM power/HR zones for the active profile.

    body = {"kind": "power"|"hr", "zones": [{"low":int,"high":int,"name":str?}] | null}
    A null/empty ``zones`` resets to auto (FTP/LTHR-derived). Each band needs
    0 ≤ low < high; honored app-wide via _power_zones / _hr_zones."""
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    kind = (body or {}).get("kind")
    key = {"power": "power_zones_custom", "hr": "hr_zones_custom"}.get(kind)
    if not key:
        return JSONResponse({"error": "kind must be 'power' or 'hr'"}, status_code=400)
    zones = (body or {}).get("zones")
    if not zones:
        pm.save_athlete({key: None})   # reset → auto
        return {"ok": True, "reset": True}
    clean = []
    try:
        for i, z in enumerate(zones):
            lo, hi = int(z["low"]), int(z["high"])
            if lo < 0 or hi <= lo or hi > 3000:
                return JSONResponse(
                    {"error": f"zone {i + 1}: need 0 ≤ low < high ≤ 3000"}, status_code=400)
            clean.append({"zone": z.get("zone") or f"Z{i + 1}",
                          "name": z.get("name") or "", "low": lo, "high": hi})
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "each zone needs integer low/high"}, status_code=400)
    pm.save_athlete({key: clean})
    return {"ok": True, "zones": clean}


# ═══════════════════════════════════════════════════════════════════════════════
# SYNC APIs
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/sync")
def api_sync():
    """Manually trigger a sync from Intervals.icu.

    A manual sync that SUCCEEDS proves the credential is alive, so it clears
    the auth-disable latch and restarts the background loop. The latch is
    one-way otherwise — it returns out of _sync_loop and only a reconnect or an
    app restart resets it — and now that a single 401 sets it, a one-off 401
    from ICU must not cost the rider background sync for the rest of the
    session. run_sync re-raises on failure, so this only runs on success.
    """
    result = db.run_sync(days=90)
    if db._auth_disabled:
        db.restart_sync()
    return result


@app.get("/api/sync/status")
def api_sync_status():
    return db.get_sync_status()


# ═══════════════════════════════════════════════════════════════════════════════
# ATHLETE METRICS APIs
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/metrics/history")
def api_metrics_history(metric: str = Query(...), days: int = Query(365)):
    """Return historical values for a metric (weight, ftp, lbm, vo2max, body_fat, etc.)."""
    if metric == "w_per_kg":
        return db.query_wkg_history(days)
    return db.query_metric_history(metric, days)


@app.get("/api/metrics/latest")
def api_metrics_latest():
    """Return the most recent value for each tracked metric."""
    return db.query_metrics_latest()


@app.post("/api/metrics/log")
async def api_metrics_log(request: Request):
    """Manually log a metric value (VO2max, body_fat, etc.)."""
    body = await _get_json_body(request)
    dt = body.get("date", clock.today().isoformat())
    metric = body.get("metric")
    value = body.get("value")
    if not metric or value is None:
        return JSONResponse({"error": "metric and value required"}, 400)
    try:
        db.log_metric(dt, metric, float(value), body.get("source", "manual"), body.get("notes"))
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, 400)
    return {"ok": True, "date": dt, "metric": metric, "value": float(value)}


# ═══════════════════════════════════════════════════════════════════════════════
# DAILY LOG (Morning Questionnaire) APIs
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/daily-log")
def api_daily_log(days: int = Query(14)):
    """Return today's log entry + recent history."""
    today = db.get_daily_log_today()
    history = db.query_daily_log(days)
    return {"today": today, "history": history}


@app.post("/api/daily-log")
async def api_daily_log_post(request: Request):
    """Submit or update today's morning questionnaire.

    v4.6.6 IMPL-C: validates each Hooper field (sleep_quality, fatigue,
    stress, soreness, mood) is an int 1..7 and rejects with 400 otherwise.
    Returns the full row with computed `hooper_index` (sum of 4 Hooper
    fields excluding mood, range 4..28). Per Hooper & Mackinnon 1995,
    hooper_index ≥ 18 = significant accumulated fatigue (IMPL-B G6 gate).
    """
    body = await _get_json_body(request)
    dt = body.get("date", clock.today().isoformat())
    try:
        entry = db.upsert_daily_log(
            dt=dt,
            sleep_quality=int(body.get("sleep_quality", 4)),
            fatigue=int(body.get("fatigue", 4)),
            soreness=int(body.get("soreness", 4)),
            stress=int(body.get("stress", 4)),
            mood=int(body.get("mood", 4)),
            notes=body.get("notes"),
            # v3.6.0 — optional, 1-10, HIGH = ready. Omitted rather than
            # defaulted: a default would invent a rating the rider never gave,
            # and upsert_daily_log carries a previously-stored value forward.
            readiness_to_train=_parse_rtt(body.get("readiness_to_train")),
        )
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, 400)
    return {"ok": True, "entry": entry}


def _parse_rtt(raw):
    """Parse an optional readiness-to-train rating: an int 1-10, or None.

    A bare ``int(raw)`` truncated instead of rejecting — 5.5 stored as 5 and
    "8" as 8 — so the range was enforced but the type was not. An upgraded
    install has no CHECK constraint on this column (SQLite cannot add one), so
    this is the only place the shape is guarded.
    """
    if raw in (None, ""):
        return None
    if isinstance(raw, bool):
        raise ValueError("readiness_to_train must be a whole number 1-10")
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float):
        if not raw.is_integer():
            raise ValueError("readiness_to_train must be a whole number 1-10")
        return int(raw)
    if isinstance(raw, str) and raw.strip().isdigit():
        return int(raw.strip())
    raise ValueError("readiness_to_train must be a whole number 1-10")


# ═══════════════════════════════════════════════════════════════════════════════
# BLOOD MARKERS API
# ═══════════════════════════════════════════════════════════════════════════════

BLOOD_MARKER_RANGES = {
    "ferritin": {"unit": "ng/mL", "optimal_low": 50, "optimal_high": 150, "flag_low": 30, "flag_high": 300},
    "vitamin_d": {"unit": "ng/mL", "optimal_low": 40, "optimal_high": 60, "flag_low": 30, "flag_high": 100},
    "testosterone": {"unit": "ng/dL", "optimal_low": 500, "optimal_high": 900, "flag_low": 300, "flag_high": 1100},
    "cortisol_am": {"unit": "mcg/dL", "optimal_low": 10, "optimal_high": 20, "flag_low": 5, "flag_high": 25},
    "crp": {"unit": "mg/L", "optimal_low": 0, "optimal_high": 1.0, "flag_low": 0, "flag_high": 3.0},
    "hemoglobin": {"unit": "g/dL", "optimal_low": 14.5, "optimal_high": 17.0, "flag_low": 13.5, "flag_high": 18.0},
    "hematocrit": {"unit": "%", "optimal_low": 42, "optimal_high": 50, "flag_low": 40, "flag_high": 52},
}


@app.get("/api/blood-markers")
def api_blood_markers():
    """Return all blood marker entries with reference ranges."""
    markers = db.query_blood_markers()
    # Add status flags
    for m in markers:
        ref = BLOOD_MARKER_RANGES.get(m["marker"], {})
        if ref:
            m["ref"] = ref
            v = m["value"]
            if ref.get("optimal_low") and ref.get("optimal_high"):
                if ref["optimal_low"] <= v <= ref["optimal_high"]:
                    m["status"] = "optimal"
                elif ref.get("flag_low") and v < ref["flag_low"]:
                    m["status"] = "low"
                elif ref.get("flag_high") and v > ref["flag_high"]:
                    m["status"] = "high"
                else:
                    m["status"] = "ok"
    return {"markers": markers, "ranges": BLOOD_MARKER_RANGES}


@app.post("/api/blood-markers")
async def api_blood_markers_post(request: Request):
    """Add a blood test result."""
    body = await _get_json_body(request)
    dt = body.get("date", clock.today().isoformat())
    marker = body.get("marker")
    value = body.get("value")
    if not marker or value is None:
        return JSONResponse({"error": "marker and value required"}, 400)
    ref = BLOOD_MARKER_RANGES.get(marker, {})
    unit = ref.get("unit") or body.get("unit")
    db.upsert_blood_marker(dt, marker, float(value), unit, body.get("notes"))
    return {"ok": True}


# ═══════════════════════════════════════════════════════════════════════════════
# POWER CURVE API
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/power-curve")
def api_power_curve(days: int = Query(90), compare_days: int = Query(365)):
    """Return power duration curve from activity data."""
    durations = ["5s", "1m", "5m", "20m", "60m"]
    icu_fields = {
        "5s": "icu_w5s", "1m": "icu_w1m", "5m": "icu_w5m",
        "20m": "icu_w20m", "60m": "icu_w60m",
    }

    activities = db.query_activities(days=max(days, compare_days))
    today = clock.today()
    cutoff_current = (today - timedelta(days=days)).isoformat()
    cutoff_compare = (today - timedelta(days=compare_days)).isoformat()

    current = {d: {"watts": 0, "date": ""} for d in durations}
    historical = {d: {"watts": 0, "date": ""} for d in durations}

    for a in activities:
        raw = a.get("raw_json")
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue

        for dur_label, field in icu_fields.items():
            val = data.get(field)
            if val and val > 0:
                act_date = a.get("date", "")
                # Historical best
                if act_date >= cutoff_compare and val > historical[dur_label]["watts"]:
                    historical[dur_label] = {"watts": round(val), "date": act_date}
                # Current period best
                if act_date >= cutoff_current and val > current[dur_label]["watts"]:
                    current[dur_label] = {"watts": round(val), "date": act_date}

    weight = config.ATHLETE_WEIGHT_KG or 72
    result_current = []
    result_historical = []
    for d in durations:
        c = current[d]
        h = historical[d]
        result_current.append({"duration": d, "watts": c["watts"], "wkg": round(c["watts"] / weight, 2), "date": c["date"]})
        result_historical.append({"duration": d, "watts": h["watts"], "wkg": round(h["watts"] / weight, 2), "date": h["date"]})

    return {"current": result_current, "historical": result_historical, "durations": durations, "ftp": config.ATHLETE_FTP_W}


# ═══════════════════════════════════════════════════════════════════════════════
# GPX API
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/gpx/{region}/{filename}")
def api_gpx_data(region: str, filename: str):
    """Parse GPX file and return trackpoints for map + elevation profile."""
    # Try matching GPX file - the filename may have .crs extension, swap to .gpx
    # Validate region doesn't contain path traversal
    if ".." in region or "/" in region or ".." in filename or "/" in filename:
        return JSONResponse({"error": "invalid path"}, 400)
    gpx_name = filename.rsplit(".", 1)[0] + ".gpx" if "." in filename else filename + ".gpx"
    path = _safe_path(active_gpx_dir(), region, gpx_name)
    if not path or not path.exists():
        path = _safe_path(active_gpx_dir(), region, filename)
    if (not path or not path.exists()) and (active_gpx_dir() / region).is_dir():
        # Try fuzzy match — find GPX with same stem prefix
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        for gpx_file in (active_gpx_dir() / region).glob("*.gpx"):
            if gpx_file.stem.lower().startswith(stem[:20].lower()):
                path = gpx_file
                break
    if not path or not path.exists():
        return JSONResponse({"error": "GPX not found", "tried": gpx_name}, 404)

    import math
    ns = {"gpx": "http://www.topografix.com/GPX/1/1"}
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return JSONResponse({"error": "GPX parse error"}, 400)

    root = tree.getroot()
    points = []
    for trkpt in root.findall(".//gpx:trkpt", ns):
        lat = float(trkpt.get("lat", 0))
        lon = float(trkpt.get("lon", 0))
        ele_el = trkpt.find("gpx:ele", ns)
        ele = float(ele_el.text) if ele_el is not None else 0
        points.append({"lat": lat, "lon": lon, "ele": round(ele, 1)})

    if len(points) < 2:
        return JSONResponse({"error": "Not enough trackpoints"}, 400)

    # Compute cumulative distance (km) using the canonical haversine.
    from geodesy import haversine as _hv
    cum_dist = [0.0]
    for i in range(1, len(points)):
        d_km = _hv(
            (points[i - 1]["lat"], points[i - 1]["lon"]),
            (points[i]["lat"], points[i]["lon"]),
        ) / 1000.0
        cum_dist.append(cum_dist[-1] + d_km)

    # Compute gradient per segment — cap to ±45% (steepest paved road ~35%)
    for i in range(len(points)):
        points[i]["d"] = round(cum_dist[i], 3)
        if i > 0 and cum_dist[i] - cum_dist[i - 1] > 0.005:  # min 5m to avoid GPS noise
            ele_diff = points[i]["ele"] - points[i - 1]["ele"]
            dist_m = (cum_dist[i] - cum_dist[i - 1]) * 1000
            grad = ele_diff / dist_m * 100
            points[i]["g"] = round(max(-45, min(45, grad)), 1)  # clamp to realistic range
        else:
            points[i]["g"] = 0

    # Downsample for transfer (max 500 points)
    step = max(1, len(points) // 500)
    sampled = [points[i] for i in range(0, len(points), step)]
    if sampled[-1] != points[-1]:
        sampled.append(points[-1])

    total_climb = sum(max(0, points[i]["ele"] - points[i - 1]["ele"]) for i in range(1, len(points)))
    max_grad = max((p["g"] for p in points), default=0)

    return {
        "points": sampled,
        "total_points": len(points),
        "total_km": round(cum_dist[-1], 1),
        "total_climb": round(total_climb),
        "max_gradient": round(max_grad, 1),
        "name": path.stem,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# WEEKLY MESOCYCLE API
# ═══════════════════════════════════════════════════════════════════════════════

def _read_stored_plan() -> "dict | None":
    """current_plan.json, or None when there is none or it does not parse."""
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return None
    try:
        with open(json_path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _stored_week_view(plan: "dict | None", week_offset: int = 0):
    """The ISO week ``week_offset`` weeks from today's, as ``plan`` stores it:
    the one week_view.WeekView every card reads."""
    import week_view as _wv

    start, _end = _wv.iso_week(clock.today(), week_offset)
    try:
        lib_by_file = _wv.library_by_file(tp.load_workout_library())
    except Exception:  # noqa: BLE001
        lib_by_file = {}
    try:
        classifications = tp._load_content_classifications() or {}
    except Exception:  # noqa: BLE001
        classifications = {}
    return _wv.build(
        plan, start, lib_by_file, tp._session_type_from_row,
        naming=lambda zf: _session_naming_lookup(zf, classifications, lib_by_file),
        offset=week_offset)


@app.get("/api/weekly-plan")
def api_weekly_plan(week_offset: int = Query(0)):
    """The stored week, as one view (src/week_view.py). week_offset=0 is the
    current ISO week, -1 last week, 1 next week.

    Until 2026-09-14 this ran a second planner on every GET and served its
    regenerated target as the home badge and the rollup; the stored week's
    sessions were overlaid by date and days before the plan were filled with
    sessions nobody was given (notes/review/http.md HTTP-2). It reads the
    plan on disk now, and a week with no plan on record says so:
    tss_target None, every day a placeholder with on_record False.
    """
    training = cached("training", get_today_metrics)
    current_ctl = _planning_ctl()
    plan = _read_stored_plan()
    result = _stored_week_view(plan, week_offset).as_weekly_plan()
    try:
        if plan:
            g = plan.get("goal", {})
            plan_goal = tp.goal_from_dict(g)
            # Readiness needs an event date that parses; a corrupt one used to
            # raise here and show none, and must not now show 100%.
            if plan_goal.target_date:
                # v4.6.7 IMPL-CAP: auto-populate endurance baseline if missing.
                if plan_goal.longest_ride_h_90d is None:
                    plan_goal.longest_ride_h_90d = _longest_ride_h_90d()
                result["event_readiness"] = tp.compute_event_readiness(plan_goal, current_ctl)

        # eFTP drift detection
        wellness = cached("wellness_7", lambda: fetch_wellness(7))
        if wellness:
            for w in reversed(wellness):
                si = w.get("sportInfo", [])
                if si and len(si) > 0 and si[0].get("eftp"):
                    eftp = round(si[0]["eftp"])
                    drift = tp._check_eftp_drift(eftp)
                    if drift:
                        result["eftp_drift"] = drift
                    break
        # F5 (v4.1.0): the 7+-day up-drift auto-apply writes the profile, so
        # it runs after the background sync (_after_background_sync), not on
        # this read (week-view contract P8). Drift is still detected above.
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning("event_readiness failed: %s", _e)

    return result


# ── 3.3.1 hotfix (B5) — automatic-FTP plausibility guard ────────────────────
# Incident: a tester's profile carried ftp=122 while his real FTP ≈258, so
# every zone chip and watt label scaled off 122 ("Z2 68–92 W"). 122 matched
# none of his recent eFTP series (195–263) — the plausible landing paths are
# the sport-UNFILTERED sportInfo[0].eftp prefill/copy chains or an accepted
# early-halted ramp test. Whatever the entry door, AUTOMATIC ingestion must
# never swallow a wildly-implausible number silently.

def _ftp_auto_ingest_ok(new_ftp, current_ftp) -> bool:
    """True when an AUTOMATICALLY-ingested FTP candidate is plausible.

    Reject when the candidate is:
      - < 100 W absolute (below any plausible adult training FTP — catches
        unit bugs and wrong-sport eFTP rows), or
      - < 60% of a real (>0) current FTP (a genuine fitness loss that size
        doesn't happen between syncs; a wrong-sport eftp does).
    MANUAL entry (settings form, accepted test results, accept-eFTP buttons)
    is deliberately NOT gated — the rider may always assert their own
    number. Callers log + keep the current value on rejection."""
    try:
        v = int(new_ftp)
    except (TypeError, ValueError):
        return False
    if v < 100:
        return False
    try:
        cur = int(current_ftp or 0)
    except (TypeError, ValueError):
        cur = 0
    if cur > 0 and v < cur * 0.60:
        return False
    return True


def _guarded_check_and_auto_apply_eftp(wellness_series):
    """Plausibility gate in FRONT of the F5 auto-apply (tp owns the engine).

    tp.check_and_auto_apply_eftp applies the NEWEST wellness record's
    sportInfo[0].eftp after a 7-day sustained up-drift. Pre-validate that
    exact candidate here (same newest-record read the engine does) so an
    implausible value is rejected + logged WITHOUT touching the profile —
    training_planner stays unchanged (parallel-wave file ownership).
    Fail-open on shape surprises: the engine has its own streak gating."""
    try:
        cand = None
        recs = sorted(wellness_series or [], key=lambda r: r.get("id", ""))
        if recs:
            si = recs[-1].get("sportInfo") or []
            ef = si[0].get("eftp") if si and isinstance(si[0], dict) else None
            if ef:
                cand = int(round(float(ef)))
        cur = int(getattr(config, "ATHLETE_FTP_W", 0) or 0)
        if cand is not None and not _ftp_auto_ingest_ok(cand, cur):
            _log.warning(
                "EVENT=ftp_auto_ingest_rejected source=eftp_auto "
                "candidate=%s current=%s — implausible automatic FTP update "
                "rejected; keeping current (set FTP in Settings if real)",
                cand, cur)
            return None
    except Exception:
        pass  # guard is best-effort; never block the engine on shape quirks
    return tp.check_and_auto_apply_eftp(wellness_series)


# ═══════════════════════════════════════════════════════════════════════════════
# WEEK SUMMARY API (for dashboard week tile)
# ═══════════════════════════════════════════════════════════════════════════════

def _classify_exposure(activity: dict, lthr: float) -> str:
    """Classify an activity's aerobic band from HR/LTHR or TRIMP-per-minute.

    Preferred signal: average HR relative to LTHR.
    Fallback: TRIMP per minute (rough intensity proxy).
    If neither is available, defaults to 'low_aerobic' (safest bucket — assumes
    the activity was gentle rather than silently dropping it from exposure).
    """
    avg_hr = activity.get("avg_hr")
    try:
        avg_hr_val = float(avg_hr) if avg_hr is not None else None
    except (TypeError, ValueError):
        avg_hr_val = None

    try:
        lthr_val = float(lthr) if lthr is not None else 0.0
    except (TypeError, ValueError):
        lthr_val = 0.0

    if avg_hr_val is not None and lthr_val > 0:
        ratio = avg_hr_val / lthr_val
        if ratio < 0.75:
            return "low_aerobic"
        if ratio < 0.88:
            return "mid_aerobic"
        if ratio < 0.96:
            return "high_aerobic"
        return "anaerobic"

    # Fallback: TRIMP-per-minute
    trimp = activity.get("trimp")
    dur_min = activity.get("duration_min") or 0
    try:
        trimp_val = float(trimp) if trimp is not None else None
    except (TypeError, ValueError):
        trimp_val = None

    if trimp_val is not None and dur_min and dur_min > 0:
        tpm = trimp_val / dur_min
        if tpm < 0.6:
            return "low_aerobic"
        if tpm < 1.2:
            return "mid_aerobic"
        if tpm < 2.0:
            return "high_aerobic"
        return "anaerobic"

    # No usable signal — park in low_aerobic bucket (gotcha: see report).
    return "low_aerobic"


# v3.5.3 — full ICU cycling taxonomy. The old 3-entry set predated sport
# being persisted on ICU envelopes; once envelopes carry real types, the
# offline /api/activities fallback feeds typed GravelRide/MountainBikeRide
# records here, and a genuine ride must never fail the planned-day match.
_CYCLING_SPORTS = frozenset({
    "Ride", "VirtualRide", "EBikeRide", "GravelRide", "MountainBikeRide",
    "EMountainBikeRide", "Handcycle", "Velomobile",
})


def _matches_planned(activities: list, session_type: str) -> bool:
    """A planned cycling workout is DONE only if a cycling activity was
    actually performed that day. Cross-sport doesn't count — the user still
    owes the specific session.
    """
    return any((a.get("sport") or "") in _CYCLING_SPORTS for a in activities)


def _exposure_split_from_tiz(tiz: dict | None) -> dict[str, float] | None:
    """Split one ride's minutes across the exposure bands using its MEASURED
    time-in-zone, rather than filing the whole ride under one average.

    Coggan power zones land on the four bands exactly as week_view.TYPE_BAND
    maps session types, which is what makes the planned and actual bars
    comparable at all:

        Z1 + Z2   -> low_aerobic    (recovery, endurance)   <- z2 / long_z2
        Z3        -> mid_aerobic    (tempo)                 <- tempo
        Z4        -> high_aerobic   (sweet spot, threshold) <- sweetspot / threshold
        Z5 .. Z7  -> anaerobic      (VO2max and above)      <- vo2max / overunder

    ``ss`` is deliberately not summed: sweet spot overlaps Z3/Z4 and counting
    it would inflate the total beyond the ride's own duration.

    Returns None when the ride carries no zone data, so the caller falls back
    to the whole-ride average rather than silently reporting zero minutes.
    """
    if not isinstance(tiz, dict):
        return None

    def _s(*keys: str) -> float:
        total = 0.0
        for k in keys:
            try:
                total += float(tiz.get(k) or 0)
            except (TypeError, ValueError):
                continue
        return total

    if _s("z1", "z2", "z3", "z4", "z5", "z6", "z7") <= 0:
        return None
    return {
        "low_aerobic": _s("z1", "z2") / 60.0,
        "mid_aerobic": _s("z3") / 60.0,
        "high_aerobic": _s("z4") / 60.0,
        "anaerobic": _s("z5", "z6", "z7") / 60.0,
    }


def _classify_exposure_with_signal(activity: dict, lthr: float) -> tuple[str, str]:
    """Like _classify_exposure but also returns the signal source.

    Returns (band, signal) where signal is one of:
      - "hr"      : classified from avg_hr / lthr
      - "trimp"   : classified from TRIMP-per-minute fallback
      - "inferred": neither signal available, defaulted to low_aerobic
                    (caller should exclude from exposure_minutes totals
                    to avoid polluting the band math)
    """
    avg_hr = activity.get("avg_hr")
    try:
        avg_hr_val = float(avg_hr) if avg_hr is not None else None
    except (TypeError, ValueError):
        avg_hr_val = None
    try:
        lthr_val = float(lthr) if lthr is not None else 0.0
    except (TypeError, ValueError):
        lthr_val = 0.0

    if avg_hr_val is not None and lthr_val > 0:
        band = _classify_exposure(activity, lthr)
        return (band, "hr")

    trimp = activity.get("trimp")
    dur_min = activity.get("duration_min") or 0
    try:
        trimp_val = float(trimp) if trimp is not None else None
    except (TypeError, ValueError):
        trimp_val = None
    if trimp_val is not None and dur_min and dur_min > 0:
        band = _classify_exposure(activity, lthr)
        return (band, "trimp")

    return ("low_aerobic", "inferred")


@app.get("/api/week-summary")
def api_week_summary(week_offset: int = Query(0)):
    """ISO week (Mon→Sun) training summary for the dashboard week tile.

    Reuses /api/activities + /api/weekly-plan + /api/settings.

    v1.7.6: ``?week_offset=-1`` requests the PREVIOUS completed week so
    the homepage can paint a "last week feedback" panel — planned vs
    actual TSS + zone-time bands + an overreach flag the dashboard uses
    to nudge the rider toward the readiness tier-down apply button when
    they've spent the prior week well above plan.
    """
    # Pin `today` to the athlete's configured timezone so "which week is
    # this?" doesn't flip around midnight when the server's local clock and
    # the user's clock disagree. Fall back to the process TZ if settings
    # don't provide one (the UI setup has no tz field today).
    try:
        from zoneinfo import ZoneInfo
        settings = api_settings()
        tz_name = settings.get("timezone") if isinstance(settings, dict) else None
        tz = ZoneInfo(tz_name) if tz_name else clock.now().astimezone().tzinfo
    except Exception:
        tz = clock.now().astimezone().tzinfo
    today = clock.now(tz).date()
    iso_year, iso_week, iso_weekday = today.isocalendar()
    # v1.7.6 — shift the target Monday by week_offset weeks so negative
    # offsets surface prior weeks. week_offset=0 keeps the legacy
    # behaviour (current ISO week).
    base_monday = today - timedelta(days=iso_weekday - 1)
    week_start = base_monday + timedelta(weeks=week_offset)
    week_end = week_start + timedelta(days=6)
    week_start_str = week_start.isoformat()
    week_end_str = week_end.isoformat()
    today_str = today.isoformat()

    # Reuse existing helpers — do NOT re-implement.
    activities_all = api_activities()
    plan = api_weekly_plan(week_offset=week_offset)
    settings = api_settings()
    lthr = settings.get("lthr") or 175

    # Filter activities to the current ISO week window.
    week_activities = []
    for a in activities_all or []:
        d = (a.get("date") or "")[:10]
        if week_start_str <= d <= week_end_str:
            week_activities.append(a)

    # Preserve the None-vs-0 distinction: `tss_target == 0` is a legitimate
    # rest-week plan, not a missing target. Falsy-check would collapse both.
    tss_target = plan.get("tss_target")
    tss_done = round(sum((a.get("tss") or 0) for a in week_activities), 1)
    if tss_target is not None and tss_target > 0:
        # WEEK2: cap overshoot at 150% so the rollup bar doesn't stretch
        # indefinitely and numbers like "312%" don't dominate the UI.
        tss_adherence_pct = round(min(150.0, tss_done / tss_target * 100))
    elif tss_target == 0:
        # WEEK2: rest-week grading. Zero load → perfect 100%. Light load
        # (≤50 TSS of accidental activity) scales down linearly; anything
        # beyond 50 TSS of "rest" is a 0% violation. Previously flipped to
        # 0% at the first joule, making every recovery ride catastrophic.
        if tss_done <= 0:
            tss_adherence_pct = 100
        else:
            tss_adherence_pct = max(
                0,
                round(100.0 - min(100.0, (tss_done / 50.0) * 100.0)),
            )
    else:
        tss_adherence_pct = None      # no plan on record: nothing to adhere to

    duration_min_done = int(round(sum((a.get("duration_min") or 0) for a in week_activities)))

    sessions = plan.get("sessions", [])
    duration_min_planned = int(round(sum(
        (s.get("duration_min") or 0) for s in sessions
    )))

    # Exposure distribution (sport-agnostic). Activities with neither HR nor
    # TRIMP signal go into a separate unclassified_minutes bucket so they
    # don't silently inflate low_aerobic (QA-EDGE #2).
    exposure_minutes = {
        "low_aerobic": 0, "mid_aerobic": 0, "high_aerobic": 0, "anaerobic": 0,
    }
    unclassified_minutes = 0
    activities_by_day: dict[str, list[dict]] = {}
    sports_counter: dict[str, int] = {}

    # Measured time-in-zone beats a whole-ride average, and Domestique already
    # records it per ride (the calendar renders it as Z1Z2/Z3Z4/Z5+). Filing a
    # three-hour endurance ride wholly under the band of its average HR put
    # every minute of a Z2 week into mid/high and reported low_aerobic as zero.
    # _load_all_rides_safe is the shared 5-minute cache, so this is one parse.
    _tiz_by_id: dict[str, dict] = {}
    try:
        for _r in _load_all_rides_safe() or []:
            _tiz = _r.get("time_in_zone")
            if not _tiz:
                continue
            _rid = str(_r.get("ride_id") or "")
            if _rid.startswith("icu_"):
                _rid = _rid[4:]
            if _rid:
                _tiz_by_id[_rid] = _tiz
    except Exception:  # noqa: BLE001 - never break the rollup over the archive
        _tiz_by_id = {}

    for a in week_activities:
        band, signal = _classify_exposure_with_signal(a, lthr)
        dur = int(round(a.get("duration_min") or 0))
        split = _exposure_split_from_tiz(_tiz_by_id.get(str(a.get("id") or "")))
        if split:
            for _b, _m in split.items():
                exposure_minutes[_b] = exposure_minutes.get(_b, 0) + _m
            # Label the day-list row with where the ride actually spent most of
            # its time, so the list and the bars cannot tell different stories.
            band = max(split, key=lambda k: split[k])
            signal = "time_in_zone"
        elif signal == "inferred":
            unclassified_minutes += dur
        else:
            exposure_minutes[band] = exposure_minutes.get(band, 0) + dur

        d = (a.get("date") or "")[:10]
        row = {
            "sport": a.get("sport") or "",
            "tss": a.get("tss"),
            "duration_min": dur,
            "avg_hr": a.get("avg_hr"),
            "name": a.get("name") or "",
            "exposure": band,
            "exposure_signal": signal,
        }
        activities_by_day.setdefault(d, []).append(row)
        sport_key = a.get("sport") or "Other"
        sports_counter[sport_key] = sports_counter.get(sport_key, 0) + 1

    exposure_minutes = {k: int(round(v)) for k, v in exposure_minutes.items()}
    total_exposure = sum(exposure_minutes.values())
    exposure_dominant = "mixed"
    if total_exposure > 0:
        for band, mins in exposure_minutes.items():
            if mins / total_exposure >= 0.60:
                exposure_dominant = band
                break

    # Planned-day accounting — ISO week is always 7 days regardless of plan
    # contents (a sparse plan with only 3 workouts still spans Mon-Sun).
    # QA-MATH #5: hard-code 7 instead of len(sessions) which was fragile.
    planned_days_total = 7
    planned_days_with_session = sum(
        1 for s in sessions if (s.get("session_type") or "").lower() != "rest"
    )

    # For each STRICTLY past planned day, bucket into done vs missed.
    # QA #1 (blocker): was `day_d > today` (inclusive today) — today's session
    # showed up as "missed" from 00:00 onward even though it might still happen
    # later in the day. Now strict past only, matching the frontend fallback.
    planned_days_done = 0
    planned_days_missed = 0
    missed_sessions: list[dict] = []

    # Pre-index activities by ISO date for O(1) lookup. We need the full
    # per-day activity list (not just a set of dates) so we can ask
    # "was this planned cycling session satisfied by an actual cycling
    # activity?" — a hike on a planned SS day no longer counts as done.
    acts_on_day: dict[str, list[dict]] = {}
    for a in week_activities:
        d = (a.get("date") or "")[:10]
        if d:
            acts_on_day.setdefault(d, []).append(a)

    for s in sessions:
        day_str = s.get("day")
        if not day_str:
            continue
        try:
            day_d = date.fromisoformat(day_str)
        except (ValueError, TypeError):
            continue
        # Future days are neither done nor missed yet. Today is allowed
        # through: it can be DONE (the ride is already on file) but never
        # MISSED (the day is not over). Excluding today outright — the
        # original fix for QA #1 above — also made it impossible to ever
        # count, so a session ridden this morning read "0 of N done" beside
        # a TSS bar that had already banked its load.
        if day_d > today:
            continue
        session_type = (s.get("session_type") or "").lower()
        day_acts = acts_on_day.get(day_str, [])
        if session_type == "rest":
            # Rest days don't count for done/missed regardless of activity.
            if day_acts:
                # User trained on a rest day — not "done" against plan,
                # not "missed" either; just leave it out of the tally.
                pass
            continue
        # A cycling-planned day needs a bike activity. Cross-sport doesn't satisfy.
        if _matches_planned(day_acts, session_type):
            planned_days_done += 1
        elif day_d < today:
            # Strictly past only: an unridden session TODAY is still pending,
            # not missed. This is what QA #1 was about.
            planned_days_missed += 1
            missed_sessions.append({
                "day": day_str,
                "day_name": s.get("day_name") or "",
                "session_type": s.get("session_type") or "",
                "tss_estimate": s.get("tss_estimate") or 0,
                "duration_min": s.get("duration_min") or 0,
                "name": s.get("zwo_name") or s.get("description") or s.get("session_type") or "",
                # Hint to the UI: the day wasn't empty, the user just did
                # something other than the bike. Frontend uses this to render
                # "plan: SS missed" subtitle on top of the actual badge.
                "did_non_cycling": bool(day_acts),
            })

    # Planned exposure-minutes, from the week view: each served file's zone
    # shares, the session's type only when no file is served. The same
    # numbers /api/weekly-plan serves (week-view contract A6).
    exposure_minutes_planned = dict(plan.get("exposure_minutes_planned") or {
        "low_aerobic": 0, "mid_aerobic": 0, "high_aerobic": 0, "anaerobic": 0,
    })

    # v1.7.6 — overreach flag for the "last week feedback" UI. Triggers
    # when actual TSS exceeds plan by >30 %, OR when high-zone minutes
    # (high_aerobic + anaerobic) exceed planned by >50 %. Either signals
    # that the user ran harder than the plan asked for, which is the
    # context behind the readiness composite's tier-down recommendation.
    high_zone_done = (exposure_minutes.get("high_aerobic", 0)
                      + exposure_minutes.get("anaerobic", 0))
    high_zone_planned = (exposure_minutes_planned.get("high_aerobic", 0)
                         + exposure_minutes_planned.get("anaerobic", 0))
    overreach_flag = False
    overreach_reasons: list[str] = []
    if tss_target is not None and tss_target > 0:
        if tss_done / max(tss_target, 1) > 1.30:
            overreach_flag = True
            overreach_reasons.append(
                f"TSS {tss_done:.0f} vs target {tss_target:.0f} "
                f"({round(tss_done / tss_target * 100)}%)"
            )
    if tss_target is None:
        pass    # no plan on record: riding harder than no plan is not overreach
    elif high_zone_planned == 0 and high_zone_done >= 30:
        overreach_flag = True
        overreach_reasons.append(
            f"{high_zone_done} min above Z2 with 0 min planned"
        )
    elif high_zone_planned > 0 and high_zone_done > high_zone_planned * 1.5:
        overreach_flag = True
        overreach_reasons.append(
            f"high-intensity {high_zone_done} vs planned {high_zone_planned} min "
            f"({round(high_zone_done / high_zone_planned * 100)}%)"
        )

    return {
        "week_start": week_start_str,
        "week_end": week_end_str,
        "today": today_str,
        "tss_target": tss_target,
        "tss_done": tss_done,
        "tss_adherence_pct": tss_adherence_pct,
        "duration_min_done": duration_min_done,
        "duration_min_planned": duration_min_planned,
        "exposure_minutes": exposure_minutes,
        "exposure_minutes_planned": exposure_minutes_planned,
        "unclassified_minutes": unclassified_minutes,
        "exposure_dominant": exposure_dominant,
        "sports": sports_counter,
        "missed_sessions": missed_sessions,
        "planned_days_total": planned_days_total,
        "planned_days_with_session": planned_days_with_session,
        "planned_days_done": planned_days_done,
        "planned_days_missed": planned_days_missed,
        "activities_by_day": activities_by_day,
        "overreach": overreach_flag,
        "overreach_reasons": overreach_reasons,
        "week_offset": week_offset,
    }


@app.post("/api/today-session/persist")
async def api_today_session_persist(request: Request):
    """F6 (v4.1.0) — persist today's adapted session back to the plan JSON.

    Caller passes ``{reason, session_type, duration_min, tss_estimate,
    description}`` — typically the output of /api/today-session's `adjusted`
    block when the user clicks "Accept". Writes back under plan_write_lock
    with an explicit ``adapted_reason`` field so it's clear why the
    prescription was modified (threshold → Z2 because readiness 45, etc.).
    """
    body = await _get_json_body(request)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)

    try:
        reason = str(body.get("reason") or "adapted")
        new_type = str(body.get("session_type") or "").strip()
        if not new_type:
            return JSONResponse({"error": "session_type required"}, 400)

        today_iso = clock.today().isoformat()
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        found = False
        for w in plan.get("weeks", []):
            for s in w.get("sessions", []):
                if s.get("day") == today_iso:
                    # The original prescription used to be OVERWRITTEN here,
                    # which made "Ride the original anyway" unable to keep its
                    # promise: the revert flag suppressed the live adjustment,
                    # but every view reading the plan still showed the
                    # persisted replacement — clicking looked like it did
                    # nothing. Stash the pre-adapt session once (never
                    # re-stash on a second adapt of the same day, or the true
                    # original would be lost) so revert-cap can restore it.
                    if not s.get("pre_adapt"):
                        s["pre_adapt"] = {
                            "session_type": s.get("session_type"),
                            "duration_min": s.get("duration_min"),
                            "tss_estimate": s.get("tss_estimate"),
                            "description": s.get("description"),
                            "zwo_file": s.get("zwo_file"),
                            "zwo_name": s.get("zwo_name"),
                        }
                    s["session_type"] = new_type
                    if body.get("duration_min") is not None:
                        s["duration_min"] = int(body["duration_min"])
                    if body.get("tss_estimate") is not None:
                        s["tss_estimate"] = float(body["tss_estimate"])
                    if body.get("description"):
                        s["description"] = str(body["description"])
                    s["adapted"] = True
                    s["adapted_reason"] = reason
                    # Clear ZWO so the UI / next match-pass re-picks for the new type.
                    s["zwo_file"] = ""
                    s["zwo_name"] = ""
                    found = True
                    break
            if found:
                break
        if not found:
            return JSONResponse({"error": f"No session at {today_iso}"}, 404)

        tp.atomic_write_plan(json_path, plan)

        return {"ok": True, "day": today_iso, "adapted_reason": reason,
                "session_type": new_type}
    except Exception:
        _log.exception("today-session persist failed")
        return JSONResponse({"detail": "Persist failed"}, 500)


# ── P2.3 (v3.0.0, G11) — retest nudges: FTP / LTHR staleness ────────────────
# Server-side rule; surfaced as ONE dismissible banner on the today card via
# the /api/today-session `retest_nudge` flag. Snooze persists in athlete.json
# (survives webview storage clears). Thresholds test-locked
# (tests/test_retest_nudges.py).
RETEST_STALE_DAYS_TESTED = 56     # manual / real-test provenance
RETEST_STALE_DAYS_ESTIMATE = 28   # icu_estimate / unknown provenance
RETEST_PROFILE_GRACE_DAYS = 28    # no nudge in the first 28d of a profile
RETEST_SNOOZE_DAYS = 14

# Provenance values that are ESTIMATES (28d rule); anything else — manual /
# tested_* enums — counts as a real test (56d rule).
_RETEST_ESTIMATE_SOURCES = frozenset({
    "", "unknown", "icu_estimate", "icu", "eftp_icu", "eftp_auto", "eftp_local",
})


def _retest_staleness(source, source_date, profile_created, today: date):
    """Pure staleness rule for one metric. Returns (stale, days_since, reason).

    * ``source_date`` None → NO nudge before profile created+28d (grace);
      after that the unknown-source rule applies from creation → stale
      ("never_tested"). A missing creation date (legacy profile) counts as
      past the grace window.
    * Dated sources: stale at 56d (manual/tested) or 28d (estimate sources).
    """
    if not source_date:
        if profile_created:
            try:
                created = date.fromisoformat(str(profile_created)[:10])
                if (today - created).days < RETEST_PROFILE_GRACE_DAYS:
                    return False, None, "profile_grace"
            except ValueError:
                pass
        return True, None, "never_tested"
    try:
        sd = date.fromisoformat(str(source_date)[:10])
    except ValueError:
        return False, None, "bad_date"
    days = (today - sd).days
    src = str(source or "").strip().lower()
    limit = (RETEST_STALE_DAYS_ESTIMATE if src in _RETEST_ESTIMATE_SOURCES
             else RETEST_STALE_DAYS_TESTED)
    return days >= limit, days, ("stale" if days >= limit else "fresh")


def _profile_created_iso(pm) -> "str | None":
    """ISO date the active profile was created (registry stamp), or None."""
    try:
        active = getattr(pm, "active_id", None)
        for p in (pm.list_profiles() or []):
            if p.get("id") == active and p.get("created"):
                return str(p["created"])[:10]
    except Exception:
        pass
    return None


def _next_ftp_test_slot() -> "str | None":
    """First suitable day in the NEXT plan week to host an ftp_test insert.

    "Next appropriate week" = the earliest eligible week starting from next
    Monday. Within it, prefer replacing a HARD slot (a test IS a hard
    session, so the week's structure is preserved); skip rest days, races,
    user-pinned days and anything already done/missed. None when no plan or
    no eligible slot (the banner then offers snooze/instructions only).
    """
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return None
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    today = clock.today()
    next_monday = today + timedelta(days=7 - today.weekday())
    hard_types = {"vo2max", "threshold", "overunder", "sweetspot", "tempo"}
    candidates: list[tuple[date, bool]] = []
    for w in plan.get("weeks", []) or []:
        for s in w.get("sessions", []) or []:
            try:
                d = date.fromisoformat(str(s.get("day") or "")[:10])
            except ValueError:
                continue
            stype = (s.get("session_type") or "").lower()
            if d < next_monday or stype in ("rest", "ftp_test"):
                continue
            if s.get("is_race") or s.get("user_swapped") or s.get("user_moved"):
                continue
            if (s.get("status") or "pending") != "pending":
                continue
            candidates.append((d, stype in hard_types))
    if not candidates:
        return None
    candidates.sort(key=lambda t: t[0])
    first_d = candidates[0][0]
    week_end = first_d + timedelta(days=6 - first_d.weekday())
    week_c = [c for c in candidates if c[0] <= week_end]
    hard_c = [c for c in week_c if c[1]]
    return (hard_c[0] if hard_c else week_c[0])[0].isoformat()


def _retest_nudge_payload(pm=None, today: "date | None" = None) -> "dict | None":
    """The /api/today-session ``retest_nudge`` flag, or None (no banner).

    Mode picks the metric: hr target_mode → LTHR (the button opens the
    20-min-TT instructions modal); power → FTP (the button inserts an
    ftp_test into the next appropriate week via /api/plan/swap-type).
    """
    try:
        if pm is None:
            from profile_manager import ProfileManager
            pm = ProfileManager.get()
        athlete = getattr(pm, "_athlete", {}) or {}
        today = today or clock.today()

        snooze = athlete.get("retest_snooze_until")
        if snooze:
            try:
                if today <= date.fromisoformat(str(snooze)[:10]):
                    return None
            except ValueError:
                pass

        created = _profile_created_iso(pm)
        mode = getattr(pm, "target_mode", "power") or "power"
        if mode == "hr":
            metric = "lthr"
            source = athlete.get("lthr_source") or "unknown"
            source_date = athlete.get("lthr_source_date")
        else:
            metric = "ftp"
            source = None
            source_date = None
            try:
                source = pm.get_ftp_source()
            except Exception:
                pass
            try:
                source_date = pm.get_ftp_source_date()
            except Exception:
                pass
            source = source or athlete.get("ftp_source") or "unknown"

        stale, days_since, reason = _retest_staleness(
            source, source_date, created, today)
        if not stale:
            return None
        payload = {
            "metric": metric,
            "source": source,
            "source_date": source_date,
            "days_since": days_since,
            "reason": reason,
            "action": "lthr_instructions" if mode == "hr" else "insert_ftp_test",
            "snooze_days": RETEST_SNOOZE_DAYS,
        }
        if payload["action"] == "insert_ftp_test":
            try:
                payload["suggested_date"] = _next_ftp_test_slot()
            except Exception:
                payload["suggested_date"] = None
        return payload
    except Exception as e:
        _log.debug(f"retest nudge skipped: {e}")
        return None


@app.post("/api/retest-nudge/snooze")
def api_retest_nudge_snooze():
    """Snooze the retest banner for 14 days (persisted in athlete.json via
    the sanctioned save_athlete path, so it survives webview storage
    clears). The date is computed server-side — nothing client-supplied."""
    until = (clock.today() + timedelta(days=RETEST_SNOOZE_DAYS)).isoformat()
    try:
        from profile_manager import ProfileManager
        ProfileManager.get().save_athlete({"retest_snooze_until": until})
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    except Exception:
        _log.exception("retest snooze failed")
        return JSONResponse({"detail": "snooze failed"}, 500)
    return {"ok": True, "until": until}


# ═══════════════════════════════════════════════════════════════════════════
# 3.4.0 W2 — CONTINUOUS MODE (IP_CONTINUOUS_MODE amendments C + D)
#
# C — deload advance: the already-computed monotony/ACWR series (the SAME
#     daily-TSS series the on-track band reads, and the SAME weekly ACWR
#     proxy the planner's G4 gate reads) now PULL the scheduled deload into
#     the current week of a continuous plan when they trip (monotony ≥ 2.0
#     or ACWR > 1.5 — grill P4). The conversion rides the extend/refit
#     machinery: tp.plan_week(is_stepback=True) rebuilds the week's shape,
#     tp._refit_session_frozen freezes past/done/user-touched days, and
#     tp.match_zwo files the remaining slots. Surfaced on /api/today-session
#     as a `deload_advance` chip with a revert endpoint mirroring the DFA
#     auto-swap pattern (FIX-CONTRACT C6): revert restores the snapshot and
#     latches the week against re-triggering.
#
# D — rotation policy: continuous_policy.suggest_today_family (hermetic fn)
#     exposed as `continuous_suggestion: {family, reason}` on
#     /api/today-session — additive, continuous goals only.
# ═══════════════════════════════════════════════════════════════════════════

def _plan_is_continuous(plan: "dict | None") -> bool:
    g = (plan or {}).get("goal") or {}
    return str(g.get("type", g.get("goal_type", "")) or "") == "continuous"


def _ride_tiz_seconds(r: dict) -> dict:
    """Per-ride time-in-zone seconds {z1..z7}, via the same envelope →
    zones.power → full-ride chain _polarized_actual_from_rides walks."""
    tiz = r.get("time_in_zone")
    if not tiz:
        zp = (r.get("zones") or {}).get("power") or {}
        if zp:
            tiz = {f"z{i}": int(zp.get(f"Z{i}") or 0) for i in range(1, 8)}
    if not tiz:
        try:
            tiz = (_load_full_ride(r) or {}).get("time_in_zone") or {}
        except Exception:  # noqa: BLE001
            tiz = {}
    return tiz if isinstance(tiz, dict) else {}


def _days_since_last_anaerobic(rides: list[dict], today: date,
                               window_days: int = 28) -> "int | None":
    """Days since the last ride with ≥ ANAEROBIC_Z67_MIN_S of Z6+Z7 time
    (the sprint contract's t150 bar). None = nothing on record in window."""
    cutoff = (today - timedelta(days=window_days)).isoformat()
    newest: "str | None" = None
    for r in rides or []:
        d = _ride_started_local_iso_date(r) or ""
        if not d or d < cutoff or d > today.isoformat():
            continue
        if newest is not None and d <= newest:
            continue
        tiz = _ride_tiz_seconds(r)
        z67 = float((tiz.get("z6") or 0) + (tiz.get("z7") or 0))
        if z67 >= cpol.ANAEROBIC_Z67_MIN_S:
            newest = d
    if newest is None:
        return None
    try:
        return (today - date.fromisoformat(newest)).days
    except ValueError:
        return None


_ANAEROBIC_CONTENT_CLASSES = frozenset({"anaerobic", "neuromuscular", "sprint"})
_LOW_AEROBIC_SESSION_TYPES = frozenset({"recovery", "z2", "long_z2"})


def _continuous_family_deficits(week_json: "dict | None", rides: list[dict],
                                today: date) -> dict:
    """This week's planned-minus-actual zone-rail minutes per stimulus family.

    Planned side: each session's minutes are split over the families using
    its matched library row's Z1%..Z6% zone shares (the honest "zone rail" —
    a 60min VO2 session is NOT 60 high-aerobic minutes); sessions without a
    matched row fall back to their session_type's family for the whole
    duration. Actual side: ride time_in_zone (z1+z2 | z3+z4+z5 | z6+z7).
    Positive deficit = still owed this week. Advisory precision only.
    """
    planned = {f: 0.0 for f in cpol.FAMILIES}
    actual = {f: 0.0 for f in cpol.FAMILIES}
    if not week_json:
        return {f: 0 for f in cpol.FAMILIES}
    try:
        lib_by_file = {row.get("File"): row for row in tp.load_workout_library()}
    except Exception:  # noqa: BLE001
        lib_by_file = {}
    for s in week_json.get("sessions", []):
        st = s.get("session_type") or ""
        dur = float(s.get("duration_min") or 0)
        if st == "rest" or dur <= 0:
            continue
        row = lib_by_file.get(s.get("zwo_file") or "")
        zshares = [float(row.get(f"Z{i}%") or 0) for i in range(1, 7)] if row else []
        if row and sum(zshares) > 0:
            planned[cpol.FAMILY_LOW] += dur * (zshares[0] + zshares[1]) / 100.0
            planned[cpol.FAMILY_HIGH] += dur * (zshares[2] + zshares[3] + zshares[4]) / 100.0
            planned[cpol.FAMILY_ANAEROBIC] += dur * zshares[5] / 100.0
            continue
        cc = ""
        try:
            cc = tp._content_class_for_zwo(s.get("zwo_file") or "") or ""
        except Exception:  # noqa: BLE001
            cc = ""
        if cc in _ANAEROBIC_CONTENT_CLASSES or st == "sprint":
            planned[cpol.FAMILY_ANAEROBIC] += dur
        elif st in _LOW_AEROBIC_SESSION_TYPES:
            planned[cpol.FAMILY_LOW] += dur
        else:
            planned[cpol.FAMILY_HIGH] += dur
    w_start = week_json.get("start") or ""
    for r in rides or []:
        d = _ride_started_local_iso_date(r) or ""
        if not d or d < w_start or d > today.isoformat():
            continue
        tiz = _ride_tiz_seconds(r)
        if not tiz:
            continue
        actual[cpol.FAMILY_LOW] += float((tiz.get("z1") or 0) + (tiz.get("z2") or 0)) / 60.0
        actual[cpol.FAMILY_HIGH] += float(
            (tiz.get("z3") or 0) + (tiz.get("z4") or 0) + (tiz.get("z5") or 0)) / 60.0
        actual[cpol.FAMILY_ANAEROBIC] += float(
            (tiz.get("z6") or 0) + (tiz.get("z7") or 0)) / 60.0
    return {f: int(round(planned[f] - actual[f])) for f in cpol.FAMILIES}


def _continuous_today_suggestion(plan: dict, sleep: dict, training: dict,
                                 rides: list[dict],
                                 today: "date | None" = None) -> dict:
    """Amendment D wiring: assemble the policy-fn inputs from stored state."""
    today = today or clock.today()
    today_iso = today.isoformat()
    cur = next((w for w in plan.get("weeks", [])
                if (w.get("start") or "") <= today_iso <= (w.get("end") or "")),
               None)
    return cpol.suggest_today_family(
        focus_pref=str((plan.get("goal") or {}).get("focus") or "both"),
        deficits=_continuous_family_deficits(cur, rides, today),
        readiness={
            "ln_rmssd_7d": sleep.get("ln_rmssd_7d"),
            "swc_lower": sleep.get("swc_lower"),
            "swc_upper": sleep.get("swc_upper"),
            "tsb": training.get("tsb"),
        },
        days_since_last_anaerobic=_days_since_last_anaerobic(rides, today),
        # A deload week (scheduled OR just advanced by amendment C) outranks
        # every hard rung — the suggestion must never contradict the deload.
        deload_week=bool((cur or {}).get("is_stepback")),
    )


def _deload_chip_payload(rec: dict) -> dict:
    """The chip-sized view of a deload_advance record (no week snapshot)."""
    return {k: rec.get(k) for k in
            ("advanced_on", "week_num", "trigger", "reason", "refit_days",
             "reverted")}


def _continuous_deload_signals(plan: dict, rides: list[dict],
                               today: date) -> "tuple[float | None, float]":
    """(monotony, acwr) from the SAME series the rest of the app computes:
    the on-track band's 7-day daily-TSS window and the planner G4 gate's
    last-completed-week actual/planned ratio."""
    monotony = cpol.foster_monotony(_daily_tss_last7(rides, today))
    dto_weeks = []
    for w in plan.get("weeks", []):
        try:
            dto_weeks.append(tp.week_from_dict({**w, "sessions": []}))
        except (KeyError, ValueError, TypeError):
            continue
    ride_rows = []
    for r in rides or []:
        d = _ride_started_local_iso_date(r) or ""
        if not d:
            continue
        ride_rows.append({"date": d, "tss": (
            r.get("tss") or (r.get("summary") or {}).get("tss")
            or (r.get("parsed_stats") or {}).get("tss") or 0)})
    try:
        acwr = tp._last_completed_week_acwr(dto_weeks, ride_rows)
    except Exception:  # noqa: BLE001
        acwr = 0.0
    return monotony, acwr


def _maybe_advance_continuous_deload(plan: dict, json_path: Path,
                                     today: "date | None" = None) -> "dict | None":
    """Amendment C: convert the CURRENT continuous week to the deload shape
    when monotony/ACWR trip. Idempotent per week (latched via the
    plan["deload_advance"] record, which a revert also latches). Returns the
    chip payload when a deload advance is active for the current week."""
    today = today or clock.today()
    weeks_json = plan.get("weeks") or []
    today_iso = today.isoformat()
    cur_idx = next((i for i, w in enumerate(weeks_json)
                    if (w.get("start") or "") <= today_iso <= (w.get("end") or "")),
                   -1)
    if cur_idx < 0:
        return None
    cur = weeks_json[cur_idx]
    rec = plan.get("deload_advance") or {}
    if rec and rec.get("week_num") == cur.get("week_num"):
        # Already advanced (chip stays up) or reverted (week latched).
        return None if rec.get("reverted") else _deload_chip_payload(rec)
    # Never two deloads back-to-back: current already a deload, the week
    # just ridden was one, or the NEXT week is the scheduled one (relief is
    # ≤7 days out — advancing would stack deload-on-deload).
    if cur.get("is_stepback"):
        return None
    if cur_idx > 0 and weeks_json[cur_idx - 1].get("is_stepback"):
        return None
    if cur_idx + 1 < len(weeks_json) and weeks_json[cur_idx + 1].get("is_stepback"):
        return None
    rides = _load_all_rides_safe()
    monotony, acwr = _continuous_deload_signals(plan, rides, today)
    trip = cpol.deload_trigger(monotony, acwr)
    if not trip:
        return None
    return _advance_continuous_deload(plan, json_path, cur_idx, trip, today)


def _deload_chip_for_today(plan: "dict | None", today: "date | None" = None) -> "dict | None":
    """The chip for a deload advance already recorded for the current week,
    read-only (None when none, or when the rider reverted it)."""
    today_iso = (today or clock.today()).isoformat()
    cur = next((w for w in (plan or {}).get("weeks") or []
                if (w.get("start") or "") <= today_iso <= (w.get("end") or "")), None)
    rec = (plan or {}).get("deload_advance") or {}
    if cur is None or not rec or rec.get("week_num") != cur.get("week_num") or rec.get("reverted"):
        return None
    return _deload_chip_payload(rec)


def _advance_continuous_deload(plan: dict, json_path: Path, cur_idx: int,
                               trip: dict, today: date) -> "dict | None":
    """Convert plan["weeks"][cur_idx] to the deload shape via the machinery:
    tp.plan_week(is_stepback=True) skeleton (×0.72 TSS, easy content) spliced
    over the remaining non-frozen days + tp.match_zwo for files. Writes the
    plan atomically and stamps the revertible deload_advance record."""
    weeks_json = plan.get("weeks") or []
    cur_json = weeks_json[cur_idx]
    goal = _goal_from_plan_dict(plan.get("goal", {}) or {})
    try:
        cur_dto = tp.week_from_dict({"phase": "continuous", **cur_json})
    except (KeyError, ValueError, TypeError):
        return None
    # Pool-collapse breaker — same fail-closed rule as the extend append
    # (3.3.1): a cache fault must degrade (skip the advance), not destroy.
    library = tp.load_workout_library()
    _collapse = tp._pool_collapse_reason(tp._build_pool_indexes(library), library)
    if _collapse:
        _log.error("E_DELOAD_ADVANCE_POOL_COLLAPSE: skipped — %s", _collapse)
        return None
    budget = tp.get_budget_for_phase("continuous", (plan or {}).get("goal") or {})
    phase = tp.Phase(
        name="continuous", start=cur_dto.start, end=cur_dto.end, weeks=1,
        focus="deload (advanced on load trigger)",
        weekly_tss_target=float(cur_dto.tss_target or 0),
        z2_pct=budget.polarized_target.get("z1_pct", 78),
        hit_per_week=budget.hit_count_max, session_types=[],
    )
    # Deterministic per (week, trigger) — re-running the same advance cannot
    # re-roll picks (pinned-seeds contract, mirrors _apply_refit_to_plan).
    salt_basis = f"deload:{cur_dto.start.isoformat()}:{cur_dto.week_num}:{trip['trigger']}"
    seed_salt = int(hashlib.sha1(salt_basis.encode()).hexdigest()[:12], 16)
    prev_sessions = None
    if cur_idx > 0:
        try:
            prev_sessions = [_planned_session_from_json(s)
                             for s in weeks_json[cur_idx - 1].get("sessions", [])]
        except (KeyError, ValueError, TypeError):
            prev_sessions = None
    pw = tp.plan_week(cur_dto.week_num, cur_dto.start, phase, goal, True,
                      prev_week_sessions=prev_sessions, seed_salt=seed_salt)
    new_by_day = {s.day: s for s in pw.sessions}
    used_names = {s.get("zwo_name") for w in weeks_json
                  for s in w.get("sessions", []) if s.get("zwo_name")}
    original_week = json.loads(json.dumps(cur_json))  # JSON-safe deep copy
    refit_days: list[str] = []
    for off, dto in enumerate(cur_dto.sessions):
        if getattr(dto, "session_type", "") == "ftp_test":
            continue  # protocol day survives (extend-path parity)
        if getattr(dto, "session_type", "") == "rest":
            continue  # a deload only makes days EASIER — never adds a ride
        if tp._refit_session_frozen(dto, today):
            continue  # past / done / user-touched days stay verbatim
        repl = new_by_day.get(getattr(dto, "day", None))
        if repl is None:
            continue
        if repl.session_type not in ("rest", "recovery"):
            tp.match_zwo(repl, library, week_num=cur_dto.week_num, day_idx=off,
                         used_names=used_names, plan_start_date=cur_dto.start,
                         seed_salt=seed_salt, micro_only=bool(((plan or {}).get("goal") or {}).get("vo2_microintervals_only", False)))
        cur_dto.sessions[off] = repl
        refit_days.append(repl.day.isoformat())
    if not refit_days:
        return None  # week effectively over — nothing left to unload
    cur_json["sessions"] = [_planned_session_to_json(s) for s in cur_dto.sessions]
    cur_json["is_stepback"] = True
    cur_json["tss_target"] = pw.tss_target  # ×0.72 (Issurin unload band)
    rec = {
        "advanced_on": today.isoformat(),
        "week_num": cur_dto.week_num,
        "trigger": trip,
        "reason": trip.get("reason", ""),
        "refit_days": refit_days,
        "reverted": False,
        "original_week": original_week,
    }
    plan["deload_advance"] = rec
    tp.atomic_write_plan(json_path, plan)
    _log.info(
        f"EVENT=continuous_deload_advanced week={cur_dto.week_num} "
        f"trigger={trip['trigger']} value={trip['value']} days={len(refit_days)}"
    )
    return _deload_chip_payload(rec)


@app.post("/api/plan/continuous/deload-revert")
async def api_continuous_deload_revert(request: Request):
    """Revert affordance for the advanced deload (mirrors the DFA auto-swap
    revert): restores the snapshot for the REMAINING days of the week (past
    days keep what was actually ridden), un-flags the stepback, and latches
    the week so the trigger cannot re-fire until next week."""
    try:
        _ = await _get_json_body(request)  # body optional — accept empty POST
    except Exception:  # noqa: BLE001
        pass
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"reverted": False, "reason": "no_plan"}, 404)
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
        rec = plan.get("deload_advance") or {}
        if not rec or rec.get("reverted"):
            return {"reverted": False, "reason": "nothing_to_revert"}
        today = clock.today()
        orig = rec.get("original_week") or {}
        wj = next((w for w in plan.get("weeks", [])
                   if w.get("week_num") == rec.get("week_num")), None)
        if wj is None or (wj.get("end") or "") < today.isoformat():
            return {"reverted": False, "reason": "week_passed"}
        by_day = {s.get("day"): s for s in orig.get("sessions", [])}
        wj["sessions"] = [
            (by_day.get(sj.get("day"), sj)
             if (sj.get("day") or "") >= today.isoformat() else sj)
            for sj in wj.get("sessions", [])
        ]
        wj["is_stepback"] = bool(orig.get("is_stepback", False))
        wj["tss_target"] = orig.get("tss_target", wj.get("tss_target"))
        rec["reverted"] = True
        rec["reverted_on"] = today.isoformat()
        plan["deload_advance"] = rec
        tp.atomic_write_plan(json_path, plan)
        _log.info(f"EVENT=continuous_deload_reverted week={rec.get('week_num')}")
        return {"reverted": True, "week_num": rec.get("week_num")}
    except Exception:
        _log.exception("continuous deload revert failed")
        return JSONResponse({"detail": "revert failed"}, 500)


@app.get("/api/today-session")
def api_today_session():
    """Return today's adjusted session based on readiness + HRV protocol.

    v1.6.1: thin wrapper around _api_today_session_impl that surfaces any
    uncaught exception as E_TODAY_SESSION_LOOKUP_FAILED in the diag ring
    before re-raising. The impl path is unchanged.
    """
    try:
        return _api_today_session_impl()
    except Exception as _e:
        _log_error(error_codes.Codes.TODAY_SESSION_LOOKUP_FAILED, exc=_e)
        raise


def _api_today_session_impl():
    """Internal — actual /api/today-session compute path."""
    # v4.4.2 §B1+B2: lazy ICU sync hook so frontpage triggers it.
    # v1.6.3: fire-and-forget background thread so the request returns
    # immediately with cached state; sync settles in the background.
    try:
        _kick_lazy_icu_sync(force_if_today_missing=True)
    except Exception as _e:
        _log.debug(f"/api/today-session: lazy ICU sync kick swallowed: {_e}")

    today_str = clock.today().isoformat()
    _jp = _plan_dir() / "current_plan.json"
    _stored = _read_stored_plan()
    # 3.4.0 W2 (amendment C): the continuous deload advance's chip. The
    # advance itself runs where rides arrive (_maybe_auto_reforecast, reached
    # by the load's POST /api/rides/sync): this read used to run it and write
    # the plan (week-view contract P8).
    _deload_chip = _deload_chip_for_today(_stored) if _plan_is_continuous(_stored) else None

    # Today's session is the stored week's, read through the one week view
    # (src/week_view.py) the This Week list and the calendar read: its
    # session_type is the served file's content, not the slot's label, so the
    # Today card cannot say SWEETSPOT over a threshold file again
    # (notes/week-view-contract.md A2). A day no stored row covers has no
    # session; nothing is regenerated to fill it (P1, P4).
    week_data = _stored_week_view(_stored, 0).as_weekly_plan()
    planned_data = next(
        (s for s in week_data["sessions"]
         if s["day"] == today_str and s.get("on_record")), None)
    if not planned_data:
        # has_plan tells a rest day inside a plan (or before it starts, or
        # after it ends) from having no plan at all, which the card offers
        # to create. The retest nudge does not depend on a session.
        from profile_manager import ProfileManager as _PM
        return {"planned": None, "adjusted": None, "reason": "No session planned today",
                "has_plan": bool(_stored and _stored.get("weeks")),
                "retest_nudge": _retest_nudge_payload(_PM.get())}

    planned = tp.PlannedSession(
        day=clock.today(), day_name=planned_data.get("day_name", ""),
        session_type=planned_data.get("session_type", "rest"),
        duration_min=planned_data.get("duration_min", 0),
        tss_estimate=planned_data.get("tss_estimate", 0),
        description=planned_data.get("description", ""),
    )

    # Get readiness and HRV data
    sleep = cached("sleep", get_sleep_metrics)
    training = cached("training", get_today_metrics)
    dfa_vals, last_dec, _last_dec_date, _newest_dfa_date = _recent_dfa_and_decoupling()
    r = compute_readiness(
        ln_rmssd_7d=sleep.get("ln_rmssd_7d"),
        swc_lower=sleep.get("swc_lower"), swc_upper=sleep.get("swc_upper"),
        tsb=training.get("tsb"), sleep_h=sleep.get("sleep_h"),
        rhr_delta=sleep.get("rhr_delta"),
        subjective=_get_soreness_subjective(),  # auto-feed from morning leg check
        recent_dfa_alpha1=dfa_vals, last_decoupling_pct=last_dec,
        last_decoupling_age_days=_age_days_from_iso(_last_dec_date),
        newest_dfa_age_days=_age_days_from_iso(_newest_dfa_date),
    )

    # v4.4.2 §B6: unify the score-None default with /api/readiness via the
    # shared helper. Promotes None→50 only when local rides exist; sets
    # data_status so the frontend can distinguish "ok" from "insufficient_data".
    merged_load = _merge_training_load(training)
    _has_local_load = merged_load.get("ctl") is not None or merged_load.get("atl") is not None
    r = _readiness_with_data_status(r, has_local_load=_has_local_load)
    if r.get("score") is None:
        r["score"] = 50  # final-fallback neutral default if no local data either

    hrv_streak = sleep.get("red_hrv_streak", 0)

    # Check yesterday's actual TSS vs planned (with cross-training weighting)
    # Sport recovery multipliers: running=1.2x, climbing=0.6x, strength=0.5x, hiking=0.4x
    SPORT_RECOVERY_WEIGHT = {
        "Ride": 1.0, "VirtualRide": 1.0, "GravelRide": 1.0,
        "MountainBikeRide": 1.0, "EBikeRide": 1.0, "EMountainBikeRide": 1.0,
        "Run": 1.2, "TrailRun": 1.3,  # higher muscular load
        "RockClimbing": 0.6,  # grip/upper body, less cardio fatigue
        "Hike": 0.4,  # low intensity
        "Workout": 0.5,  # strength, no cardio
        "IceSkate": 0.7, "Swim": 0.8,
    }
    yesterday_tss_ratio = 1.0
    yesterday_had_intense_cross = False
    # Defensive: some test fixtures use mocked DBs without the activities
    # table. activities=[] on failure means G2/G7 simply don't fire — same
    # safe default the helpers themselves use.
    try:
        activities = db.query_activities(days=3)
    except Exception:  # noqa: BLE001
        activities = []
    yesterday = (clock.today() - timedelta(days=1)).isoformat()
    yesterday_weighted_tss = 0
    for a in activities:
        if a.get("date") == yesterday:
            sport = a.get("sport") or "Ride"
            tss = a.get("tss") or 0
            dur_h = (a.get("duration_sec") or 0) / 3600
            weight = SPORT_RECOVERY_WEIGHT.get(sport, 0.7)

            # For non-cycling: use TRIMP if available (better than TSS for HR-based sports)
            raw = {}
            try:
                raw = json.loads(a.get("raw_json") or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            trimp = raw.get("trimp") or 0
            hr_zones = raw.get("icu_hr_zone_times") or []

            if sport not in ("Ride", "VirtualRide", "GravelRide"):
                # Use TRIMP as load metric for non-cycling (scaled to TSS-equivalent)
                effective_load = max(tss, trimp * TRIMP_TO_TSS_FACTOR)
                yesterday_weighted_tss += effective_load * weight

                # Detect intense cross-training via HR zone analysis
                # Z4+ time (zones 3,4,5,6 in 0-indexed array) > 20% = intense
                if len(hr_zones) >= 5:
                    total_time = sum(hr_zones) or 1
                    z4_plus = sum(hr_zones[3:])
                    if z4_plus / total_time > 0.20:
                        yesterday_had_intense_cross = True
                elif dur_h > 0 and tss / dur_h > 60:
                    yesterday_had_intense_cross = True
            else:
                yesterday_weighted_tss += tss * weight

    # On a Monday yesterday is last week's Sunday, so read that week's view.
    _yesterday_week = (week_data if clock.today().weekday() > 0
                       else _stored_week_view(_stored, -1).as_weekly_plan())
    yesterday_planned = next(
        (s["tss_estimate"] for s in _yesterday_week["sessions"]
         if s["day"] == yesterday and s["tss_estimate"] > 0),
        None,
    )
    if yesterday_planned and yesterday_planned > 0:
        yesterday_tss_ratio = yesterday_weighted_tss / yesterday_planned
    # Intense cross-training (>20% time in Z4+ HR) → force easier session today
    if yesterday_had_intense_cross:
        yesterday_tss_ratio = max(yesterday_tss_ratio, 1.5)  # triggers Z2 downgrade

    # v4.6.6 WAVE-4-FIX CRITICAL-2: pass last-4-day rides + today's daily_log
    # to the planner so G2 (48h Z5+ ceiling, Hulin 2014) and G7 (3d mean RPE,
    # Foster 1998) actually have inputs in production. Pre-fix the helpers
    # received an empty list and the gates silently never fired.
    # Note: load_all_rides() returns dicts with `started_at` (ISO timestamp);
    # the planner helpers expect a `date` field (YYYY-MM-DD) — inject it.
    try:
        _all_rides = _load_all_rides_safe()
    except Exception:  # noqa: BLE001
        _all_rides = []
    _cutoff_4d = (clock.today() - timedelta(days=4)).isoformat()
    rides_recent: list[dict] = []
    for rd in _all_rides:
        d_iso = rd.get("date") or _ride_started_local_iso_date(rd)
        if not d_iso or d_iso < _cutoff_4d:
            continue
        # Annotate so the planner helpers see the canonical YYYY-MM-DD field.
        if not rd.get("date"):
            rd = {**rd, "date": d_iso}
        # Also expose start_date_local for _last_48h_z5plus_min's primary path.
        if not rd.get("start_date_local") and rd.get("started_at"):
            rd = {**rd, "start_date_local": rd["started_at"]}
        rides_recent.append(rd)
    try:
        daily_log_today = db.get_daily_log_today() or {}
    except Exception:  # noqa: BLE001
        daily_log_today = {}
    # R4/R5 (2026-07-07): thread the C6 revert flag into the readiness dict so
    # the R5 glyco day-after demotion honors the SAME rider opt-out as the
    # DFA/decoupling caps ("revert whatever fired today", auto-clears at
    # midnight). Carried inside `r` to mirror how dfa_cap reaches the ladder;
    # the today-session payload only picks named keys, so this stays internal.
    r["cap_reverted_today"] = _is_readiness_cap_reverted_today()
    adjusted, reason = tp.adjust_today_session(
        planned=planned, readiness=r,
        hrv_streak_below_swc=hrv_streak,
        yesterday_tss_ratio=yesterday_tss_ratio,
        rides_recent=rides_recent,
        daily_log_today=daily_log_today,
    )
    # 3.4.1 M3 — the C6 rider opt-out ("Keep original" in the day modal, the
    # DFA-banner Revert) must suppress the WHOLE live adjustment for today,
    # not only the R5 glyco gate + the /api/readiness banner booleans.
    # Pre-M3 the flag left HRV-streak / readiness-score / DFA / injury-gate
    # adjustments standing, so clicking Revert visibly did nothing on the
    # today card. Same C6 semantics: one day, auto-clears at midnight.
    if reason and r["cap_reverted_today"]:
        _log.info(
            f"EVENT=today_adjustment_reverted suppressed_reason={reason!r}")
        adjusted, reason = planned, ""

    # v4.6.0 IMPL-HOMEPAGE-CONSISTENCY §3 Pillar D: surface adjustment_reason
    # explicitly so the homepage chip can render "Adjusted to {type} due to
    # {reason}" without re-parsing the legacy arrow-string. Empty when no
    # adjustment ran; otherwise mirrors `reason` (kept for back-compat).
    adjustment_reason = reason if reason else ""
    from profile_manager import ProfileManager as _PM
    resp = {
        "planned": {
            "session_type": planned.session_type,
            "duration_min": planned.duration_min,
            "tss_estimate": planned.tss_estimate,
            "description": planned.description,
            # v3.2.1: matched library file so the home card can preview the
            # actual blocks (same source the day-detail modal charts).
            "zwo_file": planned_data.get("zwo_file") or None,
            "zwo_name": planned_data.get("zwo_name") or None,
        },
        "adjusted": {
            "session_type": adjusted.session_type,
            "duration_min": adjusted.duration_min,
            "tss_estimate": adjusted.tss_estimate,
            "description": adjusted.description,
        },
        # issue #7 — surface a race on the home "Today" card when today IS a race.
        "is_race": bool(planned_data.get("is_race")),
        "race": planned_data.get("race"),
        "reason": reason,
        "adjustment_reason": adjustment_reason,
        "readiness": r.get("score"),
        "hrv_streak_below_swc": hrv_streak,
        "was_modified": reason != "",
        "hr_target": _session_hr_target(adjusted.session_type),
        "power_target": _session_power_target(adjusted.session_type),
        # hr target_mode (red-team F3): lets the today card / session modal
        # hide the watts chip and render the converter-consistent HR chip.
        "target_mode": _PM.get().target_mode,
        # F1/F2 (v4.1.0): expose DFA cap + decoupling advisory so the UI
        # can surface a "Why Z2?" tooltip on the today card.
        "dfa_cap": r.get("dfa_cap"),
        "decoupling_advisory": r.get("decoupling_advisory"),
        # P2.3 (v3.0.0, G11): FTP/LTHR retest nudge — one dismissible banner
        # on the today card; None when fresh / snoozed / in profile grace.
        "retest_nudge": _retest_nudge_payload(_PM.get()),
    }
    # 3.4.0 W2 (amendment D + C surfacing) — continuous goals only, additive
    # keys (absent for finite goals; the response shape never breaks).
    if _plan_is_continuous(_stored):
        try:
            resp["continuous_suggestion"] = _continuous_today_suggestion(
                _stored, sleep, training, _all_rides)
        except Exception as _e:  # noqa: BLE001 — advisory only
            _log.debug(f"/api/today-session: continuous suggestion failed: {_e}")
        if _deload_chip:
            resp["deload_advance"] = _deload_chip
    return resp


# Share of the subjective channel given to the optional readiness-to-train
# rating, ASYMMETRIC — and asymmetric because the evidence is.
#
# Ten Haaf 2017 (PMID 27834554) found readiness-to-train discriminating on the
# LOW side: riders who reported being unready were the overreached ones. The
# upward direction has no such support and one direct contradiction — Sansone
# 2023 (PMID 37259497) found that in free-intensity training BETTER wellness
# produced HIGHER perceived effort, because riders simply did more work. So a
# low rating is allowed to pull the channel down at full weight, while a high
# one nudges it up at less than half that.
#
# What this does NOT claim: that no single rating can ever move a day across a
# readiness band. Any continuous term with a non-zero weight moves a day that
# sits close enough to a boundary, and a signal with real discriminative value
# has to be allowed to. The BOUND on this channel is firm: at most 0.9 of the
# 1-10 axis upward and 2.25 downward.
#
# Its effect on the 0-100 composite is NOT fixed, and the first version of this
# comment wrongly said it was. The composite re-normalises over whatever
# channels are present, so the subjective channel's share grows as others go
# missing: ~2 points up / ~5 points down with all five channels, but up to
# ~10 points down when only three are available. Fewer than three channels
# returns no score at all, which is the real floor on this.
#
# The hard gates are unaffected either way: G5 and G6 read the raw Hooper
# answers, not this channel.
_RTT_WEIGHT_DOWN = 0.25
_RTT_WEIGHT_UP = 0.10


def _get_soreness_subjective() -> float | None:
    """Auto-feed daily-log into readiness subjective score (1-10).

    v4.6.6 IMPL-B — pre-v4.6.6 only soreness was consumed (other Hooper
    fields were stubbed at 4 by the dashboard form). Once IMPL-C lands the
    expanded 4-question form, this helper takes the MIN (worst) of every
    present field so high fatigue / poor sleep / high stress also pulls
    the readiness composite down — not just soreness in isolation.

    Mapping (1=best, 7=worst): score = 10 - (val - 1) * 1.5, clamped 1..10.
    Returns None when no daily_log row exists OR db is unreachable
    (defensive — some test fixtures use mocked DBs without the table).
    """
    try:
        today_log = db.get_daily_log_today()
    except Exception:  # noqa: BLE001
        return None
    if not today_log:
        return None
    components: list[float] = []
    for key in ("soreness", "fatigue", "stress", "sleep_quality"):
        v = today_log.get(key)
        if v is None:
            continue
        try:
            iv = int(v)
        except (TypeError, ValueError):
            continue
        components.append(max(1.0, min(10.0, 10.0 - (iv - 1) * 1.5)))
    # v3.6.0 — readiness-to-train enters as its OWN term, never into the min()
    # above. Direction matters: the four Hooper items are 1=best/7=worst and get
    # inverted by the mapping, while readiness-to-train is 1-10 where HIGH =
    # READY. Dropping it into the same min() would have made one slider the sole
    # determinant of the channel (min is a one-way ratchet) and, on a mis-read
    # of direction, invert the sign outright.
    #
    # Combination: the worst Hooper component stays DOMINANT and readiness-to-
    # train adjusts it, asymmetrically (see _RTT_WEIGHT_DOWN / _UP).
    #
    # An equal-weight mean was tried first and rejected on a measured case: with
    # the worst possible sleep answer and the other three merely poor (a
    # combination that trips neither the soreness cap nor the Hooper-sum gate),
    # one tap of "ready = 10" lifted the composite from 56 to 66 and cancelled
    # the readiness-under-60 all-Z2 rule outright. A single optional,
    # unvalidated self-rating must not overturn four answered questions.
    rtt = today_log.get("readiness_to_train")
    rtt_score = None
    if rtt is not None:
        try:
            rtt_score = max(1.0, min(10.0, float(rtt)))
        except (TypeError, ValueError):
            rtt_score = None
    hooper_worst = min(components) if components else None
    if hooper_worst is None:
        if rtt_score is None:
            return None
        # No Hooper answers at all: the rating is the only thing there is, but
        # it must not carry the channel on its own at full strength either —
        # pull it toward neutral so an un-anchored self-rating cannot drive the
        # composite to an extreme. (Not reachable through /api/daily-log today,
        # which requires all four Hooper items; guarded because a future caller
        # would otherwise hand one optional slider 100% of the channel.)
        _NEUTRAL = 5.5
        return _NEUTRAL + _RTT_WEIGHT_DOWN * (rtt_score - _NEUTRAL)
    if rtt_score is None:
        return hooper_worst
    delta = rtt_score - hooper_worst
    w = _RTT_WEIGHT_DOWN if delta < 0 else _RTT_WEIGHT_UP
    return hooper_worst + w * delta


def _session_hr_target(session_type: str) -> dict:
    """Return HR zone target for a session type.

    In hr target_mode (IP_HR_ONLY) the chips must speak the SAME numbers as
    the converted chart and the FIT file — the Coggan %LTHR prescription rows
    from hr_targets — not the Friel analysis zones (red-team F3: for the same
    z2 session the Friel chip said 138-151 while chart+device said 117-141).
    vo2max carries no bpm at all in hr mode (RPE, per the locked contract).
    """
    from profile_manager import ProfileManager
    pm = ProfileManager.get()
    if pm.target_mode == "hr":
        from hr_targets import _resolve_rows_bpm
        # W1: same resolver as converter/FIT/axis — custom rows flow through.
        rows = _resolve_rows_bpm(int(pm.lthr), int(pm.max_hr),
                                 _prescription_hr_rows(pm))
        coggan = {
            "rest": None,
            "recovery": {"zone": "Z1", "name": "Recovery", "low": 0,
                         "high": rows[1][1], "ceiling_only": True},
            "z2": {"zone": "Z2", "name": "Endurance", "low": rows[2][0], "high": rows[2][1]},
            "long_z2": {"zone": "Z2", "name": "Endurance", "low": rows[2][0], "high": rows[2][1]},
            "tempo": {"zone": "Z3", "name": "Tempo", "low": rows[3][0], "high": rows[3][1]},
            "sweetspot": {"zone": "Z4", "name": "Sweet Spot / Threshold", "low": rows[4][0], "high": rows[4][1]},
            "threshold": {"zone": "Z4", "name": "Threshold", "low": rows[4][0], "high": rows[4][1]},
            "overunder": {"zone": "Z4", "name": "Over-Under", "low": rows[4][0], "high": rows[4][1]},
            "vo2max": {"zone": "Z5", "name": "VO2max", "low": 0, "high": 0,
                       "rpe": "8-9", "note": "by feel — HR can't guide VO2 efforts"},
        }
        t = coggan.get(session_type)
        return t if t else {"zone": "—", "low": 0, "high": 0}
    hr_zones = _hr_zones(config.ATHLETE_LTHR, config.ATHLETE_MAX_HR)
    zone_map = {
        "rest": None, "recovery": hr_zones[0],  # Z1
        "z2": hr_zones[1], "long_z2": hr_zones[1],  # Z2
        "tempo": hr_zones[2],  # Z3
        "sweetspot": hr_zones[2],  # Z3 (upper)
        "threshold": hr_zones[3],  # Z4
        "overunder": hr_zones[3],  # Z4
        "vo2max": hr_zones[4],  # Z5
    }
    zone = zone_map.get(session_type)
    if not zone:
        return {"zone": "—", "low": 0, "high": 0}
    return {"zone": zone["zone"], "name": zone["name"], "low": zone["low"], "high": zone.get("high") or config.ATHLETE_MAX_HR}


def _session_power_target(session_type: str) -> dict:
    """Return power zone target for a session type. Suppressed in hr
    target_mode — an FTP-relative watt range is unusable without a power
    meter (red-team F3 / contract C19)."""
    from profile_manager import ProfileManager
    if ProfileManager.get().target_mode == "hr":
        return {"suppressed": True, "low": 0, "high": 0}
    ftp = config.ATHLETE_FTP_W
    targets = {
        "rest": None, "recovery": {"low": 0, "high": round(ftp * 0.55)},
        "z2": {"low": round(ftp * 0.56), "high": round(ftp * 0.75)},
        "long_z2": {"low": round(ftp * 0.56), "high": round(ftp * 0.75)},
        "tempo": {"low": round(ftp * 0.76), "high": round(ftp * 0.90)},
        "sweetspot": {"low": round(ftp * 0.88), "high": round(ftp * 0.93)},
        "threshold": {"low": round(ftp * 0.91), "high": round(ftp * 1.05)},
        "overunder": {"low": round(ftp * 0.90), "high": round(ftp * 1.05)},
        "vo2max": {"low": round(ftp * 1.06), "high": round(ftp * 1.20)},
    }
    t = targets.get(session_type)
    if not t:
        return {"zone": "—", "low": 0, "high": 0}
    return {**t, "ftp": ftp}


# ═══════════════════════════════════════════════════════════════════════════════
# PLAN API
# ═══════════════════════════════════════════════════════════════════════════════

# P4.2 (v3.0.0) — plan-drift chip threshold: the Training Plan tab shows a
# subtle "Plan assumes CTL x — you're at y. Re-plan?" chip when live CTL has
# drifted more than this fraction from the plan's generation snapshot.
# Constant is test-locked (tests/test_drift_chip.py). No auto-regen — the
# chip only offers the existing Regenerate action.
PLAN_CTL_DRIFT_THRESHOLD = 0.15


def _plan_ctl_drift(snapshot: "dict | None", live_ctl) -> "dict | None":
    """Pure comparison of live CTL vs the plan's ``ctl_snapshot``.

    Returns ``{snapshot_ctl, live_ctl, drift_pct, threshold, exceeded,
    generated_on}`` or None when either side is missing/unusable (pre-P4.2
    plans carry no snapshot — no chip, never an error).
    """
    if not isinstance(snapshot, dict):
        return None
    try:
        snap_ctl = float(snapshot.get("current_ctl"))
        live = float(live_ctl)
    except (TypeError, ValueError):
        return None
    if snap_ctl <= 0:
        return None
    drift = abs(live - snap_ctl) / snap_ctl
    return {
        "snapshot_ctl": round(snap_ctl, 1),
        "live_ctl": round(live, 1),
        "drift_pct": round(drift, 4),
        "threshold": PLAN_CTL_DRIFT_THRESHOLD,
        "exceeded": drift > PLAN_CTL_DRIFT_THRESHOLD,
        "generated_on": snapshot.get("generated_on"),
    }


@app.get("/api/plan")
def api_plan():
    # Try structured JSON first
    json_path = _plan_dir() / "current_plan.json"
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                plan_data = json.load(f)
            # v4.1.1 FIX-PLANNER B: attach per-session `zone_dist` so the
            # dashboard mini-graph can render the ACTUAL ZWO's zone
            # distribution. v1.4.0: per-session enrichment now lives in
            # _enrich_plan_for_response (single helper, replaces 5
            # duplicates). Phase-level + week-level XSS mirrors stay here.
            try:
                # Phase/week 3D mirrors are computed inline because they
                # aggregate session-level fields. Per-session fields
                # (zone_dist / score / content_class / display_name /
                # zwo_duration_min / card_state) come from the helper below.
                for w in plan_data.get("weeks", []):
                    # v1.0.6 IMPL-3D-DASHBOARD: per-week 3D XSS mirrors.
                    # PRIMARY weekly_tss is preserved untouched. Mirrors are
                    # aggregated from session-level wprime_*/pmax_* fields
                    # (IMPL-3D-PLANNER writes these when 3D fields are
                    # available). None per TSS-primary contract when absent.
                    _w_xss_cp = 0.0
                    _w_xss_wp = 0.0
                    _w_xss_pm = 0.0
                    _w_xss_any = False
                    for _s in w.get("sessions", []) or []:
                        if isinstance(_s, dict):
                            _wp = _s.get("wprime_tss") or _s.get("wprime_xss")
                            _pm = _s.get("pmax_tss") or _s.get("pmax_xss")
                            _cp = _s.get("cp_tss") or _s.get("cp_xss")
                            if _wp is not None:
                                _w_xss_wp += float(_wp); _w_xss_any = True
                            if _pm is not None:
                                _w_xss_pm += float(_pm); _w_xss_any = True
                            if _cp is not None:
                                _w_xss_cp += float(_cp); _w_xss_any = True
                    if _w_xss_any:
                        w.setdefault("weekly_xss_cp", round(_w_xss_cp, 1))
                        w.setdefault("weekly_xss_w_prime", round(_w_xss_wp, 1))
                        w.setdefault("weekly_xss_pmax", round(_w_xss_pm, 1))
                        w.setdefault(
                            "weekly_xss_total",
                            round(_w_xss_cp + _w_xss_wp + _w_xss_pm, 1),
                        )
                    else:
                        w.setdefault("weekly_xss_cp", None)
                        w.setdefault("weekly_xss_w_prime", None)
                        w.setdefault("weekly_xss_pmax", None)
                        w.setdefault("weekly_xss_total", None)
                # v1.4.0 — per-session enrichment moved to
                # _enrich_plan_for_response below. The for-loop above only
                # computes per-week 3D-XSS aggregations.
                # v1.0.6 IMPL-3D-DASHBOARD: phase-level weekly_xss_* mirrors.
                # Dashboard plan-tab phase rows render per-phase weekly_tss as
                # PRIMARY. The mirrors here let the secondary stacked bar
                # size CP/W'/Pmax. None when 3D values aren't populated.
                try:
                    _phases = plan_data.get("phases") or []
                    _weeks = plan_data.get("weeks") or []
                    for _ph in _phases:
                        if not isinstance(_ph, dict):
                            continue
                        _ph_name = (_ph.get("name") or "").lower()
                        _xc = 0.0; _xw = 0.0; _xp = 0.0
                        _any = False; _cnt = 0
                        for _wk in _weeks:
                            if not isinstance(_wk, dict):
                                continue
                            _wphase = (_wk.get("phase") or _wk.get("phase_name") or "").lower()
                            if _ph_name and _wphase and _wphase != _ph_name:
                                continue
                            _wxc = _wk.get("weekly_xss_cp")
                            _wxw = _wk.get("weekly_xss_w_prime")
                            _wxp = _wk.get("weekly_xss_pmax")
                            if _wxc is not None:
                                _xc += float(_wxc); _any = True
                            if _wxw is not None:
                                _xw += float(_wxw); _any = True
                            if _wxp is not None:
                                _xp += float(_wxp); _any = True
                            _cnt += 1
                        if _any and _cnt > 0:
                            _ph.setdefault("weekly_xss_cp", round(_xc / _cnt, 1))
                            _ph.setdefault("weekly_xss_w_prime", round(_xw / _cnt, 1))
                            _ph.setdefault("weekly_xss_pmax", round(_xp / _cnt, 1))
                            _ph.setdefault(
                                "weekly_xss_total",
                                round((_xc + _xw + _xp) / _cnt, 1),
                            )
                        else:
                            _ph.setdefault("weekly_xss_cp", None)
                            _ph.setdefault("weekly_xss_w_prime", None)
                            _ph.setdefault("weekly_xss_pmax", None)
                            _ph.setdefault("weekly_xss_total", None)
                except Exception as _e:
                    _log.debug(f"/api/plan phase xss_mirror failed: {_e}")
            except Exception as _e:
                _log.debug(f"/api/plan zone_dist annotate failed: {_e}")
            # v1.4.0 — single enrichment helper for per-session fields.
            # v1.6.0 — surface failure under E_ENRICH_FAILED so silently
            # un-enriched plans become visible in /api/diag/recent-errors.
            try:
                _enrich_plan_for_response(
                    plan_data, today_iso=clock.today().isoformat(),
                )
            except Exception as _e:
                _log_error(
                    error_codes.Codes.ENRICH_FAILED, exc=_e,
                    endpoint="/api/plan",
                    week_count=len(plan_data.get("weeks", [])) if isinstance(plan_data, dict) else 0,
                )
            # P4.2 (v3.0.0) — drift chip payload: snapshot vs live CTL (with
            # local fallback, mirroring the today-card's merged load). Chip
            # render is client-only; no auto-regen.
            try:
                _snap = plan_data.get("ctl_snapshot")
                if _snap:
                    _live_ctl = _merge_training_load(
                        cached("training", get_today_metrics)
                    ).get("ctl")
                    _drift = _plan_ctl_drift(_snap, _live_ctl)
                    if _drift:
                        plan_data["ctl_drift"] = _drift
            except Exception as _e:
                _log.debug(f"/api/plan ctl_drift skipped: {_e}")
            # Also include markdown if available
            plans = sorted(_plan_dir().glob("*.md"), reverse=True)
            md = None
            if plans:
                with open(plans[0], encoding="utf-8") as f:
                    md = f.read()
            return {"plan_json": plan_data, "plan": md, "file": str(json_path)}
        except (json.JSONDecodeError, OSError):
            pass

    plans = sorted(_plan_dir().glob("*.md"), reverse=True)
    if not plans:
        return {"plan": None, "plan_json": None}
    with open(plans[0], encoding="utf-8") as f:
        return {"plan": f.read(), "plan_json": None, "file": str(plans[0])}


@app.get("/api/plan/preview")
def api_plan_preview(
    goal: str = Query("general"),
    plan_weeks: int = Query(0),
    event_date: str | None = Query(None),
    hours_per_week: float = Query(8.0),
    start_date: str | None = Query(None),
    phase_weeks: str | None = Query(None),
):
    """v4.6.0 IMPL-PLAN-CONFIG-UI: live phase preview for the Plan Configuration
    right-panel. Mirrors /api/plan/generate's phase split so the right panel
    matches the generated grid (was previously stale because it read
    current_plan.json — only updated AFTER Generate was clicked).

    Single source of truth: tp.generate_phases(Goal, current_ctl). For a
    20-week event-prep plan: BASE 8 + BUILD1 4 + BUILD2 4 + PEAK 2 + TAPER 2.
    """
    target_date = None
    if event_date:
        try:
            target_date = date.fromisoformat(event_date)
        except (ValueError, TypeError):
            target_date = None

    # PART B: the preview must accept the backdated anchor or the right
    # panel lies about the phase split (persistence-sweep item).
    start_dt = None
    if start_date:
        try:
            start_dt = date.fromisoformat(str(start_date)[:10])
        except (ValueError, TypeError):
            start_dt = None
    if start_dt is not None and start_dt >= clock.today():
        start_dt = None  # future/today start = fresh start (legacy)

    # For event-prep, plan_weeks is auto-computed from event_date; UI
    # disables the input but we recompute server-side as defence in depth
    # so a stale/forced-edit value can't desync the right panel from the
    # generated grid.
    if goal in ("event", "event_preparation") and target_date is not None:
        days_to_event = (target_date - (start_dt or clock.today())).days
        plan_weeks = max(4, -(-days_to_event // 7))  # ceil division
    else:
        plan_weeks = max(4, int(plan_weeks or 0))

    # Map UI alias → canonical Goal type used by training_planner.
    goal_type = "event" if goal == "event_preparation" else goal

    g = tp.Goal(
        goal_type=goal_type,
        target_date=target_date,
        hours_per_week=hours_per_week,
        plan_weeks=plan_weeks,
        start_date=start_dt,
    )

    # Phase-split editor (v3.2.0): one URL-encoded JSON object, validity-gated
    # inside tp.generate_phases — the SAME validator generate uses (A9), so
    # preview==generate for lengths+dates by construction (A8 parity scope).
    _pw_req, _pw_parse_err = _parse_phase_weeks(phase_weeks)
    if _pw_req:
        g.phase_weeks = _pw_req
    # The recommendation vector seeds the editor's "rec" state client-side
    # (labels can drift ±1 at the reconcile seam, so the UI must NOT derive
    # the vector from the rendered rows — A6). None ⇒ editor disabled.
    _pw_rec, _pw_rec_reason = tp._recommended_phase_weeks(g)

    try:
        training = cached("training", get_today_metrics)
        current_ctl = _planning_ctl()
    except Exception:
        current_ctl = _PLANNING_CTL_UNKNOWN

    phases = tp.generate_phases(g, current_ctl,
                                recent_weekly_tss=_chronic_weekly_tss_safe())
    _pw_status = (getattr(g, "_phase_weeks_status", None)
                  or (f"fallback:{_pw_parse_err}" if _pw_parse_err else None))
    return {
        "ok": True,
        "plan_weeks": plan_weeks,
        "goal": goal_type,
        "event_date": target_date.isoformat() if target_date else None,
        "start_date": start_dt.isoformat() if start_dt else None,  # PART B
        # Phase-split editor (v3.2.0): "applied" | "fallback:<reason>" | None.
        "phase_weeks_status": _pw_status,
        "phase_weeks_rec": _pw_rec,
        "phase_weeks_disabled_reason": _pw_rec_reason or None,
        "phases": [
            {
                "name": p.name,
                "weeks": p.weeks,
                "start": p.start.isoformat() if hasattr(p.start, "isoformat") else str(p.start),
                "end": p.end.isoformat() if hasattr(p.end, "isoformat") else str(p.end),
                "weekly_tss": p.weekly_tss_target,
                "focus": p.focus,
            }
            for p in phases
        ],
    }


@app.get("/api/plan/entry-scan")
def api_plan_entry_scan(
    goal: str | None = Query(None),
    plan_weeks: int = Query(0),
    event_date: str | None = Query(None),
    hours_per_week: float = Query(8.0),
):
    """MODE 2 (IP_PLAN_CONTINUITY B-D3/B-LOCKED-3) — "Place me from my rides".

    Scans the ride archive backward from today and proposes an evidence-based
    entry credit for the plan the query params describe (same shape as
    /api/plan/preview — the UI's scan→propose→confirm rides the existing
    refreshPlanPreview pattern). ZERO writes: the proposal only becomes real
    when the user confirms and Generate persists the equivalent start_date
    with entry_mode="recognized".

    Params are validated manually so missing goal/target params return a
    clean 400 (FastAPI's required-Query default would 422)."""
    if not goal:
        raise HTTPException(status_code=400, detail="goal is required")
    goal_type = "event" if goal == "event_preparation" else goal

    target_date = None
    if event_date:
        try:
            target_date = date.fromisoformat(str(event_date)[:10])
        except (ValueError, TypeError):
            raise HTTPException(status_code=400, detail="invalid event_date")

    if goal_type == "event":
        if target_date is None:
            raise HTTPException(status_code=400,
                                detail="event_date is required for an event goal")
        # H1 parity with preview/generate: the runway is derived from the
        # today→target span, never trusted from the today-anchored slider.
        days_to_event = (target_date - clock.today()).days
        plan_weeks = max(4, -(-days_to_event // 7))  # ceil division
    elif plan_weeks < 1 and target_date is None:
        raise HTTPException(status_code=400,
                            detail="plan_weeks or event_date is required")
    else:
        plan_weeks = max(4, int(plan_weeks or 0)) if plan_weeks else 0

    g = tp.Goal(
        goal_type=goal_type,
        target_date=target_date,
        hours_per_week=hours_per_week,
        plan_weeks=plan_weeks,
    )

    try:
        training = cached("training", get_today_metrics)
        current_ctl = _planning_ctl()
    except Exception:
        current_ctl = _PLANNING_CTL_UNKNOWN

    result = tp.recognize_entry(g, _load_all_rides_safe(), current_ctl=current_ctl)

    # 3.3.3 (L4-UX 2): scan-result card support. Pure date math on what the
    # recognizer already computed — no new engine work. The card's headline
    # is "Matched your last N weeks — you're at week X of Y":
    #   N = proposal_weeks, X = entry_week, Y = plan_weeks (returned so the
    #   client never re-derives the server-side week budget), and the
    #   consequence line needs weeks_remaining + plan_end_date.
    proposal = int(result.get("proposal_weeks") or 0)
    sd_iso = result.get("equivalent_start_date")
    total_weeks = plan_weeks
    if goal_type == "event" and target_date is not None and sd_iso:
        # H1 parity: on a credited entry the week budget is anchor→target,
        # exactly what generate will recompute (app.py H1 block).
        try:
            _anchor = date.fromisoformat(sd_iso)
            total_weeks = max(4, -(-(target_date - _anchor).days // 7))
        except (ValueError, TypeError):
            pass
    result["plan_weeks"] = total_weeks
    result["goal"] = goal_type
    result["entry_week"] = (proposal + 1) if proposal > 0 else None
    # Wave-A contract field (defensive: fall back to plan-minus-credit when
    # an older recognizer payload omits it).
    if result.get("weeks_remaining") is None:
        result["weeks_remaining"] = max(0, total_weeks - proposal)
    if goal_type == "event" and target_date is not None:
        result["plan_end_date"] = target_date.isoformat()
    elif sd_iso and total_weeks:
        try:
            _s = date.fromisoformat(sd_iso)
            result["plan_end_date"] = (
                _s + timedelta(days=total_weeks * 7 - 1)
            ).isoformat()
        except (ValueError, TypeError):
            pass
    return result


@app.get("/api/event/projection")
def api_event_projection():
    """v4.6.7 IMPL-CAP: capability projection for the active event-prep plan.

    Returns the locked-shape dict from /tmp/MASTER_DECISIONS_v467.md §4
    (predicted_finish_h, predicted_np, predicted_tss, gap_endurance_h, ...).
    Composes Goal + athlete + fitness_state and calls
    training_planner._project_event_capability(); when ≥5 rides in the last
    30 days are persisted the helper also runs the Monod CP/W' fit on the
    aggregated 90d best efforts (fitness_estimation.compute_cp_wprime).

    Returns 200 with ``{"available": False, "reason": <str>}`` when there
    is no event-prep plan or no event_km — the dashboard widget hides
    itself in that case.
    """
    plan_path = _plan_dir() / "current_plan.json"
    if not plan_path.exists():
        return {"available": False, "reason": "no_plan"}
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        return {"available": False, "reason": f"plan_load_error: {e}"}

    g = plan.get("goal", {})
    if g.get("type") not in ("event", "event_preparation"):
        return {"available": False, "reason": "not_event_goal"}
    if not g.get("event_km"):
        return {"available": False, "reason": "no_event_km"}

    goal = tp.goal_from_dict(g)

    rides = _load_all_rides_safe()
    if goal.longest_ride_h_90d is None:
        goal.longest_ride_h_90d = _longest_ride_h_90d(rides)

    # Athlete: ftp + weight from active profile.
    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        athlete = {"ftp": pm.ftp, "weight_kg": pm.weight_kg}
    except Exception:
        athlete = {"ftp": 200, "weight_kg": 70.0}

    # Current CTL.
    try:
        training = cached("training", get_today_metrics)
        current_ctl = _planning_ctl()
    except Exception:
        current_ctl = _PLANNING_CTL_UNKNOWN

    # Best-efforts aggregate for CP/W' Monod fit (only when we have ≥5 rides
    # in the last 30 days — gate prevents fitting on stale data).
    best_efforts: dict = {}
    try:
        if rides:
            cutoff_30 = (clock.today() - timedelta(days=30)).isoformat()
            recent_count = sum(1 for r in rides if (r.get("started_at") or "")[:10] >= cutoff_30)
            if recent_count >= 5:
                best_efforts = _aggregate_best_efforts_90d(rides)
    except Exception as _e:
        _log.debug(f"capability projection: best_efforts skipped ({_e})")

    projection = tp._project_event_capability(
        goal,
        athlete,
        {"current_ctl": current_ctl},
        best_efforts_90d=best_efforts or None,
    )
    projection["available"] = True
    projection["event_name"] = goal.event_name
    projection["event_date"] = goal.target_date.isoformat() if goal.target_date else None
    projection["event_km"] = goal.event_km
    projection["event_climb_m"] = goal.event_climb_m
    projection["ftp"] = athlete["ftp"]
    projection["weight_kg"] = athlete["weight_kg"]
    projection["current_ctl"] = round(current_ctl, 1)

    # v1.0.0: weekly_history for the chart — past 12 weeks of longest-ride-h.
    # Replaces the v4.6.7 flat-line "weeks-to-event" rendering which was
    # uninformative when the event was 0-1 weeks out.
    try:
        from collections import defaultdict
        buckets: dict[tuple, float] = defaultdict(float)
        cutoff_iso = (clock.today() - timedelta(days=12 * 7)).isoformat()
        for r in rides or []:
            s = (r.get("started_at") or "")[:10]
            if not s or s < cutoff_iso:
                continue
            try:
                d = date.fromisoformat(s)
            except ValueError:
                continue
            iso = d.isocalendar()
            key = (iso[0], iso[1])  # (year, week)
            dur_h = float(r.get("duration_s") or 0) / 3600.0
            if dur_h > buckets[key]:
                buckets[key] = dur_h
        weekly_history = []
        for wk_back in range(12, -1, -1):
            target = clock.today() - timedelta(weeks=wk_back)
            iso = target.isocalendar()
            weekly_history.append({
                "week_offset": -wk_back,
                "iso_year": iso[0],
                "iso_week": iso[1],
                "longest_ride_h": round(buckets.get((iso[0], iso[1]), 0.0), 2),
            })
        projection["weekly_history"] = weekly_history
    except Exception as _e:  # noqa: BLE001
        _log.debug(f"projection weekly_history skipped: {_e}")
        projection["weekly_history"] = []
    return projection


def _replaced_weeks_before(old_plan: dict, cutoff: date) -> list[dict]:
    """The rows of ``old_plan`` a Generate replaces, as far as they were
    prescribed before ``cutoff``: every session dated before it, in its row,
    the row's end clipped to the day before. Rows the old plan had itself
    carried come along, and a day is kept once (the newer plan's).

    They are stored apart from ``weeks`` because the planner reads a plan's
    elapsed ``weeks`` as its own past (Regenerate's week numbers and 3:1
    rhythm); only the readers read these (week_view.build, the calendar)."""
    cutoff_iso = cutoff.isoformat()
    stamp = clock.today().isoformat()
    taken: set[str] = set()
    out: list[dict] = []
    for row in list(old_plan.get("weeks") or []) + list(old_plan.get("replaced_weeks") or []):
        if not isinstance(row, dict) or (row.get("start") or "") >= cutoff_iso:
            continue
        sessions = [s for s in (row.get("sessions") or [])
                    if isinstance(s, dict) and "" < (s.get("day") or "") < cutoff_iso
                    and s["day"] not in taken]
        if not sessions:
            continue
        taken.update(s["day"] for s in sessions)
        kept = dict(row, sessions=sessions)
        if (kept.get("end") or "") >= cutoff_iso:
            kept["end"] = (cutoff - timedelta(days=1)).isoformat()
        kept.setdefault("carried_from_generate", stamp)
        out.append(kept)
    return sorted(out, key=lambda w: w.get("start") or "")


@app.post("/api/plan/generate")
async def api_plan_generate(request: Request):
    """Generate a training plan from UI form data."""
    body = await _get_json_body(request)
    try:

        # Load saved training prefs as defaults (from setup wizard)
        _prefs = {}
        _prefs_file = DATA_DIR / "user_prefs.json"
        if _prefs_file.exists():
            try:
                _prefs = json.loads(_prefs_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        # Parse target_date from string to date object
        target_date = None
        event_date_str = body.get("event_date")
        if event_date_str:
            target_date = date.fromisoformat(event_date_str)

        rest_days = [int(d) for d in body.get("rest_days", _prefs.get("rest_days", [0]))]
        all_days = list(range(7))
        available_days = [d for d in all_days if d not in rest_days]
        plan_weeks = int(body.get("weeks", body.get("plan_weeks", 0)))

        # PART B (mid-plan entry): optional backdated anchor + its provenance.
        # Absent/None ⇒ today ⇒ exact legacy behavior. generate_plan validates
        # (future start / start≥target → ValueError → 400 below).
        start_date = None
        _sd_str = body.get("start_date")
        if _sd_str:
            start_date = date.fromisoformat(str(_sd_str)[:10])
        _entry_mode_raw = str(body.get("entry_mode") or "") or None
        entry_mode = (_entry_mode_raw
                      if _entry_mode_raw in ("declared", "recognized") else None)

        # H1 (evaluator): the UI's plan-weeks slider is TODAY-anchored (the
        # event-date sync computes weeks-to-event from now), and Goal.
        # weeks_available() short-circuits on plan_weeks>0 — so a backdated
        # event plan would lay a full-runway phase split into a today-sized
        # week budget and dump the difference into a multi-week peak block.
        # Mirror the preview's server-side recompute (defence in depth): for
        # event goals the week count is ALWAYS derived from the actual
        # anchor→target span, never trusted from the form.
        if body.get("goal") in ("event", "event_preparation") and target_date:
            _anchor_for_weeks = (start_date
                                 if (start_date and start_date < clock.today())
                                 else clock.today())
            _days = (target_date - _anchor_for_weeks).days
            plan_weeks = max(4, -(-_days // 7))  # ceil division

        # Phase-split editor (v3.2.0): user week-vector from the editor. Sent
        # only when custom AND ≠ recommendation; validity-gated inside
        # tp.generate_phases against the server-side week count above (A1).
        _pw_req, _pw_parse_err = _parse_phase_weeks(body.get("phase_weeks"))

        goal = tp.Goal(
            goal_type=body.get("goal", "general"),
            target_date=target_date,
            start_date=start_date,
            entry_mode=entry_mode,
            event_name=body.get("event_name") or "",
            event_km=body.get("event_km") or 0,
            event_climb_m=body.get("event_climb") or 0,
            event_type=body.get("event_type", "granfondo"),
            target_ftp=body.get("target_ftp"),
            target_ctl=body.get("target_ctl"),
            hours_per_week=body.get("hours_per_week", _prefs.get("hours_per_week", 8.0)),
            max_weekday_hours=body.get("max_weekday", 2.0),
            max_weekend_hours=body.get("max_weekend", 3.5),
            available_days=available_days,
            rest_days=rest_days,
            daily_max_hours={int(k): float(v) for k, v in body.get("daily_availability", {}).items()},
            plan_weeks=plan_weeks,
            longest_ride_h_90d=body.get("longest_ride_h_90d"),
            last_ftp_test_date=body.get("last_ftp_test_date"),
            # J1 (v2.1.0): intensity-distribution model is a user choice
            # (polarized default; pyramidal / threshold). Not hard-forced.
            # v2.3.0: a non-empty custom_bands payload selects the "custom" model.
            distribution=("custom" if _parse_custom_bands(body.get("custom_bands"))
                          else body.get("distribution", _prefs.get("distribution", "polarized"))),
            # F1 (v2.1): opt-in block periodization (default off).
            block_periodization=bool(body.get("block_periodization", _prefs.get("block_periodization", False))),
            vo2_microintervals_only=bool(body.get(
                "vo2_microintervals_only",
                _prefs.get("vo2_microintervals_only", False))),
            events=_events_from_dicts(body.get("events")),  # F7 (A + optional B/C)
            # FS1 — plan construction mode (auto | fixed_core | template).
            # v2.3.0: a custom-bands payload drives the dynamic custom blueprint,
            # so force plan_mode=template + template_id=custom (the deterministic
            # HIT-type path that actually realizes the requested training mix).
            plan_mode=("template" if _parse_custom_bands(body.get("custom_bands"))
                       else str(body.get("plan_mode", _prefs.get("plan_mode", "auto")) or "auto")),
            template_id=("custom" if _parse_custom_bands(body.get("custom_bands"))
                         else str(body.get("template_id", "") or "")),
            custom_bands=_parse_custom_bands(body.get("custom_bands")),  # v2.3.0
            phase_weeks=_pw_req,  # v3.2.0 phase-split editor
            # 3.4.0 W2: continuous-mode focus pref; sanitized to the engine's
            # vocabulary (unknown values fall back to "both").
            focus=(str(body.get("focus") or "both")
                   if str(body.get("focus") or "both") in ("ftp", "vo2", "both")
                   else "both"),
        )
        # v4.6.7 IMPL-CAP: auto-populate endurance baseline if missing.
        if goal.longest_ride_h_90d is None:
            goal.longest_ride_h_90d = _longest_ride_h_90d()

        # Phase-split editor (v3.2.0, A3): a vector identical to the
        # recommendation is NOT a custom split — store None (no badge, no
        # status). A VALID custom vector is normalized engine-side; an
        # invalid one is kept as sent so the form repopulates the user's
        # numbers while generate_phases falls back + stamps the reason.
        if goal.phase_weeks:
            _pw_norm, _pw_reason = tp.validate_phase_weeks(goal, goal.phase_weeks)
            if _pw_norm is not None:
                goal.phase_weeks = _pw_norm
            elif not _pw_reason:
                goal.phase_weeks = None

        # v4.5.0 IMPL-PLANNER: mint a fresh seed_salt so the FIRST plan is no
        # longer deterministic (was missing here — only /api/plan/regenerate
        # passed seed_salt previously, so the very first generate run always
        # produced the same library picks). Mirrors the regenerate pattern.
        seed_salt = time.time_ns()

        # v1.3.2 — read PERSISTED availability calendar from existing plan and
        # pass overrides to generate_plan. Pre-fix, /api/plan/generate ignored
        # plan["availability"] entirely → freshly generated plan produced
        # sessions on dates the user had marked as unavailable. Mirrors the
        # /api/plan/reforecast pattern.
        #
        # 3.3.3 (owner): only EXPLICIT BLOCKS (holiday/injury/illness/
        # unavailable) survive as engine overrides. Plain "available"/"rest"
        # rows are the OLD wizard's defaults densely materialized by the
        # v1.8.21 rebuild below — feeding them back into generate_plan scaled
        # the new plan's sessions to the STALE hours (owner set 1h/day, plan
        # still built 2h sessions) while the rebuilt calendar said otherwise.
        # This makes the engine input exactly what the rebuilt calendar will
        # show: new wizard defaults everywhere except explicit blocks.
        _EXPLICIT_BLOCK = {"holiday", "injury", "illness", "unavailable"}
        json_path = _plan_dir() / "current_plan.json"
        availability_overrides: dict[str, float] = {}
        old_availability_full: dict = {}  # v1.8.21 — keep type info for block-preserve
        _existing_plan = None
        if json_path.exists():
            try:
                with open(json_path, encoding="utf-8") as f:
                    _existing_plan = json.load(f)
                old_availability_full = _existing_plan.get("availability", {}) or {}
                availability_overrides = {
                    day_iso: float(entry["hours"])
                    for day_iso, entry in old_availability_full.items()
                    if isinstance(entry, dict) and "hours" in entry
                    and entry.get("type") in _EXPLICIT_BLOCK
                }
            except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
                availability_overrides = {}
                old_availability_full = {}

        # v1.11.0 — thread athlete (ftp + weight) so the event-demand planner
        # can compute event targets.
        athlete = _planner_athlete()

        # v2.1.0 (F5 + E1) — thread the rider's ACTUAL starting fitness + recent
        # load into INITIAL generation. Pre-fix, generate_plan self-fetched CTL
        # and defaulted to 37.0 ("starts like post-winter"), and the volume
        # ceiling came from availability. Now we pass the real current_ctl (ICU
        # wellness; generate_plan still falls back to local-CTL→37 when None,
        # preserving F4) and the recent mean weekly TSS from the full local
        # archive (sets the load-based weekly ceiling). Mirrors the adaptation
        # path's `cached("training", get_today_metrics)` + current_ctl read.
        current_ctl = None
        recent_weekly_tss = None
        try:
            training = cached("training", get_today_metrics)
            current_ctl = _fitness_state()["ctl"]     # None: the planner's own fallback
        except Exception:
            current_ctl = None
        recent_weekly_tss = _chronic_weekly_tss_safe()

        # Re-entry shaping inputs (SCIENCE.md "Returning after a break"):
        # how long since the last ride, and TSB now. Best-effort — None keeps
        # generate_plan's legacy behaviour.
        days_since_last_ride = None
        tsb_at_generation = None
        try:
            import ride_storage as _rs2
            from datetime import date as _date
            last = max((r.get("started_at") or "")[:10]
                       for r in _rs2.load_all_rides())
            if last:
                days_since_last_ride = (clock.today()
                                        - _date.fromisoformat(last)).days
        except Exception:
            days_since_last_ride = None
        try:
            tsb_at_generation = training.get("tsb")
        except Exception:
            tsb_at_generation = None

        phases, weeks = tp.generate_plan(
            goal, seed_salt=seed_salt,
            availability_overrides=availability_overrides or None,
            athlete=athlete,
            current_ctl=current_ctl,
            recent_weekly_tss=recent_weekly_tss,
            days_since_last_ride=days_since_last_ride,
            tsb_at_generation=tsb_at_generation,
            # A plan generated mid-week overlaps days already ridden. Without
            # these, "generate" hands the rider a fresh week on top of the one
            # they are in.
            activities=_recent_activities_for_planner(),
        )
        plan_path = tp.export_plan_md(goal, phases, weeks)

        # Also save structured JSON
        plan_dict = {
            # Through the codec: every Goal field, under the names the goal
            # block has always used.
            "goal": tp.goal_to_dict(goal),
            "phases": [
                {
                    "name": p.name,
                    "weeks": p.weeks,
                    "start": p.start.isoformat() if hasattr(p.start, "isoformat") else str(p.start),
                    "end": p.end.isoformat() if hasattr(p.end, "isoformat") else str(p.end),
                    "weekly_tss": p.weekly_tss_target,
                    "focus": p.focus,
                }
                for p in phases
            ],
            # Through the codec: every week and session field. This writer
            # kept 11 of a session's 27, so fuelling notes, matched=False and
            # the double-threshold pairing were lost at birth, and so was the
            # week's own budget (dupes.md DUP-25, http.md HTTP-9).
            "weeks": [tp.week_to_dict(w) for w in weeks],
            "generated": clock.now().isoformat(),
            # P4.2 (v3.0.0) — generation-time fitness snapshot for the
            # Training-Plan-tab drift chip (live CTL vs plan assumption).
            "ctl_snapshot": tp.plan_ctl_snapshot(current_ctl, recent_weekly_tss),
        }

        # v1.8.21 — rebuild the per-day availability calendar from the NEWLY
        # chosen weekly hours so it reflects them across the whole plan span.
        # Pre-fix /api/plan/generate carried the OLD plan["availability"]
        # verbatim (v1.3.2), so after changing weekday/weekend hours the
        # calendar still showed the old per-day values: the calendar is DENSE,
        # so the frontend's weekly-grid default-fill (loadAvailData, only fills
        # days NOT already present) was skipped for every already-stored day →
        # stale hours always won. Now: every day in the plan span is set to the
        # new weekly default (goal.daily_max_hours per weekday, else
        # max_weekday/weekend, rest_days → 0), EXCEPT days the user explicitly
        # blocked (holiday / injury / illness / unavailable), which are kept.
        # (_EXPLICIT_BLOCK defined once above, where the same set gates which
        # old rows are fed to generate_plan — 3.3.3 keeps both in lockstep.)
        _daily = goal.daily_max_hours or {}

        def _default_hours_for_weekday(wd: int) -> float:
            if wd in (goal.rest_days or []):
                return 0.0
            if wd in _daily:
                return float(_daily[wd])
            return float(goal.max_weekend_hours if wd >= 5 else goal.max_weekday_hours)

        if weeks:
            _new_avail: dict = {}
            _d = weeks[0].start
            # PART B (B-LOCKED-1 availability span): on a backdated plan the
            # calendar starts TODAY — elapsed days are not schedulable, so
            # they carry no availability rows. Fresh plans start today anyway.
            if getattr(goal, "start_date", None) and _d < clock.today():
                _d = clock.today()
            _end = weeks[-1].end
            _one = timedelta(days=1)
            while _d <= _end:
                _key = _d.isoformat()
                _old = old_availability_full.get(_key)
                if isinstance(_old, dict) and _old.get("type") in _EXPLICIT_BLOCK:
                    _new_avail[_key] = _old  # preserve explicit holiday/injury/illness
                else:
                    _h = _default_hours_for_weekday(_d.weekday())
                    _new_avail[_key] = {"hours": _h,
                                        "type": "available" if _h > 0 else "rest"}
                _d += _one
            plan_dict["availability"] = _new_avail

        # What the rider was prescribed before this plan stays on record
        # (week-view contract P4, A5): the calendar's history rows and the
        # Last-week card read it through the week view.
        if weeks and isinstance(_existing_plan, dict):
            _replaced = _replaced_weeks_before(
                _existing_plan, min(min(w.start for w in weeks), clock.today()))
            if _replaced:
                plan_dict["replaced_weeks"] = _replaced

        # v2.3.0 — realized training-type distribution (honest, computed from the
        # generated sessions) for the UI's "% distribution" readout.
        plan_dict["realized_bands"] = tp.realized_band_distribution(plan_dict)

        # Phase-split editor (v3.2.0, A1): stamp plan meta with what actually
        # happened to the requested split — "applied" | "fallback:<reason>".
        # The UI badge reads THIS, not the goal (a stored split that fell
        # back must not masquerade as applied).
        _pw_status = (getattr(goal, "_phase_weeks_status", None)
                      or (f"fallback:{_pw_parse_err}" if _pw_parse_err else None))
        if _pw_status:
            plan_dict["phase_weeks_status"] = _pw_status

        # F4c (v2.5.0, D4): a <14-day runway generates a race-week-only
        # MICRO-PLAN (single taper phase — see tp.generate_phases): rest,
        # openers, race; at most one hard touch. Surface a warning in the plan
        # payload so the UI can explain the shape instead of the user
        # wondering where the training block went. Detected via the phase
        # shape (the planner's decision), not a second clock read.
        if (goal.goal_type in ("event", "ctl") and goal.target_date
                and len(phases) == 1 and phases[0].name == "taper"):
            plan_dict["warning"] = (
                f"{goal.event_name or 'Your event'} is under two weeks away — "
                "this is a race-week plan (rest, openers, race), not a "
                "training block. Fitness for the event is already set; "
                "arriving fresh is what's left to win."
            )

        tp.atomic_write_plan(json_path, plan_dict)

        # v1.4.0 — single enrichment helper (replaces the inline duplicate).
        # Adds card_state / card_state_v2 / content_class / display_name /
        # zone_dist / score / protocol / zwo_duration_min per session.
        # v1.6.0 — surface failure under E_ENRICH_FAILED.
        try:
            _enrich_plan_for_response(plan_dict, today_iso=clock.today().isoformat())
        except Exception as _e:
            _log_error(
                error_codes.Codes.ENRICH_FAILED, exc=_e,
                endpoint="/api/plan/generate",
                week_count=len(plan_dict.get("weeks", [])) if isinstance(plan_dict, dict) else 0,
            )

        resp = {"ok": True, "plan_json": plan_dict, "plan_file": str(plan_path)}
        if plan_dict.get("warning"):
            resp["warning"] = plan_dict["warning"]  # F4c: top-level for the UI
        return resp

    except ValueError as e:
        # F4b/F4d (v2.5.0): planner input rejection — target date today/past
        # (D1) or a second priority-A event (SM4). generate_plan raises with a
        # user-facing message; surface it as a 400, not a generic 500.
        _log.warning(f"Plan generate rejected: {e}")
        return JSONResponse({"detail": str(e)}, status_code=400)
    except Exception:
        # SEC3: don't leak exception text to the client. Log the full
        # traceback server-side; return a generic detail for the UI.
        _log.exception("Plan generate failed")
        return JSONResponse({"detail": "Plan update failed"}, status_code=500)


def _maybe_auto_reforecast(profile_id: str, new_rides: int) -> "str | None":
    """v1.0.3 / v1.8.24 — best-effort auto-ADAPT on ride sync / FIT import.

    When ``new_rides > 0`` this routes through the shared ``_apply_plan_update``
    tier-dispatch: a NEW significant absence episode (missed weeks) silently
    rebuilds via regenerate_from_today + recovery ramp (Mujika/Gabbett-safe,
    never a catch-up spike), at most ONCE per absence episode (``gap_regen_latch``
    latch); otherwise it reforecasts (structure-preserving), debounced >5 min.
    The gap/latch decision runs BEFORE the reforecast time-debounce so a fresh
    gap is never suppressed by a recent reforecast. Event-taper windows are
    never silently rebuilt (the helper flags "behind plan" instead).

    Wraps everything in try/except: logs warnings but never raises. Sync /
    import responses must stay clean even if adaptation errors.

    For a continuous plan it then runs the deload advance (monotony / ACWR),
    which /api/today-session used to run on a read (week-view contract P8).
    Returns what it wrote -- the update's action, "deload_advanced", or None.
    """
    written = None
    try:
        json_path = _plan_dir() / "current_plan.json"
        if not json_path.exists():
            return None

        with tp.plan_write_lock():
            # Re-read inside the lock so a concurrent writer's fresher state
            # (reforecast_date / gap_regen_latch) is respected — the latch +
            # debounce are read-modify-write on the plan dict (grill risk 6).
            with open(json_path, encoding="utf-8") as f:
                plan = json.load(f)

            # A MISS IS THE ABSENCE OF A RIDE. Gating this whole chain on
            # new_rides > 0 meant the one thing it most needs to react to was
            # the one thing that skipped it: stop riding and nothing marks the
            # sessions missed, so reconcile never runs, the refit tier is
            # handed an empty missed list, and the week keeps the shape it was
            # born with until the rider notices and presses Update plan by
            # hand. A rider who stops riding is exactly the rider who never
            # presses it. Rides arriving still drive it immediately; otherwise
            # it runs once per day so a day that passed unridden is seen.
            today = clock.today()
            # Rides stored since the last adaptation are new, whoever fetched
            # them: the lazy sync a read kicks off stores rides but may not
            # adapt (P8), and the POST that follows it then finds the sync
            # throttled or already running and reports none added.
            ride_total = len(_load_all_rides_safe())
            seen = plan.get("adapted_ride_total")
            if isinstance(seen, int) and ride_total > seen:
                new_rides = max(new_rides, ride_total - seen)
            stamped = plan.get("reconcile_date") != today.isoformat()
            if new_rides <= 0 and not stamped:
                return None
            plan["reconcile_date"] = today.isoformat()
            plan["adapted_ride_total"] = ride_total

            activities = db.query_activities(days=120)
            training = cached("training", get_today_metrics)

            plan_dict, action, _info, _status = _apply_plan_update(
                plan,
                training=training,
                activities=activities,
                today=today,
                allow_regen=True,
                gap_debounce=True,
                reforecast_min_interval_iso=plan.get("reforecast_date"),
            )
            # "skipped" means the reforecast tier debounced. The daily stamp
            # still has to land, or every sync for the rest of the day re-runs
            # the full chain.
            if action == "skipped" and not stamped:
                return None

            plan_dict["adapted_ride_total"] = ride_total
            tp.atomic_write_plan(json_path, plan_dict)
            # Only the daily stamp landed when the reforecast debounced: nothing
            # the cards show changed, so nothing to repaint.
            written = action if action != "skipped" else None
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
        if _plan_is_continuous(plan):
            try:
                if _maybe_advance_continuous_deload(plan, json_path) and plan.get(
                        "deload_advance", {}).get("advanced_on") == clock.today().isoformat():
                    written = "deload_advanced"
            except Exception as _e:  # noqa: BLE001
                _log.warning(f"continuous deload check failed: {_e}")
    except Exception as e:  # noqa: BLE001
        _log.warning(f"auto-adapt skipped: {e}")
    return written


@app.post("/api/plan/reforecast")
async def api_plan_reforecast():
    """Reforecast the plan based on actual training data."""
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        # Get actual training data from SQLite
        activities = db.query_activities(days=120)

        # Build weekly actual TSS from activities
        actual_weekly = {}
        for a in activities:
            if not a.get("date") or not a.get("tss"):
                continue
            # Find which plan week this activity falls in
            act_date = a["date"]
            for w in plan.get("weeks", []):
                if w["start"] <= act_date <= w["end"]:
                    wk = w["week_num"]
                    actual_weekly[wk] = actual_weekly.get(wk, 0) + (a["tss"] or 0)
                    break

        # Annotate weeks with actual data
        today_str = clock.today().isoformat()
        for w in plan.get("weeks", []):
            wk = w["week_num"]
            w["actual_tss"] = round(actual_weekly.get(wk, 0), 1)
            w["is_past"] = w["end"] < today_str
            w["is_current"] = w["start"] <= today_str <= w["end"]
            if w["is_past"] and w["tss_target"]:
                diff_pct = (w["actual_tss"] - w["tss_target"]) / w["tss_target"] * 100 if w["tss_target"] else 0
                w["adherence_pct"] = round(diff_pct, 1)

        # P2 (v4.1.0) — actually call tp.reforecast() so TSB-triggered
        # future-session downshifts land on disk. Previously this endpoint
        # only annotated actual_tss + surfaced gaps; the "Reforecast" UI
        # button was a silent no-op for intensity.
        training = cached("training", get_today_metrics)
        current_ctl = _planning_ctl()
        current_tsb = training.get("tsb")

        # Flat-TSB projection for every future day unless ICU gives us more.
        # v1.5.0 — keyed by date, computed from plan["weeks"][*]["start"].
        tsb_series = None
        if current_tsb is not None:
            tsb_series = {}
            for w in plan.get("weeks", []) or []:
                try:
                    ws = date.fromisoformat(w["start"])
                except (KeyError, ValueError, TypeError):
                    continue
                for i in range(7):
                    tsb_series[ws + timedelta(days=i)] = current_tsb

        # v4.6.6 WAVE-4-FIX CRITICAL-3: pass recent_activities (G4 ACWR,
        # Gabbett 2016) and current/target polarization (G3 breach,
        # Seiler/Stöggl/Treff) to reforecast(). Pre-fix the UI Reforecast
        # button computed acwr_ratio against an empty list → G4 dead; G3
        # branch never had inputs → polarization breach never fired.
        try:
            today_d = clock.today()
            actual_pol_kwarg = _polarized_actual_from_rides(
                _load_all_rides_safe(), today_d, last_n_days=7,
            )
        except Exception:  # noqa: BLE001
            actual_pol_kwarg = None
        try:
            today_iso_str = today_d.isoformat()
            cur_phase = next(
                (w.get("phase", "") for w in plan.get("weeks", []) or []
                 if (w.get("start", "") or "") <= today_iso_str <= (w.get("end", "") or "")),
                None,
            )
            # J1 (v2.1.0): judge the breach gate against the plan's OWN
            # distribution model, so a pyramidal/threshold plan isn't held to
            # the polarized ceiling.
            _model_targets = tp.polarized_targets(plan.get("goal", {}) or {})
            target_pol_kwarg = _model_targets.get(
                (cur_phase or "").lower(), _model_targets.get("history"))
        except Exception:  # noqa: BLE001
            target_pol_kwarg = None

        # v1.0.3 — plumb plan["availability"] into reforecast so per-day
        # hour overrides actually rescale daily duration / TSS. Sparse:
        # only days the user touched are in the dict; absent days keep
        # current planned duration.
        availability_overrides = {
            day_iso: float(entry["hours"])
            for day_iso, entry in plan.get("availability", {}).items()
            if isinstance(entry, dict) and "hours" in entry
        }

        # v1.5.0 — single-layer reforecast. Replaces the inline PlannedWeek
        # build + tp.reforecast() call + the v1.4.0 propagation helper.
        # Single mutation site (drift class A closure).
        plan, sessions_changed, reforecast_info = tp.reforecast_dict(
            plan,
            today_iso=today_iso_str,
            tsb_series=tsb_series,
            recent_activities=activities,
            actual_polarization=actual_pol_kwarg,
            target_polarization=target_pol_kwarg,
            availability_overrides=availability_overrides,
        )

        # Save updated plan
        plan["reforecast_date"] = clock.now().isoformat()
        plan["last_reforecast_info"] = reforecast_info
        tp.atomic_write_plan(json_path, plan)

        # Also detect gaps (tiered absence detection). v1.5.0: rebuild
        # pw_list from the (now-mutated) plan dict — the previous local
        # pw_list was inlined into tp.reforecast_dict.
        gaps_pw_list = tp._plan_dict_to_planned_weeks(plan)
        # v2.4.0 — gap math counts CYCLING load only; a run/swim must not inflate
        # the week and suppress the recovery ramp.
        gaps = tp.detect_plan_gaps(
            gaps_pw_list,
            [a for a in (activities or []) if _is_cycling_sport(a.get("sport", ""))],
            current_ctl,
        )
        plan["gaps"] = gaps

        return {
            "ok": True, "plan_json": plan, "gaps": gaps,
            "reforecast": reforecast_info,
            "sessions_changed": sessions_changed,
        }

    except Exception:
        # SEC3: don't leak exception text.
        _log.exception("Plan reforecast failed")
        return JSONResponse({"detail": "Plan update failed"}, 500)


@app.post("/api/plan/mark-unavailable")
async def api_mark_unavailable(request: Request):
    """Mark a date range as unavailable (holiday/injury/illness)."""
    body = await _get_json_body(request)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        period = {
            "start": body.get("start"),
            "end": body.get("end"),
            "type": body.get("type", "holiday"),  # holiday, injury, illness
            "daily_tss": body.get("daily_tss", 0),
            "reason": body.get("reason", ""),
        }

        if not period["start"] or not period["end"]:
            return JSONResponse({"error": "start and end dates required"}, 400)
        try:
            date.fromisoformat(period["start"])
            date.fromisoformat(period["end"])
        except (ValueError, TypeError):
            return JSONResponse({"error": "Invalid date format (use YYYY-MM-DD)"}, 400)

        unavailable = plan.get("unavailable_periods", [])
        unavailable.append(period)
        plan["unavailable_periods"] = unavailable

        tp.atomic_write_plan(json_path, plan)

        return {"ok": True, "period": period, "plan_json": plan}

    except Exception:
        # SEC3: don't leak exception text.
        _log.exception("Plan mark-unavailable failed")
        return JSONResponse({"detail": "Plan update failed"}, 500)


@app.post("/api/plan/save-availability")
async def api_save_availability(request: Request):
    """Save the monthly availability calendar data to the plan JSON.

    v1.3.1 HIGH fix: now invokes ``tp.reforecast(availability_overrides=...)``
    so per-day hour overrides actually rescale ``duration_min`` /
    ``tss_estimate`` (and zero out hours=0 sessions) on disk. Pre-fix this
    endpoint only persisted ``plan["availability"]`` — the popover told the
    user to "click Generate Plan" to apply, which was confusing UX.
    """
    body = await _get_json_body(request)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        # body = { "availability": { "2026-04-01": { "hours": 1.5, "type": "available" }, ... } }
        # v1.7.3 — capture prior availability BEFORE overwriting so we can
        # diff incoming vs old and feed only USER-CHANGED days into the
        # reforecast scaler. Pre-v1.7.3 the frontend POSTed every day in
        # the visible calendar (180 days of weekly-grid defaults), so the
        # scaling loop saw the entire window as "overrides" and (a)
        # diluted the targeted day's effect (b) couldn't tell user-intent
        # from auto-fill. v1.7.1 patched the dilution via per-day cap,
        # but the cap is one-way (shrink only) so once a session was
        # capped down the user could not raise it back up — they reported
        # "0 sessions changed" on subsequent updates.
        prior_availability = plan.get("availability", {}) or {}
        new_availability = body.get("availability", {}) or {}
        plan["availability"] = new_availability

        def _hours_of(entry: "dict | None") -> "float | None":
            if not isinstance(entry, dict):
                return None
            h = entry.get("hours")
            try:
                return float(h) if h is not None else None
            except (TypeError, ValueError):
                return None

        # ── v1.3.1 HIGH fix: reflow plan immediately ──────────────────────
        # v1.5.0 — collapsed into tp.reforecast_dict.
        # v1.7.1 — also pass tsb_series + recent_activities so downstream
        # G3 / TSB-based downshifts actually fire after an availability
        # change. Pre-v1.7.1 these were both empty (v1.3.3 perf trade-off
        # against 2× ICU HTTPS per future hard session). v1.6.3 moved ICU
        # sync off the request thread, and ``cached("training", ...)``
        # serves the local snapshot in ~ms, so the perf concern no longer
        # justifies dropping downstream propagation.
        sessions_modified = 0
        try:
            # v1.7.3 — only feed CHANGED days into the scaler. A day's
            # ``hours`` value must differ from the prior plan's entry
            # (or be new) to count as user intent. Days the frontend
            # auto-filled with the weekly-grid default never enter the
            # override set, so they keep the planner's existing duration
            # untouched.
            availability_overrides: dict[str, float] = {}
            for day_iso, entry in new_availability.items():
                new_h = _hours_of(entry)
                if new_h is None:
                    continue
                old_h = _hours_of(prior_availability.get(day_iso))
                if old_h is None or abs(new_h - old_h) > 1e-6:
                    availability_overrides[day_iso] = new_h
            if not availability_overrides:
                # No user edits → nothing to reflow. Persist the new
                # availability dict (might carry type metadata changes
                # like holiday-without-hours-change) and return.
                tp.atomic_write_plan(json_path, plan)
                return {"ok": True, "sessions_modified": 0}

            # Build a flat-projected tsb_series from the cached training
            # snapshot so per-day _tsb_at lookups inside reforecast hit
            # the dict (no fall-through to live ICU). ALWAYS a dict — when
            # ICU is unreachable (current_tsb None) an empty dict makes
            # _tsb_at return None per day; passing None here re-triggered
            # the pre-v1.3.3 bug (one get_today_metrics fetch PER future
            # hard session, ~36 round-trips per UPDATE click).
            tsb_series: dict = {}
            try:
                training_snap = cached("training", get_today_metrics)
                current_tsb = training_snap.get("tsb")
            except Exception:
                current_tsb = None
            if current_tsb is not None:
                for w in plan.get("weeks", []) or []:
                    try:
                        ws = date.fromisoformat(w["start"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    for i in range(7):
                        tsb_series[ws + timedelta(days=i)] = current_tsb
            try:
                recent_activities = db.query_activities(days=120)
            except Exception:
                recent_activities = []

            plan, sessions_modified, _ri = tp.reforecast_dict(
                plan,
                tsb_series=tsb_series,
                recent_activities=recent_activities,
                availability_overrides=availability_overrides,
                # propagation_days=None lets reforecast emit its own
                # touched ∪ g3_dropped set so downstream G3 downshifts
                # land on disk too. Pre-v1.7.1 we pinned this to
                # availability keys only, which suppressed downstream
                # propagation entirely.
            )
        except Exception:  # noqa: BLE001
            # Reflow is best-effort: if reforecast errors we still persist the
            # availability dict so the next /api/plan/reforecast picks it up.
            _log.exception("save-availability reflow skipped")

        tp.atomic_write_plan(json_path, plan)

        return {"ok": True, "sessions_modified": sessions_modified}

    except Exception:
        # SEC3: don't leak exception text.
        _log.exception("Plan save-availability failed")
        return JSONResponse({"detail": "Plan update failed"}, 500)


def _regenerate_plan_dict(
    plan: dict,
    *,
    current_ctl: float,
    activities: "list[dict]",
    seed_salt: int,
) -> "tuple[dict, dict]":
    """v1.8.24 — shared regenerate-from-today core (extracted from
    /api/plan/regenerate so the manual endpoint AND the auto-on-sync path use
    ONE regeneration code path).

    Reconstructs the Goal + PlannedWeek list from ``plan`` (round-tripping ALL
    session fields so regenerate_from_today's v1.8.20 preservation sees the
    rider's edits), runs ``tp.regenerate_from_today`` with the supplied
    ``seed_salt`` (nonzero ⇒ per-call ZWO/HIT variety), and overlays the
    regenerated phases/weeks onto a shallow copy of the original plan so every
    top-level key (availability, reforecast_date, goal, …) survives.

    Returns ``(plan_dict, regen_info)``. Does NOT write to disk — the caller
    persists via ``tp.atomic_write_plan`` so the latch/status writes land in
    the same critical section.
    """
    # Reconstruct Goal via the canonical helper — the old inline build here
    # carried plan_mode/template_id but DROPPED distribution/custom_bands/
    # events/block_periodization/start_date/entry_mode/plan_weeks/
    # available_days/daily_max_hours, so every regen reverted a pyramidal/
    # threshold/custom plan to whatever the sticky process-global model was
    # and lost the B/C race rows + mini-tapers.
    g = plan.get("goal", {})
    goal = _goal_from_plan_dict(g)
    if goal.target_date is None:
        # Non-event goals: keep the legacy regen horizon (12 weeks out).
        goal.target_date = clock.today() + timedelta(weeks=12)
    # The persisted plan_weeks is the GENERATION-time week count; on a rebuild
    # it is stale (weeks_available() short-circuits on plan_weeks>0 — the H1
    # trap), which would split phases into a longer budget than the remaining
    # runway. Zero it so weeks_available derives from target_date, exactly as
    # the pre-fix inline goal (plan_weeks unset) behaved.
    goal.plan_weeks = 0
    # v3.2.0 merge note: with the canonical helper this site now CARRIES
    # phase_weeks — the engine's validity gate (A1) decides applied-vs-
    # fallback against this rebuild's runway; the write-site below mirrors
    # goal._phase_weeks_status instead of clearing a "lossy" stale stamp.
    # v4.6.7 IMPL-CAP: auto-populate endurance baseline if missing.
    if goal.longest_ride_h_90d is None:
        goal.longest_ride_h_90d = _longest_ride_h_90d()

    # Reconstruct PlannedWeek list.
    # v1.8.20 — round-trip ALL session fields (user_moved/status/dismissed_at/
    # completion_matches/…) so regenerate_from_today's preservation logic sees
    # the rider's edits and carries them; the old 8-field reconstruction
    # zeroed them, so every edit was silently wiped.
    old_weeks = []
    for w in plan.get("weeks", []):
        old_weeks.append(tp.week_from_dict(w))

    unavailable = plan.get("unavailable_periods", [])

    # v1.11.0 — thread athlete (ftp + weight) so the event-demand planner can
    # compute event targets on regen too.
    athlete = _planner_athlete()

    # Regenerate (seed_salt forces shuffle variance per call — B3)
    # The rider's chronic load, fetched once: the rebuild's ACWR ceiling and
    # the drift chip's snapshot read the same number.
    _recent_wtss = _chronic_weekly_tss_safe()
    _regen_kwargs = dict(
        goal=goal, old_plan_weeks=old_weeks,
        current_ctl=current_ctl,
        unavailable_periods=unavailable,
        activities=activities,
        seed_salt=seed_salt,
        recent_weekly_tss=_recent_wtss,
    )
    # Pass athlete only if regenerate_from_today accepts it (the kwarg is being
    # added in training_planner concurrently — guard so a stale signature in a
    # narrow build window can't crash the hot ride-sync regen path).
    try:
        import inspect as _inspect
        if "athlete" in _inspect.signature(tp.regenerate_from_today).parameters:
            _regen_kwargs["athlete"] = athlete
    except (ValueError, TypeError):
        pass
    new_phases, all_weeks, regen_info = tp.regenerate_from_today(**_regen_kwargs)

    # Build updated plan JSON.
    # v1.8.20 — START from a shallow copy of the ORIGINAL plan so every
    # top-level key survives (availability calendar, reforecast_date,
    # last_reforecast_info, goal, …); the old fixed-key-set rebuild dropped
    # availability + reforecast_date on every regen. Overlay ONLY the
    # regenerated phases/weeks + regen markers (rebinding the list refs, not
    # mutating the originals). Sessions serialize via the canonical helper
    # (all 22 fields) so preserved edits round-trip intact.
    plan_dict = dict(plan)
    plan_dict["phases"] = [
        {
            "name": p.name, "weeks": p.weeks,
            "start": p.start.isoformat(), "end": p.end.isoformat(),
            "weekly_tss": p.weekly_tss_target, "focus": p.focus,
        }
        for p in new_phases
    ]
    plan_dict["weeks"] = [tp.week_to_dict(w) for w in all_weeks]
    plan_dict["regenerated"] = clock.now().isoformat()
    # 3.3.2 (Lapo #2): a regen is as fresh as a recalc — stamp recalc_date
    # too. The shallow copy carried the OLD stamp verbatim, so a user whose
    # recalc_date was stale (3.3.0 storm history) re-armed the auto-recalc
    # gate on every Plan-tab visit no matter how often they regenerated —
    # the visit then rebuilt (and, pre-fix, taper-flattened) their fresh plan.
    plan_dict["recalc_date"] = clock.now().isoformat()
    plan_dict["regen_info"] = regen_info
    # Phase-split editor (v3.2.0, A1): the shallow copy above would carry a
    # STALE phase_weeks_status from the old plan while the phases were just
    # rebuilt — refresh it from this regen (None ⇒ recommendation ⇒ no key).
    _pws = regen_info.get("phase_weeks_status")
    if _pws:
        plan_dict["phase_weeks_status"] = _pws
    else:
        plan_dict.pop("phase_weeks_status", None)
    # v4.3.0 B3: persist the per-regen entropy salt so external readers
    # (UI, tests) can detect that a fresh regen happened.
    plan_dict["last_regen_at"] = seed_salt
    # P4.2 (v3.0.0) — refresh the drift snapshot: a regen re-anchors the plan
    # on live fitness, so the chip's baseline moves with it.
    plan_dict["ctl_snapshot"] = tp.plan_ctl_snapshot(current_ctl, _recent_wtss)
    return plan_dict, regen_info


def _ramp_band(absence_days: int) -> str:
    """v1.8.24 — recovery-ramp tier band (mirrors build_recovery_ramp's
    duration buckets). Used in the auto-regen episode-latch key so the plan is
    rebuilt only when the band CHANGES, not on every extra missed week."""
    if absence_days <= 14:
        return "s"   # 3-week ramp
    if absence_days <= 28:
        return "m"   # 5-week ramp
    return "l"       # 6-week ramp


def _current_absence_episode(old_weeks, gaps: dict, today: date):
    """v1.8.24 — identify the CURRENT unbroken missed-week run anchored at the
    most-recent COMPLETED plan week. Returns ``(first_week_num, last_week_num)``
    or ``None`` when the most-recent completed week was trained normally (i.e.
    any gap is historical, not current). This is the "recent-gap gate" that
    stops an OLD recovered gap from triggering perpetual auto-regen.
    """
    completed = sorted(
        (w for w in old_weeks if w.end < today), key=lambda w: w.week_num,
    )
    if not completed:
        return None
    gap_nums = {gw["week_num"] for gw in gaps.get("gap_weeks", [])}
    last = completed[-1]
    if last.week_num not in gap_nums:
        return None  # recent week trained normally → no current episode
    first = last.week_num
    i = len(completed) - 1
    while i - 1 >= 0 and completed[i - 1].week_num in gap_nums \
            and completed[i - 1].week_num == completed[i].week_num - 1:
        first = completed[i - 1].week_num
        i -= 1
    return (first, last.week_num)


# Goal.events from a list of event dicts: the codec's reader.
_events_from_dicts = tp._target_events_from_dicts

# The goal block's reader is the codec's (tp.goal_from_dict). It used to be
# written out here and in five other places, each with its own subset of
# fields, and the rider's targets were read by none of them (dupes.md DUP-5).
_goal_from_plan_dict = tp.goal_from_dict

def _parse_custom_bands(raw) -> dict:
    """Validate a custom intensity-distribution payload (v2.3.0). Keeps only the
    four hard-band keys with non-negative numeric values; returns {} when empty
    or invalid so the plan falls back to its named distribution."""
    if not isinstance(raw, dict):
        return {}
    out: dict = {}
    for k in ("tempo_ss", "threshold", "vo2", "sprint"):
        try:
            v = float(raw.get(k, 0) or 0)
        except (TypeError, ValueError):
            v = 0.0
        out[k] = max(0.0, v)
    return out if sum(out.values()) > 0 else {}


def _parse_phase_weeks(raw) -> "tuple[dict | None, str]":
    """Phase-split editor (v3.2.0, A9): accept a dict (POST /api/plan/generate
    body) or ONE URL-encoded JSON object string (GET /api/plan/preview query
    param). Shape-only parse — the engine-side tp.validate_phase_weeks owns
    every rail. Returns (dict|None, reason); reason is non-empty only for an
    unparseable payload (which the endpoints surface as fallback:<reason>)."""
    if raw is None or raw == "" or raw == {}:
        return None, ""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None, "phase_weeks is not valid JSON"
    if not isinstance(raw, dict):
        return None, "phase_weeks must be an object of {phase: weeks}"
    # Evaluator HIGH-2 (round 2): request.json() accepts bare Infinity/NaN;
    # an invalid vector is persisted as-sent for form repopulation, and a
    # non-finite float ANYWHERE in current_plan.json makes every later
    # response serialization 500 (starlette allow_nan=False) — a stored DoS.
    # A top-level isfinite walk missed nested shapes ({"base": [Infinity]}),
    # so probe by serializing the whole payload with allow_nan=False:
    # rejects non-finite at any depth.
    try:
        json.dumps(raw, allow_nan=False)
    except ValueError:
        return None, "phase_weeks contains a non-finite number"
    return dict(raw), ""


def _planner_athlete() -> "dict | None":
    """The rider's FTP and weight for the event-demand planner, or None.

    pm.ftp and pm.weight_kg are default-backed (200 / 70.0), so the raw athlete
    store tells a set value from a fabricated default: a brand-new user with no
    FTP or weight gets None, and the planner then skips the event targets.
    Generate, regenerate and recalculate each assembled this inline; refit did
    not, and planned without it."""
    try:
        from profile_manager import ProfileManager
        _pm = ProfileManager.get()
        _raw = getattr(_pm, "_athlete", {}) or {}
        if _raw.get("ftp") and _raw.get("weight_kg"):
            return {"ftp": _pm.ftp, "weight_kg": _pm.weight_kg}
    except Exception:
        pass
    return None


def _apply_refit_to_plan(plan: dict, today: date) -> "dict | None":
    """v2.0.7 — run the missed-hard week-refit on ``plan`` in place.

    Round-trips the plan's weeks into PlannedWeek DTOs (all session fields, so
    the §6.12 preservation predicate sees the rider's edits), calls
    ``tp.refit_remaining_week`` with a deterministic seed derived from
    (plan_start, week_num, sorted missed dates), and writes the refitted
    remaining-day sessions back into the matching plan-dict week by date.

    Returns the ``refit_info`` dict when at least one day changed, else None
    (caller falls through to the reforecast tier unchanged).
    """
    weeks_json = plan.get("weeks", [])
    if not weeks_json:
        return None
    goal = _goal_from_plan_dict(plan.get("goal", {}) or {})
    dto_weeks: list = []
    for w in weeks_json:
        try:
            dto_weeks.append(tp.week_from_dict(w))
        except (KeyError, ValueError, TypeError):
            return None

    cur = next((w for w in dto_weeks if w.start <= today <= w.end), None)
    if cur is None:
        return None
    missed_dates = sorted(
        s.day.isoformat() for s in cur.sessions
        if getattr(s, "status", "") == "missed" and tp._session_is_hit(s)
        and getattr(s, "day", None)
    )
    # Deterministic seed: stable for a given absence so the latch + seed both
    # keep the refit from re-rolling on repeat syncs (planner is otherwise
    # non-deterministic — see planner-test-nondeterminism memory).
    seed_basis = f"{dto_weeks[0].start.isoformat()}:{cur.week_num}:{','.join(missed_dates)}"
    seed_salt = int(hashlib.sha1(seed_basis.encode()).hexdigest()[:12], 16)

    # Refit's event emphasis needs the rider's FTP and weight; without them it
    # never reached production (the Step 5 review, L2).
    _, refit_info = tp.refit_remaining_week(
        goal, dto_weeks, today, seed_salt=seed_salt, athlete=_planner_athlete(),
    )
    if refit_info.get("action") != "refitted" or not refit_info.get("refit_days"):
        return None

    # Write the changed remaining-day sessions back into the plan dict (match by
    # date within the current week). Only refit_days were mutated.
    changed = set(refit_info["refit_days"])
    by_date = {s.day.isoformat(): s for s in cur.sessions}
    for wj in weeks_json:
        if wj.get("week_num") != cur.week_num:
            continue
        for i, sj in enumerate(wj.get("sessions", [])):
            if sj.get("day") in changed:
                wj["sessions"][i] = _planned_session_to_json(by_date[sj["day"]])
        break
    return refit_info


def _apply_plan_update(
    plan: dict,
    *,
    training: dict,
    activities: "list[dict]",
    today: date,
    allow_regen: bool = True,
    gap_debounce: bool = True,
    reforecast_min_interval_iso: "str | None" = None,
) -> "tuple[dict, str, dict, str]":
    """v1.8.24 — THE single plan-adaptation core. Tier-dispatches:

      • significant CURRENT gap → regenerate_from_today (recovery ramp)
      • otherwise               → reforecast_dict (structure-preserving rebalance)

    Used by both the manual ``/api/plan/update`` button and the ride-sync auto
    path; BOTH pass ``gap_debounce=True`` (see the route at ~9533), so refits are
    latch-gated — at most one silent rebuild per absence EPISODE via
    ``plan["gap_regen_latch"]`` (and the missed-hard tier via
    ``plan["missed_refit_latch"]``).

    Guards (see /tmp/MASTER_DECISIONS_tasks12 §11):
      • REPLACES reforecast — never regen-then-reforecast (mangles the ramp).
      • RECENT-gap gate — an old recovered gap does not trigger a rebuild.
      • EVENT-TAPER guard — inside max(21, taper) days of an event we never
        silently recompute the taper; we reforecast + flag "behind plan".
      • EPISODE latch — key = f"{first_missed_week}:{ramp_band}" so a rebuild
        fires only on a NEW or band-deepened absence, not every sync.

    Returns ``(plan_dict, action, info, status_message)`` where action is
    "rebuilt" | "rebalanced". Does NOT persist — the caller writes inside its
    own plan_write_lock so the latch/status writes are in the same critical
    section.
    """
    now_iso = clock.now().isoformat()
    current_ctl = _planning_ctl()
    current_tsb = training.get("tsb")


    # v1.8.25 — RECONCILE FIRST. Mark the current week's sessions done/missed
    # from actual activities BEFORE adapting, so this happens automatically on
    # every sync / Update-plan (no manual "Reconcile Week" click). Idempotent
    # (dedup by activity_id). Runs before regen/reforecast so the regen path's
    # v1.8.20 preservation carries the freshly-marked done/missed statuses.
    # ``reconciled`` counts what the two bookkeeping passes below changed. The
    # reforecast tier can debounce out and return "skipped", and its caller
    # takes that as "nothing to write" — which silently threw away the misses
    # these passes had just marked, so a rider who synced twice inside five
    # minutes ended the day with a plan that still called every past session
    # pending. Bookkeeping is not reforecast work and must survive that gate.
    reconciled = 0
    try:
        # Before the rematch, so a restored session is judged against the
        # ride that arrived late on its own day.
        reconciled += len(_undo_auto_moves_for_ridden_days(plan, today) or ())
    except Exception:  # noqa: BLE001 — best-effort; never block adapt
        _log.exception("auto-move undo skipped")
    try:
        reconciled += _reconcile_current_week(plan, today)[0] or 0
    except Exception:  # noqa: BLE001 — reconcile is best-effort; never block adapt
        _log.exception("auto-reconcile skipped")

    # v2.3.0 — AUTO-RESCHEDULE missed sessions (replaces the manual "reschedule
    # missed sessions?" banner). Runs AFTER reconcile (misses are marked) and
    # BEFORE gap/refit (those tiers see the already-relocated plan). A relocated
    # session becomes pending, so the missed-hard refit tier won't also
    # redistribute it; misses with no free in-week slot stay missed and fall
    # through to the refit tier as before.
    try:
        reconciled += len(_auto_apply_missed_moves(plan, today) or ())
    except Exception:  # noqa: BLE001 — best-effort; never block adapt
        _log.exception("auto-reschedule skipped")

    # Reconstruct PlannedWeek list for gap detection (full-field round-trip so
    # past statuses are visible to detect_plan_gaps' actual-TSS sum).
    old_weeks = []
    for w in plan.get("weeks", []):
        try:
            old_weeks.append(tp.week_from_dict(w))
        except (KeyError, ValueError, TypeError):
            continue

    # v2.4.0 — cycling-only gap math (a run/swim must not mask a real cycling gap).
    gaps = tp.detect_plan_gaps(
        old_weeks,
        [a for a in (activities or []) if _is_cycling_sport(a.get("sport", ""))],
        current_ctl,
    )
    episode = _current_absence_episode(old_weeks, gaps, today)
    ctl_gap = gaps.get("ctl_gap", 0) or 0
    consecutive = gaps.get("consecutive_missed", 0) or 0

    # needs_regen_now: the ctl-gap branch is inherently CURRENT; the
    # consecutive-missed branch must additionally be a RECENT episode (gate
    # against an old recovered gap nagging forever).
    needs_regen_now = (ctl_gap > 15) or (consecutive >= 2 and episode is not None)

    # EVENT-TAPER guard: never silently recompute an event taper from a
    # background adaptation. Inside the taper window we reforecast + flag.
    taper_blocks = False
    g = plan.get("goal", {})
    if g.get("type") == "event" and g.get("event_date"):
        try:
            days_to_event = (date.fromisoformat(g["event_date"]) - today).days
            taper_window = max(21, int(getattr(tp, "TAPER_DAYS", 12)))
            if 0 <= days_to_event <= taper_window:
                taper_blocks = True
            elif days_to_event < 0:
                # FC5d (v2.5.0, L3-6): the event date has PASSED — never
                # auto-rebuild toward a stale target (post-race CTL decay used
                # to re-fire the ctl-gap regen against it; the regen core now
                # refuses with a ValueError, so block the tier here instead of
                # turning every post-race sync into a 500).
                taper_blocks = True
        except (ValueError, TypeError):
            pass

    # Episode latch key (stable within an absence band; changes on a new/deeper
    # absence). Falls back to a CTL bucket when the gap is ctl-driven only.
    # The band is derived from the CURRENT episode's own span — NOT
    # detect_plan_gaps' global ``absence_days`` (= the longest missed run
    # ANYWHERE in history), which would mis-key the latch whenever an older,
    # longer, already-recovered gap exists.
    if episode is not None:
        episode_weeks = episode[1] - episode[0] + 1
        episode_key = f"{episode[0]}:{_ramp_band(episode_weeks * 7)}"
    else:
        episode_key = f"ctl:{round(ctl_gap / 5.0) * 5}"
    latched = (plan.get("gap_regen_latch", {}) or {}).get("key") == episode_key

    do_regen = (
        needs_regen_now and allow_regen and not taper_blocks
        and not (gap_debounce and latched)
    )

    if do_regen:
        seed_salt = time.time_ns()
        plan_dict, regen_info = _regenerate_plan_dict(
            plan, current_ctl=current_ctl, activities=activities, seed_salt=seed_salt,
        )
        plan_dict["gap_regen_latch"] = {"key": episode_key, "at": now_iso}
        ramp_weeks = regen_info.get("recovery_ramp_weeks", 0) if isinstance(regen_info, dict) else 0
        if ramp_weeks:
            status = (f"Plan rebuilt: {ramp_weeks}-week recovery ramp — "
                      f"you missed {consecutive} week{'s' if consecutive != 1 else ''}.")
        else:
            status = "Plan rebuilt from your current fitness."
        info = {"gaps": gaps, "regen_info": regen_info, "episode_key": episode_key}
        plan_dict["last_update_info"] = {"action": "rebuilt", "message": status, "at": now_iso}
        return plan_dict, "rebuilt", info, status

    # ── missed-hard week-refit tier (v2.0.7, IP_missed_hard_refit.md) ─────────
    # Between the rebuild tier and the plain reforecast: when a HARD session was
    # missed in the CURRENT week and ≥1 remaining trainable day is left, re-fit
    # the remaining days so the missed stimulus is redistributed within the
    # safety guards (no catch-up spike). Latched per absence (key = the sorted
    # missed-date set) so it fires once, not on every sync. A missed EASY
    # session does NOT trigger this — only hard. Falls through to reforecast
    # when no hard miss, no remaining day, already latched, or nothing changed.
    cur_week_dto, _cur_idx = _load_current_week_dto(plan, today)
    if cur_week_dto is not None:
        missed_hard = sorted(
            s.day.isoformat() for s in cur_week_dto.sessions
            if getattr(s, "status", "") == "missed" and tp._session_is_hit(s)
            and getattr(s, "day", None)
        )
        has_remaining = any(
            (s.day >= today
             and getattr(s, "session_type", "") != "rest"
             and not tp._refit_session_frozen(s, today))
            for s in cur_week_dto.sessions if getattr(s, "day", None)
        )
        refit_key = "|".join(missed_hard)
        refit_latched = (plan.get("missed_refit_latch", {}) or {}).get("key") == refit_key
        if missed_hard and has_remaining and not refit_latched:
            try:
                refit_info = _apply_refit_to_plan(plan, today)
            except Exception:  # noqa: BLE001 — refit is best-effort; fall through
                _log.exception("missed-hard refit skipped")
                refit_info = None
            if refit_info:
                plan["missed_refit_latch"] = {"key": refit_key, "at": now_iso}
                miss_n = len(refit_info["missed_dates"])
                day_n = len(refit_info["refit_days"])
                status = (
                    f"Missed {miss_n} hard session{'s' if miss_n != 1 else ''} — "
                    f"refit {day_n} remaining day{'s' if day_n != 1 else ''} this "
                    f"week (within your safety limits).")
                plan["last_update_info"] = {"action": "refitted", "message": status,
                                            "at": now_iso}
                info = {"gaps": gaps, "refit_info": refit_info}
                return plan, "refitted", info, status

    # ── reforecast tier (structure-preserving rebalance) ─────────────────────
    # 5-min debounce applies to THIS tier only (the regen tier above always
    # acts). Prevents reforecast churn on back-to-back ride syncs.
    if reforecast_min_interval_iso:
        try:
            last_dt = datetime.fromisoformat(reforecast_min_interval_iso)
            if (clock.now() - last_dt).total_seconds() < 300:
                # "reconciled" when the bookkeeping passes above actually
                # changed something: the debounce is about reforecast churn,
                # and callers treat only "skipped" as nothing-to-write, so a
                # bare "skipped" here would discard those marks.
                if reconciled:
                    return plan, "reconciled", {"gaps": gaps}, ""
                return plan, "skipped", {"gaps": gaps}, ""
        except (ValueError, TypeError):
            pass

    # v1.0.6 — opportunistic 3D inputs (W'bal / capacity / ACWR). None ⇒
    # TSS-only path (preserves all prior behaviour); only advisory checks read
    # them. Mirrors the pre-v1.8.24 inline extraction in _maybe_auto_reforecast.
    wprime_balance_24h = w_prime = wprime_acwr = None
    try:
        if training.get("wprime_balance_24h") is not None:
            wprime_balance_24h = float(training["wprime_balance_24h"])
        _wp = training.get("w_prime") or training.get("wprime")
        if _wp is not None:
            w_prime = float(_wp)
        if training.get("wprime_acwr") is not None:
            wprime_acwr = float(training["wprime_acwr"])
    except (TypeError, ValueError):
        pass

    tsb_series = None
    if current_tsb is not None:
        tsb_series = {}
        for w in plan.get("weeks", []) or []:
            try:
                ws = date.fromisoformat(w["start"])
            except (KeyError, ValueError, TypeError):
                continue
            for i in range(7):
                tsb_series[ws + timedelta(days=i)] = current_tsb
    availability_overrides = {
        day_iso: float(entry["hours"])
        for day_iso, entry in plan.get("availability", {}).items()
        if isinstance(entry, dict) and "hours" in entry
    }
    plan, modified, reforecast_info = tp.reforecast_dict(
        plan,
        today_iso=today.isoformat(),
        tsb_series=tsb_series,
        recent_activities=activities,
        availability_overrides=availability_overrides,
        wprime_balance_24h=wprime_balance_24h,
        w_prime=w_prime,
        wprime_acwr=wprime_acwr,
    )
    plan["reforecast_date"] = now_iso
    plan["last_reforecast_info"] = reforecast_info
    if taper_blocks and needs_regen_now:
        # Behind plan but inside the event taper — do NOT silently rebuild;
        # surface a banner so the rider decides (manual Update plan still acts).
        status = ("You're behind plan, but your event is close — review and "
                  "rebuild manually if needed (auto-rebuild paused near events).")
        plan["last_update_info"] = {"action": "behind_plan", "message": status,
                                    "at": now_iso, "behind_plan": True}
        info = {"gaps": gaps, "taper_blocked": True}
        return plan, "rebalanced", info, status
    # Every mutator in the reforecast tier is downward-only and none of them
    # read status=="missed", so an absence lands here and changes nothing —
    # yet the rider was told "Plan rebalanced to today's fitness", which reads
    # as "we adjusted for the days you missed" when the plan is byte-identical.
    # Say which of the two actually happened.
    if modified or reconciled:
        status = "Plan rebalanced to today's fitness."
    else:
        status = "Plan checked — no change needed."
    plan["last_update_info"] = {"action": "rebalanced", "message": status,
                                "at": now_iso, "modified": bool(modified)}
    info = {"gaps": gaps, "reforecast_info": reforecast_info}
    return plan, "rebalanced", info, status


@app.post("/api/plan/regenerate")
async def api_plan_regenerate_dynamic(request: Request):
    """Regenerate plan from today after missed training/injury/holiday.

    Science: Mujika 2000/2001/2003, Gabbett 2016, Gundersen 2016.

    v4.3.0 B3: now mints a fresh ``seed_salt = time.time_ns()`` on each call
    and writes it into ``plan["last_regen_at"]``. The salt is forwarded into
    ``regenerate_from_today`` → ``plan_week`` → ``_pick_session`` and
    ``match_zwo`` so consecutive regenerations produce visibly different
    HIT-variant + ZWO-pick distributions.

    v1.8.24 — body delegates to the shared ``_regenerate_plan_dict`` core.
    """
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)

    try:

        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        # v4.3.0 B3: per-regen entropy salt. time.time_ns() gives us a fresh
        # 19+ bit-of-entropy nonce per call — guaranteed different across two
        # back-to-back POSTs on any modern OS clock.
        seed_salt = time.time_ns()

        # Get current CTL
        training = cached("training", get_today_metrics)
        current_ctl = _planning_ctl()

        activities = db.query_activities(days=120)

        plan_dict, regen_info = _regenerate_plan_dict(
            plan, current_ctl=current_ctl, activities=activities, seed_salt=seed_salt,
        )

        tp.atomic_write_plan(json_path, plan_dict)

        return {"ok": True, "plan_json": plan_dict, "regen_info": regen_info}

    except ValueError as e:
        # FC5d (v2.5.0, L3-6): planner input rejection (event date passed) —
        # user-facing message, 400 not 500. Mirrors /api/plan/generate (F4b).
        _log.warning(f"Plan regenerate rejected: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, 500)


@app.post("/api/plan/add-race")
async def api_plan_add_race(request: Request):
    """IP-1 — add a B/C race to the EXISTING plan and re-periodize forward.

    Appends the race to the plan's ``goal.events`` then runs the shared regenerate
    core (``regenerate_from_today``), which re-applies the B/C mini-taper (trim
    volume, keep intensity) + recovery around the race and preserves past weeks.
    RESHAPES existing weeks — does NOT change the plan length or the A-event date.
    """
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)
    try:
        body = await request.json()
    except Exception:
        body = {}
    ev_date = str((body or {}).get("date") or "").strip()
    if not ev_date:
        return JSONResponse({"error": "date required (YYYY-MM-DD)"}, status_code=400)
    try:
        d = date.fromisoformat(ev_date)
    except ValueError:
        return JSONResponse({"error": "date must be YYYY-MM-DD"}, status_code=400)
    if d < clock.today():
        return JSONResponse({"error": "race date is in the past"}, status_code=400)
    prio = str((body or {}).get("priority") or "B").upper()
    if prio not in ("B", "C"):
        prio = "B"
    new_event = {
        "date": ev_date,
        "priority": prio,
        "name": str((body or {}).get("name") or "").strip(),
        "event_km": (body or {}).get("km") or (body or {}).get("event_km") or 0,
        "event_climb": (body or {}).get("climb") or (body or {}).get("event_climb") or 0,
        "event_type": str((body or {}).get("type") or (body or {}).get("event_type") or "crit"),
    }
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
        goal = plan.setdefault("goal", {})
        events = [e for e in (goal.get("events") or []) if (e or {}).get("date") != ev_date]
        events.append(new_event)
        goal["events"] = events

        seed_salt = time.time_ns()
        training = cached("training", get_today_metrics)
        current_ctl = _planning_ctl()
        activities = db.query_activities(days=120)
        plan_dict, regen_info = _regenerate_plan_dict(
            plan, current_ctl=current_ctl, activities=activities, seed_salt=seed_salt,
        )
        tp.atomic_write_plan(json_path, plan_dict)
        return {"ok": True, "plan_json": plan_dict, "regen_info": regen_info, "added": new_event}
    except ValueError as e:
        # FC5d (v2.5.0, L3-6): the shared regen core refuses a passed A-event
        # date — surface it as a 400 with the user-facing message.
        _log.warning(f"Plan add-race rejected: {e}")
        return JSONResponse({"error": str(e)}, status_code=400)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/plan/update")
async def api_plan_update(debounce: int = Query(0)):
    """v1.8.24 — the ONE primary "Update plan" action.

    Tier-dispatches through the shared ``_apply_plan_update`` core: a current
    significant absence → regenerate_from_today + recovery ramp; otherwise a
    structure-preserving reforecast. Latch-debounced so re-clicking does not
    reshuffle future workout picks for the same already-handled absence (the
    advanced "Rebuild from scratch" → /api/plan/regenerate is the explicit
    force-rebuild). Returns the new plan + a human status message.

    v1.8.25 — ``debounce=1`` (used by the auto Plan-open catch-up sequence) also
    debounces the REFORECAST tier on ``reforecast_date`` (>5 min), so reopening
    the tab repeatedly is a cheap no-op instead of re-running reforecast every
    time. The manual "Update plan" button omits it (always acts).
    """
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)
    try:
        with tp.plan_write_lock():
            with open(json_path, encoding="utf-8") as f:
                plan = json.load(f)
            training = cached("training", get_today_metrics)
            activities = db.query_activities(days=120)
            plan_dict, action, info, status = _apply_plan_update(
                plan,
                training=training,
                activities=activities,
                today=clock.today(),
                allow_regen=True,
                gap_debounce=True,
                reforecast_min_interval_iso=(plan.get("reforecast_date") if debounce else None),
            )
            if action == "skipped":
                # reforecast tier debounced (<5 min) — nothing changed; return the
                # plan as-is so the UI still renders, with a no-op action.
                return {"ok": True, "action": "skipped", "status_message": "",
                        "plan_json": plan, "gaps": (info or {}).get("gaps")}
            tp.atomic_write_plan(json_path, plan_dict)
        try:
            _enrich_plan_for_response(plan_dict, today_iso=clock.today().isoformat())
        except Exception:  # noqa: BLE001
            pass
        return {"ok": True, "action": action, "status_message": status,
                "plan_json": plan_dict,
                "gaps": (info or {}).get("gaps"),
                "regen_info": (info or {}).get("regen_info")}
    except Exception:
        _log.exception("Plan update failed")
        return JSONResponse({"detail": "Plan update failed"}, 500)


# ═══════════════════════════════════════════════════════════════════════════════
# FIT WORKOUT EXPORT (Garmin/Wahoo) — uses Garmin FIT SDK
# ═══════════════════════════════════════════════════════════════════════════════

def build_fit_workout_bytes(session_type: str, duration_min: int,
                            name: str, zwo_file: str | None = None,
                            view: str | None = None, cap: int = 0) -> bytes:
    """Generate FIT workout bytes. Shared by ``/api/export/fit-workout`` AND
    ``launcher.JsApi.save_fit`` (issue #5) so the desktop native-save path can
    produce the bytes IN-PROCESS, with no WKWebView ``fetch()`` that can resolve
    with an empty body.

    v1.0.1: session_type normalised so the planner form ("sweetspot") and the
    dashboard <select> values ("sweet_spot") route to the same branch.
    v1.0.3: when ``zwo_file`` is supplied, transcode that exact ZWO into FIT so
    the FIT body matches what the rider sees in MyWhoosh / Tacx / Zwift.

    task #24: ``cap=1`` (PROMPT APPROVE) or the profile "on" toggle caps the
    ZWO's short reps to the rider's measured-power envelope BEFORE transcode
    (only for a power FIT -- hr view wins, bpm prescriptions untouched).

    Raises ImportError if fit_tool is missing, FileNotFoundError if ``zwo_file``
    is named but absent, ValueError if the result has zero workout steps.
    """
    from fit_tool.fit_file_builder import FitFileBuilder  # noqa: F401 (availability check)

    # view override (modal ⚡/❤ toggle): what you SEE is what the FIT gets.
    # 'hr' only honoured with a sane LTHR (same invariant as the settings gate).
    hr_force: bool | None = None
    if view == "hr":
        from profile_manager import ProfileManager
        _pm = ProfileManager.get()
        hr_force = bool(_pm.lthr_is_set and _pm.max_hr > _pm.lthr)
    elif view == "power":
        hr_force = False

    ftp = config.ATHLETE_FTP_W
    if zwo_file:
        zwo_path = _safe_path(active_workout_dir(), zwo_file)
        if not zwo_path or not zwo_path.exists():
            raise FileNotFoundError(f"ZWO file not found: {zwo_file}")
        # task #24: cap the ZWO text pre-transcode when active + power FIT.
        # hr_force=True means an hr FIT is being built -> no cap (hr wins).
        cap_text = None
        if hr_force is not True:
            from profile_manager import ProfileManager
            _pmc = ProfileManager.get()
            if _capacity_cap_active(_pmc, force=bool(cap)):
                capped = _cap_zwo_bytes(zwo_path.read_bytes(),
                                        Path(zwo_file).name, _pmc)
                cap_text = capped.decode("utf-8", "replace")
        fit_data = _build_fit_workout_from_zwo(
            name, zwo_path, ftp, hr_force=hr_force, zwo_text=cap_text)
    else:
        st_norm = (session_type or "").lower().replace("_", "").replace("-", "")
        blocks = []
        warmup = min(10, duration_min // 8)
        cooldown = min(5, duration_min // 12)
        main_min = duration_min - warmup - cooldown
        blocks.append({"name": "Warmup", "min": warmup, "pctLow": 45, "pctHigh": 65, "intensity": "warmup"})
        if st_norm == "vo2max":
            reps = min(5, max(3, main_min // 7))
            for i in range(reps):
                blocks.append({"name": f"VO2 {i+1}", "min": 4, "pctLow": 106, "pctHigh": 115, "intensity": "active"})
                if i < reps - 1:
                    blocks.append({"name": "Rest", "min": 3, "pctLow": 40, "pctHigh": 40, "intensity": "rest"})
        elif st_norm == "threshold":
            blocks.append({"name": "FTP 1", "min": 20, "pctLow": 95, "pctHigh": 100, "intensity": "active"})
            blocks.append({"name": "Rest", "min": 5, "pctLow": 50, "pctHigh": 50, "intensity": "rest"})
            blocks.append({"name": "FTP 2", "min": 20, "pctLow": 95, "pctHigh": 100, "intensity": "active"})
        elif st_norm == "sweetspot":
            for i in range(3):
                blocks.append({"name": f"SS {i+1}", "min": 15, "pctLow": 88, "pctHigh": 93, "intensity": "active"})
                if i < 2:
                    blocks.append({"name": "Rest", "min": 5, "pctLow": 55, "pctHigh": 55, "intensity": "rest"})
        elif st_norm == "overunder":
            for i in range(3):
                for j in range(4):
                    blocks.append({"name": "Over", "min": 2, "pctLow": 105, "pctHigh": 105, "intensity": "active"})
                    blocks.append({"name": "Under", "min": 1, "pctLow": 90, "pctHigh": 90, "intensity": "active"})
                if i < 2:
                    blocks.append({"name": "Rest", "min": 5, "pctLow": 50, "pctHigh": 50, "intensity": "rest"})
        else:
            blocks.append({"name": session_type, "min": main_min, "pctLow": 56, "pctHigh": 75, "intensity": "active"})
        blocks.append({"name": "Cooldown", "min": cooldown, "pctLow": 60, "pctHigh": 40, "intensity": "cooldown"})
        fit_data = _build_fit_workout(name, blocks, ftp, hr_force=hr_force)

    if not fit_data:
        raise ValueError("FIT generation produced no data")
    return fit_data


@app.get("/api/export/fit-workout")
def api_export_fit_workout(
    session_type: str = Query("z2"),
    duration_min: int = Query(75),
    name: str = Query("Workout"),
    zwo_file: str | None = Query(None),
    view: str | None = Query(None),  # 'hr'|'power' — modal toggle: WYSIWYG FIT
    cap: int = Query(0),  # task #24: 1 = PROMPT APPROVE (cap short reps)
):
    """Generate a FIT binary workout file for Garmin Edge / Wahoo ELEMNT / Karoo.

    Thin wrapper around build_fit_workout_bytes() (issue #5 extracted the
    generation so the desktop native-save bridge can reuse it in-process).
    """
    from fastapi.responses import Response
    try:
        fit_data = build_fit_workout_bytes(session_type, duration_min, name, zwo_file, view=view, cap=cap)
    except ImportError as ie:
        return JSONResponse({"error": f"fit_tool not installed. Run: pip install fit_tool. Detail: {ie}"}, 500)
    except FileNotFoundError as fe:
        return JSONResponse({"error": str(fe)}, 404)
    except ValueError as ve:
        return JSONResponse({"error": str(ve)}, 422)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, 500)
    # Sanitize aggressively — any char outside [A-Za-z0-9_.-] becomes '_'.
    # The prior quote-only escape accepted CR/LF, which would let a user inject
    # extra Content-Disposition headers into the response.
    safe_name = re.sub(r'[^\w.\-]', '_', name) or "workout"
    return Response(
        content=fit_data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.fit"'},
    )


def _fit_apply_hr_target(step, pct_lo: float, pct_hi: float, dur_s: float,
                         step_name: str, lthr: int, max_hr: int,
                         hr_rows_override: dict | None = None,
                         intensity_kind: str = "active") -> None:
    """Set one FIT workout step's target for hr target_mode (IP_HR_ONLY C8).

    Guidable segments (per hr_targets.power_target_to_hr) get a real
    HEART_RATE custom target. FIT encodes custom_target_heart_rate_* as
    bpm + 100 (values ≤100 mean %HRmax) — verified against fit_tool, which
    stores the raw field without applying the offset itself. RPE segments get
    an OPEN target (duration-only step) with the RPE cue in the step name so
    the head unit shows guidance text instead of an unreachable bpm.

    Red-team D4: recovery/cooldown/Z1 steps get NO bpm floor (low = 1 bpm).
    HR decays slowly after hard work and idles low on easy days — a floored
    range makes the head unit beep "HR too low" at a rider who is doing it
    right. The CEILING is the useful half ("keep it under X"); keep only it.
    """
    from fit_tool.profile.profile_type import WorkoutStepTarget
    from hr_targets import power_target_to_hr

    tgt = power_target_to_hr(pct_lo, pct_hi, dur_s, lthr, max_hr,
                             hr_rows_override=hr_rows_override)
    no_floor = intensity_kind in ("rest", "cooldown") or tgt.get("zone") == 1
    if tgt["kind"] == "hr":
        lo = 1 if no_floor else tgt["bpm_low"]
        step.target_type = WorkoutStepTarget.HEART_RATE
        step.custom_target_heart_rate_low = lo + 100
        step.custom_target_heart_rate_high = tgt["bpm_high"] + 100
        step.target_value = 0  # 0 = custom target (not a zone)
        step.workout_step_name = step_name[:15]
    elif tgt["kind"] == "hr_ramp":
        # FIT has no HR ramp; target the span (device shows a range to move
        # through). Callers that staircase ramps pass flat sub-steps instead.
        lo = min(tgt["bpm_start"], tgt["bpm_end"])
        hi = max(tgt["bpm_start"], tgt["bpm_end"])
        if no_floor:
            lo = 1  # cooldown: ceiling only — HR decay can't be paced downward
        step.target_type = WorkoutStepTarget.HEART_RATE
        step.custom_target_heart_rate_low = lo + 100
        step.custom_target_heart_rate_high = max(hi, lo + 1) + 100
        step.target_value = 0
        step.workout_step_name = step_name[:15]
    else:  # rpe
        step.target_type = WorkoutStepTarget.OPEN
        rpe = (f"RPE {tgt['rpe_low']}" if tgt["rpe_low"] == tgt["rpe_high"]
               else f"RPE {tgt['rpe_low']}-{tgt['rpe_high']}")
        step.workout_step_name = rpe[:15]


def _build_fit_workout(name: str, blocks: list[dict], ftp: int,
                       hr_force: bool | None = None) -> bytes:
    """Build a proper FIT binary workout file using fit_tool.

    Produces a valid .fit file readable by Garmin Edge, Wahoo ELEMNT, and other ANT+ devices.
    Workout steps use absolute power targets in watts (calculated from %FTP).
    ``hr_force`` overrides the profile target_mode (modal ⚡/❤ toggle).
    """
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.workout_message import WorkoutMessage
    from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
    from fit_tool.profile.profile_type import FileType, Manufacturer, Sport, Intensity, WorkoutStepDuration, WorkoutStepTarget

    builder = FitFileBuilder()

    # File ID
    file_id = FileIdMessage()
    file_id.type = FileType.WORKOUT
    # DEVELOPMENT (255) is the honest, spec-correct manufacturer id for an
    # unregistered third-party app like Domestique. Using a real brand's id
    # (garmin/zwift/peaksware) would be spoofing. If it turns out an importer
    # truly rejects development-stamped workouts, the correct fix is to register
    # a manufacturer id with Garmin FIT — not to impersonate another product.
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = 12345
    # time_created is REQUIRED by TrainingPeaks / Vekta / Garmin Connect — a
    # workout FIT without it imports as EMPTY (the reported bug). Value is ms
    # since the Unix epoch; fit_tool converts to the FIT epoch on encode.
    file_id.time_created = int(clock.now().timestamp() * 1000)
    builder.add(file_id)

    # Workout header
    workout = WorkoutMessage()
    workout.workout_name = name
    workout.sport = Sport.CYCLING
    workout.num_valid_steps = len(blocks)
    builder.add(workout)

    # Workout steps
    INTENSITY_MAP = {
        "active": Intensity.ACTIVE,
        "rest": Intensity.REST,
        "warmup": Intensity.WARMUP,
        "cooldown": Intensity.COOLDOWN,
    }

    _hr_on = _fit_hr_mode() if hr_force is None else hr_force
    hr_mode = _fit_hr_params() if _hr_on else None
    for i, b in enumerate(blocks):
        step = WorkoutStepMessage()
        step.message_index = i
        step.duration_type = WorkoutStepDuration.TIME
        # Seconds, through the sub-field: fit_tool applies the field's 1000x
        # scale itself, so a value pre-multiplied by 1000 decoded as 1000 h.
        step.duration_time = b["min"] * 60
        step.intensity = INTENSITY_MAP.get(b.get("intensity", "active"), Intensity.ACTIVE)
        # v2.4.2 — every step needs a name. Garmin's own canonical workout files
        # (and TrainingPeaks / Vekta) expect wkt_step_name on each step; without it
        # strict importers report "no workout in this file". Cap to 15 chars.
        step_name = str(b.get("name") or "Step")

        if hr_mode:
            # hr target_mode (IP_HR_ONLY C8): guidable steps carry a real FIT
            # HEART_RATE target; RPE steps go OPEN (duration only) with the cue
            # in the step name — no false bpm to chase on a sprint.
            # Red-team F1: block pctLow/pctHigh is a ramp ONLY for warmup/
            # cooldown; for work blocks it's a POWER BAND (VO2 106-115 etc.) —
            # feeding a band as a ramp made 4-min VO2 steps a 1-bpm pinned
            # threshold target. Bands convert as steady at the band top (the
            # zone-defining end, same hard_pct semantic as the converter).
            _kind = b.get("intensity", "active")
            if _kind in ("warmup", "cooldown"):
                _p_start, _p_end = b["pctLow"], b["pctHigh"]
            else:
                _p_start = _p_end = max(b["pctLow"], b["pctHigh"])
            _fit_apply_hr_target(step, _p_start, _p_end,
                                 b["min"] * 60, step_name, *hr_mode,
                                 intensity_kind=_kind)
        else:
            step.target_type = WorkoutStepTarget.POWER
            # v2.4.3 — %FTP targets, not absolute watts. FIT workout_power encodes a
            # value <1000 as % of FTP and >=1000 as (watts + 1000). %FTP is the
            # portable form (the workout scales to the athlete's FTP on the device /
            # in TrainingPeaks, matching ZWO's relative model) and is what structured-
            # workout importers expect. pctLow/pctHigh are already percentages.
            step.custom_target_power_low = max(1, round(b["pctLow"]))
            step.custom_target_power_high = max(1, round(b["pctHigh"]))
            step.target_value = 0  # 0 = custom target (not a zone)
            step.workout_step_name = step_name[:15]

        builder.add(step)

    # Build and return binary FIT data
    fit_file = builder.build()
    return fit_file.to_bytes()


def _build_fit_workout_from_zwo(name: str, zwo_path: Path, ftp: int,
                                hr_force: bool | None = None,
                                zwo_text: str | None = None) -> bytes:
    """v1.0.3 fix-forward(workout-detail UX): transcode a ZWO file directly
    into a FIT workout. The previous code path keyed only on
    ``(session_type, duration_min)`` so the FIT body could be a generic Tempo
    block while the ZWO download was the matched library file (a ladder, an
    over-under, etc.). When both files exist for the same session they MUST
    represent the same workout.

    task #24: when ``zwo_text`` is supplied (the measured-capacity-capped body)
    it is parsed instead of the file on disk, so the FIT matches the capped ZWO
    the rider sees. ``zwo_path`` is still used for the not-found error message.

    Element coverage:
    - ``<SteadyState Duration=N Power=P>`` → 1 step at duration_time=N s,
      power = ftp * P (watts).
    - ``<Warmup>`` / ``<Ramp>`` → 1 step at the segment's average power
      (most head units don't render true ramps; average is sufficient).
    - ``<Cooldown>`` → 1 step at min(low,high) end power. Same convention as
      v4.6.8 fix in the visualiser.
    - ``<IntervalsT Repeat=N OnDuration=A OnPower=B OffDuration=C OffPower=D>``
      → expanded into 2*N alternating on/off steps.
    - ``<FreeRide Duration=N>`` → single step at 50% FTP (ZWO doesn't pin a
      target; pick a sensible default so the head unit shows something).

    The fit_tool import stays inside the function so PyInstaller's
    hiddenimports list (which already references the same names as
    `_build_fit_workout`) keeps working unchanged.
    """
    from fit_tool.fit_file_builder import FitFileBuilder
    from fit_tool.profile.messages.file_id_message import FileIdMessage
    from fit_tool.profile.messages.workout_message import WorkoutMessage
    from fit_tool.profile.messages.workout_step_message import WorkoutStepMessage
    from fit_tool.profile.profile_type import (
        FileType, Manufacturer, Sport, Intensity, WorkoutStepDuration, WorkoutStepTarget,
    )

    if zwo_text is not None:
        root = ET.fromstring(zwo_text)
    else:
        root = ET.parse(zwo_path).getroot()
    workout_el = root.find("workout")

    # hr target_mode (IP_HR_ONLY): decided ONCE up front because it changes how
    # ramps are expanded below — the power-mode 30 s staircase would put every
    # ramp sub-step under HR_MIN_SEG_S and wrongly degrade whole warmups to RPE,
    # so in hr mode a ramp stays ONE step spanning its HR range.
    # ``hr_force`` (modal ⚡/❤ toggle) overrides the profile target_mode.
    _hr_on = _fit_hr_mode() if hr_force is None else hr_force
    hr_mode = _fit_hr_params() if _hr_on else None

    # (intensity, duration_seconds, power_start_fraction, power_end_fraction)
    # start/end are IN TIME ORDER (equal for flat steps).
    raw_steps: list[tuple[str, int, float, float]] = []
    if workout_el is not None:
        for el in workout_el:
            tag = el.tag
            dur = int(float(el.get("Duration", 0) or 0))
            if tag == "SteadyState":
                pw = float(el.get("Power", 0.65))
                raw_steps.append(("active", dur, pw, pw))
            elif tag in ("Warmup", "Ramp", "Cooldown"):
                # v1.8.17 — FIT workout steps have no native power RAMP, so the
                # pre-v1.8.17 code collapsed each Warmup/Ramp/Cooldown to ONE
                # flat step at the average power. That made the FIT a visibly
                # different workout from the ZWO: a 0.65→1.05 ramp (diagonal
                # sawtooth in MyWhoosh/Zwift) became a flat 0.85 block on
                # Garmin/Wahoo. Fix: STAIRCASE the ramp into ~30 s sub-steps
                # that step linearly from PowerLow to PowerHigh, so the FIT
                # power profile matches the ZWO ramp shape.
                plo = float(el.get("PowerLow", 0.5))
                phi = float(el.get("PowerHigh", 0.7))
                intensity = "warmup" if tag == "Warmup" else (
                    "cooldown" if tag == "Cooldown" else "active")
                if dur <= 0:
                    pass
                elif hr_mode:
                    # One un-staircased step, direction in time order (the
                    # chart's v2.0.6 rule: Warmup up, Cooldown down, Ramp as
                    # authored). power_target_to_hr handles the endpoint map.
                    lo, hi = min(plo, phi), max(plo, phi)
                    if tag == "Cooldown":
                        p_start, p_end = hi, lo
                    elif tag == "Warmup":
                        p_start, p_end = lo, hi
                    else:
                        p_start, p_end = plo, phi
                    raw_steps.append((intensity, dur, p_start, p_end))
                else:
                    # 1 sub-step per ~30 s, capped 1..8 so short ramps stay
                    # cheap and long ones still read as a smooth ramp.
                    n_sub = max(1, min(8, round(dur / 30)))
                    base = dur // n_sub
                    rem = dur - base * n_sub
                    for i in range(n_sub):
                        sub_dur = base + (1 if i < rem else 0)
                        if sub_dur <= 0:
                            continue
                        # Mid-point power of this sub-step along the ramp.
                        frac = (i + 0.5) / n_sub
                        p = plo + (phi - plo) * frac
                        raw_steps.append((intensity, sub_dur, p, p))
            elif tag == "IntervalsT":
                reps = int(el.get("Repeat", 1))
                on_d = int(float(el.get("OnDuration", 0) or 0))
                off_d = int(float(el.get("OffDuration", 0) or 0))
                on_p = float(el.get("OnPower", 0.95))
                off_p = float(el.get("OffPower", 0.50))
                for _ in range(reps):
                    raw_steps.append(("active", on_d, on_p, on_p))
                    raw_steps.append(("rest", off_d, off_p, off_p))
            elif tag == "FreeRide":
                # v3.11.3: a FreeRide is an OPEN step — no target at all. The old
                # "default to 50% FTP so the head unit isn't blank" turned the
                # 20-min / 60-min FTP-test blocks into a 50%-of-FTP prescription
                # on Garmin/Wahoo — worse than erg-locking at the old FTP.
                raw_steps.append(("active", dur, None, None))
            # Anything else (e.g. <textevent>) is ignored.

    INTENSITY_MAP = {
        "active": Intensity.ACTIVE,
        "rest": Intensity.REST,
        "warmup": Intensity.WARMUP,
        "cooldown": Intensity.COOLDOWN,
    }

    # issue #5 — a ZWO with no recognized workout elements would otherwise yield
    # a header-only FIT (num_valid_steps=0) that a head unit silently rejects.
    # Fail loudly so callers return an error instead of a useless 0-step file.
    if not raw_steps:
        raise ValueError(f"ZWO produced no workout steps: {zwo_path.name}")

    builder = FitFileBuilder()
    file_id = FileIdMessage()
    file_id.type = FileType.WORKOUT
    # DEVELOPMENT (255) is the honest, spec-correct manufacturer id for an
    # unregistered third-party app like Domestique. Using a real brand's id
    # (garmin/zwift/peaksware) would be spoofing. If it turns out an importer
    # truly rejects development-stamped workouts, the correct fix is to register
    # a manufacturer id with Garmin FIT — not to impersonate another product.
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = 12345
    # REQUIRED by TrainingPeaks / Vekta — without time_created the workout
    # imports as empty. ms since Unix epoch; fit_tool converts to FIT epoch.
    file_id.time_created = int(clock.now().timestamp() * 1000)
    builder.add(file_id)

    workout = WorkoutMessage()
    workout.workout_name = name
    workout.sport = Sport.CYCLING
    workout.num_valid_steps = len(raw_steps)
    builder.add(workout)

    for i, (intensity_kind, dur_s, p_start, p_end) in enumerate(raw_steps):
        step = WorkoutStepMessage()
        step.message_index = i
        step.duration_type = WorkoutStepDuration.TIME
        step.duration_time = dur_s  # seconds; fit_tool applies the 1000x scale
        step.intensity = INTENSITY_MAP.get(intensity_kind, Intensity.ACTIVE)
        # v2.4.2 — name every step (Garmin canonical / TrainingPeaks / Vekta expect
        # wkt_step_name; missing names → "no workout in this file" on import).
        step_name = {
            "warmup": "Warm up", "cooldown": "Cool down",
            "rest": "Recovery", "active": "Work",
        }.get(intensity_kind, "Work")

        if p_start is None:
            # FreeRide → OPEN target (duration only), in power AND hr mode: the
            # rider is measuring their FTP here, not riding a number.
            step.target_type = WorkoutStepTarget.OPEN
            step.workout_step_name = "Free ride"
            builder.add(step)
            continue
        if hr_mode:
            # hr target_mode (IP_HR_ONLY C8): HEART_RATE targets where HR can
            # guide, OPEN + RPE-in-name where it can't (short / supra-threshold).
            _fit_apply_hr_target(step, p_start * 100, p_end * 100, dur_s,
                                 step_name, *hr_mode,
                                 intensity_kind=intensity_kind)
        else:
            step.target_type = WorkoutStepTarget.POWER
            step.workout_step_name = step_name
            # v2.4.3 — %FTP targets (value <1000 = % of FTP; >=1000 = watts+1000).
            # %FTP is the portable form importers expect + scales to the athlete's FTP.
            # p_start/p_end are FTP fractions (0.65 = 65%), so ×100 → percent
            # (equal for flat steps — power mode never emits an unstaircased ramp).
            step.custom_target_power_low = max(1, round(p_start * 100))
            step.custom_target_power_high = max(1, round(p_end * 100))
            step.target_value = 0  # 0 = custom target (not a zone)
        builder.add(step)

    fit_file = builder.build()
    return fit_file.to_bytes()


# ═══════════════════════════════════════════════════════════════════════════════
# ROLLING PLAN AUTO-RECALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

# The plan codec lives with the dataclasses (training_planner.session_to_dict
# and friends); these names stay because handlers and tests call them.
_planned_session_from_json = tp.session_from_dict
_planned_session_to_json = tp.session_to_dict


def _load_current_week_dto(plan: dict, today: date):
    """Build a PlannedWeek DTO from plan JSON for the week containing `today`.

    Returns (planned_week, idx) or (None, -1) if no current week found.
    §6.12 hazard: the DTO MUST round-trip status/user_moved/moved_from/
    completion_matches/dismissed_at — otherwise regen/projection discards them.
    v1.8.20: routes through ``_planned_session_from_json`` (all 22 fields).
    """
    weeks_data = plan.get("weeks", [])
    for i, w in enumerate(weeks_data):
        try:
            w_start = date.fromisoformat(w["start"])
            w_end = date.fromisoformat(w["end"])
        except (KeyError, ValueError):
            continue
        if w_start <= today <= w_end:
            return tp.week_from_dict(w), i
    return None, -1


_NON_CYCLING_HINTS = ("run", "swim", "walk", "hike", "row", "climb", "ski",
                      "skat", "yoga", "weight", "strength", "workout", "elliptical",
                      "kayak", "canoe", "surf", "golf", "tennis", "soccer", "football")


def _is_cycling_sport(sport) -> bool:
    """True if an activity should reconcile against a planned (cycling) session.

    Cycling sport_type values (Strava/ICU): Ride, VirtualRide, GravelRide,
    MountainBikeRide, EBikeRide, Handcycle, Velomobile, … — anything with
    ride/bike/cycl. Empty/unknown sport is treated as cycling (local FIT rides
    often carry no sport tag, and Domestique is a cycling app). A clearly
    non-cycling activity (RockClimbing, Run, Swim, …) returns False so it is
    NOT matched to a cycling session — it never pollutes the plan.
    """
    s = (sport or "").strip().lower()
    if not s:
        return True
    if any(k in s for k in ("ride", "bike", "cycl", "velomobile", "handcycle")):
        return True
    return not any(k in s for k in _NON_CYCLING_HINTS)


def _collect_week_activities(current_week, today: date, include_today: bool = False):
    """Gather actual activities within [week_start, today) (or today+1 if include_today).

    Dedups by (date, rounded TSS / 5). Returns dicts rich enough for the
    rematch classifier (intensity_factor, duration_min, id passed through).
    Non-cycling activities (rock climbing, runs, …) are excluded so they never
    match a planned cycling session.
    """
    week_start_iso = current_week.start.isoformat()
    upper_iso = (today + timedelta(days=1)).isoformat() if include_today else today.isoformat()
    actual = []
    seen_keys: list = []

    def _add(a: dict):
        if not _is_cycling_sport(a.get("sport", "")):
            return  # non-cycling (rock climbing, run, …) — don't reconcile as a ride
        # An activities row is not the shape the classifier reads: it carries
        # duration_sec and keeps icu_intensity inside raw_json. Read as it
        # was, a ridden session scored 0 min and no intensity, was marked
        # missed, and the auto-reschedule moved it onto the next day
        # (2026-09-15).
        raw = {}
        if isinstance(a.get("raw_json"), str):
            try:
                raw = json.loads(a["raw_json"])
            except ValueError:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        d = (a.get("date") or a.get("start_date_local", "") or "")[:10]
        if not (week_start_iso <= d < upper_iso):
            return
        tss = float(a.get("tss") or a.get("icu_training_load") or raw.get("icu_training_load") or 0)
        moving_s = a.get("moving_time") or raw.get("moving_time") or a.get("duration_sec") or 0
        dur_min = float(a.get("duration_min") or (moving_s or 0) / 60 or 0)
        if_ = a.get("intensity_factor") or a.get("icu_intensity") or raw.get("icu_intensity")
        real_load = tss > 0
        if tss <= 0:
            # A ride stored without a load (intervals.icu had no FTP for it,
            # or never analysed it) is still a ride: estimated from duration
            # and intensity where there is one, else kept at 0 TSS so its
            # duration counts. Before, it was dropped and its day was missed.
            try:
                f = float(if_) if if_ is not None else 0.0
                f = f / 100 if f > 3 else f
            except (TypeError, ValueError):
                f = 0.0
            tss = round(dur_min / 60 * f * f * 100, 1) if f > 0 else 0.0
            if dur_min <= 0:
                return
        # v2.4.0 — dedup key includes DURATION so two genuinely-different same-day
        # rides (e.g. a short hard session + a long commute) with near-equal TSS
        # are NOT collapsed; cross-source copies of the SAME ride share
        # date+tss+duration and still dedup.
        # Two identical commutes are two rides, though: copies of one ride
        # start within seconds of each other, commutes hours apart. Rows whose
        # start times agree within two minutes and whose durations agree are
        # one ride, whatever their loads (a copy stored without one is
        # estimated, or 0); the copy with a real load is the one kept. Without
        # start times, rows in the same date/TSS/duration buckets are one.
        key = (d, round(tss / 5) * 5, round(dur_min / 5) * 5)
        start = None
        try:
            start = datetime.fromisoformat(str(raw.get("start_date_local") or a.get("start_date_local") or "")[:19])
        except ValueError:
            start = None
        entry = {
            "date": d,
            "tss": tss,
            "duration_min": dur_min,
            "intensity_factor": if_,
            "id": a.get("id") or a.get("icu_id"),
            "sport": a.get("sport", ""),
        }
        for seen in seen_keys:
            if start is not None and seen["start"] is not None:
                same = (abs((start - seen["start"]).total_seconds()) <= 120
                        and abs(dur_min - seen["dur"]) <= max(5.0, 0.1 * max(dur_min, seen["dur"])))
            else:
                same = seen["key"] == key
            if same:
                if real_load and not seen["real"]:
                    actual[seen["idx"]] = entry
                    seen.update(real=True, key=key)
                return
        seen_keys.append({"key": key, "start": start, "dur": dur_min, "real": real_load, "idx": len(actual)})
        actual.append(entry)

    try:
        from db import query_activities
        days_since_start = (today - current_week.start).days + 2
        for a in query_activities(days=max(7, days_since_start)):
            _add(a)
    except Exception as e:
        _log.debug(f"ICU activities fetch failed: {e}")
    try:
        from ride_storage import list_rides
        for r in list_rides():
            _add(r)
    except Exception as e:
        _log.debug(f"Local rides fetch failed: {e}")
    return actual


@app.get("/api/plan/daily-adapt")
def api_plan_daily_adapt():
    """Daily-adapt PROJECTION (fix26 §6.1) — read-only preview.

    *** THIS ENDPOINT NEVER WRITES TO current_plan.json. ***

    Returns what a legacy daily-adapt pass *would* have done as
    `projected_adaptations[]`. The UI branches on
    `info.projection_only === true` and surfaces a non-intrusive hint.

    Writes now require explicit user action via /api/plan/move-session
    or /api/plan/rematch?apply=1.
    """

    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return {"action": "no_plan", "projection_only": True}

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        today = clock.today()
        current_week, _idx = _load_current_week_dto(plan, today)
        if not current_week:
            return {"action": "no_current_week", "projection_only": True}

        actual = _collect_week_activities(current_week, today, include_today=False)
        _unchanged, info = tp.daily_adapt_plan(current_week, actual, today)
        info["projection_only"] = True  # defensive: re-mark in case caller strips
        return info

    except Exception:
        # SEC3: don't leak exception text.
        _log.exception("Plan daily-adapt projection failed")
        return {"action": "error", "detail": "Plan projection failed", "projection_only": True}


def _apply_move_session(
    plan: dict, src_iso: str, dst_iso: str, reset_status_to_pending: bool = False,
    auto: bool = False,
) -> dict | None:
    """Relocate the session at ``src_iso`` to ``dst_iso`` within ``plan['weeks']``
    (in place). Same-ISO-week move only. Returns the moved-session payload, or
    None if the move is invalid / the source session isn't found. Does NOT
    persist — the caller writes the plan. Mirrors the /api/plan/move-session
    mutation so both the endpoint and the auto-reschedule path share one impl.

    ``reset_status_to_pending`` forces the relocated session to ``pending`` (used
    by the auto-reschedule path: a rescheduled session is no longer "missed", so
    the missed-hard refit tier won't also redistribute it).

    FC5a (v2.5.0, L3-1): ``auto=True`` marks the relocated session with the
    ``auto_moved`` provenance field and NEVER ``user_moved`` — that pin is
    reserved for actual user drags (v1.8.20 move-persistence). The old shared
    flag self-immunized auto-moves: a missed hard auto-relocated onto race eve
    became user_moved=True, which froze it against refit/regen/reforecast.

    FC3 (v2.5.0, L3-2/L3-10): a race entry is never moved, and nothing is ever
    moved ONTO a race day — returns None (the endpoint 400s before calling).
    """
    try:
        src_d = date.fromisoformat(src_iso)
        dst_d = date.fromisoformat(dst_iso)
    except ValueError:
        return None
    if src_iso == dst_iso:
        return None
    if src_d.isocalendar()[:2] != dst_d.isocalendar()[:2]:
        return None

    weeks_data = plan.get("weeks", [])

    def _find_week_idx(target: date) -> int | None:
        for i, w in enumerate(weeks_data):
            try:
                w_start = date.fromisoformat(w["start"])
                w_end = date.fromisoformat(w["end"])
            except (KeyError, ValueError):
                continue
            if w_start <= target <= w_end:
                return i
        return None

    src_week_idx = _find_week_idx(src_d)
    dst_week_idx = _find_week_idx(dst_d)
    if src_week_idx is None or dst_week_idx is None:
        return None

    src_week = weeks_data[src_week_idx]
    dst_week = weeks_data[dst_week_idx]
    src_sessions = src_week.get("sessions", [])
    dst_sessions = dst_week.get("sessions", [])
    src_session = next((s for s in src_sessions if s.get("day") == src_iso), None)
    if not src_session:
        return None
    dst_session = next((s for s in dst_sessions if s.get("day") == dst_iso), None)

    # FC3 (v2.5.0): the race entry is immutable — not movable, not overwritable.
    if src_session.get("is_race") or (dst_session or {}).get("is_race"):
        return None

    moved_payload = {k: v for k, v in src_session.items()}
    moved_payload["day"] = dst_iso
    moved_payload["day_name"] = (
        dst_session["day_name"] if dst_session else dst_d.strftime("%a")
    )
    if auto:
        # FC5a: auto-relocations carry their own provenance and never the
        # user pin (a prior user_moved on the source is superseded — the
        # session no longer sits where the user put it).
        moved_payload["user_moved"] = False
        moved_payload["auto_moved"] = True
    else:
        moved_payload["user_moved"] = True
        moved_payload["auto_moved"] = False
    moved_payload["moved_from"] = src_iso
    if reset_status_to_pending or moved_payload.get("status", "pending") == "pending":
        moved_payload["status"] = "pending"
    elif moved_payload.get("status") == "missed" and dst_d >= clock.today():
        # Dragged onto a day still to come: owed there, not missed; left
        # "missed", the auto-reschedule moved it off the day it was put on.
        moved_payload["status"] = "pending"
    if auto:
        # What the move displaced, so a ride that turns up later on the
        # source day can undo it (_undo_auto_moves_for_ridden_days). Kept at
        # plan level: session fields outside the planner's DTO do not survive
        # a reforecast.
        plan.setdefault("auto_moves", []).append({
            "from": src_iso, "to": dst_iso, "at": clock.now().isoformat(),
            "displaced": json.loads(json.dumps(dst_session)) if dst_session else None,
        })
        plan["auto_moves"] = plan["auto_moves"][-20:]

    new_src = []
    for s in src_sessions:
        if s.get("day") == src_iso:
            new_src.append({
                "day": src_iso, "day_name": src_session["day_name"],
                "session_type": "rest", "duration_min": 0,
                "tss_estimate": 0, "description": f"Moved to {dst_iso}",
                "zwo_file": "", "zwo_name": "",
                "status": f"moved_from:{dst_iso}",
                "user_moved": False, "moved_from": "",
                "completion_matches": None, "dismissed_at": "",
            })
        elif src_week_idx == dst_week_idx and s.get("day") == dst_iso:
            new_src.append(moved_payload)
        else:
            new_src.append(s)
    if src_week_idx == dst_week_idx:
        if not any(s.get("day") == dst_iso for s in new_src):
            new_src.append(moved_payload)
        src_week["sessions"] = new_src
    else:
        src_week["sessions"] = new_src
        new_dst = []
        placed = False
        for s in dst_sessions:
            if s.get("day") == dst_iso:
                new_dst.append(moved_payload)
                placed = True
            else:
                new_dst.append(s)
        if not placed:
            new_dst.append(moved_payload)
        dst_week["sessions"] = new_dst

    plan["last_move"] = {"date": src_iso, "new_date": dst_iso, "at": clock.now().isoformat()}
    return moved_payload


def _compute_missed_suggestions(plan: dict, today: date) -> list[dict]:
    """Propose same-ISO-week reschedule slots for missed sessions (the six-rule
    first-fit from MASTER §1). Pure over the given plan dict; shared by the
    /api/plan/missed-suggestions endpoint and the auto-reschedule path."""
    goal = plan.get("goal", {}) or {}
    rest_days_idx = set(int(d) for d in goal.get("rest_days", []) or [])
    avail_days_raw = goal.get("available_days")
    if avail_days_raw is None:
        avail_days_idx = set(d for d in range(7) if d not in rest_days_idx)
    else:
        avail_days_idx = set(int(d) for d in avail_days_raw)
    availability = plan.get("availability", {}) or {}

    try:
        classifications = tp._load_content_classifications() or {}
    except Exception:
        classifications = {}
    _missed_lib_by_file: dict[str, dict] = {}
    try:
        for _w in tp.load_workout_library():
            _fname = _w.get("File")
            if _fname:
                _missed_lib_by_file[_fname] = _w
    except Exception:
        _missed_lib_by_file = {}

    sess_by_day: dict[str, dict] = {}
    for w in plan.get("weeks", []) or []:
        for s in w.get("sessions", []) or []:
            day_iso = s.get("day", "")
            if day_iso:
                sess_by_day[day_iso] = s

    # FC5a (v2.5.0, L3-1): collect A/B event dates — a HARD session must never
    # be auto-moved into the T-2..T+0 freshness window of any of them (that is
    # exactly the window every taper/eve pass protects; the audit saw a missed
    # vo2max relocated onto race eve and then self-immunized there).
    event_days: list[date] = []
    _ev_iso = goal.get("event_date")
    if _ev_iso:
        try:
            event_days.append(date.fromisoformat(str(_ev_iso)[:10]))
        except (TypeError, ValueError):
            pass
    for _e in goal.get("events") or []:
        if not isinstance(_e, dict):
            continue
        if str(_e.get("priority", "B") or "B").upper() not in ("A", "B"):
            continue
        try:
            event_days.append(date.fromisoformat(str(_e.get("date"))[:10]))
        except (TypeError, ValueError):
            continue

    def _is_available_slot(d_iso: str, missed_iso: str,
                           missed_sess: "dict | None" = None,
                           easy_takeover: bool = False) -> bool:
        try:
            d = date.fromisoformat(d_iso)
        except ValueError:
            return False
        if d < today:
            return False
        if d_iso == missed_iso:
            return False
        try:
            missed_d = date.fromisoformat(missed_iso)
        except ValueError:
            return False
        if d.isocalendar()[:2] != missed_d.isocalendar()[:2]:
            return False
        wd = d.weekday()
        if wd not in avail_days_idx:
            return False
        if wd in rest_days_idx:
            return False
        # FC5a (v2.5.0, L3-1): no HARD destination inside T-2..T+0 of an A/B
        # event. Openers are exempt — a short touch ride is what belongs there.
        if (missed_sess is not None and tp._session_is_hit(missed_sess)
                and not missed_sess.get("is_opener")):
            for ed in event_days:
                if 0 <= (ed - d).days <= 2:
                    return False
        entry = availability.get(d_iso)
        if isinstance(entry, dict) and entry.get("type") == "unavailable":
            return False
        sess = sess_by_day.get(d_iso)
        if sess is None:
            return False
        stype = sess.get("session_type")
        if stype != "rest":
            # The first-fit allowed only empty rest slots, so a rider whose
            # week had none lost the missed session entirely — dropped without
            # a word, and the week rode out on its easy back-half. The
            # redistribution evidence (SCIENCE.md) supports exactly one
            # narrower move: ONE missed HARD session may take over a remaining
            # EASY day. A move, never an addition; the displaced easy volume
            # is written off, because missed z2 is not worth compensating and
            # intensity is what preserves adaptation (Hickson 1985). Easy
            # misses never take over anything.
            if not easy_takeover:
                return False
            if stype not in ("recovery", "z2", "endurance", "long_z2"):
                return False
            # Only a day still being ridden can be taken over. The generic
            # status checks below reject done/moved/dismissed but let
            # "missed" through — and a missed easy day is exactly the day
            # the rider is NOT riding; landing the quality there loses it
            # twice over.
            if (sess.get("status") or "pending") != "pending":
                return False
            # >=48h from any other hard day (the evidence's spacing bound,
            # approximated as no hard neighbour on the adjacent days). A
            # missed or dismissed neighbour does not count — it will not be
            # ridden.
            for nb in (d - timedelta(days=1), d + timedelta(days=1)):
                nb_sess = sess_by_day.get(nb.isoformat())
                if (nb_sess and tp._session_is_hit(nb_sess)
                        and (nb_sess.get("status") or "pending")
                        not in ("missed", "dismissed")):
                    return False
        stat = (sess.get("status") or "")
        if stat in ("done", "done_partial", "ambiguous"):
            return False
        if stat.startswith("moved_from:"):
            return False
        if sess.get("user_moved") is True:
            return False
        if sess.get("dismissed_at"):
            return False
        return True

    misses: list[dict] = []
    for w in plan.get("weeks", []) or []:
        for s in w.get("sessions", []) or []:
            if (s.get("status") or "") != "missed":
                continue  # (missed_race is terminal — never a candidate, L3-2)
            if s.get("is_race"):
                continue  # FC3: the race entry itself is never rescheduled
            if not s.get("day", ""):
                continue
            misses.append(s)
    misses.sort(key=lambda s: s.get("day", ""))

    used: set[str] = set()
    takeover_weeks: set = set()   # at most ONE easy-day takeover per ISO week
    suggestions: list[dict] = []
    for miss in misses:
        missed_iso = miss.get("day", "")
        try:
            missed_d = date.fromisoformat(missed_iso)
        except ValueError:
            continue
        iso_year, iso_week = missed_d.isocalendar()[:2]
        same_week_dates: list[str] = []
        for d_iso in sess_by_day.keys():
            try:
                dd = date.fromisoformat(d_iso)
            except ValueError:
                continue
            if dd.isocalendar()[:2] == (iso_year, iso_week):
                same_week_dates.append(d_iso)
        same_week_dates.sort()

        chosen: str | None = None
        chosen_reason: str = ""
        for cand_iso in same_week_dates:
            if cand_iso in used:
                continue
            if not _is_available_slot(cand_iso, missed_iso, missed_sess=miss):
                continue
            chosen = cand_iso
            cand_sess = sess_by_day.get(cand_iso, {})
            chosen_reason = "rest_slot" if cand_sess.get("session_type") == "rest" else "unfilled_available_day"
            break
        if chosen is None and tp._session_is_hit(miss) \
                and (iso_year, iso_week) not in takeover_weeks:
            # Second chance for a missed HARD session in a week with no free
            # rest slot: take over an easy day. Capped at one per week so a
            # badly-missed week never converts its whole back half to
            # intensity — the cap comes from the same evidence as the move.
            for cand_iso in same_week_dates:
                if cand_iso in used:
                    continue
                if not _is_available_slot(cand_iso, missed_iso,
                                          missed_sess=miss,
                                          easy_takeover=True):
                    continue
                chosen = cand_iso
                chosen_reason = "easy_day_takeover"
                takeover_weeks.add((iso_year, iso_week))
                break
        if chosen is None:
            continue

        used.add(chosen)
        try:
            suggested_day_name = date.fromisoformat(chosen).strftime("%a")
        except ValueError:
            suggested_day_name = ""
        sess_type = miss.get("session_type", "")
        dur = int(miss.get("duration_min", 0) or 0)
        summary_bits = []
        if sess_type:
            summary_bits.append(sess_type)
        if dur:
            summary_bits.append(f"{dur}min")
        summary = ", ".join(summary_bits) if summary_bits else (miss.get("description") or "")
        _missed_dn, _missed_zdur = _session_naming_lookup(
            miss.get("zwo_file") or "", classifications, _missed_lib_by_file,
        )
        suggestions.append({
            "missed_date": missed_iso,
            "missed_session_type": sess_type,
            "missed_summary": summary,
            "missed_display_name": _missed_dn,
            "missed_zwo_duration_min": _missed_zdur,
            "suggested_date": chosen,
            "suggested_day_name": suggested_day_name,
            "reason": chosen_reason,
        })
    return suggestions


def _undo_auto_moves_for_ridden_days(plan: dict, today: date) -> list[dict]:
    """Undo an auto-move whose source day turns out to have been ridden.

    A ride can reach intervals.icu a day late (the phone syncs the head unit
    the next afternoon). By then the session was marked missed and moved, and
    the ride landed on a rest stub nothing matches. When the source day now
    carries enough riding to count (the rematch rules: two of three axes, or
    ``_ridden_status``) and the moved session is still unridden where it was
    put, the session goes back to its day and the destination gets back what
    it displaced. The next rematch then judges the session against the ride.
    """
    records = plan.get("auto_moves") or []
    if not records:
        return []
    sessions = {s.get("day"): (w, s) for w in plan.get("weeks", []) or []
                for s in w.get("sessions", []) or []}
    undone, keep = [], []
    for rec in records:
        src, dst = rec.get("from", ""), rec.get("to", "")
        src_w, src_s = sessions.get(src, (None, None))
        dst_w, dst_s = sessions.get(dst, (None, None))
        still_moved = (src_s is not None and dst_s is not None
                       and str(src_s.get("status") or "") == f"moved_from:{dst}"
                       and dst_s.get("auto_moved") and dst_s.get("moved_from") == src)
        if not still_moved:
            continue                      # superseded by later edits: forget it
        if str(dst_s.get("status") or "pending") not in ("pending", "missed"):
            keep.append(rec)              # ridden where it was put: the move stands
            continue
        try:
            src_d = date.fromisoformat(src)
            week, _ = _load_current_week_dto(plan, src_d)
            acts = [a for a in _collect_week_activities(week, today, include_today=True)
                    if a.get("date") == src] if week else []
            probe = tp.session_from_dict({**dst_s, "day": src, "status": "pending"})
            judged = [{**tp.classify_rematch(probe, a), "activity": a} for a in acts]
            ridden = any(j["matched_axes"] >= 2 for j in judged) or (
                bool(judged) and tp._ridden_status(judged, tp._session_planned_if(probe)) is not None)
        except Exception:  # noqa: BLE001 - undo is best effort; the move stands
            _log.exception("auto-move undo: judging %s failed", src)
            keep.append(rec)
            continue
        if not ridden:
            keep.append(rec)
            continue
        restored = {k: v for k, v in dst_s.items()}
        restored.update({"day": src, "day_name": src_s.get("day_name", restored.get("day_name")),
                         "status": "pending", "auto_moved": False, "moved_from": ""})
        src_w["sessions"][src_w["sessions"].index(src_s)] = restored
        back = rec.get("displaced") or {
            "day": dst, "day_name": dst_s.get("day_name", ""), "session_type": "rest",
            "duration_min": 0, "tss_estimate": 0, "description": "Rest", "zwo_file": "",
            "zwo_name": "", "status": "pending"}
        dst_w["sessions"][dst_w["sessions"].index(dst_s)] = back
        undone.append({"from": dst, "to": src})
    plan["auto_moves"] = keep
    return undone


def _auto_apply_missed_moves(plan: dict, today: date) -> list[dict]:
    """Automatically relocate missed sessions to their suggested same-week slots
    (replaces the old manual "reschedule missed sessions?" banner). Self-limiting:
    a relocated session becomes ``pending`` and its origin a rest stub, so a
    re-run finds nothing to move; a genuinely new miss (re-marked by reconcile)
    is the only thing that triggers another move. Returns the applied moves."""
    try:
        suggestions = _compute_missed_suggestions(plan, today)
    except Exception:
        _log.exception("auto-reschedule: suggestion compute failed")
        return []
    # Only misses in the plan week containing today move on their own; an
    # older week's are history. Reconcile also judges the previous plan week,
    # and with weeks that are not Monday-Sunday its last day shares an ISO
    # week with today, so a miss there was moved into the next plan week.
    current = next((w for w in plan.get("weeks", []) or []
                    if str(w.get("start", "")) <= today.isoformat() <= str(w.get("end", ""))), None)
    if current is None:
        return []
    suggestions = [x for x in suggestions
                   if str(current["start"]) <= str(x.get("missed_date", "")) <= str(current["end"])
                   and str(current["start"]) <= str(x.get("suggested_date", "")) <= str(current["end"])]
    applied: list[dict] = []
    for s in suggestions:
        src = s.get("missed_date")
        dst = s.get("suggested_date")
        if not src or not dst:
            continue
        moved = _apply_move_session(plan, src, dst, reset_status_to_pending=True,
                                    auto=True)  # FC5a: auto_moved, never user_moved
        if moved:
            applied.append({"from": src, "to": dst})
    return applied


@app.post("/api/plan/move-session")
async def api_plan_move_session(request: Request):
    """Move a session from one date to another (fix26 §6.2).

    Body: {"date": "YYYY-MM-DD", "new_date": "YYYY-MM-DD"}

    - Sets `user_moved=True` on the session at its new slot.
    - Records `moved_from=<original iso date>`.
    - The vacated slot becomes a rest with `status=moved_from:<new_date>`.
    - §6.12: regenerate_from_today honors user_moved=True — never re-prescribed.
    """
    body = await _get_json_body(request)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)

    src_iso = str(body.get("date", "")).strip()
    dst_iso = str(body.get("new_date", "")).strip()
    if not src_iso or not dst_iso:
        return JSONResponse({"error": "date and new_date required"}, 400)
    try:
        date.fromisoformat(src_iso)
        date.fromisoformat(dst_iso)
    except ValueError:
        return JSONResponse({"error": "Invalid date format (use YYYY-MM-DD)"}, 400)
    if src_iso == dst_iso:
        return JSONResponse({"error": "date and new_date must differ"}, 400)

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        # v4.1.1 FIX-PICKER-MOVE (Bug E): week-boundary check now uses ISO
        # Mon-Sun — NOT the iterate-stored-weeks approach. The old check
        # required BOTH src and dst to be inside the same stored week,
        # but stored weeks in current_plan.json may use legacy Fri-Thu
        # boundaries (or Sat-Fri on older plans), while the dashboard
        # UI renders the week as Mon-Sun (src/week_view.py).
        # That meant Tue 04-21 → Fri 04-24 (same ISO week 17) was
        # rejected because they sat in adjacent stored Fri-Thu slices.
        # Root cause: comparison axis mismatch between UI (ISO) and
        # handler (stored).  We now validate on ISO week and let the
        # writer below thread each date into whichever stored week
        # actually contains it.
        src_d = date.fromisoformat(src_iso)
        dst_d = date.fromisoformat(dst_iso)
        src_iso_week = src_d.isocalendar()[:2]  # (iso_year, iso_week)
        dst_iso_week = dst_d.isocalendar()[:2]
        if src_iso_week != dst_iso_week:
            return JSONResponse(
                {"error": "move must stay within the current ISO week (Mon-Sun)"},
                400,
            )

        weeks_data = plan.get("weeks", [])

        def _find_week_idx(target: date) -> int | None:
            for i, w in enumerate(weeks_data):
                try:
                    w_start = date.fromisoformat(w["start"])
                    w_end = date.fromisoformat(w["end"])
                except (KeyError, ValueError):
                    continue
                if w_start <= target <= w_end:
                    return i
            return None

        src_week_idx = _find_week_idx(src_d)
        dst_week_idx = _find_week_idx(dst_d)

        # v4.3.0 B1 — Lazy-load + 422 fix.
        # Symptom: user dragged a session within the visible Mon-Sun week and
        # got a cryptic "no stored week contains source date" toast.
        # Root cause: ``plan["weeks"]`` may have been written by a stale planner
        # whose week boundaries don't cover the source date (legacy Sat-Fri vs
        # the new Mon-Sun grid the UI shows). When that happens we re-read the
        # plan straight from disk (a sibling process or a recent regen may have
        # written newer weeks) and retry. If that STILL fails we return 422
        # with a structured error code so the UI can show a clean toast
        # ("Couldn't find that session. Try refreshing.") instead of the raw
        # 404 string.
        if src_week_idx is None or dst_week_idx is None:
            try:
                with open(json_path, encoding="utf-8") as f:
                    plan_disk = json.load(f)
                weeks_data = plan_disk.get("weeks", [])
                src_week_idx = _find_week_idx(src_d)
                dst_week_idx = _find_week_idx(dst_d)
                if src_week_idx is not None or dst_week_idx is not None:
                    # Lazy-load found something — swap the in-memory plan to
                    # the disk version so all downstream writes see it.
                    plan = plan_disk
            except (OSError, json.JSONDecodeError) as _e:
                _log.debug(f"move-session lazy-reload failed: {_e}")

        if src_week_idx is None:
            return JSONResponse(
                {"error": "source_session_not_found", "date": src_iso}, 422
            )
        if dst_week_idx is None:
            return JSONResponse(
                {"error": "target_week_not_found", "date": dst_iso}, 422
            )

        # FC3 (v2.5.0, L3-2/L3-10): the race entry is goal-owned — a drag must
        # not move it, and nothing may be dropped ONTO it. The sanctioned write
        # path for the race is the goal-level add/edit-race flow.
        for w in plan.get("weeks", []) or []:
            for s in w.get("sessions", []) or []:
                if s.get("day") in (src_iso, dst_iso) and s.get("is_race"):
                    return JSONResponse(
                        {"error": "race day is fixed — edit the race instead"},
                        400,
                    )

        # Mutation is shared with the auto-reschedule path (_apply_move_session)
        # so both apply the identical same-week relocation + rest-stub rewrite.
        moved_payload = _apply_move_session(plan, src_iso, dst_iso)
        if moved_payload is None:
            return JSONResponse(
                {"error": "source_session_not_found", "date": src_iso}, 422
            )

        tp.atomic_write_plan(json_path, plan)

        return {"ok": True, "moved": moved_payload, "vacated": src_iso}

    except Exception:
        _log.exception("Plan move-session failed")
        return JSONResponse({"detail": "Move failed"}, 500)


@app.get("/api/plan/missed-suggestions")
def api_plan_missed_suggestions():
    """v1.0.3 — propose same-week reschedule slots for missed sessions.

    Read-only. Walks ``plan["weeks"]`` for sessions with ``status=="missed"``,
    finds same-ISO-week candidate slots that satisfy all six rules in
    MASTER §1, and emits at most one suggestion per missed session via
    greedy first-fit by ``missed_date`` ascending.

    Acceptance happens client-side: the dashboard POSTs the existing
    ``/api/plan/move-session`` with ``{date: missed_date,
    new_date: suggested_date}``. No new mutation endpoint.
    """
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return {"suggestions": []}

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        # Delegates to the shared suggestion builder (also used by the
        # auto-reschedule path in _apply_plan_update).
        return {"suggestions": _compute_missed_suggestions(plan, clock.today())}

    except Exception:
        _log.exception("missed-suggestions failed")
        return {"suggestions": []}


# ═══════════════════════════════════════════════════════════════════════════════
# v4.3.0 — Calendar overlay (B4 + B5 + B6)
# ═══════════════════════════════════════════════════════════════════════════════

# Z-zone bucket boundaries (% FTP) for the calendar's
# z1z2 / z3z4 / z5+ split. Matches Coggan zones from zones.py:
#   Z1+Z2 = 0..75% FTP   (active recovery + endurance)
#   Z3+Z4 = 76..105% FTP (tempo + sweetspot + threshold)
#   Z5+   = 106%+ FTP    (vo2max, anaerobic, neuromuscular)
_CAL_LOW_PCT  = 0.755   # max for "z1z2" bucket
_CAL_MID_PCT  = 1.055   # max for "z3z4" bucket; above = z5+


def _ride_zone_split_minutes(ride: dict, ftp: int | None) -> tuple[float, float, float]:
    """Return (z1z2_min, z3z4_min, z5plus_min) for a ride.

    Strategy (in order of preference):
      1. ``ride["zones"]["power"]`` (ICU/FIT-imported with zone-time present) —
         maps Coggan Z1..Z2 → z1z2, Z3..Z4 → z3z4, Z5..Z7 → z5plus directly.
      2. Fall back to ``ride["samples"]["power"]`` against the supplied FTP
         (per MASTER §3 — "If FIT lacks RR/zone-time, compute z1/.../z5+ from
         power-time array using FTP from profile").
      3. If both fail, return (0, 0, 0).
    """
    zones_block = (ride.get("zones") or {}).get("power") or {}
    if zones_block:
        z1 = float(zones_block.get("Z1") or 0)
        z2 = float(zones_block.get("Z2") or 0)
        z3 = float(zones_block.get("Z3") or 0)
        z4 = float(zones_block.get("Z4") or 0)
        z5 = float(zones_block.get("Z5") or 0)
        z6 = float(zones_block.get("Z6") or 0)
        z7 = float(zones_block.get("Z7") or 0)
        if (z1 + z2 + z3 + z4 + z5 + z6 + z7) > 0:
            return (
                round((z1 + z2) / 60.0, 1),
                round((z3 + z4) / 60.0, 1),
                round((z5 + z6 + z7) / 60.0, 1),
            )

    # Fallback: power-time integration.
    samples = (ride.get("samples") or {}).get("power") or []
    if samples and ftp and ftp > 0:
        low = mid = hi = 0
        low_thresh = _CAL_LOW_PCT * ftp
        mid_thresh = _CAL_MID_PCT * ftp
        for p in samples:
            try:
                pw = float(p)
            except (TypeError, ValueError):
                continue
            if pw <= 0:
                continue
            if pw <= low_thresh:
                low += 1
            elif pw <= mid_thresh:
                mid += 1
            else:
                hi += 1
        return (
            round(low / 60.0, 1),
            round(mid / 60.0, 1),
            round(hi / 60.0, 1),
        )
    return (0.0, 0.0, 0.0)


def _planned_zone_split_minutes(session: dict) -> tuple[float, float, float]:
    """Return (planned_z1z2_min, planned_z3z4_min, planned_z5plus_min) for a
    planned session. Reads zone_dist when present (set by /api/plan from the
    library), else falls back to a heuristic from session_type + duration.
    """
    dur = float(session.get("duration_min") or 0)
    if dur <= 0:
        return (0.0, 0.0, 0.0)

    zd = session.get("zone_dist")
    if zd and isinstance(zd, dict):
        # zone_dist values are percentages (0-100). Map: Z1+Z2 → low, Z3+Z4
        # → mid, Z5+Z6 → hi.
        low_pct = float(zd.get("z1") or 0) + float(zd.get("z2") or 0)
        mid_pct = float(zd.get("z3") or 0) + float(zd.get("z4") or 0)
        hi_pct  = float(zd.get("z5") or 0) + float(zd.get("z6") or 0)
        total = low_pct + mid_pct + hi_pct
        if total > 0:
            return (
                round(dur * low_pct / 100.0, 1),
                round(dur * mid_pct / 100.0, 1),
                round(dur * hi_pct / 100.0, 1),
            )

    # Heuristic fallback by session_type. Conservative assignments:
    stype = (session.get("session_type") or "").lower()
    if stype in ("rest",):
        return (0.0, 0.0, 0.0)
    if stype in ("recovery", "z2", "long_z2", "endurance"):
        return (round(dur, 1), 0.0, 0.0)
    if stype in ("tempo", "sweetspot"):
        return (round(dur * 0.55, 1), round(dur * 0.45, 1), 0.0)
    if stype in ("threshold", "overunder"):
        return (round(dur * 0.45, 1), round(dur * 0.55, 1), 0.0)
    if stype in ("vo2max", "sprint", "anaerobic"):
        # Warmup + recovery in z1z2; intervals in z5+; some z3z4 transition.
        return (round(dur * 0.55, 1), round(dur * 0.10, 1), round(dur * 0.35, 1))
    if stype == "ftp_test":
        return (round(dur * 0.40, 1), round(dur * 0.45, 1), round(dur * 0.15, 1))
    # Unknown: lump in z3z4 so it's at least visible.
    return (0.0, round(dur, 1), 0.0)


# ── v1.4.0 — locked session field contract ────────────────────────────────
# Every key emitted in /api/plan session payloads MUST be in this set.
# When adding a new session field, add it here OR fix a regression that
# leaked an unsanctioned key. SESSION_FIELDS_LOCKED is enforced by
# tests/test_v140_session_fields_contract.py.
SESSION_FIELDS_LOCKED: frozenset[str] = frozenset({
    # core (training_planner.PlannedSession)
    "day", "day_name", "session_type", "duration_min", "tss_estimate",
    "description", "zwo_file", "zwo_name", "status",
    # adapt + redraw + dismiss + move
    "adapted", "adapted_reason", "variation",
    "dismissed_at", "completion_matches", "user_moved", "moved_from",
    # P2.1 (v3.0.0, G10): execution score written at completion-match time
    # ({score, basis, components, verdict, activity_id, computed_at}).
    "execution",
    # 3D mirrors (None when 3D unavailable; v1.0.6)
    "wprime_tss", "pmax_tss", "cp_tss",
    "wprime_xss", "pmax_xss", "cp_xss",
    # synthetic history shells (calendar payload only; v4.3.0)
    "_synthetic_history",
    # match_zwo redraw seeds
    "profile_id",
    # enriched (added by _enrich_plan_for_response)
    "card_state", "card_state_v2", "content_class", "display_name",
    "zone_dist", "score", "protocol", "zwo_duration_min", "zwo_tss",
    # availability (v1.3.5+)
    "availability_hours", "availability_type",
})


def classify_card_state_v2(
    session: dict, has_actual: bool, today_iso: str,
) -> str:
    """v1.4.0 — pure 10-state classifier per CALENDAR_REDESIGN §5d.

    Inputs → output, no side effects. Single source of truth for the new
    10-state UI dispatch. Legacy 4-string `_classify_card_state` is kept
    for wire back-compat (mapped via :func:`legacy_card_state`).

    States:
      past_no_ride, past_planned_no_ride, past_actual_only,
      past_planned_actual, today_planned, today_actual,
      future_planned, future_unavailable, future_rest, missing_workout
    """
    day_iso = session.get("day") or ""
    stype = (session.get("session_type") or "").lower()
    zwo = session.get("zwo_file") or ""
    avail = session.get("availability_hours")

    # 1. has_actual ALWAYS wins. Even on rest or unavailable days, a logged
    #    ride surfaces in the calendar (rider rode anyway).
    if has_actual:
        if day_iso < today_iso:
            return "past_planned_actual" if zwo else "past_actual_only"
        if day_iso == today_iso:
            return "today_actual"
        # future + actual: treat as past_actual_only
        return "past_actual_only"

    # 2. rest beats availability=0 for visual reasons (no red badge on a
    #    rest day the user also marked unavailable — already a rest day).
    if stype == "rest":
        if day_iso < today_iso:
            # past + rest + no actual: show as past_no_ride (genuine rest)
            return "past_no_ride"
        if day_iso > today_iso:
            return "future_rest"
        return "rest"  # today's rest

    # 3. availability=0 → future_unavailable (only meaningful for today/future).
    if avail is not None:
        try:
            if float(avail) == 0 and day_iso >= today_iso:
                return "future_unavailable"
        except (TypeError, ValueError):
            pass

    # 4. past/today/future split.
    if day_iso < today_iso:
        return "past_planned_no_ride"
    if day_iso == today_iso:
        return "today_planned"
    if not zwo:
        return "missing_workout"
    return "future_planned"


def legacy_card_state(state10: str) -> str:
    """v1.4.0 — map new 10-state → legacy 4-state wire contract.

    Dashboard renderCalDay + plan grid hardcode 4 strings (`completed`,
    `rest`, `missing_workout`, `planned`). Keep `card_state` on the wire
    using these 4 values for back-compat; emit `card_state_v2` alongside
    so future UI rev can use the 10-state without a wire break.
    """
    if state10 in ("today_actual", "past_planned_actual", "past_actual_only"):
        return "completed"
    if state10 in ("rest", "future_rest", "past_no_ride"):
        return "rest"
    if state10 == "missing_workout":
        return "missing_workout"
    if state10 == "future_unavailable":
        # bottom-cal already paints UNAVAILABLE via availability_hours==0;
        # fall through to "rest" so plan grid doesn't render yellow ⚠.
        return "rest"
    if state10 == "past_planned_no_ride":
        return "planned"  # past unfilled — UI can paint it red via isPast
    return "planned"


# v1.4.2 — mtime-keyed cache for _enrich_plan_for_response.
# Per-call ride scan + library load costs ~50 ms on plans with N=84
# sessions. Cache invalidates on plan mtime change (every mutation
# endpoint already touches the JSON via tmp+rename) or today rollover.
# Single-process FastAPI worker → no thread race; plan writes are also
# serialized via tp.plan_write_lock().
_ENRICH_CACHE: dict[str, tuple[float, dict]] = {}
_ENRICH_CACHE_TTL = 300  # 5 min


def _enrich_plan_for_response(plan_dict: dict, today_iso: str) -> dict:
    """v1.4.0 — single enrichment helper (CALENDAR_REDESIGN §5b).
    v1.4.2 — wrapped with mtime-keyed cache; uncached body lives in
    :func:`_enrich_plan_for_response_uncached`.

    Mutates ``plan_dict["weeks"][*]["sessions"][*]`` in place AND returns
    the plan for chaining. Adds the seven enrichment fields per session
    + ``card_state`` (legacy 4-string) + ``card_state_v2`` (new 10-state).
    Replaces the previously-duplicated blocks in /api/plan,
    /api/plan/generate, /api/plan/save-availability, /api/plan/reforecast,
    and /api/plan/redraw-day.

    Idempotent: calling twice produces the same output. Safe to chain
    after every plan mutation.
    """
    # Cache key includes mtime + size so concurrent ms-coincident writes
    # can't collide (grill I5). today_iso busts on rollover.
    plan_path = _plan_dir() / "current_plan.json"
    try:
        st = plan_path.stat()
        key = f"{st.st_mtime:.6f}|{st.st_size}|{today_iso}"
    except OSError:
        # No persisted plan (e.g. first-run /api/plan/generate). Skip the
        # cache; the result is short-lived anyway.
        return _enrich_plan_for_response_uncached(plan_dict, today_iso)
    now = time.time()
    cached = _ENRICH_CACHE.get(key)
    if cached and (now - cached[0]) < _ENRICH_CACHE_TTL:
        # Apply cached enrichment to the live plan_dict (so callers get the
        # same in-place-mutated object semantics as the uncached path).
        _apply_cached_enrichment(plan_dict, cached[1])
        return plan_dict
    enriched = _enrich_plan_for_response_uncached(plan_dict, today_iso)
    _ENRICH_CACHE[key] = (now, _snapshot_enrichment(enriched))
    # Bound cache size — keep last 4 entries by insertion time. Avoids
    # unbounded growth when developers tap mtime in tight loops.
    if len(_ENRICH_CACHE) > 4:
        oldest = sorted(_ENRICH_CACHE.items(), key=lambda kv: kv[1][0])[:-4]
        for k, _ in oldest:
            _ENRICH_CACHE.pop(k, None)
    return enriched


# v1.4.2 — enrichment fields written by _enrich_plan_for_response_uncached.
# Snapshot/apply on the cache hot path uses this list.
_ENRICH_FIELDS = (
    "display_name", "zwo_duration_min", "zone_dist", "score", "protocol",
    "content_class", "card_state", "card_state_v2", "zwo_tss",
)


def _snapshot_enrichment(plan_dict: dict) -> dict:
    """Capture the enrichment fields for cache storage. Keyed by ISO day
    so the snapshot is independent of week/session ordering.
    """
    snap: dict[str, dict] = {}
    for w in plan_dict.get("weeks", []) or []:
        for s in w.get("sessions", []) or []:
            day = s.get("day")
            if not day:
                continue
            snap[day] = {f: s.get(f) for f in _ENRICH_FIELDS if f in s}
    return snap


def _apply_cached_enrichment(plan_dict: dict, snap: dict) -> None:
    """Replay cached enrichment fields onto a live plan_dict."""
    for w in plan_dict.get("weeks", []) or []:
        for s in w.get("sessions", []) or []:
            day = s.get("day")
            if not day:
                continue
            cached_fields = snap.get(day)
            if not cached_fields:
                continue
            for f, v in cached_fields.items():
                s[f] = v


def _enrich_plan_for_response_uncached(plan_dict: dict, today_iso: str) -> dict:
    """v1.4.2 — uncached body of :func:`_enrich_plan_for_response`. Same
    semantics as before v1.4.2; the wrapper above adds caching only.
    """
    # Library + classifications loaded once per call.
    try:
        library = tp.load_workout_library()
        lib_by_file = {w.get("File"): w for w in library if w.get("File")}
    except Exception as _e:
        _log.debug(f"_enrich_plan_for_response: library load failed: {_e}")
        lib_by_file = {}
    try:
        classifications = tp._load_content_classifications() or {}
    except Exception:
        classifications = {}

    # Rides-by-date for has_actual flag (used by card_state).
    rides_by_date: dict[str, bool] = {}
    try:
        import ride_storage as _rs
        for _r in _rs.list_rides():
            _d = _ride_started_local_iso_date(_r)
            if _d:
                rides_by_date[_d] = True
        for _f in sorted(_rides_fit_dir().glob("*.fit")):
            try:
                _st = _f.stat()
                _ddt = datetime.fromtimestamp(
                    _st.st_mtime, timezone.utc,
                ).astimezone().date().isoformat()
                rides_by_date[_ddt] = True
            except OSError:
                pass
    except Exception:
        pass

    for w in plan_dict.get("weeks", []) or []:
        for s in w.get("sessions", []) or []:
            zwo = s.get("zwo_file") or ""
            meta = lib_by_file.get(zwo) if zwo else None
            _dn, _zdur = _session_naming_lookup(zwo, classifications, lib_by_file)
            s["display_name"] = _dn
            s["zwo_duration_min"] = _zdur
            if meta:
                s["zone_dist"] = {
                    "z1": meta.get("Z1%", 0), "z2": meta.get("Z2%", 0),
                    "z3": meta.get("Z3%", 0), "z4": meta.get("Z4%", 0),
                    "z5": meta.get("Z5%", 0), "z6": meta.get("Z6%", 0),
                }
                s["score"] = meta.get("Score")
                s["protocol"] = meta.get("Protocol")
                s["content_class"] = meta.get("content_class") or ""
                # v3.4.5 — matched FILE's TSS for the day-modal TSS stat.
                s["zwo_tss"] = meta.get("TSS")
            else:
                s.setdefault("zone_dist", None)
                s.setdefault("score", None)
                s.setdefault("content_class", "")
                s.setdefault("zwo_tss", None)
            _has_actual = bool(rides_by_date.get(s.get("day", "")))
            # Legacy 4-state on wire (back-compat); v2 alongside for migration.
            s["card_state"] = _classify_card_state(
                s, has_actual=_has_actual, library_lookup=lib_by_file,
            )
            s["card_state_v2"] = classify_card_state_v2(
                s, has_actual=_has_actual, today_iso=today_iso,
            )
    return plan_dict


# v1.5.0 — `_propagate_reforecast_to_dict` lived here in v1.4.0 as the
# single propagation helper. v1.5.0 collapsed it into `tp.reforecast_dict`
# (training_planner.py); the propagation logic is now `_apply_reforecast_to_dict`
# inside training_planner. Removed from app.py to enforce the new single
# mutation site (drift class A closure).


def _classify_card_state(session: dict, has_actual: bool, library_lookup: dict | None) -> str:
    """v4.3.0 B6 — return the card_state string used by the UI dispatcher.

    Possible values (per MASTER §3 + spec):
      - ``"completed"``      : a matching ride exists for this date (takes
                               precedence over rest so an unplanned ride on
                               a rest day still surfaces) — v4.5.1 §B2
      - ``"rest"``           : session_type == "rest" (no workout to click)
      - ``"missing_workout"``: zwo_file is empty/null OR its content_class
                               doesn't match the planner session_type
      - ``"planned"``        : zwo_file present + content_class consistent
    """
    stype = (session.get("session_type") or "").lower()
    # v4.5.1 §B2 — has_actual must be checked BEFORE the rest branch so that
    # an unplanned ride on a planned-rest day classifies as "completed"
    # (was returning "rest" with the actual silently shadowed).
    if has_actual:
        return "completed"
    if stype == "rest":
        return "rest"
    zwo = session.get("zwo_file") or ""
    if not zwo:
        return "missing_workout"

    # Cross-check content_class against session_type when we have a library
    # entry for this zwo. Loose match — any of the planner's accepted
    # categories for that session_type counts as consistent.
    if library_lookup is not None:
        meta = library_lookup.get(zwo)
        if meta is not None:
            cc = (meta.get("content_class") or "").lower()
            if cc:
                # Reuse training_planner's session_type → category mapping.
                # v1.0.0: accept-sets aligned with what WORKOUT_MIX_PREFERENCE
                # actually emits. Pre-fix, z2/long_z2/recovery were strict on
                # {endurance, recovery} but the planner intentionally mixes
                # tempo (Z2 + tempo finisher) and mixed (Z2-dominant with
                # small Z3 cap) into those slots — so legit picks were being
                # flagged as missing_workout, showing the yellow ↻ icon.
                # User saw "regenerate fixes it" because the per-cell redraw
                # resamples until match_zwo lucks into the strict set.
                accepted_by_type = {
                    "z2":         {"endurance", "recovery", "mixed", "tempo"},
                    "long_z2":    {"endurance", "recovery", "mixed", "tempo", "sweet_spot"},
                    "recovery":   {"recovery", "endurance", "mixed"},
                    "tempo":      {"tempo", "sweet_spot", "mixed", "endurance"},
                    "sweetspot":  {"sweet_spot", "tempo", "threshold", "mixed"},
                    "threshold":  {"threshold", "sweet_spot", "over_under", "mixed"},
                    "vo2max":     {"vo2max", "vo2_short", "anaerobic", "mixed"},
                    "overunder":  {"over_under", "threshold", "mixed"},
                    "sprint":     {"neuromuscular", "anaerobic", "vo2max", "vo2_short"},
                    "ftp_test":   {"ftp_test"},
                }
                accepted = accepted_by_type.get(stype, set())
                if accepted and cc not in accepted:
                    return "missing_workout"

    return "planned"


def _ride_started_local_iso_date(ride: dict) -> str | None:
    """Best-effort local-TZ ISO date from ride.started_at.

    JSON rides write ISO timestamps with offset (`+02:00`); FIT rides have
    ``started_at`` set from fs mtime in UTC. We strip to YYYY-MM-DD per the
    local timezone, which matches the plan's session.day field (always
    naive YYYY-MM-DD in local time).
    """
    started = ride.get("started_at") or ""
    if not started:
        return None
    try:
        # Tolerate trailing Z (UTC) and offset-naive.
        s = started.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return started[:10]
        return dt.astimezone().date().isoformat()
    except (TypeError, ValueError):
        return started[:10] if len(started) >= 10 else None


def _actual_blocks_from_laps(ride: dict, ftp: "int | None") -> list:
    """Lap timeline -> mini-sprite blocks ({min, pctLow, pctHigh} each)."""
    laps = ride.get("intervals")
    ride_ftp = ride.get("ftp_at_ride") or ftp
    if not isinstance(laps, list) or not laps or not ride_ftp:
        return []
    if len(laps) > 120:   # degenerate 1-lap-per-second exports: not a structure
        return []
    out = []
    for lp in laps:
        try:
            dur = float(lp.get("duration_s") or 0)
            pw = lp.get("avg_power_w")
            if dur <= 0 or pw is None:
                continue
            pct = round(100.0 * float(pw) / float(ride_ftp), 1)
            out.append({"min": round(dur / 60.0, 2), "pctLow": pct, "pctHigh": pct})
        except (TypeError, ValueError):
            continue
    return out


def _summarize_ride_for_calendar(
    ride: dict, ftp: int | None, planned_session: dict | None = None,
) -> dict:
    """Project a ride dict (FIT, JSON, or ICU-normalized) into the canonical
    actual-payload shape per MASTER §3. Used by both /api/calendar's primary
    actual and actual_secondary lists.

    v4.4.0: ICU-normalized records (top-level §3 fields, source='icu') are
    also accepted — they bypass the legacy summary/parsed_stats lookups.

    v1.8.2: optional ``planned_session`` kwarg. When supplied (calendar
    primary-ride path), the payload gains a ``compare`` block from
    ``analytics.compare_plan_to_actual``. Back-compat: when omitted (legacy
    callers, actual_secondary list) the kwarg defaults to None and no
    ``compare`` block is added.
    """
    # v4.4.0 — ICU rides already carry the §3 fields at the top level.
    if (ride.get("source") == "icu"
            or (ride.get("ride_id") or "").startswith("icu_")):
        duration_sec = ride.get("duration_s") or 0
        duration_min = round(float(duration_sec) / 60.0, 1) if duration_sec else 0.0
        tiz = ride.get("time_in_zone") or {}
        z12_sec = (tiz.get("z1") or 0) + (tiz.get("z2") or 0)
        z34_sec = (tiz.get("z3") or 0) + (tiz.get("z4") or 0)
        z5p_sec = sum(tiz.get(f"z{i}") or 0 for i in (5, 6, 7))
        # v1.8.0 §F2 — pass through polarization classification + confidence
        # so the calendar can color activity cards by load profile.
        _pol = ride.get("polarization") or {}
        payload = {
            "ride_id": ride.get("ride_id") or "",
            "name": ride.get("name") or "",
            "source": "icu",
            "duration_min": duration_min,
            "tss": ride.get("tss"),
            "avg_power_w": int(ride.get("avg_power_w") or 0),
            "z1z2_min": round(z12_sec / 60.0, 1),
            "z3z4_min": round(z34_sec / 60.0, 1),
            "z5plus_min": round(z5p_sec / 60.0, 1),
            "decoupling_pct": ride.get("decoupling_pct"),
            "started_at": ride.get("started_at") or "",
            "classification": _pol.get("classification") if isinstance(_pol, dict) else None,
            "pol_confidence": _pol.get("confidence") if isinstance(_pol, dict) else None,
            # Same lap-blocks contract as the legacy path below — this early
            # return is the branch every SYNCED ride takes, and it was the
            # one initially missed: the test asserting blocks on an icu ride
            # failed with KeyError while the legacy-path tests passed.
            "blocks": _actual_blocks_from_laps(ride, ftp),
        }
        if planned_session is not None:
            from analytics import compare_plan_to_actual
            payload["compare"] = compare_plan_to_actual(planned_session, payload)
        return payload

    summary = ride.get("summary") or {}
    parsed = ride.get("parsed_stats") or {}

    duration_sec = (
        summary.get("duration_sec")
        or parsed.get("duration_sec")
        or 0
    )
    duration_min = round(float(duration_sec) / 60.0, 1) if duration_sec else 0.0
    tss = summary.get("tss") or parsed.get("tss")
    avg_p = summary.get("avg_power") or parsed.get("avg_power") or 0
    decoupling = summary.get("decoupling_pct")

    z1z2, z3z4, z5plus = _ride_zone_split_minutes(ride, ftp)

    name = (
        (ride.get("metadata") or {}).get("name")
        or ride.get("name")
        or summary.get("workout_name")
        or ""
    )

    # v1.8.0 §F2 — pass through polarization classification + confidence
    # so the calendar can color activity cards by load profile.
    _pol = ride.get("polarization") or {}
    payload = {
        "ride_id": ride.get("ride_id") or ride.get("id") or "",
        "name": name,
        "source": ride.get("source") or ("json" if "samples" in ride else "fit"),
        "duration_min": duration_min,
        "tss": tss,
        "avg_power_w": int(avg_p) if avg_p else 0,
        "z1z2_min": z1z2,
        "z3z4_min": z3z4,
        "z5plus_min": z5plus,
        "decoupling_pct": decoupling,
        "started_at": ride.get("started_at") or parsed.get("start_time") or "",
        "classification": _pol.get("classification") if isinstance(_pol, dict) else None,
        "pol_confidence": _pol.get("confidence") if isinstance(_pol, dict) else None,
        # The grid's completed-day sprite used to keep showing the PLANNED
        # silhouette — a rider compared it with intervals.icu's card, which
        # draws the ride you actually did, and read ours as "a standard
        # preview, completely different from the blocks I did". ICU renders
        # stream-downsampled zone-colored bars; the lap timeline stored on
        # every synced ride (start_s / duration_s / avg_power_w) carries the
        # same structure with zero extra fetches, so completed days ship it
        # as ready-to-render blocks in the sprite's own {min, pctLow,
        # pctHigh} shape. Empty for rides without laps or a known FTP — the
        # client keeps the planned silhouette there.
        "blocks": _actual_blocks_from_laps(ride, ftp),
    }
    if planned_session is not None:
        from analytics import compare_plan_to_actual
        payload["compare"] = compare_plan_to_actual(planned_session, payload)
    return payload


def _load_full_ride(ride: dict) -> dict:
    """Hydrate a ride summary dict with the heavier parsed payload.

    /api/rides returns trimmed entries (no samples for JSON rides, no
    parsed_stats for FIT). For zone-split fallback we may need the full
    payload — use this lazily so the merge isn't quadratic for users with
    large archives.
    """
    rid = ride.get("ride_id") or ride.get("id") or ""
    src = ride.get("source") or ""
    if not rid:
        return ride
    # v4.4.0 — ICU records already carry the §3 fields; nothing to hydrate.
    if src == "icu" or rid.startswith("icu_"):
        return ride
    try:
        if src == "fit":
            # v4.4.0: fit ride_ids are now prefixed `fit_<stem>`; legacy callers
            # may still pass the bare stem.
            stem = rid[4:] if rid.startswith("fit_") else rid
            fit_path = _rides_fit_dir() / f"{stem}.fit"
            if fit_path.exists():
                stats = _parse_fit_stats(fit_path)
                return {
                    **ride,
                    "parsed_stats": stats,
                    "started_at": stats.get("start_time") or ride.get("started_at"),
                }
        else:
            import ride_storage as _rs
            full = _rs.get_ride(rid)
            if full:
                return {**full, "ride_id": rid, "source": "json"}
    except Exception as _e:
        _log.debug(f"_load_full_ride failed for {rid}: {_e}")
    return ride


# ─────────────────────────────────────────────────────────────────────────────
# v4.4.0 — On-track summary helpers (CONCEPT-SCI §3, §6 + CONCEPT-DATA §3)
# ─────────────────────────────────────────────────────────────────────────────

def _annotate_phase_week_indices(weeks: list[dict]) -> None:
    """Fill ``phase_week_index`` (1-based) and ``phase_weeks_total`` per
    contiguous phase block. Mutates ``weeks`` in place.
    """
    i = 0
    n = len(weeks)
    while i < n:
        phase = (weeks[i].get("phase") or "")
        # Skip pure-history shells from contributing to phase counters —
        # they're conceptually pre-plan, not part of the phase ladder.
        if phase == "history" or not phase:
            weeks[i]["phase_week_index"] = 0
            weeks[i]["phase_weeks_total"] = 0
            i += 1
            continue
        j = i
        while j < n and (weeks[j].get("phase") or "") == phase:
            j += 1
        block_len = j - i
        for k, idx in enumerate(range(i, j), start=1):
            weeks[idx]["phase_week_index"] = k
            weeks[idx]["phase_weeks_total"] = block_len
        i = j


def _annotate_planned_ctl_eow(weeks: list[dict], plan: dict) -> None:
    """Fill each calendar week's ``planned_ctl_eow``: the CTL the plan projects
    at the end of the week's last projected day (see _planned_ctl_by_day)."""
    by_day = _planned_ctl_by_day(plan)
    for w in weeks:
        start, end = w.get("start_date") or "", w.get("end_date") or ""
        inside = [d for d in by_day if start <= d <= end]
        if inside:
            w["planned_ctl_eow"] = by_day[max(inside)]


def _recent_activities_for_planner(days: int = 30) -> list:
    """Recent activities in the shape the planner's completed-load helpers read.

    Best-effort: an empty list means the planner behaves exactly as it did
    before, which is the right failure mode for something that only ever
    SUBTRACTS work.
    """
    try:
        return [dict(r) for r in db.query_activities(days=days)]
    except Exception:  # noqa: BLE001
        logging.getLogger(__name__).warning(
            "recent activities unavailable for the planner", exc_info=True)
        return []


def _polarized_actual_from_rides(
    rides: list[dict], today: date, last_n_days: int = 7
) -> dict:
    """Actual three-zone split over the last N days of rides.

    Folded through zones.THREE_ZONE_FROM_COGGAN:
      - z1 = Coggan z1+z2          (< 76% FTP, below LT1)
      - z2 = Coggan z3+z4          (76-105%, spanning LT2 -- the grey zone)
      - z3 = Coggan z5+z6+z7       (>= 106%, above LT2)

    This used to put Coggan Z4 -- work AT threshold -- in the hard band, which
    disagreed with analytics.compute_polarization_block on the ride detail card
    and with intervals.icu, so the same ride showed two different polarization
    readings on two screens.

    Returns ``{"z1_pct": int, "z2_pct": int, "z3_pct": int}``. Empty rides →
    all zeros.
    """
    cutoff = (today - timedelta(days=last_n_days)).isoformat()
    z12 = z3 = z4p = 0.0
    for r in rides:
        d = _ride_started_local_iso_date(r) or ""
        if not d or d < cutoff:
            continue
        # ICU normalized: tiz at top level. JSON: zones.power. FIT: parsed.
        tiz = r.get("time_in_zone")
        if not tiz:
            zp = (r.get("zones") or {}).get("power") or {}
            if zp:
                tiz = {f"z{i}": int(zp.get(f"Z{i}") or 0) for i in range(1, 8)}
        if not tiz:
            full = _load_full_ride(r)
            tiz = full.get("time_in_zone") or {}
        if not tiz:
            continue
        _b = _zones_mod.three_zone(tiz)
        z12 += _b["z1"]
        z3  += _b["z2"]
        z4p += _b["z3"]
    total = z12 + z3 + z4p
    if total <= 0:
        return {"z1_pct": 0, "z2_pct": 0, "z3_pct": 0}
    return {
        "z1_pct": int(round(z12 / total * 100)),
        "z2_pct": int(round(z3  / total * 100)),
        "z3_pct": int(round(z4p / total * 100)),
    }


def _planned_ctl_by_day(plan: dict) -> dict:
    """{day_iso: end-of-day CTL the plan projects}, from the plan's anchor:
    the intervals.icu CTL stamped when it was generated (plan["ctl_snapshot"]),
    on the day it was generated, forward over the stored sessions' TSS.

    It started from a local EWMA over the FIT-only ride list, None for a rider
    whose rides arrive from intervals.icu, and then from 37: a plan generated
    at CTL 29.6 drew its band around 38.6 (the audit's S-2). A plan with no
    anchor has no curve."""
    snap = (plan or {}).get("ctl_snapshot") or {}
    try:
        start_ctl = float(snap.get("current_ctl"))
    except (TypeError, ValueError):
        return {}
    anchor = str(snap.get("generated_on") or "")[:10]
    tss_by_day: dict = {}
    for w in (plan or {}).get("weeks") or []:
        for s in w.get("sessions") or []:
            day = s.get("day") or ""
            if day and anchor and day >= anchor:
                try:
                    tss_by_day[day] = tss_by_day.get(day, 0.0) + float(s.get("tss_estimate") or 0)
                except (TypeError, ValueError):
                    pass
    if not tss_by_day:
        return {}
    first = date.fromisoformat(anchor)
    last = date.fromisoformat(max(tss_by_day))
    days = [(first + timedelta(days=i)).isoformat() for i in range((last - first).days + 1)]
    series = tp.forecast_ctl(start_ctl, [tss_by_day.get(d, 0.0) for d in days])
    # forecast_ctl prepends the start: series[i + 1] is the end of days[i].
    return {d: float(series[i + 1]) for i, d in enumerate(days)}


def _planned_ctl_today(plan: dict, today: date) -> float | None:
    """End-of-today CTL the plan projects (see _planned_ctl_by_day)."""
    by_day = _planned_ctl_by_day(plan)
    today_iso = today.isoformat()
    past = [d for d in by_day if d <= today_iso]
    return by_day[max(past)] if past else None


def _actual_ctl_today(rides: list[dict], today: date) -> float | None:
    """Today's CTL from the fitness owner (intervals.icu); None when unknown."""
    return _fitness_state(today)["ctl"]


def _intensity_dist_match(actual: dict, target: dict) -> float:
    """Score 0..100 measuring how close ``actual`` polarized split matches
    ``target``. Lower L1 distance → higher score. Perfect match = 100.
    """
    if not actual or not target:
        return 0.0
    keys = ("z1_pct", "z2_pct", "z3_pct")
    diff = sum(abs(int(actual.get(k) or 0) - int(target.get(k) or 0)) for k in keys)
    # Max possible L1 difference is 200 (e.g., 100/0/0 vs 0/0/100).
    return max(0.0, 100.0 - (diff / 2.0))


def _ctl_ramp_score(actual: float | None, planned: float | None) -> float | None:
    """Score 0..100: 100 = exactly on plan; 50 at ±5 TSS off; 0 at ±10+."""
    if actual is None or planned is None:
        return None
    delta = abs(float(actual) - float(planned))
    if delta <= 5.0:
        return 100.0 - (delta / 5.0) * 50.0  # 100 at 0, 50 at 5
    if delta >= 10.0:
        return 0.0
    return 50.0 - ((delta - 5.0) / 5.0) * 50.0  # 50 at 5, 0 at 10


def _hrv_trend_score() -> float | None:
    """7-day HRV (rMSSD) trend score per Plews 2013. 100 = stable/up.

    v1.3.3 perf: route through ``cached()`` (5-min TTL) so /api/calendar
    no longer fires a fresh ICU HTTP round-trip every dashboard load.
    """
    try:
        wellness = cached("wellness_14", lambda: fetch_wellness(14))
    except Exception:
        return None
    rmssds = [float(w.get("hrv") or w.get("rmssd") or 0) for w in (wellness or [])]
    rmssds = [v for v in rmssds if v > 0]
    if len(rmssds) < 7:
        return None
    last7 = rmssds[-7:]
    prev7 = rmssds[-14:-7] if len(rmssds) >= 14 else last7
    avg_last = sum(last7) / len(last7)
    avg_prev = sum(prev7) / len(prev7)
    if avg_prev <= 0:
        return None
    pct = (avg_last - avg_prev) / avg_prev * 100.0
    # Plews: drop > 5% over 7d is concerning.
    if pct >= 0:
        return 100.0
    if pct <= -5.0:
        return 0.0
    return 100.0 + (pct / 5.0) * 100.0  # linear -5..0% maps to 0..100


def _daily_tss_last7(rides: list[dict], today: date) -> list[float]:
    """Per-day TSS over the last 7 calendar days (oldest → newest, today
    last). 3.4.0 W2: extracted from _monotony_score so the on-track band
    AND the continuous deload trigger read the SAME series."""
    cutoff = (today - timedelta(days=7)).isoformat()
    daily_tss: list[float] = [0.0] * 7
    for r in rides:
        d = _ride_started_local_iso_date(r) or ""
        if not d or d < cutoff:
            continue
        try:
            day_dt = date.fromisoformat(d)
        except ValueError:
            continue
        delta = (today - day_dt).days
        if 0 <= delta < 7:
            tss = (r.get("tss")
                   or (r.get("summary") or {}).get("tss")
                   or (r.get("parsed_stats") or {}).get("tss") or 0)
            try:
                daily_tss[6 - delta] += float(tss)
            except (TypeError, ValueError):
                pass
    return daily_tss


def _monotony_score(rides: list[dict], today: date) -> float | None:
    """Foster monotony score per CONCEPT-SCI §1. 100 = monotony in [1.2, 1.8]."""
    monotony = cpol.foster_monotony(_daily_tss_last7(rides, today))
    if monotony is None:
        return None  # no load in window
    # (all-equal weeks come back as cpol.MONOTONY_CAP → 0.0 below, same as
    # the pre-W2 inline sd<=0 branch)
    if 1.2 <= monotony <= 1.8:
        return 100.0
    if monotony >= 2.5:
        return 0.0
    if monotony < 1.2:
        return max(0.0, 100.0 - (1.2 - monotony) * 100.0)
    # 1.8..2.5 maps 100→0
    return max(0.0, 100.0 - ((monotony - 1.8) / 0.7) * 100.0)


def _days_until_next_phase(weeks: list[dict], today: date) -> int:
    """Days until the next phase change. Returns 0 if no next phase."""
    cur_phase = None
    for w in weeks:
        try:
            sd = date.fromisoformat(w.get("start_date") or "")
            ed = date.fromisoformat(w.get("end_date") or "")
        except ValueError:
            continue
        if sd <= today <= ed:
            cur_phase = w.get("phase") or ""
            break
    if cur_phase is None:
        return 0
    for w in weeks:
        try:
            sd = date.fromisoformat(w.get("start_date") or "")
        except ValueError:
            continue
        if sd > today and (w.get("phase") or "") != cur_phase:
            return (sd - today).days
    return 0


def _build_summary_block(
    plan: dict, rides: list[dict], today: date, weeks: list[dict]
) -> dict:
    """v4.4.0 — assemble the top-level on-track summary block.

    Per MASTER §3 / CONCEPT-SCI §6: compliance band, CTL trajectory,
    polarized split, phase countdown, composite on-track score.
    """
    # Find current week record.
    cur = next((w for w in weeks if w.get("is_current")), None)

    cur_phase = (cur.get("phase") if cur else "") or ""
    phase_idx = (cur.get("phase_week_index") if cur else 0) or 0
    phase_total = (cur.get("phase_weeks_total") if cur else 0) or 0

    days_to_next = _days_until_next_phase(weeks, today)

    # Days until event (best-effort from plan goal).
    days_to_event = 0
    try:
        goal = (plan or {}).get("goal") or {}
        td = goal.get("target_date") or goal.get("event_date")
        if td:
            ed = date.fromisoformat(td)
            days_to_event = max(0, (ed - today).days)
    except (ValueError, TypeError):
        pass

    # Compliance — v1.3.1 HIGH fix: prefer to-date math for the headline
    # band so Wednesday doesn't read as red just because Thu/Fri/Sat/Sun
    # haven't happened yet. Falls back to full-week ratio when to-date
    # isn't emitted (e.g. legacy callers passing weeks without the field).
    comp_ratio = 0.0
    if cur:
        comp_ratio = float(
            cur.get("completion_pct_to_date")
            if cur.get("completion_pct_to_date") is not None
            else (cur.get("completion_pct") or 0.0)
        )
    comp_pct_int = int(round(comp_ratio * 100))
    band = tp.compliance_band(comp_ratio if comp_ratio > 0 else None)

    # CTL.
    ctl_actual = _actual_ctl_today(rides, today)
    ctl_planned = _planned_ctl_today(plan, today)
    ctl_low = (ctl_planned - 5.0) if ctl_planned is not None else None
    ctl_high = (ctl_planned + 5.0) if ctl_planned is not None else None

    # Polarized.
    actual_pol = _polarized_actual_from_rides(rides, today, last_n_days=7)
    # The active model's row for this phase; falls back to "history" for a
    # phase name the table does not carry.
    _tid = tp.polarized_targets((plan or {}).get("goal") or {})
    target_pol = _tid.get((cur_phase or "").lower(), _tid.get("history", {}))

    # Sub-scores → composite.
    tss_comp_score: float | None
    if comp_ratio <= 0:
        tss_comp_score = None
    else:
        # Map ratio to a 0..100 score: 1.0 = 100, ±0.20 = 75, beyond ±0.50 = 0
        delta = abs(1.0 - comp_ratio)
        if delta <= 0.20:
            tss_comp_score = 100.0 - (delta / 0.20) * 25.0
        elif delta <= 0.50:
            tss_comp_score = 75.0 - ((delta - 0.20) / 0.30) * 75.0
        else:
            tss_comp_score = 0.0

    intensity_score = _intensity_dist_match(actual_pol, target_pol) if any(
        actual_pol.values()
    ) else None

    ctl_score = _ctl_ramp_score(ctl_actual, ctl_planned)
    hrv_score = _hrv_trend_score()
    mono_score = _monotony_score(rides, today)

    composite = tp.on_track_score(
        tss_compliance=tss_comp_score,
        intensity_dist_match=intensity_score,
        ctl_ramp_in_band=ctl_score,
        hrv_trend_ok=hrv_score,
        monotony_ok=mono_score,
    )

    return {
        "current_phase": cur_phase,
        "phase_week_index": phase_idx,
        "phase_weeks_total": phase_total,
        "days_until_next_phase": days_to_next,
        "days_until_event": days_to_event,
        "compliance_pct_this_week": comp_pct_int,
        "compliance_band": band,
        "ctl_actual": (round(float(ctl_actual), 1) if ctl_actual is not None else None),
        "ctl_planned_today": (round(float(ctl_planned), 1) if ctl_planned is not None else None),
        "ctl_band_low": (round(float(ctl_low), 1) if ctl_low is not None else None),
        "ctl_band_high": (round(float(ctl_high), 1) if ctl_high is not None else None),
        "polarized_actual": actual_pol,
        "polarized_target": dict(target_pol),
        "on_track_score": composite,
        "on_track_band": tp.on_track_band(composite),
    }


def merge_plan_with_rides(plan: dict, rides: list[dict]) -> dict:
    """v4.3.0 B4 — build the calendar payload merging plan-sessions with rides.

    Args:
        plan: ``current_plan.json`` dict (must have ``weeks`` + ``goal``).
        rides: flat ride list as emitted by ``/api/rides`` (mix of JSON and
            FIT entries; we hydrate as needed).

    Returns the canonical calendar payload per MASTER §3 — ready to ship as
    the ``/api/calendar`` JSON body.
    """
    import week_view as _wv

    today = clock.today()
    today_iso = today.isoformat()
    iso_year, iso_week, _iso_day = today.isocalendar()

    ftp = getattr(config, "ATHLETE_FTP_W", None)

    # ── Library lookup for card_state cross-check (B6) ──────────────────────
    try:
        library = tp.load_workout_library()
        lib_by_file: dict = {}
        for w in library:
            f = w.get("File") or ""
            if f:
                lib_by_file[f] = w
    except Exception as _e:
        _log.debug(f"calendar: library load failed: {_e}")
        lib_by_file = {}

    # v1.0.4 IMPL-WIRING: classifications for display_name (Layer 3, MASTER §3).
    try:
        classifications = tp._load_content_classifications() or {}
    except Exception:
        classifications = {}

    # ── Index rides by local-TZ date for O(N) merge ─────────────────────────
    rides_by_date: dict[str, list[dict]] = {}
    for r in rides:
        # v2.4.0 — cycling only: a run/swim on a planned day must not become the
        # day's "actual" ride or count toward cycling load. (Empty/unknown sport
        # counts as cycling, so untagged local FIT rides still show.)
        if not _is_cycling_sport(r.get("sport", "")):
            continue
        d = _ride_started_local_iso_date(r)
        if not d:
            continue
        rides_by_date.setdefault(d, []).append(r)

    # v1.3.1 HIGH fix — surface user-blocked days in the calendar payload so
    # the THIS WEEK render can show an UNAVAILABLE badge instead of the
    # planned session card. Sparse: only days the user touched are present.
    availability_map: dict[str, dict] = {
        k: v for k, v in (plan.get("availability") or {}).items()
        if isinstance(v, dict)
    }

    # ── Past 12 weeks of history + plan weeks (avoid double-counting) ───────
    # A replaced plan's prescriptions (api_plan_generate) are on record too.
    plan_weeks = ((plan.get("replaced_weeks") or []) + (plan.get("weeks") or [])) if plan else []
    plan_dates: set[str] = set()
    for w in plan_weeks:
        for s in (w.get("sessions") or []):
            d = s.get("day")
            if d:
                plan_dates.add(d)

    # Build "history" weeks for the past 12 weeks that don't overlap plan.
    history_weeks: list[dict] = []
    earliest_plan_start: date | None = None
    for w in plan_weeks:
        try:
            wd = date.fromisoformat(w["start"])
            earliest_plan_start = wd if earliest_plan_start is None or wd < earliest_plan_start else earliest_plan_start
        except (KeyError, ValueError):
            continue

    # 12 ISO weeks back from today's Monday
    monday_today = today - timedelta(days=today.weekday())
    # Iterate range covers 12 weeks ago through current week (back=0). Without
    # `back=0`, a no-plan profile with a recent ride wouldn't surface this
    # week's ride at all because the planless current-week shell never fires.
    for back in range(12, -1, -1):
        wstart = monday_today - timedelta(weeks=back)
        wend = wstart + timedelta(days=6)
        # Skip if this entirely overlaps a plan week (the plan week wins).
        if earliest_plan_start is not None and wend >= earliest_plan_start:
            # Only emit "history" weeks that fall fully before the plan starts.
            # If the plan starts mid-window, we emit a partial history shell
            # so the UI shows actuals — the plan path will own its weeks.
            pass
        # Build a synthetic week with no planned sessions so actuals still surface.
        synthetic_sessions = []
        for off in range(7):
            day_d = wstart + timedelta(days=off)
            if day_d.isoformat() in plan_dates:
                continue  # plan owns it; skip in history
            synthetic_sessions.append({
                "day": day_d.isoformat(),
                "day_name": day_d.strftime("%a"),
                "session_type": "rest",
                "duration_min": 0,
                "tss_estimate": 0,
                "description": "",
                "zwo_file": "",
                "zwo_name": "",
                "_synthetic_history": True,
            })
        if synthetic_sessions:
            yr, wk, _ = wstart.isocalendar()
            history_weeks.append({
                "week_num": -back,
                "iso_year": yr,
                "iso_week": wk,
                "start": wstart.isoformat(),
                "end": wend.isoformat(),
                "phase": "history",
                "tss_target": 0,
                "is_stepback": False,
                "sessions": synthetic_sessions,
            })

    # Combine and sort chronologically.
    combined_weeks = list(history_weeks) + list(plan_weeks)
    combined_weeks.sort(key=lambda w: w.get("start") or "")

    # ── v4.3.1 FIX-SERVER: dedupe by (iso_year, iso_week) ───────────────────
    # When the user's local-TZ today sits at an ISO-week boundary AND the
    # stored plan slices weeks differently from history (e.g. plan anchored
    # Sun-Sat vs history Mon-Sun), two weeks may share the same ISO key.
    # That caused two `is_current=True` weeks AND two `is_today=True` days
    # for the boundary date. Prefer the planned-week record (drop the
    # history-only duplicate) so there's exactly one anchor per ISO week.
    deduped: list[dict] = []
    seen_iso: dict[tuple, int] = {}
    for w in combined_weeks:
        try:
            w_start_dt = date.fromisoformat(w["start"])
        except (KeyError, ValueError):
            deduped.append(w)
            continue
        wy_k, wk_k, _ = w_start_dt.isocalendar()
        key = (wy_k, wk_k)
        existing_idx = seen_iso.get(key)
        if existing_idx is None:
            seen_iso[key] = len(deduped)
            deduped.append(w)
            continue
        # Duplicate ISO key → prefer a stored row over a history shell, then
        # the current plan's row (a Generate's replaced rows never own a week
        # when the current plan has one, as in week_view.build), then the one
        # with planned content.
        existing = deduped[existing_idx]
        def _rank(row):
            sessions = row.get("sessions") or []
            return (not all(s.get("_synthetic_history") for s in sessions),
                    not row.get("carried_from_generate"),
                    any((s.get("zwo_file") or "") and not s.get("_synthetic_history")
                        for s in (row.get("sessions") or [])))
        if _rank(w) > _rank(existing):
            deduped[existing_idx] = w
        # Otherwise keep the first (already in `deduped`).
    combined_weeks = deduped

    # v4.4.0 §6 — flatten ALL plan sessions across all weeks into a single
    # day-keyed map so ISO-week Monday anchoring doesn't drop sessions
    # whose stored plan-week happened to start mid-week (e.g. Sunday).
    _global_plan_sessions_by_day: dict[str, dict] = {}
    for _pw in plan_weeks:
        for _s in (_pw.get("sessions") or []):
            _d = _s.get("day")
            if _d:
                _global_plan_sessions_by_day[_d] = _s

    # ── Project each week into the §3 schema ────────────────────────────────
    out_weeks: list[dict] = []
    for w in combined_weeks:
        try:
            w_start_raw = date.fromisoformat(w["start"])
            w_end_raw = date.fromisoformat(w["end"])
        except (KeyError, ValueError):
            continue

        # v4.4.0 §6 — ISO-week Monday-anchored boundary EVERYWHERE on server.
        # Plan-weeks are stored from whatever date generate_plan was run on
        # (which can be a Sunday). Normalize to the ISO-week Monday so the
        # /api/calendar payload is always Mon-Sun.
        w_start = w_start_raw - timedelta(days=w_start_raw.weekday())
        w_end = w_start + timedelta(days=6)

        wy, wk, _ = w_start.isocalendar()
        # Canonical is_current — computed against the single iso_year/iso_week
        # captured once at the top of merge_plan_with_rides. After dedupe
        # above, exactly one week (if any) can match.
        is_current = (wy == iso_year and wk == iso_week)

        planned_tss = 0.0
        actual_tss = 0.0
        planned_z12 = planned_z34 = planned_z5p = 0.0
        actual_z12 = actual_z34 = actual_z5p = 0.0
        # v1.3.1 HIGH fix — time-aware mid-week pacing. Sum planned-to-date
        # (only for days where date <= today) so the headline % isn't graded
        # against the FULL week on Wednesday.
        planned_tss_td = 0.0
        planned_z12_td = planned_z34_td = planned_z5p_td = 0.0
        days_elapsed = 0
        on_record = False   # a stored session covers some day of this week

        days_out: list[dict] = []
        # v4.4.0 §6 — pull sessions from the *global* plan map (across all
        # plan weeks) so that ISO-week Monday anchoring doesn't drop a Sun
        # session whose stored plan-week happened to start on Sunday.
        sessions_by_day = dict(_global_plan_sessions_by_day)
        for off in range(7):
            day_d = w_start + timedelta(days=off)
            day_iso = day_d.isoformat()
            # v1.3.1 HIGH fix — count days that have elapsed within this week
            # (date <= today). On Wed: Mon,Tue,Wed → 3. On Sun: 7. On Mon: 1.
            if day_iso <= today_iso:
                days_elapsed += 1
            sess = sessions_by_day.get(day_iso)
            # If this is a history-only shell (no plan), pull only the
            # week's own sessions (which include the synthetic rest rows).
            if (w.get("phase") or "") == "history" or not w.get("sessions"):
                local_map = {s.get("day"): s for s in (w.get("sessions") or [])}
                sess = local_map.get(day_iso) or sess
            day_rides = rides_by_date.get(day_iso, [])

            # Choose primary ride (longest duration); secondary list = rest.
            primary_ride = None
            secondary: list[dict] = []
            if day_rides:
                hydrated = [_load_full_ride(r) for r in day_rides]
                # Sort by duration desc — accept ICU (top-level duration_s),
                # JSON (summary.duration_sec), and FIT (parsed_stats) shapes.
                def _dur(r):
                    return float(
                        r.get("duration_s")
                        or ((r.get("summary") or {}).get("duration_sec"))
                        or ((r.get("parsed_stats") or {}).get("duration_sec"))
                        or 0
                    )
                hydrated.sort(key=_dur, reverse=True)
                primary_ride = hydrated[0]
                secondary = hydrated[1:]

            actual_payload = None
            secondary_list: list[dict] = []
            # v1.8.2 MATCH-IMPL — pass the same-day planned session into the
            # calendar summarizer so the primary ride payload carries a
            # `compare` block (analytics.compare_plan_to_actual). Synthetic
            # history shells don't carry a real plan, so they get None.
            _planned_for_compare = (
                sess if (sess and not sess.get("_synthetic_history")) else None
            )
            if primary_ride is not None:
                actual_payload = _summarize_ride_for_calendar(
                    primary_ride, ftp, planned_session=_planned_for_compare,
                )
                actual_tss += float(actual_payload["tss"] or 0)
                actual_z12  += actual_payload["z1z2_min"]
                actual_z34  += actual_payload["z3z4_min"]
                actual_z5p  += actual_payload["z5plus_min"]
            for r in secondary:
                # Secondary rides are extras (free rides on a planned day),
                # never "the planned one" — pass None to keep them
                # un-compared.
                _sec = _summarize_ride_for_calendar(r, ftp)
                secondary_list.append(_sec)
                # v1.8.25 FIX — secondary rides STILL count toward the week's
                # actual load. Pre-fix only the primary (longest) ride fed
                # actual_tss / zone minutes, so a day with 2+ rides (e.g.
                # commute + trainer) under-counted the week → the on-track bar
                # and completion% read falsely "behind".
                actual_tss += float(_sec.get("tss") or 0)
                actual_z12 += float(_sec.get("z1z2_min") or 0)
                actual_z34 += float(_sec.get("z3z4_min") or 0)
                actual_z5p += float(_sec.get("z5plus_min") or 0)

            planned_payload = None
            if sess and not sess.get("_synthetic_history"):
                on_record = True
                # v1.0.4 IMPL-WIRING — display_name + zwo_duration_min on every
                # calendar planned cell so the dashboard cascade can pick the
                # canonical title and the actual library duration.
                _zwo = sess.get("zwo_file") or ""
                _dn, _zdur = _session_naming_lookup(_zwo, classifications, lib_by_file)
                planned_payload = {
                    # The served file's identity, the one the Today card and
                    # the This Week list read (src/week_view.py).
                    "session_type": _wv.derived_type(
                        sess, lib_by_file, tp._session_type_from_row),
                    # Always "": the dashboard names, colours, gates and opens
                    # a cell by session_type, with content_class as an override
                    # through tables of its own that know only the broad
                    # classes. The read here was of a key no library row
                    # carries; filling it from ContentClass refused to open
                    # 624 of 4,307 files (tempo_intervals, threshold_ladder...)
                    # and turned long rides into plain endurance cells.
                    "content_class": "",
                    "name": sess.get("zwo_name") or sess.get("description") or "",
                    "display_name": _dn,
                    "zwo_duration_min": _zdur,
                    "duration_min": sess.get("duration_min") or 0,
                    "tss": sess.get("tss_estimate") or 0,
                    # v3.4.5 — matched FILE's TSS so the day modal opened from a
                    # calendar cell shows the file's load, not the slot budget.
                    "zwo_tss": (lib_by_file.get(_zwo) or {}).get("TSS"),
                    "score": sess.get("score"),
                    "zwo_file": _zwo,
                    # The rider's own marks on the day, so the This Week strip
                    # (which reads this cell) can show a skip with its undo,
                    # and the day modal opens on the real status.
                    "status": sess.get("status") or "pending",
                    "user_moved": bool(sess.get("user_moved")),
                    "is_race": bool(sess.get("is_race")),
                    "race": sess.get("race"),
                    # P2.1 (G10): execution score for the week-strip badge
                    # (✓ NN) + the day-modal breakdown line.
                    "execution": (sess.get("execution")
                                  if isinstance(sess.get("execution"), dict)
                                  else None),
                }
                pz12, pz34, pz5p = _planned_zone_split_minutes(sess)
                planned_z12 += pz12
                planned_z34 += pz34
                planned_z5p += pz5p
                planned_tss += float(planned_payload["tss"] or 0)
                # v1.3.1 HIGH fix — accumulate to-date totals for the
                # mid-week pacing math.
                if day_iso <= today_iso:
                    planned_z12_td += pz12
                    planned_z34_td += pz34
                    planned_z5p_td += pz5p
                    planned_tss_td += float(planned_payload["tss"] or 0)

            # card_state is emitted on the day-row (not on planned alone) so
            # the UI dispatcher can read it without nesting.
            card_state = _classify_card_state(
                sess if sess else {"session_type": "rest"},
                has_actual=(actual_payload is not None),
                library_lookup=lib_by_file,
            )

            day_out = {
                "date": day_iso,
                "dow": day_d.weekday() + 1,  # 1=Mon..7=Sun
                "is_today": (day_iso == today_iso),
                "planned": planned_payload,
                "actual": actual_payload,
                "card_state": card_state,
                # issue #7 — race day (A or B/C); top-level so This Week + grid
                # render it distinctly without reaching into `planned`.
                "race": (sess.get("race") if sess and sess.get("is_race") else None),
            }
            # v1.3.1 HIGH fix — emit availability_hours / availability_type
            # so the UI can render an UNAVAILABLE badge for user-blocked days.
            avail_entry = availability_map.get(day_iso)
            if avail_entry is not None:
                try:
                    day_out["availability_hours"] = float(avail_entry.get("hours", 0) or 0)
                except (TypeError, ValueError):
                    day_out["availability_hours"] = 0.0
                day_out["availability_type"] = avail_entry.get("type") or ""
            if secondary_list:
                day_out["actual_secondary"] = secondary_list
            days_out.append(day_out)

        completion_pct = 0.0
        if planned_tss > 0:
            completion_pct = round(min(actual_tss / planned_tss, 2.0), 3)
        # v1.3.1 HIGH fix — for the current week, grade actual against the
        # to-date planned target so Wednesday doesn't read as "behind".
        # Past + future weeks keep the full-week ratio (unchanged behavior).
        completion_pct_to_date = completion_pct
        if is_current and planned_tss_td > 0:
            completion_pct_to_date = round(
                min(actual_tss / planned_tss_td, 2.0), 3
            )

        # v4.4.0 — per-week additions: phase_week_index, target_polarized,
        # planned_ctl_eow. phase_week_index counts from the start of the
        # current phase block in the plan (1-based). planned_ctl_eow is
        # filled in below by the second pass once we have a global series.
        phase_name = (w.get("phase") or "").lower()
        _tid_rows = tp.polarized_targets((plan or {}).get("goal") or {})
        target_polarized = _tid_rows.get(
            phase_name, _tid_rows.get("history", {}))

        out_weeks.append({
            "iso_year": wy,
            "iso_week": wk,
            "start_date": w_start.isoformat(),
            "end_date": w_end.isoformat(),
            "phase": w.get("phase") or "",
            "is_stepback": bool(w.get("is_stepback")),
            "is_current": is_current,
            # None, not 0, for a week no stored plan covers: "no plan on
            # record" (week-view contract P4), as /api/week-summary says it.
            "planned_tss": round(planned_tss, 1) if on_record else None,
            "actual_tss": round(actual_tss, 1),
            "planned_z1z2_min": round(planned_z12, 1),
            "actual_z1z2_min": round(actual_z12, 1),
            "planned_z3z4_min": round(planned_z34, 1),
            "actual_z3z4_min": round(actual_z34, 1),
            "planned_z5plus_min": round(planned_z5p, 1),
            "actual_z5plus_min": round(actual_z5p, 1),
            # v1.3.1 HIGH fix — to-date planned totals (sum of planned
            # for days where date <= today, capped at the full week).
            "planned_tss_to_date": round(planned_tss_td, 1),
            "planned_z1z2_min_to_date": round(planned_z12_td, 1),
            "planned_z3z4_min_to_date": round(planned_z34_td, 1),
            "planned_z5plus_min_to_date": round(planned_z5p_td, 1),
            "days_elapsed": days_elapsed,
            "days_total": 7,
            "completion_pct": completion_pct,
            "completion_pct_to_date": completion_pct_to_date,
            "phase_week_index": 0,        # second-pass below
            "phase_weeks_total": 0,        # second-pass below
            "target_polarized": dict(target_polarized),
            "planned_ctl_eow": None,       # second-pass below
            "days": days_out,
        })

    # ── v4.4.0 second pass: phase_week_index + planned_ctl_eow ──────────────
    _annotate_phase_week_indices(out_weeks)
    _annotate_planned_ctl_eow(out_weeks, plan)

    # ── v4.4.0 — top-level on-track summary block (CONCEPT-SCI §6) ──────────
    summary = _build_summary_block(plan, rides, today, out_weeks)

    # v4.6.7 F4 — goal block for the calendar event-day marker. Keeps the
    # field names locked in MASTER_DECISIONS_v467 §4. Defaults applied when
    # plan has no goal block (e.g. legacy plans).
    _goal = plan.get("goal", {}) or {}
    _goal_type = _goal.get("type") or _goal.get("goal_type") or "weeks"
    _event_date = _goal.get("event_date") or _goal.get("target_date")
    _event_date_emit = _event_date if _goal_type in ("event", "event_preparation") else None
    _end_date = out_weeks[-1].get("end_date") if out_weeks else None

    return {
        "today": today_iso,
        "current_iso_week": {"year": iso_year, "week": iso_week},
        "weeks": out_weeks,
        "summary": summary,
        "goal": {
            "type": _goal_type,
            "event_date": _event_date_emit,
            "end_date": _end_date,
        },
    }


@app.get("/api/calendar")
def api_calendar():
    """v4.3.0 B4+B5+B6 + v4.4.0 — unified calendar overlay (intervals.icu-style).

    Returns the merged plan-vs-actual payload per MASTER_DECISIONS_v44 §3.
    Past 12 weeks of history (rides only — no plan) PLUS every week in the
    current plan. Each week aggregates planned/actual TSS + zone-time
    minutes. Each session carries a ``card_state`` field for UI dispatch.

    v4.4.0: ICU-synced activities surface here too. Lazy-syncs on the first
    /api/calendar call after boot if creds are present and last sync was
    > 1 hour ago.
    """
    # v4.4.0 — best-effort ICU sync hook (no-op when creds missing or throttled).
    # v4.4.2 §B2: force-resync if today's date isn't represented locally and
    # last_sync was > 30 min ago (catches "user just rode, ICU has it but
    # cache is stale" case).
    # v1.6.3: fire-and-forget — endpoint must not block on the per-ride FIT
    # augment loop.  Calendar serves the existing local cache; the next
    # /api/calendar after the sync completes will reflect new rides.
    try:
        _kick_lazy_icu_sync(force_if_today_missing=True)
    except Exception as _e:
        _log_error(error_codes.Codes.CALENDAR_ICU_SYNC, exc=_e)

    plan: dict = {}
    json_path = _plan_dir() / "current_plan.json"
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                plan = json.load(f)
        except json.JSONDecodeError as _e:
            # v1.6.0 — corrupt plan file is a render-blocker for the
            # homepage's THIS-WEEK card. Surface E_PLAN_PARSE_CORRUPT in
            # the response so the frontend can paint a clear "Plan
            # unreadable, regenerate" message instead of silently
            # falling back to history-only weeks.
            _log_error(error_codes.Codes.PLAN_PARSE_CORRUPT, exc=_e, path=str(json_path))
            return {
                "error": error_codes.Codes.PLAN_PARSE_CORRUPT,
                "weeks": [],
                "summary": {},
                "current_iso_week": None,
                "today": clock.today().isoformat(),
            }
        except OSError as _e:
            # OS-level read failure is recoverable (perms, transient I/O).
            # Fall through to "no plan" — caller still wants ride history.
            _log_error(error_codes.Codes.PLAN_LOAD_OS_ERROR, exc=_e, path=str(json_path))

    # v4.4.0 — single rides source: ICU + FIT merged via ride_storage; legacy
    # JSON rides retained for back-compat (pre-v4 archive).
    rides: list[dict] = []
    try:
        rides = list(_load_all_rides_safe())
    except Exception as _e:
        _log_error(error_codes.Codes.CALENDAR_RIDES_LOAD, exc=_e)

    try:
        import ride_storage
        legacy = ride_storage.list_rides()
        for r in legacy:
            r.setdefault("ride_id", r.get("id", ""))
            r.setdefault("source", "json")
        rides.extend(legacy)
    except Exception as _e:
        _log_error(error_codes.Codes.CALENDAR_LEGACY_RIDES, exc=_e)

    try:
        return merge_plan_with_rides(plan, rides)
    except Exception as _e:
        _log_error(error_codes.Codes.CALENDAR_MERGE, exc=_e)
        return {
            "error": error_codes.Codes.CALENDAR_MERGE,
            "weeks": [],
            "summary": {},
            "current_iso_week": None,
            "today": clock.today().isoformat(),
        }


def _fetch_ride_for_execution(activity_id) -> "dict | None":
    """P2.1 (G10) — re-fetch the FULL ride record for an activity_id.

    The week-activities collector (:func:`_collect_week_activities`) strips
    time-in-zone, so the execution-score persist step re-fetches the ride
    from the archive (TiZ lives on the ride record: ride_storage
    ``time_in_zone`` / ``hr_time_in_zone``). Matches ``ride_id`` /
    ``external_id`` / the ``icu_<id>`` form; falls back to the legacy JSON
    rides dir. Returns None when not found (callers skip scoring).
    """
    if activity_id in (None, ""):
        return None
    aid = str(activity_id)
    try:
        for r in _load_all_rides_safe():
            if not isinstance(r, dict):
                continue
            rid = str(r.get("ride_id") or "")
            ext = str(r.get("external_id") or "")
            if aid in (rid, ext) or rid == f"icu_{aid}":
                return r
    except Exception as e:
        _log.debug(f"execution ride fetch (archive) failed: {e}")
    try:
        from ride_storage import list_rides
        for r in list_rides():
            if isinstance(r, dict) and aid in (str(r.get("id") or ""),
                                               str(r.get("ride_id") or "")):
                return r
    except Exception as e:
        _log.debug(f"execution ride fetch (legacy) failed: {e}")
    return None


def _execution_for_match(s_json: dict, activity_id) -> "dict | None":
    """P2.1 (G10) — compute the persisted ``execution`` dict for a session
    that just (re-)matched ``activity_id``. Returns None when the ride can't
    be fetched or nothing is scoreable (caller decides what to keep)."""
    ride = _fetch_ride_for_execution(activity_id)
    if ride is None:
        return None
    try:
        from profile_manager import ProfileManager
        mode = ProfileManager.get().target_mode or "power"
    except Exception:
        mode = "power"
    try:
        import execution_score
        result = execution_score.score_ride(s_json, ride, mode)
    except Exception:
        _log.exception("execution score_ride failed")
        return None
    if result.get("score") is None:
        return None
    # ADDITIVE block evaluation: "which prescribed blocks did I actually do?"
    # The three locked axes (duration/load/intensity) cannot see structure —
    # stopping after 10 of 13 reps survives TSS and time-in-zone nearly intact.
    # Never affects score/verdict (both pinned by tests) and never raises: a
    # ride with no laps, a session with no matched file, or laps that do not
    # settle the question simply has no "blocks" key.
    blocks = _block_eval_for(s_json, ride) if BLOCK_EVAL_SURFACED else None
    out = {**result, "activity_id": activity_id,
           "computed_at": clock.now().isoformat()}
    if blocks is not None:
        out["blocks"] = blocks
    return out


# The grader is rebuilt on real lap timestamps and reads the one real lapped
# ride here correctly — but an independent adversarial pass then produced three
# separate false green ticks against it (an abandoned over/under certified
# complete off its 90 % under-legs; a block ridden as two halves with a five
# minute rest reported done in full; a flat 85 % FTP ride with auto-lap read as
# eighteen VO2 blocks), and the library harness still misgrades about 1 % of
# constructed sessions. Every one of those is pinned by a failing test in
# tests/test_357_block_evaluation.py.
#
# Surfaced in v3.7.0: the library harness (15 rider behaviours x every
# interval workout, ~24,700 cases) grades zero wrong, and two independent
# adversarial passes' findings are all closed and pinned as tests.
BLOCK_EVAL_SURFACED = True


def _block_eval_for(s_json: dict, ride: dict) -> "dict | None":
    """Lap-based block grading for a matched session. None when not gradeable.

    v3.7.0 — restored, now that laps carry the offset on the ride clock where
    each of them started (ride_storage persists ICU's start_index). Four
    earlier versions inferred that offset from block shapes and each shipped a
    different class of confident wrong verdict; score_blocks stays silent
    unless the laps settle every block.
    """
    try:
        # FTP-tests IP W1a: tests are graded by their calculator (the FTP
        # suggestion), never by blocks. The 60-min protocol especially —
        # graded as blocks, an hour abandoned at 20 min still reads
        # "completed" (confirmed false green), and the ramp's to-failure
        # shape has no meaningful block contract either.
        if (s_json.get("session_type") or "") == "ftp_test":
            return None
        laps = ride.get("intervals") or []
        zwo = (s_json.get("zwo_file") or "").strip()
        if not laps or not zwo:
            return None
        import structure_fidelity as _sf
        path = active_workout_dir() / os.path.basename(zwo)
        if not path.exists():
            return None
        segs = _sf.parse_zwo_file(path)
        if not segs:
            return None
        ftp = ride.get("ftp_at_ride") or config.ATHLETE_FTP_W
        return _sf.score_blocks(segs, laps, ftp)
    except Exception as e:  # noqa: BLE001 — advisory axis, never break scoring
        _log.debug(f"block eval skipped: {e}")
        return None


def _apply_rematch_preview_to_plan(plan: dict, week_idx: int, preview: dict) -> int:
    """Apply a rematch ``preview`` (from tp.rematch_week) onto the plan's
    week ``week_idx`` sessions — set status + dedup'd completion_matches.
    Mutates ``plan`` in place. Returns the count of sessions whose status
    actually changed. Idempotent (dedup by activity_id) so it's safe to run on
    every sync / Plan-tab open.

    P2.1 (G10): whenever a completion-match entry is written (append OR the
    auto-reconcile update-in-place), the session's ``execution`` score is
    recomputed from the freshly re-fetched full ride — so a re-match to a
    different activity can never leave a stale score behind.
    """
    matches_by_date = {m["session_date"]: m for m in preview.get("matches", [])}
    changed = 0
    for s_json in plan["weeks"][week_idx]["sessions"]:
        day = s_json.get("day")
        m = matches_by_date.get(day)
        if not m:
            continue
        new_status = m["new_status"]
        if s_json.get("status") != new_status:
            changed += 1
        s_json["status"] = new_status
        if m.get("activity_id") and new_status in ("done", "ambiguous", "done_partial"):
            existing = s_json.get("completion_matches") or []
            if not isinstance(existing, list):
                existing = []
            entry = {
                "activity_id": m["activity_id"],
                "matched_axes": m["matched_axes"],
                "score": m["score"],
                "axes": m["axes"],
                "details": m.get("details"),
                "applied_at": clock.now().isoformat(),
            }
            # dedup by activity_id, UPDATE-in-place (auto-reconcile runs every
            # sync now — a blind append would stack duplicates).
            prior = next(
                (i for i, e in enumerate(existing)
                 if isinstance(e, dict) and e.get("activity_id") == m["activity_id"]),
                None,
            )
            if prior is not None:
                existing[prior] = entry
            else:
                existing.append(entry)
            s_json["completion_matches"] = existing
            # P2.1 (G10) — (re)compute the execution score for the entry just
            # written. On fetch failure keep an existing score ONLY if it was
            # computed from the SAME activity (never leave a stale score from
            # a different ride).
            try:
                exec_block = _execution_for_match(s_json, m["activity_id"])
                if exec_block is not None:
                    s_json["execution"] = exec_block
                else:
                    prior_exec = s_json.get("execution")
                    if (isinstance(prior_exec, dict)
                            and prior_exec.get("activity_id") != m["activity_id"]):
                        s_json["execution"] = None
            except Exception:
                _log.exception("execution score persist skipped")
    return changed


def _reconcile_current_week(plan: dict, today: date) -> "tuple[int, dict | None]":
    """v1.8.25 — mark the CURRENT week's sessions done/missed/ambiguous from
    actual activities (the /api/plan/rematch?apply=1 logic), idempotently.
    Mutates ``plan`` in place; returns (sessions_changed, preview|None).

    This is what makes reconciliation AUTOMATIC: the auto-adapt path
    (_apply_plan_update, fired by ride-sync) calls it so completed rides are
    matched to planned sessions on sync — no manual "Reconcile Week" click.
    """
    # Yesterday's plan week too: a finished day is judged the morning after,
    # when a week's last day already belongs to the previous week.
    n, preview, seen = 0, None, set()
    for anchor in (today - timedelta(days=1), today):
        week, week_idx = _load_current_week_dto(plan, anchor)
        if not week or week_idx in seen:
            continue
        seen.add(week_idx)
        actual = _collect_week_activities(week, today, include_today=True)
        preview = tp.rematch_week(week, actual, today)
        n += _apply_rematch_preview_to_plan(plan, week_idx, preview)
    if n:
        plan["last_rematch"] = clock.now().isoformat()
    return n, preview


@app.post("/api/plan/rematch")
async def api_plan_rematch(request: Request, apply: int = Query(0)):
    """Rematch sessions to actual activities (fix26 §6.3, §6.9).

    Query: apply=0 → preview (default, no write). apply=1 → write statuses.

    Classifier (§6.9): 3/3 axes (TSS ±15% AND duration ±20% AND IF-band
    match) → status=done. 2/3 → ambiguous. <2 → no_match → missed if past,
    pending if future. §6.11: missed never auto-dismisses.

    When apply=1, writes back session.status + session.completion_matches[].
    """
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        today = clock.today()
        current_week, week_idx = _load_current_week_dto(plan, today)
        if not current_week:
            return {"action": "no_current_week"}

        actual = _collect_week_activities(current_week, today, include_today=True)
        preview = tp.rematch_week(current_week, actual, today)

        if not apply:
            return {"ok": True, "apply": False, **preview}

        # R3 (2026-07-07): surface the CHANGED count — the helper always
        # computed it and the endpoint discarded it, so the UI showed the
        # week's cumulative match total ("6 rides reconciled") on every
        # planner open even when nothing new happened.
        changed = _apply_rematch_preview_to_plan(plan, week_idx, preview)
        plan["last_rematch"] = clock.now().isoformat()
        tp.atomic_write_plan(json_path, plan)

        return {"ok": True, "apply": True, "changed": changed, **preview}

    except Exception:
        _log.exception("Plan rematch failed")
        return JSONResponse({"detail": "Rematch failed"}, 500)


@app.post("/api/plan/re-draw")
async def api_plan_re_draw(request: Request):
    """FIX-CONTRACT C4 — per-day workout re-draw via JSON body.

    UI U8 contract: body ``{date: "YYYY-MM-DD"}`` (or legacy ``{day: int}``
    as day-of-week index resolved against the current plan start).

    3.3.1 hotfix (B2): now routes through the MODERN retry pair
    (:func:`_pick_redraw_candidate` — the 24-attempt widen_band ladder —
    + :func:`_accept_redraw_apply` + reforecast) instead of delegating to
    the legacy one-shot :func:`api_plan_rematch_day`. The legacy one-shot
    did a single exact-duration ``match_zwo`` with no widen and no retries,
    so the calendar/plan-grid ↻ button dead-ended in "no_candidate" on any
    day the sparse band couldn't serve — while the day-modal Rematch (same
    intent) happily widened its way to a pick. Response stays a superset of
    the old contract: ``{ok, action:"redrawn", day, zwo_file, zwo_name,
    variation}`` on success; ``{ok:false, action:"rest_day"|"no_candidate"}``
    on benign rejections (test_plan_redraw_endpoint pins these). The
    path-param ``/api/plan/rematch/{day}`` endpoint is untouched for any
    external callers.
    """
    try:
        body = await _get_json_body(request)
    except Exception:
        body = {}
    day_iso = str(body.get("date") or body.get("day") or "").strip()
    # Allow day-of-week index (0..6) as a convenience; resolve against the
    # current plan's current week start.
    # v4.2.0 IMPL-WEEKLY: _load_current_week_dto returns a PlannedWeek
    # dataclass, not a dict — was previously subscripted as `wk["start"]`
    # which threw a TypeError swallowed by the bare `except` and caused a
    # 400 fall-through. Use attribute access; .start is already a date.
    if day_iso.isdigit():
        try:
            idx = int(day_iso)
            if 0 <= idx < 7:
                json_path = _plan_dir() / "current_plan.json"
                if json_path.exists():
                    with open(json_path, encoding="utf-8") as f:
                        plan = json.load(f)
                    today = clock.today()
                    wk, _wi = _load_current_week_dto(plan, today)
                    if wk:
                        day_iso = (wk.start + timedelta(days=idx)).isoformat()
        except Exception:
            pass
    if not day_iso:
        return JSONResponse({"error": "date (YYYY-MM-DD) required"}, 400)
    try:
        date.fromisoformat(day_iso)
    except ValueError:
        return JSONResponse({"error": "Invalid date format (use YYYY-MM-DD)"}, 400)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
        try:
            candidate = _pick_redraw_candidate(plan, day_iso)
        except tp.NoCandidateWorkoutError:
            # True exhaustion (widened band included). NOT an error state:
            # the day legitimately remains a fileless zone-target session —
            # the UI maps this action to the "keeps its zone targets" toast.
            return {"ok": False, "action": "no_candidate", "day": day_iso}
        except ValueError as e:
            reason = str(e)
            if reason.startswith("No session at"):
                return JSONResponse({"error": reason}, 404)
            # rest_day keeps its legacy action string (test-pinned); other
            # rejections (e.g. race day) surface their reason in the toast.
            if reason == "rest_day":
                return {"ok": False, "action": "rest_day", "day": day_iso}
            return {"ok": False, "action": "invalid", "error": reason,
                    "day": day_iso}
        apply_res = _accept_redraw_apply(plan, day_iso, candidate)
        tp.atomic_write_plan(json_path, plan)
        return {"ok": True, "action": "redrawn", "day": day_iso,
                "zwo_file": candidate["zwo_file"],
                "zwo_name": candidate["zwo_name"],
                "variation": candidate["variation"],
                "duration_min": candidate.get("duration_min"),
                "tss_estimate": candidate.get("tss_estimate"),
                "sessions_modified": apply_res.get("sessions_modified", 0)}
    except Exception:
        _log.exception("Plan re-draw failed")
        return JSONResponse({"detail": "Re-draw failed"}, 500)


# v1.7.0 — shared helper for the rematch pipeline. preview-redraw picks a
# candidate without persisting; accept-redraw applies the candidate and
# triggers a full reforecast so downstream TSS/availability re-flow. The
# legacy api_plan_rematch_day still exists for backward compat and now
# delegates to this helper.
def _pick_redraw_candidate(plan: dict, day_iso: str, exclude_extra: "list[str] | None" = None) -> dict:
    """Pick a fresh ZWO match for the session at ``day_iso`` without
    mutating the plan on disk.

    Returns ``{ok, day, session_type, zwo_file, zwo_name, variation,
    duration_min, tss_estimate, Category}`` on success. The caller is
    responsible for persisting via ``_accept_redraw_apply`` when the user
    confirms.

    Raises ``tp.NoCandidateWorkoutError`` when match_zwo's pool is empty
    even with relaxed filters; raises ``ValueError`` for missing/rest sessions.
    ``exclude_extra`` is an explicit list of ZWO names to also exclude on
    top of the same-week pool (used by the UI's Reshuffle button so the
    user doesn't see the just-rejected candidate again).
    """
    target_week = None
    target_session = None
    for w in plan.get("weeks", []):
        for s in w.get("sessions", []):
            if s.get("day") == day_iso:
                target_week = w
                target_session = s
                break
        if target_session:
            break
    if not target_session or not target_week:
        raise ValueError(f"No session at {day_iso}")
    if target_session.get("session_type") == "rest":
        raise ValueError("rest_day")
    # FC3 (v2.5.0, L3-10): redraw installed a training ZWO on the race entry.
    if target_session.get("is_race"):
        raise ValueError("race day is fixed — edit the race instead")

    # Same-week workout names — SOFT avoid (variety within the week).
    same_week: set[str] = set()
    for s in target_week.get("sessions", []):
        nm = s.get("zwo_name") or ""
        if nm:
            same_week.add(nm)
    # User-rejected reshuffles this session — HARD exclude (must not reappear).
    hard_exclude: set[str] = {str(nm) for nm in (exclude_extra or []) if nm}

    library = tp.load_workout_library()
    week_num = target_week.get("week_num", 0)
    day_idx = (date.fromisoformat(day_iso) - date.fromisoformat(target_week["start"])).days
    base_variation = int(target_session.get("variation", 0)) + 1

    # v1.9.2 — HARD exclusion via retry loop. match_zwo's used_names is only a
    # soft −15 penalty, so once the (small, exact-duration) candidate tier was
    # cycled the same file kept winning → "Reshuffle did nothing". Retry with a
    # bumped seed until the pick is genuinely NOT in hard_exclude; if every
    # attempt lands on an already-rejected file the distinct pool is exhausted,
    # so we surface ``exhausted`` and the UI tells the user they've seen them all.
    planned = None
    variation = base_variation
    exhausted = False
    for attempt in range(24):
        cand = tp.PlannedSession(
            day=date.fromisoformat(day_iso),
            day_name=target_session.get("day_name", ""),
            session_type=target_session.get("session_type", "z2"),
            duration_min=int(target_session.get("duration_min", 0) or 0),
            tss_estimate=float(target_session.get("tss_estimate", 0) or 0),
            description=target_session.get("description", ""),
        )
        variation = base_variation + attempt
        cand.profile_id = f"{variation}"
        try:
            tp.match_zwo(
                cand, library,
                week_num=week_num + variation * 100,
                day_idx=day_idx,
                used_names=same_week | hard_exclude,
                raise_on_empty=True,
                hr_bias=_hr_bias(),
                exact_duration=True,  # closest-duration tier (v1.8.24)
                # Tester bug (post-3.2.2): a sparse cell's 8%/3-min band can
                # hold ONE alternative — the retry loop then re-offered it
                # forever. Grill P5: an in-band fresh candidate wins at attempt
                # 0 (soft −15 penalty never blocks it) and singleton bands never
                # yield regardless of patience — so widen after 4 attempts, not
                # 8, leaving 20 attempts in the widened band. Grill P3: the
                # widened band grows DOWNWARD only (shorter files), upper edge
                # stays slot+5 — availability holds even on reshuffle.
                widen_band=(attempt >= 4),
             micro_only=bool(((plan or {}).get("goal") or {}).get("vo2_microintervals_only", False)),)
        except tp.NoCandidateWorkoutError:
            # 3.3.1 hotfix (B2): an empty pool at attempts 0-3 must NOT abort
            # the whole ladder — the widened band (attempt >= 4) is exactly
            # the rescue for a slot whose exact-duration tier is empty. Keep
            # climbing; if every attempt (widened included) comes up empty,
            # `planned` stays None and the post-loop check raises the same
            # NoCandidateWorkoutError the callers already map to no_candidate.
            continue
        planned = cand
        if cand.zwo_name and cand.zwo_name not in hard_exclude:
            break  # genuinely new pick
    else:
        # Every attempt returned an already-rejected file → pool exhausted.
        exhausted = True

    if planned is None or not planned.zwo_file:
        raise tp.NoCandidateWorkoutError("no candidate for reshuffle")

    # Library metadata gives us the REAL duration + TSS of the picked
    # workout — pre-v1.7.0 the rematch kept the planner's original
    # tss_estimate even when the new ZWO was very different from the
    # original, so the dashboard's load math was stale.
    lib_meta = next(
        (w for w in library if (w.get("Name") == planned.zwo_name or w.get("File") == planned.zwo_file)),
        {},
    )
    return {
        "ok": True,
        "day": day_iso,
        "session_type": planned.session_type,
        "zwo_file": planned.zwo_file,
        "zwo_name": planned.zwo_name,
        "variation": variation,
        "duration_min": int(round(float(lib_meta.get("Duration(min)") or planned.duration_min or 0))),
        "tss_estimate": round(float(lib_meta.get("TSS") or planned.tss_estimate or 0), 1),
        "if": float(lib_meta.get("IF") or 0),
        "Category": str(lib_meta.get("Category") or ""),
        # v1.9.2 — True when every remaining candidate is already in the
        # reject list: the UI shows "you've seen all options" instead of
        # silently repeating a workout.
        "exhausted": exhausted,
    }


def _accept_redraw_apply(plan: dict, day_iso: str, candidate: dict) -> dict:
    """Mutate ``plan`` in place to install ``candidate`` on ``day_iso``
    and run a full reforecast so downstream TSS / TSB / availability
    flow catches up.

    Caller persists via ``tp.atomic_write_plan``.
    """
    target = None
    for w in plan.get("weeks", []):
        for s in w.get("sessions", []):
            if s.get("day") == day_iso:
                target = s
                break
        if target:
            break
    if not target:
        raise ValueError(f"No session at {day_iso}")
    # FC3 (v2.5.0, L3-10): never install a workout over the race entry.
    if target.get("is_race"):
        raise ValueError("race day is fixed — edit the race instead")

    target["zwo_file"] = str(candidate.get("zwo_file") or "")
    target["zwo_name"] = str(candidate.get("zwo_name") or candidate.get("zwo_file") or "")
    target["variation"] = int(candidate.get("variation") or 0)
    target["status"] = "pending"
    # PIN IT. An accepted redraw is the rider's deliberate choice — the same
    # thing swap-type and ftp-test-type already pin — yet this path left the
    # day unpinned and then ran a reforecast over it. That reforecast applies
    # the availability calendar literally: a 51-min accepted workout on a
    # 60-min day is a 17% change, over the 15% re-match threshold, so it
    # re-matched the day while EXCLUDING the file just accepted. The rider
    # accepted one workout and watched a different one land in the plan.
    target["user_swapped"] = True
    target["adapted"] = False
    # v1.7.0 — actually carry the new workout's TSS / duration into the
    # plan so downstream reforecast (and the dashboard's load math) sees
    # the truth, not the planner's stale estimate.
    new_tss = candidate.get("tss_estimate")
    if new_tss is not None:
        try:
            target["tss_estimate"] = float(new_tss)
        except (TypeError, ValueError):
            pass
    new_dur = candidate.get("duration_min")
    if new_dur is not None:
        try:
            target["duration_min"] = int(new_dur)
        except (TypeError, ValueError):
            pass

    plan["last_rematch_day"] = {
        "date": day_iso,
        "at": clock.now().isoformat(),
        "new_zwo": target["zwo_file"],
    }

    # Mirror the _maybe_auto_reforecast input shape so downstream
    # propagation is consistent with the ride-sync rematch path.
    try:
        try:
            activities = db.query_activities(days=120)
        except Exception:
            activities = []
        try:
            training = cached("training", get_today_metrics)
        except Exception:
            training = {}
        current_tsb = training.get("tsb")
        tsb_series = None
        if current_tsb is not None:
            tsb_series = {}
            for w in plan.get("weeks", []) or []:
                try:
                    ws = date.fromisoformat(w["start"])
                except (KeyError, ValueError, TypeError):
                    continue
                for i in range(7):
                    tsb_series[ws + timedelta(days=i)] = current_tsb
        availability_overrides = {
            day_iso2: float(entry["hours"])
            for day_iso2, entry in plan.get("availability", {}).items()
            if isinstance(entry, dict) and "hours" in entry
        }
        # v1.8.1 SPEED-B: fast path for accept-redraw. A single-session
        # swap does not move the prior week's actual/planned ratio (G4
        # ACWR) nor the rolling polarization split (G3); skipping those
        # blocks cuts reforecast wall-clock from ~1.7 s to <0.5 s warm.
        # Availability scaling + downstream TSS propagation still run so
        # the cascade and per-day duration overrides remain correct.
        plan, sessions_modified, _ri = tp.reforecast_dict(
            plan,
            today_iso=clock.today().isoformat(),
            tsb_series=tsb_series,
            recent_activities=activities,
            availability_overrides=availability_overrides,
            accept_redraw_fast=True,
        )
        return {"ok": True, "day": day_iso, "sessions_modified": sessions_modified}
    except Exception:
        # Reforecast is best-effort: the swap itself is persisted by the
        # caller; downstream propagation can be retried via /api/plan/reforecast.
        _log.exception("accept-redraw reforecast skipped")
        return {"ok": True, "day": day_iso, "sessions_modified": 0, "reforecast": "skipped"}


@app.post("/api/plan/preview-redraw")
async def api_plan_preview_redraw(request: Request):
    """v1.7.0 — preview a rematch candidate without persisting.

    Body: ``{"date": "YYYY-MM-DD", "exclude_extra": [zwo_name, ...]}``
    Returns the candidate payload from ``_pick_redraw_candidate``. The UI
    paints the workout preview chart, then asks the user to Accept /
    Reshuffle (call again with the picked zwo_name appended to
    ``exclude_extra``) / Decline (drop the panel).
    """
    body = await _get_json_body(request)
    day_iso = str(body.get("date") or "").strip()
    exclude_extra = body.get("exclude_extra") or []
    if not day_iso:
        return JSONResponse({"error": "date required"}, 400)
    try:
        date.fromisoformat(day_iso)
    except ValueError:
        return JSONResponse({"error": "Invalid date format (use YYYY-MM-DD)"}, 400)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)
    with open(json_path, encoding="utf-8") as f:
        plan = json.load(f)
    try:
        return _pick_redraw_candidate(plan, day_iso, exclude_extra=exclude_extra)
    except tp.NoCandidateWorkoutError:
        return {"ok": False, "action": "no_candidate", "day": day_iso}
    except ValueError as e:
        reason = str(e)
        return {"ok": False, "action": "invalid", "day": day_iso, "reason": reason}


@app.post("/api/plan/accept-redraw")
async def api_plan_accept_redraw(request: Request):
    """v1.7.0 — accept a previewed rematch candidate.

    Body: ``{"date": "YYYY-MM-DD", "zwo_file": "...", "zwo_name": "...",
    "variation": N, "tss_estimate": N, "duration_min": N}`` (typically
    forwarded verbatim from a prior preview-redraw response).
    Persists the candidate and triggers a full ``tp.reforecast_dict`` so
    downstream sessions adjust to the new TSS / availability flow.
    """
    body = await _get_json_body(request)
    day_iso = str(body.get("date") or "").strip()
    zwo_file = str(body.get("zwo_file") or "").strip()
    if not day_iso or not zwo_file:
        return JSONResponse({"error": "date + zwo_file required"}, 400)
    try:
        date.fromisoformat(day_iso)
    except ValueError:
        return JSONResponse({"error": "Invalid date format"}, 400)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
        result = _accept_redraw_apply(plan, day_iso, body)
        tp.atomic_write_plan(json_path, plan)
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    except Exception:
        _log.exception("Plan accept-redraw failed")
        return JSONResponse({"detail": "accept-redraw failed"}, 500)


# v2.3.0 — valid Swap-type targets (user-facing training types → engine keys).
# P2.3 (v3.0.0, G11): + ftp_test so the retest-nudge banner's "Schedule test"
# button can place a test via this machinery (planner + matcher + rematch
# already speak ftp_test end-to-end).
_SWAP_TYPES = {"recovery", "z2", "tempo", "sweetspot", "threshold",
               "overunder", "vo2max", "sprint", "ftp_test"}


def _swap_session_type_apply(plan: dict, day_iso: str, new_type: str, new_dur: int, micro_only: "bool | None" = None) -> dict:
    """Mutate the session at ``day_iso`` to ``new_type`` + ``new_dur``, pin it
    (user_swapped → reforecast/refit won't demote it), match a workout for the
    new type, then reforecast the rest of the plan so downstream load rebalances.
    Caller persists via ``tp.atomic_write_plan``."""
    target = None
    target_week = None
    for w in plan.get("weeks", []):
        for s in w.get("sessions", []):
            if s.get("day") == day_iso:
                target, target_week = s, w
                break
        if target:
            break
    if not target:
        raise ValueError(f"No session at {day_iso}")
    # FC3 (v2.5.0, L3-10): swap-type pinned a vo2max onto the race day with the
    # race flag still attached (user_swapped then immunized it everywhere).
    if target.get("is_race"):
        raise ValueError("race day is fixed — edit the race instead")
    # FTP-tests IP F8: a maximal protocol is only valid on fresh legs. Refuse
    # converting a day already RIDDEN hard (test-today after intervals reads a
    # depressed FTP that then mis-sets every zone of the next cycle).
    _easy_f8 = {"rest", "z2", "long_z2", "recovery"}
    if (new_type == "ftp_test"
            and target.get("status") in ("done", "done_partial", "ambiguous")
            and target.get("session_type") not in _easy_f8):
        raise ValueError(
            "a hard session was already ridden that day — a valid FTP test "
            "needs fresh legs; schedule it for another day")

    tss_per_h = tp.TSS_PER_HOUR.get(new_type, 60)
    target["session_type"] = new_type
    target["duration_min"] = int(new_dur)
    target["tss_estimate"] = round(new_dur / 60 * tss_per_h)
    target["status"] = "pending"
    target["user_swapped"] = True   # pin: the user's deliberate choice wins
    target["adapted"] = False
    target["zwo_file"] = ""
    target["zwo_name"] = ""

    # Match a workout for the NEW type so the day shows a real session (the user
    # can Rematch afterward). Best-effort — a missing match leaves the type set.
    try:
        excluded = {s.get("zwo_name") for s in (target_week.get("sessions", []) if target_week else []) if s.get("zwo_name")}
        planned = tp.PlannedSession(
            day=date.fromisoformat(day_iso), day_name=target.get("day_name", ""),
            session_type=new_type, duration_min=int(new_dur),
            tss_estimate=float(target["tss_estimate"]), description="",
        )
        library = tp.load_workout_library()
        week_num = target_week.get("week_num", 0) if target_week else 0
        try:
            day_idx = (date.fromisoformat(day_iso) - date.fromisoformat(target_week["start"])).days
        except Exception:
            day_idx = 0
        tp.match_zwo(planned, library, week_num=week_num, day_idx=day_idx,
                     used_names=excluded, raise_on_empty=False,
                     hr_bias=_hr_bias(), micro_only=(bool(((plan or {}).get("goal") or {}).get("vo2_microintervals_only", False)) if micro_only is None else bool(micro_only)))
        if planned.zwo_file:
            target["zwo_file"] = planned.zwo_file
            target["zwo_name"] = planned.zwo_name
            if getattr(planned, "duration_min", None):
                target["duration_min"] = int(planned.duration_min)
            if getattr(planned, "tss_estimate", None):
                target["tss_estimate"] = round(float(planned.tss_estimate))
    except Exception:
        _log.exception("swap-type match_zwo skipped")

    # F8: single-day fresh-legs easing — the generation/recalc passes run
    # _ensure_fresh_legs_before_ftp_tests, but a manually scheduled test lands
    # outside those passes. Ease the PREVIOUS day when it is a hard, still
    # untouched future slot (never a ridden day, never a user-placed one, and
    # yesterday-relative-to-a-test-today stays as ridden history).
    if new_type == "ftp_test":
        try:
            prev_iso = (date.fromisoformat(day_iso) - timedelta(days=1)).isoformat()
            prev = next((s for w in plan.get("weeks", [])
                         for s in w.get("sessions", [])
                         if s.get("day") == prev_iso), None)
            if (prev is not None
                    and prev.get("session_type") not in _easy_f8
                    and prev.get("session_type") != "ftp_test"
                    and prev.get("status", "pending") == "pending"
                    and not prev.get("user_swapped")
                    and not prev.get("user_moved")
                    and not prev.get("is_race")
                    and prev_iso >= clock.today().isoformat()):
                prev["session_type"] = "recovery"
                prev["zwo_file"] = ""
                prev["zwo_name"] = ""
                prev["duration_min"] = min(int(prev.get("duration_min") or 45), 45)
                prev["tss_estimate"] = min(float(prev.get("tss_estimate") or 30.0), 30.0)
                prev["description"] = (
                    "Recovery — eased to leave fresh legs for tomorrow's FTP "
                    "test. A maximal protocol read off a fatigued day sets a "
                    "wrong FTP for the whole cycle.")
        except Exception:
            _log.exception("ftp-test fresh-legs easing skipped")

    plan["last_swap_day"] = {"date": day_iso, "at": clock.now().isoformat(),
                             "new_type": new_type}

    # Reforecast the rest (mirror accept-redraw). The pinned swapped day is
    # protected by the user_swapped guards in reforecast + refit.
    sessions_modified = 0
    try:
        try:
            activities = db.query_activities(days=120)
        except Exception:
            activities = []
        try:
            training = cached("training", get_today_metrics)
        except Exception:
            training = {}
        current_tsb = training.get("tsb")
        tsb_series = None
        if current_tsb is not None:
            tsb_series = {}
            for w in plan.get("weeks", []) or []:
                try:
                    ws = date.fromisoformat(w["start"])
                except (KeyError, ValueError, TypeError):
                    continue
                for i in range(7):
                    tsb_series[ws + timedelta(days=i)] = current_tsb
        availability_overrides = {
            d2: float(entry["hours"])
            for d2, entry in plan.get("availability", {}).items()
            if isinstance(entry, dict) and "hours" in entry
        }
        plan, sessions_modified, _ri = tp.reforecast_dict(
            plan, today_iso=clock.today().isoformat(),
            tsb_series=tsb_series, recent_activities=activities,
            availability_overrides=availability_overrides,
        )
    except Exception:
        _log.exception("swap-type reforecast skipped")

    return {"ok": True, "day": day_iso, "session_type": new_type,
            "duration_min": target["duration_min"], "tss_estimate": target["tss_estimate"],
            "zwo_file": target.get("zwo_file", ""), "zwo_name": target.get("zwo_name", ""),
            "sessions_modified": sessions_modified}


@app.post("/api/plan/swap-type")
async def api_plan_swap_type(request: Request):
    """v2.3.0 — swap a single day to a DIFFERENT training type + duration, then
    reforecast the rest. The swapped day is pinned; Rematch still works after.

    Body: ``{"date":"YYYY-MM-DD", "session_type":"vo2max", "duration_min": 60}``
    """
    body = await _get_json_body(request)
    day_iso = str(body.get("date") or "").strip()
    new_type = str(body.get("session_type") or "").strip()
    try:
        new_dur = int(body.get("duration_min") or 0)
    except (TypeError, ValueError):
        new_dur = 0
    if not day_iso or not new_type:
        return JSONResponse({"error": "date + session_type required"}, 400)
    try:
        date.fromisoformat(day_iso)
    except ValueError:
        return JSONResponse({"error": "Invalid date format"}, 400)
    if new_type not in _SWAP_TYPES:
        return JSONResponse({"error": f"Unknown session_type: {new_type}"}, 400)
    # v3.7.1 — per-swap VO2max protocol choice. Absent key ⇒ fall back to the
    # plan's own preference, so a swap made without touching the control does
    # what the plan would have done. Only meaningful on a VO2max slot; the
    # planner ignores it elsewhere.
    micro_raw = body.get("microintervals_only")
    # Clamp duration to the type ceiling (e.g. vo2max ≤75, sprint ≤45); floor 30.
    ceil = tp.TYPE_CEILING.get(new_type)
    new_dur = new_dur or 60
    if ceil:
        new_dur = min(new_dur, ceil)
    new_dur = max(new_dur, 30)

    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
        _micro = (bool(micro_raw) if micro_raw is not None
                  else bool((plan.get("goal", {}) or {})
                            .get("vo2_microintervals_only", False)))
        result = _swap_session_type_apply(plan, day_iso, new_type, new_dur,
                                          micro_only=_micro)
        tp.atomic_write_plan(json_path, plan)
        return result
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 400)
    except Exception:
        _log.exception("Plan swap-type failed")
        return JSONResponse({"detail": "swap-type failed"}, 500)


def _ftp_test_family(fname: str) -> "str | None":
    # ponytail: the filename already partitions the families — no sub-tags.
    f = (fname or "").lower()
    if f.startswith("ftp_test_ramp"):
        return "ramp"
    if f.startswith("ftp_test_coggan"):
        return "coggan_20min"
    if f.startswith("ftp_test_60min"):
        return "sixty_min"
    return None


# FTP-tests IP W1b: the file each protocol card serves. Ramp is pinned to the
# 6%/min ladder (the ONLY protocol detect_ramp_halt's step reconstruction
# matches: 56% start, +6%/min) — the old closest-to-45min pick landed on the
# 8%/min 43-min file, whose steeper steps over-read FTP for anaerobic riders
# even beyond the ramp's baseline bias. Fallback duration keeps the endpoint
# alive if the preferred file is ever renamed.
# (file, duration_min, nominal TSS). v3.11.3: the measured blocks are FreeRide
# (no target — a test must not erg-lock at the OLD FTP), so the library's
# computed TSS under-counts them; the nominal is the honest load of a real
# attempt and wins when the file's number is lower.
_FTP_TEST_PREFERRED_FILE = {
    "ramp": ("ftp_test_ramp_ladder21_200pct_35min.zwo", 35, 60),
    "coggan_20min": ("ftp_test_coggan_3x1min-1min_95pct_59min.zwo", 59, 75),
    "sixty_min": ("ftp_test_60min_steady_86min.zwo", 86, 100),
}


@app.post("/api/plan/ftp-test-type")
async def api_plan_ftp_test_type(request: Request):
    """v3.2.0 — per-session FTP-test choice. The rider picks ramp or 20-min in
    the workout modal (no global setting); this swaps THAT ftp_test session's
    workout to a file of the chosen family. Body: {date, test_type}."""
    body = await _get_json_body(request)
    day_iso = str(body.get("date") or "").strip()
    test_type = str(body.get("test_type") or "").strip()
    if test_type not in ("ramp", "coggan_20min", "sixty_min"):
        return JSONResponse(
            {"error": "test_type must be ramp, coggan_20min or sixty_min"}, 400)
    try:
        date.fromisoformat(day_iso)
    except ValueError:
        return JSONResponse({"error": "Invalid date"}, 400)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
        target = next((s for w in plan.get("weeks", [])
                       for s in w.get("sessions", []) if s.get("day") == day_iso), None)
        if not target:
            return JSONResponse({"error": f"No session at {day_iso}"}, 404)
        if target.get("session_type") != "ftp_test":
            return JSONResponse({"error": "not an FTP-test session"}, 400)
        # W1b: serve the family's PREFERRED protocol file (ramp = the 6%/min
        # ladder detect_ramp_halt reconstructs); duration-closest is only the
        # renamed-file fallback.
        pref_file, want_dur, nominal_tss = _FTP_TEST_PREFERRED_FILE[test_type]
        cands = [w for w in tp.load_workout_library()
                 if _ftp_test_family(w.get("File", "")) == test_type]
        if not cands:
            return JSONResponse({"error": f"no {test_type} workout in library"}, 404)
        pick = next((w for w in cands if w.get("File") == pref_file), None)
        if pick is None:
            pick = min(cands,
                       key=lambda w: abs((w.get("Duration(min)") or 0) - want_dur))
        target["zwo_file"] = pick["File"]
        target["zwo_name"] = pick.get("Name") or pick["File"]
        target["duration_min"] = int(pick.get("Duration(min)") or want_dur)
        # W1b: keep the load forecast honest — the hour-of-power carries ~2×
        # the TSS of a ramp, and reforecast/eval read tss_estimate.
        target["tss_estimate"] = max(round(float(pick.get("TSS") or 0)), nominal_tss)
        target["ftp_test_type"] = test_type
        target["user_swapped"] = True   # the rider's deliberate choice
        tp.atomic_write_plan(json_path, plan)
        # v3.11.3: hand back the FILE-derived enrichment the day modal reads
        # (display_name / zwo_duration_min / zwo_tss) so the panel refreshes
        # without a full plan reload.
        try:
            _dn, _zdur = _session_naming_lookup(
                pick["File"], tp._load_content_classifications() or {},
                {pick["File"]: pick})
        except Exception:
            _dn, _zdur = (pick.get("Name") or pick["File"]), int(pick.get("Duration(min)") or 0)
        return {"ok": True, "test_type": test_type, "zwo_file": pick["File"],
                "zwo_name": target["zwo_name"],
                "duration_min": target["duration_min"],
                "tss_estimate": target.get("tss_estimate"),
                "display_name": _dn, "zwo_duration_min": _zdur,
                "zwo_tss": pick.get("TSS")}
    except Exception:
        _log.exception("ftp-test-type swap failed")
        return JSONResponse({"detail": "ftp-test-type failed"}, 500)


@app.post("/api/plan/rematch/{day}")
async def api_plan_rematch_day(day: str):
    """P6 (v4.1.0) — re-draw a single day's workout.

    Unlike /api/plan/rematch which is a completion-classifier, this endpoint
    actually re-rolls the ZWO workout for the given day. Excludes the
    session's current ZWO and every other ZWO already used this week, picks
    a new one from the same session_type bucket via match_zwo, persists.

    Keeps the completion-classifier behavior as a fallback if no workouts
    are available: returns action="no_candidate" so the UI can fall back to
    the classifier rematch call.

    v1.7.0 — kept for backward compat. New UI uses preview-redraw +
    accept-redraw so the user can Accept / Decline / Reshuffle and so
    downstream sessions reforecast on accept.
    """
    try:
        date.fromisoformat(day)
    except ValueError:
        return JSONResponse({"error": "Invalid date format (use YYYY-MM-DD)"}, 400)

    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        # Locate the session + containing week
        target_week = None
        target_session = None
        for w in plan.get("weeks", []):
            for s in w.get("sessions", []):
                if s.get("day") == day:
                    target_week = w
                    target_session = s
                    break
            if target_session:
                break
        if not target_session:
            return JSONResponse({"error": f"No session at {day}"}, 404)

        if target_session.get("session_type") == "rest":
            return {"ok": False, "action": "rest_day", "day": day}

        # Build exclusion set: every other session's zwo_name in this week
        # PLUS the current session's current pick (so we get a real re-draw).
        excluded = set()
        for s in target_week.get("sessions", []):
            nm = s.get("zwo_name") or ""
            if nm:
                excluded.add(nm)

        # Build a PlannedSession for match_zwo
        planned = tp.PlannedSession(
            day=date.fromisoformat(day),
            day_name=target_session.get("day_name", ""),
            session_type=target_session.get("session_type", "z2"),
            duration_min=int(target_session.get("duration_min", 0) or 0),
            tss_estimate=float(target_session.get("tss_estimate", 0) or 0),
            description=target_session.get("description", ""),
        )

        library = tp.load_workout_library()
        week_num = target_week.get("week_num", 0)
        day_idx = (date.fromisoformat(day) - date.fromisoformat(target_week["start"])).days

        try:
            # Bump the seed with an incremented variation counter so identical
            # session-type on same day picks a DIFFERENT workout each call.
            variation = int(target_session.get("variation", 0)) + 1
            planned.profile_id = f"{variation}"  # seeds into match_zwo RNG
            tp.match_zwo(
                planned, library,
                week_num=week_num + variation * 100,
                day_idx=day_idx,
                used_names=excluded,
                raise_on_empty=True,
                hr_bias=_hr_bias(),
                # v1.8.24 — closest-duration match on reshuffle (see helper).
                exact_duration=True,
             micro_only=bool(((plan or {}).get("goal") or {}).get("vo2_microintervals_only", False)),)
        except tp.NoCandidateWorkoutError:
            return {"ok": False, "action": "no_candidate", "day": day}

        if not planned.zwo_file:
            return {"ok": False, "action": "no_candidate", "day": day}

        target_session["zwo_file"] = planned.zwo_file
        target_session["zwo_name"] = planned.zwo_name
        target_session["variation"] = variation
        target_session["status"] = "pending"
        plan["last_rematch_day"] = {"date": day, "at": clock.now().isoformat(),
                                    "new_zwo": planned.zwo_file}

        tp.atomic_write_plan(json_path, plan)

        return {"ok": True, "action": "redrawn", "day": day,
                "zwo_file": planned.zwo_file, "zwo_name": planned.zwo_name,
                "variation": variation}
    except Exception:
        _log.exception("Plan rematch-day failed")
        return JSONResponse({"detail": "Rematch-day failed"}, 500)


@app.post("/api/plan/dismiss-session")
async def api_plan_dismiss_session(request: Request):
    """Dismiss a session (§6.8 — stays visible greyed; §6.11 never auto).

    Body: {"date": "YYYY-MM-DD", "undo": bool}
    Sets status=dismissed (or back to pending if undo=true).
    """
    body = await _get_json_body(request)
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan found"}, 404)

    d_iso = str(body.get("date", "")).strip()
    undo = bool(body.get("undo", False))
    if not d_iso:
        return JSONResponse({"error": "date required"}, 400)
    try:
        date.fromisoformat(d_iso)
    except ValueError:
        return JSONResponse({"error": "Invalid date format"}, 400)

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        # L3-11 (v2.5.0): operate on ALL sessions matching the date, not the
        # first match — on a (legacy) duplicated date the earlier row's copy
        # got dismissed while the later row's conflicting copy stayed pending,
        # so the user's dismissal "didn't stick". Post-FC1 duplicates can't be
        # produced anymore; the all-matches loop stays as defense for plans
        # persisted before the clip engine.
        matched = [s for w in plan.get("weeks", [])
                   for s in w.get("sessions", []) if s.get("day") == d_iso]
        if not matched:
            return JSONResponse({"error": f"no session at {d_iso}"}, 404)
        # FC3 (v2.5.0): the race entry is not dismissable — goal-owned.
        if any(s.get("is_race") for s in matched):
            return JSONResponse(
                {"error": "race day is fixed — edit the race instead"}, 400)
        for s in matched:
            if undo:
                s["status"] = "pending"
                s["dismissed_at"] = ""
            else:
                s["status"] = "dismissed"
                s["dismissed_at"] = clock.now().isoformat()

        tp.atomic_write_plan(json_path, plan)

        return {"ok": True, "dismissed": not undo}

    except Exception:
        _log.exception("Plan dismiss-session failed")
        return JSONResponse({"detail": "Dismiss failed"}, 500)


@app.post("/api/plan/auto-recalc")
def api_plan_auto_recalc():
    """Auto-recalculate plan if >7 days since last recalc. Called on tab load.

    A POST since 2026-09-14 (decision D8): it rebuilds and writes the plan,
    and was a GET the Plan tab and the morning adapter called as a read."""

    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return {"action": "no_plan"}

    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        # Check if recalc needed (>7 days since last)
        last_recalc = plan.get("recalc_date") or plan.get("generated", "")
        if last_recalc:
            try:
                last_dt = datetime.fromisoformat(last_recalc)
                # Normalize so naive/aware mismatch can't raise TypeError.
                # Saved plans may include a tz offset (saved_at uses
                # `.astimezone().isoformat()` elsewhere); strip it so the
                # subtraction with `clock.now()` is always homogeneous.
                if last_dt.tzinfo is not None:
                    last_dt = last_dt.replace(tzinfo=None)
                days_since = (clock.now() - last_dt).days
                if days_since < 7:
                    # Still fresh — return event readiness only.
                    # 3.3.2 (Lapo #2): readiness is DECORATIVE on this branch —
                    # its own try, so a readiness/metrics exception can never
                    # convert a FRESH verdict into a full plan rebuild (the old
                    # shared except turned any ValueError/TypeError here into a
                    # silent every-visit recalc).
                    readiness = {}
                    try:
                        training = cached("training", get_today_metrics)
                        current_ctl = _planning_ctl()
                        g = plan.get("goal", {})
                        goal = tp.goal_from_dict(g)
                        if goal.target_date:           # one that parses
                            if goal.longest_ride_h_90d is None:
                                goal.longest_ride_h_90d = _longest_ride_h_90d()
                            readiness = tp.compute_event_readiness(goal, current_ctl)
                    except Exception:  # noqa: BLE001 — decorative readiness only
                        _log.exception("auto-recalc: fresh-branch readiness failed (returning fresh)")
                        readiness = {}
                    return {"action": "fresh", "days_since_recalc": days_since, "event_readiness": readiness}
            except (ValueError, TypeError):
                pass

        # Recalc needed — rebuild plan
        training = cached("training", get_today_metrics)
        current_ctl = _planning_ctl()

        # Reconstruct Goal via the canonical helper — the old inline build here
        # dropped distribution/custom_bands/events/block_periodization/
        # plan_mode/start_date/…, so a weekly recalc rebudgeted non-polarized
        # plans to the sticky process default (same defect as the regen core's
        # inline Goal, fixed the same way). NOTE: recalculate_plan itself runs
        # no _mark_race_days/_apply_secondary_event_tapers pass — carrying
        # goal.events here feeds its adjusted_goal, but the taper gap is a
        # separate engine issue.
        g = plan.get("goal", {})
        goal = _goal_from_plan_dict(g)
        if goal.target_date is None and goal.goal_type != "continuous":
            # Non-event goals (FTP, general): use target_date or default 12
            # weeks out. 3.4.0 W2: NOT for continuous — the rolling goal has
            # no target by definition (grill P1 item 16, the A1 fabrication);
            # recalculate_plan routes it to the extend path regardless.
            goal.target_date = date.fromisoformat(g["target_date"]) if g.get("target_date") else (clock.today() + timedelta(weeks=12))
        # Stale generation-time week count must not outlive the shrinking
        # runway (weeks_available short-circuits on plan_weeks>0 — H1 trap).
        goal.plan_weeks = 0
        # v3.2.0 merge note: the canonical helper carries phase_weeks here
        # too — the engine's validity gate decides applied-vs-fallback and
        # the scheduler write-site mirrors goal._phase_weeks_status.
        # v4.6.7 IMPL-CAP: auto-populate endurance baseline if missing.
        if goal.longest_ride_h_90d is None:
            goal.longest_ride_h_90d = _longest_ride_h_90d()

        # Reconstruct plan weeks.
        # v1.8.20 parity — round-trip ALL session fields via the canonical
        # helper: the old 6-field rebuild zeroed user_moved/status/dismissed_at/
        # completion_matches/is_race/zwo_file/…, so every weekly recalc write
        # wiped rider edits + race markers from every week, past ones included.
        old_weeks = []
        for w in plan.get("weeks", []):
            old_weeks.append(tp.week_from_dict(w))

        # Get eFTP for drift detection
        eftp = None
        wellness = cached("wellness_7", lambda: fetch_wellness(7))
        if wellness:
            for w in reversed(wellness):
                si = w.get("sportInfo", [])
                if si and len(si) > 0 and si[0].get("eftp"):
                    eftp = round(si[0]["eftp"])
                    break

        # v2.0.3 F5 — thread athlete (ftp + weight) so recalculate_plan can
        # compute event targets, matching the generate + regenerate paths
        # (without it, a weekly recalc reverted to the legacy +5/+5 CTL step).
        recalc_athlete = _planner_athlete()

        new_phases, all_weeks, recalc_info = tp.recalculate_plan(
            goal=goal, current_plan_weeks=old_weeks,
            current_ctl=current_ctl, current_eftp=eftp,
            athlete=recalc_athlete,
            recent_weekly_tss=_chronic_weekly_tss_safe(),
        )

        if recalc_info.get("action") == "no_change":
            return {"action": "no_change", **recalc_info}

        # Save updated plan — START from a shallow copy of the ORIGINAL plan so
        # every top-level key survives (availability calendar, adoption/regen
        # markers, …), mirroring the v1.8.20 regen-path fix. Sessions serialize
        # via the canonical helper (all 22 fields): the old inline 8-field dict
        # dropped is_race/race/status/user_moved/dismissed_at/completion_matches
        # from EVERY week on EVERY weekly recalc write.
        plan_dict = dict(plan)
        if new_phases:
            plan_dict["phases"] = [
                {"name": p.name, "weeks": p.weeks,
                 "start": p.start.isoformat(), "end": p.end.isoformat(),
                 "weekly_tss": p.weekly_tss_target, "focus": p.focus}
                for p in new_phases
            ]
        plan_dict["weeks"] = [tp.week_to_dict(w) for w in all_weeks]
        plan_dict["recalc_date"] = clock.now().isoformat()
        plan_dict["recalc_info"] = recalc_info
        # Phase-split editor (v3.2.0, A1): the overlay copy above keeps every
        # old top-level key, so the previous rebuild's phase_weeks_status
        # would survive as a STALE badge — mirror THIS recalc's status: set
        # when the engine surfaced one, pop when it didn't (no custom split).
        _pws = recalc_info.get("phase_weeks_status")
        if _pws:
            plan_dict["phase_weeks_status"] = _pws
        else:
            plan_dict.pop("phase_weeks_status", None)

        tp.atomic_write_plan(json_path, plan_dict)

        return {"action": "recalculated", "plan_json": plan_dict, **recalc_info}

    except Exception:
        # SEC3: don't leak exception text.
        _log.exception("Plan auto-recalc failed")
        return JSONResponse({"action": "error", "detail": "Plan update failed"}, 500)


# ═══════════════════════════════════════════════════════════════════════════════
# GoldenCheetah API (optional)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/gc/status")
def api_gc_status():
    """Check if GoldenCheetah API is running on localhost:12021."""
    import urllib.request
    try:
        req = urllib.request.Request("http://localhost:12021/")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return {"available": True, "status": resp.status}
    except Exception:
        return {"available": False}


# ═══════════════════════════════════════════════════════════════════════════════
# RIDE IMPORT + HISTORY (v4.0.0-alpha — post-ride viewer, no live runtime)
# ═══════════════════════════════════════════════════════════════════════════════
#
# The live ride runtime was removed in the trainer-rip pivot. Rides are now
# recorded outside Domestique (Tacx / MyWhoosh / Golden Cheetah / any FIT
# producer) and imported afterwards via POST /api/ride/import.
#
# Storage layout:
#   ~/.domestique/rides/<iso>.fit  -- raw imported FIT file
# ride_storage.list_rides / get_ride read per-profile JSON rides from the
# existing archive and are still used by /api/rides* for the post-pivot
# history list.


def _resample_series_1hz(ts: list, values: list) -> list:
    """FTP-tests IP — strict 1 Hz grid for the test calculators.

    Zwift writes 1 Hz FITs, but Garmin/Wahoo "smart recording" writes a
    record only when something changes (0.2–1 Hz, uneven). Every FTP-test
    window in fitness_estimation counts SAMPLES as seconds, so on a sparse
    file a "20-min" window would silently span far more wall time and
    under-read the test. Rebuild second-by-second using record timestamps:
    hold the last value across gaps ≤ 3 s (sensor cadence), zero beyond
    (coasting/pause — matches how power meters behave when stopped).

    Returns ``values`` unchanged when timestamps are absent/unusable or the
    stream is already ~1 Hz (the overwhelmingly common case — zero risk to
    the existing Zwift path).
    """
    n = len(values)
    if n < 2 or len(ts) != n:
        return values
    pairs = [(t, v) for t, v in zip(ts, values) if t is not None]
    if len(pairs) < max(2, int(0.9 * n)):
        return values  # too many missing timestamps to trust the grid
    t0 = pairs[0][0]
    tN = pairs[-1][0]
    span = tN - t0
    if span <= 0 or span > 24 * 3600:
        return values
    # Already ~1 Hz? Grill F2: the tolerance must not scale unbounded with
    # ride length — on a 3000-sample ride a flat 2% allowance (60 s) swallowed
    # a genuine 60-s autopause, letting a 20-min window span the pause as
    # contiguous time and over-read the test. Absolute cap 10 s.
    if abs(span - (len(pairs) - 1)) <= max(2.0, min(10.0, 0.02 * len(pairs))):
        return values
    out = [0] * (int(span) + 1)
    prev_t = None
    prev_v = 0
    for t, v in pairs:
        i = int(t - t0)
        if 0 <= i < len(out):
            out[i] = v
        if prev_t is not None:
            gap_lo = int(prev_t - t0) + 1
            gap_hi = min(i, gap_lo + 3)  # hold ≤3 s, zero beyond
            for j in range(gap_lo, gap_hi):
                if 0 <= j < len(out):
                    out[j] = prev_v
        prev_t, prev_v = t, v
    return out


def _sitko_advisory(best_20min_w: int) -> "dict | None":
    """FTP-tests IP F3 — Sitko 2023 (PMID 37802084) level advisory for the
    Coggan 20-min test. The universal 0.95 factor is a population average:
    measured 60-min/20-min ratios ran ~0.96 in professionals down to ~0.88 in
    recreational riders, so one factor over-reads weaker riders. ADVISORY
    ONLY — the suggested FTP stays 0.95 × best-20; the modal just shows the
    level-typical band so the rider can temper an Accept.

    Gated on a touched profile: weight_kg=70.0 AND ftp=200 is the untouched
    default signature (ProfileManager seeds both), and W/kg computed from
    defaults would fabricate the band.
    """
    try:
        if best_20min_w <= 0:
            return None
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        weight = float(getattr(pm, "weight_kg", 0) or 0)
        # Grill pipeline/F4: 70.0 is the SEEDED default — a rider who set FTP
        # but never entered weight still carries it, and a band computed from
        # a fabricated 70 kg is exactly what this gate exists to prevent.
        # Suppress on the default weight unconditionally (a genuine 70.0 kg
        # rider loses an advisory line, never a number).
        if weight <= 0 or abs(weight - 70.0) < 1e-9:
            return None
        wkg = best_20min_w / weight
        if wkg >= 4.7:
            level, band = "well-trained/elite", (0.94, 0.96)
        elif wkg >= 3.5:
            level, band = "trained", (0.91, 0.94)
        else:
            level, band = "recreational", (0.88, 0.91)
        return {
            "sitko_wkg": round(wkg, 2),
            "sitko_level": level,
            "sitko_factor_band": [band[0], band[1]],
            "sitko_ftp_band": [int(round(best_20min_w * band[0])),
                               int(round(best_20min_w * band[1]))],
        }
    except Exception:
        return None


def _parse_fit_stats(fit_path: Path) -> dict:
    """Parse a FIT file and return a shallow stats dict.

    Reads duration, distance, avg/max power, avg/max HR, avg cadence,
    TSS (if the producing app stored it), sport, and record count.
    Uses ``fit_tool.FitFile.from_file`` so the same dependency that
    writes FIT exports also reads them.
    """
    try:
        from fit_tool.fit_file import FitFile
    except Exception as e:
        log_ride_import.warning(f"fit_tool import failed: {e}")
        return {"error": "fit_tool_unavailable"}

    try:
        ff = FitFile.from_file(str(fit_path))
    except Exception as e:
        log_ride_import.warning(f"FIT parse failed ({fit_path.name}): {e}")
        return {"error": "parse_failed", "detail": str(e)[:160]}

    sample_count = 0
    power_sum = 0
    power_n = 0
    power_max = 0
    hr_sum = 0
    hr_n = 0
    hr_max = 0
    cad_sum = 0
    cad_n = 0
    speed_max = 0.0
    distance_m = 0.0
    duration_s = 0
    total_kj = 0.0
    start_time = None
    sport = None
    tss = None
    # T1/T3 (v4.1.0): retain the 1Hz power + cadence streams so we can run
    # FTP-test detection + ramp-halt detection after the scan.
    power_series: list[int] = []
    cadence_series: list[int] = []
    record_ts: list = []  # per-record timestamps (s), parallel to the series
    # FTP-tests IP W2b: the FIT's embedded workout name — Zwift stamps the
    # ZWO's <name> here, and exported FITs are named by DATE, so the file
    # name alone almost never identifies the protocol. This does.
    fit_workout_name = ""

    try:
        for rec in ff.records:
            msg = rec.message
            mtype = type(msg).__name__
            if mtype == "RecordMessage":
                sample_count += 1
                try:
                    _ts = _fit_val(msg, "timestamp")
                    if _ts is not None:
                        _tsf = float(_ts)
                        # fit_tool serves ms-since-epoch; tolerate seconds too.
                        record_ts.append(_tsf / 1000.0 if _tsf > 1e11 else _tsf)
                    else:
                        record_ts.append(None)
                except Exception:
                    record_ts.append(None)
                p_val = 0
                try:
                    pw = _fit_val(msg, "power")
                    if pw is not None:
                        p = int(pw)
                        power_sum += p
                        power_n += 1
                        p_val = p
                        if p > power_max:
                            power_max = p
                except Exception:
                    pass
                power_series.append(p_val)
                try:
                    hr = _fit_val(msg, "heart_rate")
                    if hr is not None:
                        h = int(hr)
                        hr_sum += h
                        hr_n += 1
                        if h > hr_max:
                            hr_max = h
                except Exception:
                    pass
                c_val = 0
                try:
                    cad = _fit_val(msg, "cadence")
                    if cad is not None:
                        cad_sum += int(cad)
                        cad_n += 1
                        c_val = int(cad)
                except Exception:
                    pass
                cadence_series.append(c_val)
                try:
                    sp = _fit_val(msg, "speed")
                    if sp is not None and float(sp) > speed_max:
                        speed_max = float(sp)
                except Exception:
                    pass
                try:
                    d = _fit_val(msg, "distance")
                    if d is not None:
                        distance_m = max(distance_m, float(d))
                except Exception:
                    pass
            elif mtype == "SessionMessage":
                try:
                    v = _fit_val(msg, "total_timer_time")
                    if v is not None:
                        duration_s = int(round(float(v)))
                except Exception:
                    pass
                try:
                    v = _fit_val(msg, "total_distance")
                    if v is not None:
                        distance_m = max(distance_m, float(v))
                except Exception:
                    pass
                try:
                    v = _fit_val(msg, "total_work")
                    if v is not None:
                        total_kj = float(v) / 1000.0
                except Exception:
                    pass
                try:
                    v = _fit_val(msg, "training_stress_score")
                    if v is not None:
                        tss = float(v)
                except Exception:
                    pass
                try:
                    sport = str(_fit_val(msg, "sport") or "")
                except Exception:
                    sport = None
                try:
                    v = _fit_val(msg, "start_time")
                    if v is not None:
                        start_time = str(v)
                except Exception:
                    pass
            elif mtype == "WorkoutMessage":
                try:
                    v = (getattr(msg, "workout_name", None)
                         or _fit_val(msg, "wkt_name"))
                    if v:
                        fit_workout_name = str(v)
                except Exception:
                    pass
    except Exception as e:
        log_ride_import.warning(f"FIT record scan aborted: {e}")

    # T1/T2/T3 (v4.1.0): FTP-test detection + formula-driven suggestion +
    # ramp-halt post-hoc scan. Guarded so a corrupted stream won't break
    # the whole import — detection failures just omit the extra fields.
    is_ftp_test = False
    ftp_test_type = None
    ftp_test_suggestion = None
    ftp_test_halted = False
    ftp_test_halt_at_step = None
    # E3 (v4.1.0): local eFTP suggestion from best-efforts. Built from the
    # retained power series with the same scaling table the live-session
    # planner uses. We only surface a suggestion (don't auto-apply) when
    # it differs from stored FTP by >3%.
    local_eftp_suggestion = None
    try:
        import fitness_estimation as _fe
        # Strict 1 Hz grid for the test math — smart-recording FITs are
        # sparse and would silently stretch every "20-min" window.
        power_series = _resample_series_1hz(record_ts, power_series)
        cadence_series = _resample_series_1hz(record_ts, cadence_series)
        # W2b: hint = file name + the FIT's embedded workout name (the
        # detector normalizes separators, so the ZWO's display <name>
        # matches its family token too). evaluate_ftp_test is the SINGLE
        # recognition+calculation entry point shared with the ICU sync path
        # (power_curve backfill) — the two paths can never drift.
        # FIX-CONTRACT M3: prior_ftp / pct_delta / method carried on the
        # suggestion payload so U1's banner renders the delta line.
        _eval = _fe.evaluate_ftp_test(
            power_series, cadence_series=cadence_series,
            filename_hint=[fit_path.name, fit_workout_name],
            prior_ftp=int(config.ATHLETE_FTP_W or 0),
        )
        if _eval:
            is_ftp_test = True
            ftp_test_type = _eval["ftp_test_type"]
            ftp_test_suggestion = _eval["ftp_test_suggestion"]
            ftp_test_halted = bool(_eval.get("ftp_test_halted"))
            ftp_test_halt_at_step = _eval.get("ftp_test_halt_step")
            # F3: Sitko level advisory — profile-dependent, so applied here
            # (fitness_estimation stays profile-free). Any suggestion scored
            # by the 20-min method qualifies, including a sixty_min fallback.
            if ftp_test_suggestion.get("method") == "coggan_20min":
                _sitko = _sitko_advisory(
                    int(ftp_test_suggestion.get("best_20min") or 0))
                if _sitko:
                    ftp_test_suggestion.update(_sitko)
    except Exception as _e:
        log_ride_import.debug(f"FTP-test detection skipped: {_e}")

    # E3 (v4.1.0): local eFTP estimate from best-efforts for every ride.
    # Only surfaces a suggestion when stored FTP is known AND the local
    # estimate differs by >3% — a proposal, not an auto-apply. F5 handles
    # the 7-day-sustained auto-apply separately.
    try:
        if power_series and config.ATHLETE_FTP_W and config.ATHLETE_FTP_W > 0:
            from fitness_estimation import estimate_ftp, extract_best_efforts
            from training_live import RideSample
            samples = [RideSample(power=int(p or 0)) for p in power_series]
            best_efforts = extract_best_efforts(samples)
            local_ftp = estimate_ftp(best_efforts)
            if local_ftp:
                pct_diff = abs(local_ftp - config.ATHLETE_FTP_W) / config.ATHLETE_FTP_W * 100
                if pct_diff > 3:
                    local_eftp_suggestion = {
                        "suggested_ftp": int(local_ftp),
                        "current_ftp": int(config.ATHLETE_FTP_W),
                        "diff_pct": round(pct_diff, 1),
                        "source": "eftp_local",
                    }
    except Exception as _e:
        log_ride_import.debug(f"local eFTP suggestion skipped: {_e}")

    out = {
        "sport": sport,
        "start_time": start_time,
        "sample_count": sample_count,
        "duration_sec": duration_s,
        "distance_km": round(distance_m / 1000.0, 3) if distance_m else 0.0,
        "avg_power": round(power_sum / power_n) if power_n else 0,
        "max_power": int(power_max),
        "avg_hr": round(hr_sum / hr_n) if hr_n else 0,
        "max_hr": int(hr_max),
        "avg_cadence": round(cad_sum / cad_n) if cad_n else 0,
        "max_speed_mps": round(float(speed_max), 3),
        "total_kj": round(total_kj, 1),
        "tss": tss,
        # T1/T2/T3 (v4.1.0):
        "is_ftp_test": is_ftp_test,
        "ftp_test_type": ftp_test_type,
        "ftp_test_suggestion": ftp_test_suggestion,
        "ftp_test_halted": ftp_test_halted,
        "ftp_test_halt_at_step": ftp_test_halt_at_step,
        # FIX-CONTRACT M4: UI reads ftp_test_halt_step (shorter); expose both.
        "ftp_test_halt_step": ftp_test_halt_at_step,
        # E3 (v4.1.0):
        "local_eftp_suggestion": local_eftp_suggestion,
    }
    # v1.3.6: wire compute_ride_xss into the FIT-import path so future
    # rides populate ss_cp_daily / ss_w_prime_daily / ss_pmax_daily in
    # athlete_metrics. Pre-fix this hook only existed in tests, which is
    # why the 3D-fitness chart placeholder fired forever — there were no
    # SS rows for _augment_wellness_with_3d_fitness to convolve.
    try:
        import ride_storage as _rs136
        _rs136.compute_ride_xss(power_series, started_at=start_time, summary=out)
    except Exception as _e:
        log_ride_import.debug(f"compute_ride_xss skipped in _parse_fit_stats: {_e}")
    return out


@app.post("/api/ride/import")
async def api_ride_import(
    fit_file: UploadFile = File(...),
):
    """Import a FIT file from the user's recording app.

    Accepts a multipart/form-data body with one file part keyed ``fit_file``.
    Persists the raw bytes under ``~/.domestique/rides/<iso>.fit`` using the
    upload time as the id; that keeps ids stable + sortable and gives the
    caller a cheap name for later GET by id.

    Returns ``{ride_id, parsed_stats}`` where ``parsed_stats`` is the shallow
    analytics dict from :func:`_parse_fit_stats`. Errors short-circuit with
    a 4xx so the UI can render a message.
    """
    if not fit_file or not fit_file.filename:
        raise HTTPException(status_code=400, detail="fit_file required")

    # Validate extension (defence; the UI sets accept=".fit" but the API
    # is also callable from curl/Postman).
    lower = fit_file.filename.lower()
    if not (lower.endswith(".fit") or lower.endswith(".gz")):
        raise HTTPException(status_code=400, detail="file must have .fit extension")

    data = await fit_file.read()
    # Rough upper bound -- rides longer than ~24h would blow past this;
    # 50 MB covers every real-world case. Reject anything larger so we
    # do not silently consume disk on a bad upload.
    if len(data) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="file too large (>50 MB)")
    if len(data) < 32:
        raise HTTPException(status_code=400, detail="file too small to be a FIT")

    # Stable id: upload time in ISO form (filesystem-safe ":" replacement).
    now = clock.now(timezone.utc).astimezone()
    ride_id = now.strftime("%Y-%m-%dT%H-%M-%S")
    fit_path = _rides_fit_dir() / f"{ride_id}.fit"

    # Collision resilience: append a counter if two uploads land in the
    # same second. Extremely rare in practice; cheap to cover.
    _c = 1
    while fit_path.exists():
        ride_id_try = f"{ride_id}_{_c}"
        fit_path = _rides_fit_dir() / f"{ride_id_try}.fit"
        _c += 1
        if _c > 50:
            raise HTTPException(status_code=409, detail="id collision; try again")
    ride_id = fit_path.stem

    try:
        fit_path.write_bytes(data)
    except OSError as e:
        log_ride_import.exception(f"ride import write failed: {e}")
        raise HTTPException(status_code=500, detail="failed to persist FIT")

    parsed = _parse_fit_stats(fit_path)

    # W4 (v2.5.0): compute + persist the load sidecar (tss + load_source)
    # at ingestion so load_all_rides / CTL / ATL / weekly-actual count this
    # ride without re-parsing the FIT on every listing (pre-W4 imports are
    # lazily backfilled by ride_storage.load_all_rides). Best-effort: a
    # failure never blocks the import; readers stay untouched.
    try:
        import ride_storage as _rs_w4
        _load = _rs_w4.write_fit_load_sidecar(fit_path)
        if isinstance(_load, dict) and _load.get("tss") is not None:
            # Surface in the import response; never clobber a TSS the FIT
            # itself carried (pre-existing behavior for power rides).
            if parsed.get("tss") is None:
                parsed["tss"] = _load["tss"]
            parsed.setdefault("load_source", _load.get("load_source"))
    except Exception as _e:
        log_ride_import.debug(f"W4 load sidecar skipped: {_e}")

    log_ride_import.info(
        f"EVENT=ride_imported id={ride_id} bytes={len(data)} "
        f"samples={parsed.get('sample_count')} "
        f"duration_s={parsed.get('duration_sec')} "
        f"distance_km={parsed.get('distance_km')}"
    )

    # F7 (v4.1.0) — fire-and-forget FIT upload to Intervals.icu.
    # Only attempts when credentials are present; failure is logged but
    # never propagates — the local import is the source of truth.
    icu_upload = {"ok": False, "skipped": True}
    try:
        from training import upload_fit_to_icu
        res = upload_fit_to_icu(fit_path, retry=1)
        icu_upload = res
        if res.get("ok"):
            log_ride_import.info(
                f"EVENT=icu_fit_upload_relayed id={ride_id} status={res.get('status')}"
            )
        else:
            log_ride_import.warning(
                f"EVENT=icu_fit_upload_failed id={ride_id} "
                f"status={res.get('status')} detail={res.get('detail')}"
            )
    except Exception as _e:
        log_ride_import.warning(f"ICU upload skipped: {_e}")

    # v1.0.3 — best-effort auto-reforecast: a FIT import = 1 new ride.
    # Helper swallows all exceptions; the import response stays unchanged.
    _maybe_auto_reforecast("default", 1)

    return {
        "ride_id": ride_id,
        "bytes": len(data),
        "parsed_stats": parsed,
        "icu_upload": icu_upload,
    }


# ─── Saved-ride archive (pre-existing JSON rides produced by the old
# live session path). Kept for backward compatibility so historical rides
# remain browsable in the post-ride viewer. New rides land via
# POST /api/ride/import above.


# ═══════════════════════════════════════════════════════════════════════════════
# v4.4.0 — ICU activity sync into local rides store
# ═══════════════════════════════════════════════════════════════════════════════

# Last-sync-at markers. v3.4.2 M7: PER-PROFILE — resolved through the
# ride_storage AC2a seams so the marker sits next to the archive it throttles
# (<profile>/rides/icu/ and <profile>/wellness/). These were the last two
# paths still pointing at the legacy GLOBAL ~/.domestique locations, which
# (a) leaked one athlete's sync timestamp into every other profile — a fresh
# profile skipped its initial sync for up to 1h, (b) made db.purge_profile_data
# contract A4 a lie (it wipes <profile>/rides/icu/ incl. .last_sync_at, but
# the readers looked at the global file), and (c) kept resurrecting the dead
# global dirs on every stamp.
def _migrate_legacy_sync_marker(marker: Path, legacy: Path) -> None:
    """One-time move of a pre-M7 legacy GLOBAL last-sync marker into the
    active profile's marker location.

    Only fires when the profile marker doesn't exist yet AND the legacy
    global file does: the profile active at upgrade time inherits the
    timestamp (existing installs keep their throttle window instead of
    mass re-syncing), then the legacy file is unlinked so a later-created
    profile — or a post-purge re-resolve — can never inherit another
    athlete's timestamp. Best-effort: any failure degrades to
    "never synced", never raises.
    """
    try:
        if marker.exists() or not legacy.exists():
            return
        marker.write_text(
            legacy.read_text(encoding="utf-8").strip(), encoding="utf-8"
        )
        try:
            legacy.unlink()
        except OSError:
            pass
        _log.info(f"migrated legacy sync marker {legacy} -> {marker}")
    except Exception as e:
        _log.debug(f"legacy sync marker migration skipped: {e}")


def _icu_sync_state_path() -> Path | None:
    """Marker for the last rides sync — ``<profile>/rides/icu/.last_sync_at``
    via ride_storage._icu_rides_dir (single AC2a source of truth), so a
    profile switch or purge takes its throttle state with it.

    No active profile → None ("never synced"); readers/writers no-op —
    this path must NEVER raise (it sits under every lazy-sync hook).
    """
    try:
        import ride_storage as _rs_m7
        marker = _rs_m7._icu_rides_dir() / ".last_sync_at"
    except Exception:
        return None
    _migrate_legacy_sync_marker(
        marker, _user_data_dir / "rides" / "icu" / ".last_sync_at"
    )
    return marker


def _icu_wellness_sync_state_path() -> Path | None:
    """v4.5.0 — separate marker for last wellness sync (rides + wellness
    have different cadence requirements: rides matter ASAP after a ride,
    wellness is daily).

    v3.4.2 M7: per-profile ``<profile>/wellness/.last_sync_at`` via
    ride_storage._wellness_dir — same contract as _icu_sync_state_path
    (None when no active profile; one-time legacy-global move-in)."""
    try:
        import ride_storage as _rs_m7
        marker = _rs_m7._wellness_dir() / ".last_sync_at"
    except Exception:
        return None
    _migrate_legacy_sync_marker(
        marker, _user_data_dir / "wellness" / ".last_sync_at"
    )
    return marker


def _icu_credentials_present() -> bool:
    """True iff ICU is configured — OAuth OR legacy Basic auth.

    v2.2.8 FIX: OAuth-only setups carry an ``ICU_ACCESS_TOKEN`` (Bearer) and an
    EMPTY ``ICU_API_KEY``. This gate previously required ICU_API_KEY, so every
    sync path that checks it (``_sync_icu_activities``, the lazy-sync hook, etc.)
    early-returned ``no_credentials`` for OAuth users — recent rides silently
    never synced even though ``training.py`` authenticates fine via the Bearer
    token. Mirror training.py's auth precedence: access token first, Basic pair
    as the fallback.
    """
    if getattr(config, "ICU_ACCESS_TOKEN", None):
        return True
    return bool(
        getattr(config, "ICU_ATHLETE_ID", None)
        and getattr(config, "ICU_API_KEY", None)
    )


def _read_last_sync_at() -> float | None:
    p = _icu_sync_state_path()
    if p is None or not p.exists():
        return None
    try:
        return float(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_last_sync_at(ts: float) -> None:
    p = _icu_sync_state_path()
    if p is None:
        _log.debug("_write_last_sync_at skipped: no active profile")
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(ts), encoding="utf-8")
    except OSError as e:
        _log.debug(f"_write_last_sync_at failed: {e}")


def _read_last_wellness_sync_at() -> float | None:
    p = _icu_wellness_sync_state_path()
    if p is None or not p.exists():
        return None
    try:
        return float(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_last_wellness_sync_at(ts: float) -> None:
    p = _icu_wellness_sync_state_path()
    if p is None:
        _log.debug("_write_last_wellness_sync_at skipped: no active profile")
        return
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(ts), encoding="utf-8")
    except OSError as e:
        _log.debug(f"_write_last_wellness_sync_at failed: {e}")


def _sync_icu_wellness(force: bool = False, days: int = 90) -> dict:
    """v4.5.0 — pull recent ICU wellness records + persist locally.

    Returns ``{added, total}`` counters. Skips with reason when:
      - ICU credentials missing
      - last wellness sync < 1h ago and force=False
    """
    if not _icu_credentials_present():
        return {"added": 0, "total": 0, "skipped": "no_credentials"}

    if not force:
        last = _read_last_wellness_sync_at()
        if last is not None and (time.time() - last) < 3600:
            try:
                import ride_storage as _rs
                total = len(_rs.load_recent_wellness(days=days))
            except Exception:
                total = 0
            return {"added": 0, "total": total, "skipped": "throttled"}

    import training as _training
    import ride_storage as _rs

    # AC1: snapshot the owning profile at TASK START — the fetch below runs
    # outside any lock; every write section is gated on the snapshot.
    _snap = _sync_task_snapshot()

    try:
        records = _training.fetch_recent_wellness(days=days)
    except Exception as e:
        _log.warning(f"_sync_icu_wellness: fetch failed: {e}")
        try:
            total = len(_rs.load_recent_wellness(days=days))
        except Exception:
            total = 0
        return {"added": 0, "total": total, "skipped": f"fetch_failed:{type(e).__name__}"}

    added = 0
    try:
        for rec in records or []:
            if not isinstance(rec, dict):
                continue
            with db.sync_write_gate(_snap):
                path = _rs.persist_wellness(rec)
            if path is not None:
                added += 1

        with db.sync_write_gate(_snap):
            _write_last_wellness_sync_at(time.time())
    except db.SyncAborted:
        # Profile switched mid-task — writes stop; already-persisted records
        # went to the snapshot profile's dirs, nothing lands in the new one.
        _log.info("_sync_icu_wellness: aborted mid-task (profile switched)")
        return {"added": added, "total": added, "skipped": "profile_switched"}
    try:
        total = len(_rs.load_recent_wellness(days=days))
    except Exception:
        total = added
    return {"added": added, "total": total}


# v1.7.5: bumped from 5.0 → 45.0. v1.7.2 set the timeout aggressively
# because the rematch UI surfaced the augment latency directly. v1.6.3
# moved ICU sync (and the augment call inside it) to a background daemon
# thread, so there is no UX urgency. A ~3h Garmin Fenix 8 FIT carries
# 30k+ records and fit_tool parse takes ~20s; 5s was killing every
# real-world ride's DFA computation in a permanent retry loop (status
# 'timeout' is retry-eligible, but the next retry would also hit 5s
# → never converged). 45s comfortably covers 95th-percentile rides
# while still bailing on truly pathological FITs.
_DFA_AUGMENT_TIMEOUT_S: float = 45.0

# v2.0.5 — the 45s cap above is safe ONLY for fresh records (heavy-FIT parse
# headroom) and the explicit force=backfill path. The user-facing sync
# (/api/rides/sync) runs this augment SYNCHRONOUSLY on the request thread — the
# "background daemon" the comment above assumes is not the path the Plan-tab
# catch-up overlay awaits. So a record that already failed once at 45s must NOT
# burn another 45s on every subsequent sync: re-tries of a 'timeout' record use
# a short cap, and after _DFA_MAX_TIMEOUT_RETRIES the 'timeout' status goes
# sticky (skipped like icu_deleted) — these are rides with no recoverable RR
# data that will never converge. Auto-heals on a _DFA_ALGO_VERSION bump.
_DFA_AUGMENT_RETRY_TIMEOUT_S: float = 5.0
_DFA_MAX_TIMEOUT_RETRIES: int = 2
# Overall wall-clock budget for the augment loop inside ONE foreground sync.
# Once exceeded, remaining records skip augment this call and are picked up on
# the next sync (they keep a non-final status, so they stay retry-eligible).
# Keeps the whole sync comfortably under the UI's 40s per-step ceiling even if
# a batch of slow/fresh records lands at once (e.g. first sync after a break).
_SYNC_AUGMENT_BUDGET_S: float = 25.0

# v2.2.9 — live activity-sync progress so the dashboard banner can show
# "Syncing X of Y activities · N new" + a real % instead of an indeterminate
# "Syncing activities…" that never reported counts (and looked stuck). Written
# by _sync_icu_activities as it processes the ICU feed; read by
# /api/sync/progress. Plain dict guarded by a lock (single-flight sync, but the
# poller reads concurrently from the request thread).
_sync_progress_lock = threading.Lock()
_icu_sync_progress: dict = {
    "state": "idle",   # idle | running | done
    "total": 0,        # activities fetched this sync
    "done": 0,         # activities processed so far
    "added": 0,        # NEW activities persisted
    "updated": 0,
    "ts": 0.0,         # last-update epoch (for stale detection)
}


def _set_sync_progress(**kw) -> None:
    with _sync_progress_lock:
        _icu_sync_progress.update(kw)
        _icu_sync_progress["ts"] = time.time()

# v2.2.11 — serialize the progress-owning activity sync. The lazy kick is
# single-flight (_icu_sync_in_progress), but the FORCED path (/api/rides/sync)
# never shared that guard, so a lazy + a forced sync could run _sync_icu_activities
# concurrently and interleave writes to the single _icu_sync_progress global —
# the home banner then bounced ("11 of 49 → 30 → 16…"). This lock lets only ONE
# activity sync run at a time; a second concurrent call returns "already_running"
# without resetting progress.
_sync_exec_lock = threading.Lock()

# v1.8.14 — DFA algorithm version. Bump whenever the DFA α1 maths changes in
# a way that invalidates previously-stored values, so already-"computed"
# records (normally sticky) get recomputed on the next sync/backfill instead
# of keeping a stale value forever.
#   v1: pre-v1.8.14, no RR-artifact rejection (systematically biased α1 low).
#   v2: v1.8.14, Malik 20% artifact filter (analytics._filter_rr_artifacts).
#   v3: v1.8.14, also stores HRVT1/HRVT2 (HR+power) + 3-zone intensity minutes
#       (analytics.compute_dfa_threshold_analysis).
_DFA_ALGO_VERSION: int = 3


def _augment_icu_record_with_dfa(rec_path: Path, external_id: str,
                                   force: bool = False) -> None:
    """v1.0.7 — fetch raw FIT for an ICU activity, compute DFA α1, merge back.

    Skips work when the persisted record already has a final
    ``dfa_alpha1_status`` (one of ``computed`` / ``no_rr_data`` /
    ``sanity_rejected``). The ``fetch_failed`` status is retried so a
    transient ICU outage doesn't permanently disable DFA for that ride.

    v1.6.3: hard 5 s timeout on the combined fetch+parse step. Without the
    cap a single slow ICU response or a >10k-record FIT file could keep
    the background sync thread busy for minutes (and pre-v1.6.3, when the
    sync was synchronous, hang the request thread). On timeout the record
    is persisted with ``dfa_alpha1_status='timeout'`` so the next sync
    retries it.
    """
    import json as _json
    import tempfile as _tempfile
    import os as _os
    import concurrent.futures as _futures

    try:
        rec = _json.loads(rec_path.read_text(encoding="utf-8"))
    except Exception as e:
        _log.debug(f"_augment_icu_record_with_dfa: read failed: {e}")
        return
    if not isinstance(rec, dict):
        return

    # Final-state statuses are sticky; only retry fetch_failed and timeout.
    # v1.8.10 — ``icu_deleted`` is sticky too (no point hammering ICU for
    # an activity it has deleted). Backfill endpoint can clear it via
    # ``force=True`` if the user manually re-uploaded.
    # v1.8.14 — a record is sticky ONLY if it was also computed by the CURRENT
    # DFA algorithm version. After the artifact-filter fix the algo version
    # bumped to 2, so every record stamped with an older (or absent) version
    # falls through and recomputes on the next sync/backfill — auto-healing
    # stale α1 values WITHOUT requiring the user to force a backfill. Records
    # with genuinely no RR data (no_rr_data) still re-run once under v2 but
    # land back on no_rr_data harmlessly; ``icu_deleted`` likewise re-checks
    # once (the activity may have been re-uploaded since).
    existing_status = rec.get("dfa_alpha1_status")
    _stamped_version = rec.get("dfa_algo_version")
    _timeout_count = int(rec.get("dfa_timeout_count") or 0)
    if not force and _stamped_version == _DFA_ALGO_VERSION:
        if existing_status in (
                "computed", "no_rr_data", "sanity_rejected", "icu_deleted"):
            return
        # v2.0.5 — a 'timeout' record retried _DFA_MAX_TIMEOUT_RETRIES times is
        # sticky too: no recoverable RR data, only burns the sync budget.
        if existing_status == "timeout" and _timeout_count >= _DFA_MAX_TIMEOUT_RETRIES:
            return

    import training as _training

    def _fetch_and_compute() -> "dict | None":
        # v1.8.10 augment chain — try lazy paths before the heavyweight
        # FIT fetch. Each step is gated so a fast path can succeed even
        # when ICU has deleted the underlying activity.
        #
        # Order:
        #   1. Cached streams.hrv on the local record  — zero network.
        #   2. Live fetch of streams.hrv from ICU       — single GET, fast.
        #   3. Live fetch of the raw FIT from ICU       — heavyweight fallback.
        #
        # Returns the dfa dict on the first successful path, or None when
        # all three fail. Runs in a worker thread so the outer
        # .result(timeout=_DFA_AUGMENT_TIMEOUT_S) can abandon it.
        from analytics import (
            compute_dfa_alpha1_from_hrv_stream as _dfa_from_stream,
            compute_dfa_alpha1_for_fit as _dfa_from_fit,
            compute_dfa_threshold_analysis as _dfa_thresholds,
        )

        def _with_thresholds(out, streams):
            # v1.8.14 — when α1 computed from a stream dict that ALSO carries
            # heartrate + watts, attach HRVT1/HRVT2 (HR+power) + 3-zone minutes.
            # Best-effort: any failure leaves the base α1 result intact.
            if not (isinstance(out, dict) and out.get("dfa_alpha1_status") == "computed"):
                return out
            try:
                ta = _dfa_thresholds(streams.get("hrv"),
                                     streams.get("heartrate"),
                                     streams.get("watts"))
                out["dfa_hrvt1"] = ta.get("hrvt1")
                out["dfa_hrvt2"] = ta.get("hrvt2")
                out["dfa_zone_minutes"] = ta.get("zone_minutes")
            except Exception as e:
                _log.debug(f"_dfa_thresholds({external_id}) failed: {e}")
            return out

        # Step 1 — local cached streams.
        try:
            local_streams = rec.get("streams")
            if isinstance(local_streams, dict):
                hrv = local_streams.get("hrv")
                if isinstance(hrv, list) and any(s for s in hrv):
                    out = _dfa_from_stream(hrv)
                    if isinstance(out, dict) and out.get("dfa_alpha1_status") == "computed":
                        return _with_thresholds(out, local_streams)
                    # If lazy stream path produced "no_rr_data" because the
                    # cached blob was a power-only stream with hrv=[None,...],
                    # fall through to fresh fetch.
                    if isinstance(out, dict) and out.get("rr_intervals_count", 0) > 0:
                        return out
        except Exception as e:
            _log.debug(f"_augment_icu_record_with_dfa({external_id}) local-stream path: {e}")

        # Step 2 — fresh streams fetch.
        try:
            fresh = _training.fetch_activity_streams(external_id)
            if isinstance(fresh, dict):
                hrv = fresh.get("hrv")
                if isinstance(hrv, list) and any(s for s in hrv):
                    out = _dfa_from_stream(hrv)
                    if isinstance(out, dict) and out.get("dfa_alpha1_status") == "computed":
                        return _with_thresholds(out, fresh)
                    if isinstance(out, dict) and out.get("rr_intervals_count", 0) > 0:
                        return out
        except Exception as e:
            _log.debug(f"_augment_icu_record_with_dfa({external_id}) fresh-stream path: {e}")

        # Step 3 — FIT fallback.
        try:
            raw = _training.fetch_activity_fit_file(external_id)
        except Exception as e:
            _log.debug(f"fetch_activity_fit_file({external_id}) raised: {e}")
            return None
        if not raw:
            return None
        tmp_path = None
        try:
            with _tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as tf:
                tf.write(raw)
                tmp_path = tf.name
            return _dfa_from_fit(Path(tmp_path))
        except Exception as e:
            _log.debug(f"_augment_icu_record_with_dfa({external_id}) compute: {e}")
            return None
        finally:
            if tmp_path:
                try:
                    _os.unlink(tmp_path)
                except OSError:
                    pass

    dfa: "dict | None" = None
    timed_out = False
    # v2.0.5 — fresh records (and the explicit force=backfill path) get the full
    # 45s for heavy-FIT parses; a record already stuck at 'timeout' failed once
    # at 45s, so cap its retry short to keep the foreground sync responsive.
    _cap = (_DFA_AUGMENT_RETRY_TIMEOUT_S
            if (not force and existing_status == "timeout")
            else _DFA_AUGMENT_TIMEOUT_S)
    # NOTE: do NOT use ``with ThreadPoolExecutor(...)`` — its ``__exit__``
    # calls ``shutdown(wait=True)`` which would block until the worker
    # finishes, defeating the timeout entirely. Construct with daemon-style
    # threads (default since Python 3.9 is non-daemon, so call
    # ``shutdown(wait=False, cancel_futures=True)`` explicitly and let the
    # worker thread continue running in the background; it lives on the
    # ICU-sync daemon thread anyway, so it dies at process exit).
    _ex = _futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dfa_augment")
    try:
        fut = _ex.submit(_fetch_and_compute)
        try:
            dfa = fut.result(timeout=_cap)
        except _futures.TimeoutError:
            timed_out = True
            fut.cancel()
            _log.debug(
                f"_augment_icu_record_with_dfa({external_id}) "
                f"timed out after {_cap}s"
            )
    finally:
        _ex.shutdown(wait=False, cancel_futures=True)

    if timed_out:
        rec["dfa_alpha1_status"] = "timeout"
        # v2.0.5 — count retries so the status goes sticky after
        # _DFA_MAX_TIMEOUT_RETRIES (the skip-gate above), instead of being
        # re-attempted on every sync forever.
        rec["dfa_timeout_count"] = _timeout_count + 1
        rec.setdefault("dfa_alpha1_avg", None)
        rec.setdefault("dfa_alpha1_series", [])
        rec.setdefault("dfa_alpha1_lt1_minutes", None)
        rec.setdefault("rr_intervals_count", 0)
    elif not isinstance(dfa, dict):
        # v1.8.10 — when ALL three augment paths failed (no local stream,
        # ICU 404'd both /streams and /fit-file), the activity is gone from
        # ICU and there's no way to recover the RR data. Mark sticky as
        # ``icu_deleted`` so subsequent backfill passes skip it instead of
        # hammering ICU forever. The DFA diagnostic on the homepage will
        # surface the count to the user.
        rec["dfa_alpha1_status"] = "icu_deleted"
        rec.setdefault("dfa_alpha1_avg", None)
        rec.setdefault("dfa_alpha1_series", [])
        rec.setdefault("dfa_alpha1_lt1_minutes", None)
        rec.setdefault("rr_intervals_count", 0)
    else:
        rec["dfa_alpha1"] = dfa.get("dfa_alpha1_avg")
        rec["dfa_alpha1_avg"] = dfa.get("dfa_alpha1_avg")
        rec["dfa_alpha1_series"] = dfa.get("dfa_alpha1_series") or []
        rec["dfa_alpha1_lt1_minutes"] = dfa.get("dfa_alpha1_lt1_minutes")
        rec["dfa_alpha1_status"] = dfa.get("dfa_alpha1_status") or "no_rr_data"
        rec["rr_intervals_count"] = int(dfa.get("rr_intervals_count") or 0)
        # v1.8.14 (algo v3) — HRVT1/HRVT2 + 3-zone intensity minutes, when the
        # stream path produced them (None on the FIT-only fallback path).
        rec["dfa_hrvt1"] = dfa.get("dfa_hrvt1")
        rec["dfa_hrvt2"] = dfa.get("dfa_hrvt2")
        rec["dfa_zone_minutes"] = dfa.get("dfa_zone_minutes")

    # v1.8.14 — stamp the algorithm version on every write path (timeout,
    # icu_deleted, and computed). The sticky-skip gate above compares this
    # against _DFA_ALGO_VERSION so a future maths change auto-recomputes
    # stale records. timeout is non-sticky regardless, but stamping it keeps
    # the field present on every record for consistent diagnostics.
    rec["dfa_algo_version"] = _DFA_ALGO_VERSION

    try:
        rec_path.write_text(_json.dumps(rec, indent=2), encoding="utf-8")
    except OSError as e:
        _log.debug(f"_augment_icu_record_with_dfa: write back: {e}")


def _sync_icu_activities(force: bool = False) -> dict:
    """v2.2.11 — serialized wrapper. Only one activity sync runs at a time so the
    shared progress global isn't clobbered by an overlapping run (lazy + forced).
    A second concurrent call returns ``already_running`` with the current ride
    count instead of resetting progress. A genuine "Sync now" still runs whenever
    nothing else is mid-flight (the common case)."""
    if not _sync_exec_lock.acquire(blocking=False):
        return {
            "added": 0, "updated": 0,
            "total": len(_load_all_rides_safe()),
            "status": "already_running",
            "skipped": "already_running",
            "last_sync_at": _read_last_sync_at(),
        }
    try:
        return _sync_icu_activities_locked(force=force)
    finally:
        _sync_exec_lock.release()


def _sync_icu_activities_locked(force: bool = False) -> dict:
    """v4.4.0 — pull recent ICU activities + persist normalized records.

    Returns ``{added, updated, total, status, last_sync_at}`` counters. The
    ``status`` field is ``"ok"`` on a successful fetch, ``"throttled"`` when
    the 1h throttle blocked the call, ``"no_credentials"`` when ICU is not
    configured, or ``"fetch_failed"`` when the network/API call raised
    (paired with a ``skipped`` reason explaining why). v4.5.1 §B1 added
    ``status`` + ``last_sync_at`` (epoch float, may be None) so the UI can
    render a truthful toast instead of always reading "0 new rides".

    ``force=True`` bypasses the throttle (used by ``POST /api/rides/sync``).
    """
    if not _icu_credentials_present():
        return {
            "added": 0, "updated": 0, "total": 0,
            "status": "no_credentials",
            "skipped": "no_credentials",
            "last_sync_at": _read_last_sync_at(),
        }

    last = _read_last_sync_at()
    if not force:
        if last is not None and (time.time() - last) < 3600:
            return {
                "added": 0, "updated": 0,
                "total": len(_load_all_rides_safe()),
                "status": "throttled",
                "skipped": "throttled",
                "last_sync_at": last,
            }

    import training as _training
    import ride_storage as _rs

    # AC1: snapshot the owning profile at TASK START (this function runs on the
    # domestique.icu_sync daemon thread and on forced request threads). The
    # network fetch stays OUTSIDE any lock; each write below is gated.
    _snap = _sync_task_snapshot()

    # v3.5.3 — incremental window. .last_sync_at was only a 1-hour throttle;
    # the fetch itself always pulled the full 90-day window and re-persisted
    # every activity in it, so every app open looked (and cost) like a full
    # resync. Bound the window by time-since-last-sync instead: 7-day floor
    # because ICU's window filters on activity START date, not upload time
    # (a ride uploaded days late must still fall inside), +2d slack, 90-day
    # cap preserves first-sync and post-OAuth-reset (_write_last_sync_at(0))
    # behavior unchanged.
    # force=True ("Sync Now", the today-missing kick) gets the FULL window —
    # it is the user's escape hatch for late-uploaded rides and ICU-side
    # edits older than the incremental window; without it a ride recorded
    # 10 days ago but uploaded today would be silently unreachable forever.
    _sync_days = 90 if (force or not last) else min(90, max(7, int((time.time() - last) / 86400) + 2))
    try:
        activities = _training.fetch_recent_activities(days=_sync_days)
    except Exception as e:
        _log.warning(f"_sync_icu_activities: fetch failed: {e}")
        return {
            "added": 0, "updated": 0,
            "total": len(_load_all_rides_safe()),
            "status": "fetch_failed",
            "skipped": f"fetch_failed:{type(e).__name__}",
            "last_sync_at": _read_last_sync_at(),
        }

    added = 0
    updated = 0
    aborted = False
    added_paths: list[Path] = []
    icu_dir = _rs._icu_rides_dir()
    # v2.2.9 — publish live progress so the dashboard banner shows real counts
    # ("Syncing X of Y activities · N new") instead of an indeterminate strip.
    _activities = activities or []
    _set_sync_progress(state="running", total=len(_activities), done=0, added=0, updated=0)
    # v2.0.5 — bound the TOTAL augment time for this foreground sync. The augment
    # runs synchronously on the request thread the Plan-tab catch-up overlay
    # awaits; without a ceiling a batch of slow records (e.g. first sync after a
    # break) blows past the UI's 40s step timeout and strands the overlay,
    # skipping the reconcile that adapts the week.
    _aug_deadline = time.monotonic() + _SYNC_AUGMENT_BUDGET_S
    _aug_deferred = 0
    for _idx, a in enumerate(_activities, 1):
        # Cycling-ish only — same filter the planner uses elsewhere. The
        # ICU activities feed includes runs/swims that we don't model.
        sport = (a.get("type") or a.get("sport_type") or "").lower()
        if sport and "ride" not in sport and "cycling" not in sport and "bike" not in sport and "virtual" not in sport:
            # We still keep all activities — the user may want to see them in
            # the calendar. Removing this filter altogether would be safer; we
            # err on the side of inclusive.
            pass
        ext = a.get("id") or a.get("activity_id")
        if not ext:
            _set_sync_progress(done=_idx, added=added, updated=updated)
            continue
        existed = (icu_dir / f"{ext}.json").exists()
        # AC1: each persist is a gated write section — a profile switch between
        # activities aborts the rest of the pass (writes never re-resolve to
        # the NEW profile's dirs).
        try:
            with db.sync_write_gate(_snap):
                path = _rs.persist_icu_activity(a)
        except db.SyncAborted:
            aborted = True
            _log.info("_sync_icu_activities: aborted mid-pass (profile switched)")
            break
        if path is None:
            _set_sync_progress(done=_idx, added=added, updated=updated)
            continue
        if existed:
            updated += 1
        else:
            added += 1
            added_paths.append(path)
        _set_sync_progress(done=_idx, added=added, updated=updated)
        # v1.0.7 — augment the persisted ICU record with DFA α1 from the raw
        # FIT (when ICU's payload didn't carry it). We only fetch the FIT once
        # per activity (skip when the persisted record already has a non-null
        # ``dfa_alpha1_status``). Failure is non-fatal — the record stays as
        # persisted, status='fetch_failed' so the dashboard can render copy.
        # v2.0.5 — past the per-sync augment budget, skip enrichment for the
        # remaining records; they keep a non-final status and are augmented on
        # the next sync (prevents the loop from blowing the UI's 40s ceiling).
        if time.monotonic() < _aug_deadline:
            try:
                _augment_icu_record_with_dfa(path, str(ext))
            except Exception as e:
                _log.debug(f"_augment_icu_record_with_dfa({ext}) failed: {e}")
        else:
            _aug_deferred += 1

    _set_sync_progress(state="done", done=len(_activities), added=added, updated=updated)
    if _aug_deferred:
        _log.info(
            f"_sync_icu_activities: DFA augment budget ({_SYNC_AUGMENT_BUDGET_S}s) "
            f"reached — deferred {_aug_deferred} record(s) to next sync"
        )
    if aborted:
        # AC1: never stamp .last_sync_at for the NEW profile after a mid-pass
        # switch (it would suppress the new profile's own first sync for 1h).
        if added or updated:
            clear_cache()
        return {
            "added": added, "updated": updated,
            "total": len(_load_all_rides_safe()),
            "status": "aborted",
            "skipped": "profile_switched",
            "last_sync_at": _read_last_sync_at(),
        }
    now = time.time()
    try:
        with db.sync_write_gate(_snap):
            _write_last_sync_at(now)
    except db.SyncAborted:
        _log.info("_sync_icu_activities: last_sync_at stamp skipped (profile switched)")
    # v3.5.3 — one-time sport backfill for ICU envelopes OUTSIDE the (now
    # incremental) fetch window. The sport key only started persisting in
    # v3.5.3, and the bounded window above means old files would never be
    # re-fetched to pick it up — leaving historical hikes/runs mislabeled as
    # rides forever. db.activities.id IS the ICU id and its sport column was
    # always mapped correctly, so copy it across once (marker-guarded).
    try:
        with db.sync_write_gate(_snap):
            _rs.backfill_icu_sports_from_db()
            # v3.8.1 (issue #9) — drop blank records a Strava-origin stub wrote
            # before the guard existed. Cheap, idempotent, and it is what lets
            # the rider's own FIT import finally win the dedupe.
            _rs.purge_stub_icu_records()
            # Deletions made ON intervals.icu propagate here too: sync only
            # ever upserted, so a ride deleted upstream (the double-upload
            # case: Karoo + Garmin, one copy removed) stayed on the calendar
            # forever. Ids come from the raw fetched list — not the persisted
            # subset — and the fetch provably succeeded to reach this line;
            # the aborted pass skips this block entirely.
            if not aborted:
                _pruned = _rs.prune_deleted_icu_records(
                    {str(a.get("id") or a.get("activity_id"))
                     for a in _activities
                     if a.get("id") or a.get("activity_id")},
                    _sync_days)
                if _pruned:
                    # Not counted as "updated" — the toast would report ghost
                    # rides as new activity. The cache drop is what matters:
                    # every reader must stop seeing the pruned records now.
                    clear_cache()
    except db.SyncAborted:
        pass
    except Exception as e:  # noqa: BLE001 — best-effort heal, never break sync
        _log.debug(f"sport backfill swallowed: {e}")
    # v2.2.6 perf — new/updated rides were just written to disk; drop the
    # memoised archive caches (cached("all_rides") + cached("recent_dfa_
    # decoupling")) so the `total` below — and every downstream reader on this
    # and the lazy-sync path — sees them immediately, not after the 5-min TTL.
    if added or updated:
        clear_cache()
    total = len(_load_all_rides_safe())
    # The sync fetches and stores; adapting the plan to what arrived is the
    # caller's (POST /api/rides/sync). The lazy sync that reads kick off used
    # to adapt here, and so a GET wrote the plan (week-view contract P8).
    # v1.3.0 IMPL-PR-DETECTION: queue toasts for newly-imported rides that
    # landed at least one major PR. Per PATCH G7: ONE line per ride. Best-
    # effort — failures must not break the sync return shape.
    if added > 0 and added_paths:
        try:
            _maybe_queue_pr_toasts(added_paths)
        except Exception as e:
            _log.debug(f"_maybe_queue_pr_toasts swallowed: {e}")
    return {
        "added": added, "updated": updated, "total": total,
        "status": "ok",
        "last_sync_at": now,
    }


def _sync_icu_rides_and_wellness(force: bool = False) -> dict:
    """v4.5.0 — combined helper: rides + wellness in one call.

    Used by ``POST /api/rides/sync?force=1`` and the lazy-sync hook so a
    single user-triggered "Sync now" pulls both rides and wellness without
    requiring two round-trips. Returns the rides counters plus
    ``wellness_added`` and ``wellness_total``.
    """
    rides_result = _sync_icu_activities(force=force)
    wellness_result = _sync_icu_wellness(force=force)
    return {
        **rides_result,
        "wellness_added": wellness_result.get("added", 0),
        "wellness_total": wellness_result.get("total", 0),
        "wellness_skipped": wellness_result.get("skipped"),
    }


def _chronic_weekly_tss_safe() -> "float | None":
    """The rider's chronic weekly load off the memoised archive; None when
    the archive cannot answer, so the planner falls back to CTL x 7. Every
    planning entry point passes this in: derived inside the planner, the
    fallback parsed the archive again (the ultrareview of 2026-09-14)."""
    try:
        import ride_storage as _rs
        return _rs.chronic_weekly_tss(_load_all_rides_safe())
    except Exception:
        return None


def _load_all_rides_safe() -> list[dict]:
    """Defensive wrapper around ride_storage.load_all_rides().

    v2.2.6 perf — load_all_rides() parses the whole ride archive (~500ms on a
    large data dir) and has many callers across the home path. Memoise via the
    shared 5-min cache (cleared by clear_cache() on sync / FIT import) so a
    home-page load pays it once, not once per card. Callers treat the returned
    list as read-only (the one caller that sorts already copies it first).
    """
    return cached("all_rides", _load_all_rides_uncached)


def _load_all_rides_uncached() -> list[dict]:
    try:
        import ride_storage as _rs
        return _rs.load_all_rides()
    except Exception as e:
        _log.warning(f"load_all_rides failed: {e}")
        return []


def _longest_ride_h_90d(rides: list[dict] | None = None) -> float | None:
    """v4.6.7 IMPL-CAP: longest single-ride duration (hours) in last 90 days.

    Auto-populates ``Goal.longest_ride_h_90d`` so the capability projection
    has an endurance baseline without forcing the user to enter one. Returns
    None when no recent rides have a usable duration_s field.
    """
    if rides is None:
        rides = _load_all_rides_safe()
    if not rides:
        return None
    cutoff = (clock.today() - timedelta(days=90)).isoformat()
    longest_s = 0
    for r in rides:
        started = (r.get("started_at") or "")[:10]
        if not started or started < cutoff:
            continue
        dur_s = r.get("duration_s") or r.get("moving_s") or 0
        try:
            dur_s = int(dur_s)
        except (TypeError, ValueError):
            continue
        if dur_s > longest_s:
            longest_s = dur_s
    if longest_s <= 0:
        return None
    return round(longest_s / 3600.0, 2)


def _aggregate_best_efforts_90d(rides: list[dict] | None = None,
                                profile_id: str = "default") -> dict[int, int]:
    """v1.3.0 shim — aggregates best efforts across rides into a 4-tier dict.

    GRILL-WAVE2A W2A-G1 fix: honours BOTH call patterns (rides supplied vs not).
    Capability-projection at app.py:6150 passes a pre-filtered last-30d ride
    list and EXPECTS those exact rides to be aggregated (including FIT-imported
    rides not yet in ICU cache). The earlier "rides arg is unused" implementation
    silently dropped FIT rides — that's the BLOCKER fix here.

    Modes:
    1. ``rides`` supplied (legacy capability-projection path): walks the
       supplied list, returns per-duration max watts seen across those rides.
       Honours the caller's filtering (last-30d in capability case).
    2. ``rides`` is None (v1.3.0 default): delegates to
       ``power_curve.aggregate_power_curve(profile_id, 90)`` for a 90-day
       window from the local ICU cache.

    Either way, output shape is locked to {180, 300, 600, 1200} → max_w
    (the original Monod-tier contract).

    """
    target_durations = (180, 300, 600, 1200)
    if rides is not None:
        # Legacy mode (capability-projection): walk the supplied rides + aggregate
        # per-tier max watts. Honours caller's filtering.
        result: dict[int, int] = {d: 0 for d in target_durations}
        for r in rides:
            efforts = r.get("efforts") or []
            for e in efforts:
                try:
                    secs = int(e.get("secs") or 0)
                    watts = int(e.get("watts") or 0)
                except (TypeError, ValueError):
                    continue
                if secs in target_durations and watts > result[secs]:
                    result[secs] = watts
        # Drop tiers with zero (no caller-supplied effort at that duration)
        return {d: w for d, w in result.items() if w > 0}
    # v1.3.0 default mode: delegate to power_curve. AC2a: an un-passed
    # profile_id resolves to the ACTIVE profile (was hardcoded "default").
    import power_curve
    if profile_id == "default":
        profile_id = _active_profile_id_or_default()
    full = power_curve.aggregate_power_curve(profile_id, window_days=90)
    return {pt["duration_s"]: pt["watts"] for pt in full["rider_curve"]
            if pt["duration_s"] in target_durations}


def _pr_toast_queue_path() -> Path:
    """v1.3.0 IMPL-PR-DETECTION: per-version toast queue file.

    Stores ``[{"ride_id": ..., "started_at": ..., "n_prs": int}]`` for rides
    that landed major PRs since the last dashboard open. Drained by the GET
    /api/ride/.../prs/toast-queue endpoint.
    """
    return domestique_home() / "last_pr_toast.json"


def _read_pr_toast_queue() -> list[dict]:
    p = _pr_toast_queue_path()
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [e for e in data if isinstance(e, dict)]
    except (json.JSONDecodeError, OSError) as e:
        _log.debug(f"_read_pr_toast_queue: {e}")
    return []


def _write_pr_toast_queue(entries: list[dict]) -> None:
    p = _pr_toast_queue_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except OSError as e:
        _log.warning(f"_write_pr_toast_queue: {e}")


def _maybe_queue_pr_toasts(added_paths: list[Path]) -> None:
    """v1.3.0 IMPL-PR-DETECTION: append a toast entry for each newly-imported
    ride that had at least one major PR.

    Per PATCH G7: caps at ONE line per ride ("New PRs on yesterday's ride —
    open ride detail to see all"). The full PR list is in the ride summary;
    this queue just signals to the dashboard which rides to highlight.
    """
    if not added_paths:
        return
    queue = _read_pr_toast_queue()
    seen = {e.get("ride_id") for e in queue if isinstance(e, dict)}
    for path in added_paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        prs = data.get("prs") or []
        if not isinstance(prs, list):
            continue
        n_major = sum(1 for p in prs if isinstance(p, dict)
                      and p.get("tier") == "major")
        if n_major <= 0:
            continue
        ride_id = data.get("ride_id") or ""
        if not ride_id or ride_id in seen:
            continue
        queue.append({
            "ride_id": ride_id,
            "started_at": data.get("started_at") or "",
            "n_prs": n_major,
        })
        seen.add(ride_id)
    _write_pr_toast_queue(queue)


@app.post("/api/ride/{ride_id}/rpe")
async def api_ride_rpe(ride_id: str, request: Request):
    """v3.5.6 — store the rider's session RPE for a ride.

    Foster modified CR-10 (0-10 integer). That scale is used because the whole
    session-RPE evidence base is built on it; Borg 6-20 and CR-100 are
    statistically interchangeable but produce different ABSOLUTE numbers, which
    would silently break every published threshold. ``rpe_scale`` is pinned on
    the record so a future scale change cannot reinterpret old ratings.

    Optional by design: never required, never blocks ride ingestion, and
    ``DELETE``-like clearing is supported by posting ``null``. Timing is
    deliberately permissive — the popular "wait 30 minutes" rule is NOT
    supported by the evidence that tested it (session-RPE is stable from ~5 min
    to 24 h post-exercise), so we accept whenever the rider gets to it and let
    readers judge staleness from ``rpe_at``.
    """
    if not isinstance(ride_id, str) or not re.match(r"^[\w\-]+$", ride_id or ""):
        return JSONResponse({"error": "bad ride_id"}, 400)
    try:
        body = await _get_json_body(request)
    except Exception:
        body = {}
    raw = body.get("rpe", None)
    if raw is None:
        rpe = None
    else:
        try:
            rpe = int(round(float(raw)))
        except (TypeError, ValueError):
            return JSONResponse({"error": "rpe must be a number 0-10"}, 400)
        if not (0 <= rpe <= 10):
            return JSONResponse({"error": "rpe out of range 0-10"}, 400)
    import ride_storage as _rs
    # v3.6.0 — a FIT-only rider (no intervals.icu account) has no ICU record to
    # hold rider input, so their rating goes in the FIT's load sidecar. The
    # control renders for every ride, so both id forms must work; before this
    # it 404'd for `fit_` and the rating silently failed.
    if ride_id.startswith("fit_"):
        ok = _rs.set_fit_rpe(
            ride_id[4:], rpe,
            clock.now().isoformat() if rpe is not None else None)
        if not ok:
            return JSONResponse({"error": "unknown ride"}, 404)
        clear_cache()
        return {"ok": True, "ride_id": ride_id, "rpe": rpe,
                "scale": "foster_cr10" if rpe is not None else None}
    ext = ride_id[4:] if ride_id.startswith("icu_") else ride_id
    path = _rs._icu_rides_dir() / f"{ext}.json"
    if not path.exists():
        return JSONResponse({"error": "unknown ride"}, 404)
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
        if rpe is None:
            for k in ("rpe", "rpe_at", "rpe_scale"):
                rec.pop(k, None)
        else:
            rec["rpe"] = rpe
            rec["rpe_at"] = clock.now().isoformat()
            rec["rpe_scale"] = "foster_cr10"
        path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError) as e:
        _log.warning(f"rpe write failed for {ext}: {e}")
        return JSONResponse({"error": "write failed"}, 500)
    clear_cache()
    return {"ok": True, "ride_id": ride_id, "rpe": rpe,
            "scale": "foster_cr10" if rpe is not None else None}


@app.get("/api/ride/{ride_id}/prs")
def api_ride_prs(ride_id: str):
    """v1.3.0 — return the cached PR list for a ride.

    Reads ``prs[]`` from the persisted ICU envelope; recomputes lazily when
    the field is missing (older rides imported before v1.3.0). 404 when the
    ride id is unknown.
    """
    if not isinstance(ride_id, str) or not ride_id or len(ride_id) > 80:
        return JSONResponse({"error": "bad ride_id"}, 400)
    if not re.match(r"^[\w\-]+$", ride_id):
        return JSONResponse({"error": "bad ride_id"}, 400)
    import ride_storage as _rs
    ext = ride_id[4:] if ride_id.startswith("icu_") else ride_id
    rec = _rs.get_icu_ride(ext)
    if rec is None:
        return JSONResponse({"error": "ride not found"}, 404)
    prs = rec.get("prs")
    if isinstance(prs, list):
        return {"ride_id": ride_id, "prs": prs}
    # Lazy compute when the field is missing (pre-v1.3.0 imports).
    try:
        import power_curve
        prs = power_curve.compute_ride_prs(ride_id)
    except Exception as e:
        _log.warning(f"api_ride_prs lazy compute failed: {e}")
        prs = []
    # Best-effort cache the result back into the envelope so re-reads are O(1).
    try:
        rec["prs"] = prs
        path = _rs._icu_rides_dir() / f"{ext}.json"
        path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    except Exception as e:
        _log.debug(f"api_ride_prs persist failed: {e}")
    return {"ride_id": ride_id, "prs": prs}


@app.post("/api/ride/{ride_id}/prs/recompute")
def api_ride_prs_recompute(ride_id: str):
    """v1.3.0 — force a recompute of the PR list for a ride and persist back.

    Useful when efforts were backfilled after the initial import — the
    rolling-best changes, so the rebased PR list does too. Returns the
    fresh list. 404 when the ride id is unknown.
    """
    if not isinstance(ride_id, str) or not ride_id or len(ride_id) > 80:
        return JSONResponse({"error": "bad ride_id"}, 400)
    if not re.match(r"^[\w\-]+$", ride_id):
        return JSONResponse({"error": "bad ride_id"}, 400)
    import ride_storage as _rs
    ext = ride_id[4:] if ride_id.startswith("icu_") else ride_id
    rec = _rs.get_icu_ride(ext)
    if rec is None:
        return JSONResponse({"error": "ride not found"}, 404)
    # W2B-G9 fix: on compute failure, return 500 WITHOUT touching the
    # persisted record. The prior `prs[]` (if any) stays intact so a
    # transient compute error doesn't wipe data.
    try:
        import power_curve
        prs = power_curve.compute_ride_prs(ride_id)
    except Exception as e:
        _log.warning(f"api_ride_prs_recompute failed: {e}")
        return JSONResponse({"error": str(e)}, 500)
    try:
        rec["prs"] = prs
        path = _rs._icu_rides_dir() / f"{ext}.json"
        path.write_text(json.dumps(rec, indent=2), encoding="utf-8")
    except Exception as e:
        _log.warning(f"api_ride_prs_recompute persist failed: {e}")
    return {"ride_id": ride_id, "prs": prs}


@app.get("/api/profile/pr-toast-queue")
def api_profile_pr_toast_queue(drain: int = Query(0)):
    """v1.3.0 IMPL-PR-DETECTION (W2B-G4 fix) — read pending PR toasts.

    The dashboard polls this on init to surface a single line per ride
    that landed a major PR since the user last opened the page. Per
    PATCH G7 the cap is one entry per ride. Pass ``?drain=1`` to clear
    the queue after read (the dashboard does this on the first poll
    each session so toasts don't replay forever).
    """
    queue = _read_pr_toast_queue()
    if drain:
        try:
            _write_pr_toast_queue([])
        except Exception as e:
            _log.debug(f"pr-toast-queue drain failed: {e}")
    return {"queue": queue, "count": len(queue)}


# v1.6.3 — background-thread lazy ICU sync. Pre-v1.6.3 the three frontpage
# endpoints (/api/activities, /api/today-session, /api/calendar) all called
# _maybe_lazy_icu_sync() synchronously on the request thread. _sync_icu_activities
# downloads + parses the raw FIT for every new ICU ride to augment DFA α1,
# which on a cold install (many new rides) takes minutes and starves the
# uvicorn threadpool — the dashboard renders an indefinite loading spinner.
# Fix: spawn a daemon thread, return immediately. An Event guard prevents N
# threads under concurrent requests. _SYNC_SLOW_THRESHOLD_S=10 emits
# E_SYNC_BLOCKING_SLOW so the diag modal carries evidence on the next slow
# sync. _last_sync_elapsed_s is exposed for the test suite.
_icu_sync_in_progress: threading.Event = threading.Event()
_SYNC_SLOW_THRESHOLD_S: float = 10.0
_last_sync_elapsed_s: float | None = None


def _kick_lazy_icu_sync(force_if_today_missing: bool = False) -> bool:
    """Fire-and-forget wrapper around ``_maybe_lazy_icu_sync``.

    Returns ``True`` iff a new background thread was started. Returns
    ``False`` (silently) when a sync is already in flight, so concurrent
    requests don't stack N redundant threads.

    The thread is daemonic so it never blocks process exit. Endpoints
    that call this serve whatever is cached locally and the dashboard
    becomes interactive immediately; the next request after the sync
    completes sees the fresh data.
    """
    if _icu_sync_in_progress.is_set():
        return False
    _icu_sync_in_progress.set()

    def _runner() -> None:
        global _last_sync_elapsed_s
        t0 = time.time()
        try:
            _maybe_lazy_icu_sync(force_if_today_missing=force_if_today_missing)
        except Exception as e:
            _log.debug(f"_kick_lazy_icu_sync: background sync swallowed: {e}")
        finally:
            elapsed = time.time() - t0
            _last_sync_elapsed_s = elapsed
            _icu_sync_in_progress.clear()
            if elapsed > _SYNC_SLOW_THRESHOLD_S:
                try:
                    _log_error(
                        error_codes.Codes.SYNC_BLOCKING_SLOW,
                        ms=int(elapsed * 1000),
                        threshold_s=_SYNC_SLOW_THRESHOLD_S,
                    )
                except Exception as e:
                    _log.debug(f"_kick_lazy_icu_sync: slow-warn log swallowed: {e}")

    threading.Thread(
        target=_runner,
        name="domestique.icu_sync",
        daemon=True,
    ).start()
    return True


def _maybe_lazy_icu_sync(force_if_today_missing: bool = False) -> None:
    """Best-effort lazy ICU sync hook (v4.4.2 wired into more endpoints).

    Default behavior (1h throttle): fires if creds exist and last sync was
    > 1h ago. Failure is silent — callers still serve whatever is cached
    locally.

    v4.4.2 §B2: ``force_if_today_missing=True`` adds a second trigger:
    when today's date is NOT represented in load_all_rides() AND last
    sync was > 30 min ago, force a re-sync (bypasses 1h throttle inside
    `_sync_icu_activities` via ``force=True``). Catches the "user just
    rode, ICU has it but our local cache is stale" case.

    v4.5.0: also pulls wellness records (HRV/Sleep/RHR/weight) on the same
    1h cadence so /api/readiness can fall back to the local store when the
    live ICU wellness path is rate-limited / 403'd.
    """
    if not _icu_credentials_present():
        return
    last = _read_last_sync_at()
    fired = False
    # Standard 1h-throttle path (covers cold-start + idle return).
    if last is None or (time.time() - last) >= 3600:
        try:
            _sync_icu_activities(force=False)
            fired = True
        except Exception as e:
            _log.debug(f"_maybe_lazy_icu_sync(throttled) swallowed: {e}")
    # v4.4.2 §B2: today-missing fallback. Skip if we already fired above,
    # or if last sync was very recent (< 30 min).
    if force_if_today_missing and not fired:
        if last is not None and (time.time() - last) < 1800:
            # too recent — but still try wellness below
            pass
        else:
            today_iso = clock.today().isoformat()
            try:
                rides = _load_all_rides_safe()
                has_today = any(
                    _ride_started_local_iso_date(r) == today_iso for r in rides
                )
            except Exception as e:
                _log.debug(f"_maybe_lazy_icu_sync today-check swallowed: {e}")
                has_today = True  # be conservative — skip force
            if not has_today:
                try:
                    _sync_icu_activities(force=True)
                except Exception as e:
                    _log.debug(f"_maybe_lazy_icu_sync(force) swallowed: {e}")
    # v4.5.0: wellness sync on its own 1h throttle (independent of rides).
    last_w = _read_last_wellness_sync_at()
    if last_w is None or (time.time() - last_w) >= 3600:
        try:
            _sync_icu_wellness(force=False)
        except Exception as e:
            _log.debug(f"_maybe_lazy_icu_sync(wellness) swallowed: {e}")


def _detect_plan_load_alert() -> bool:
    """v4.6.6 IMPL-A G4 — read-only post-sync check (Foster 1998).

    Returns True iff a same-day ride exists whose actual TSS exceeds
    today's planned ``tss_estimate`` by more than 1.5×. This signals a
    session-load spike (Foster 1998 Med Sci Sports Exerc 30:1164-1168 —
    session-RPE × duration spikes are the strongest predictor of
    short-term overreaching) and the dashboard renders it as a toast so
    the user knows to consider a recovery day. Mutating the plan is the
    job of /api/plan/reforecast — this hook is read-only.
    """
    try:
        json_path = _plan_dir() / "current_plan.json"
        if not json_path.exists():
            return False
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
        today_iso = clock.today().isoformat()
        # Find today's planned session (skip rest days with tss_estimate == 0).
        planned_estimate = 0.0
        for w in plan.get("weeks", []):
            if w.get("start", "") <= today_iso <= w.get("end", ""):
                for s in w.get("sessions", []):
                    if s.get("day") == today_iso:
                        planned_estimate = float(s.get("tss_estimate") or 0)
                        break
                break
        if planned_estimate <= 0:
            return False  # no plan or rest day → no spike to flag
        rides = _load_all_rides_safe()
        for r in rides or []:
            rd = (
                _ride_started_local_iso_date(r)
                or r.get("date")
                or (r.get("start_date_local") or "")[:10]
                or ""
            )
            if rd != today_iso:
                continue
            actual = float(r.get("tss") or r.get("icu_training_load") or 0)
            if actual > planned_estimate * 1.5:
                return True
        return False
    except Exception as e:  # noqa: BLE001
        _log.debug(f"_detect_plan_load_alert swallowed: {e}")
        return False


@app.post("/api/rides/sync")
def api_rides_sync(force: int = Query(0)):
    """v4.4.0 — pull last 90 days of ICU activities into the local rides
    store and return counts.

    v4.5.0: ``?force=1`` bypasses the 1h sync throttle AND runs a wellness
    pull in the same call. Returns ``wellness_added`` and ``wellness_total``
    alongside the rides counters when force=1. Without force, the existing
    1h throttle inside ``_sync_icu_activities`` applies.

    v4.6.6 IMPL-A G4: response now carries ``plan_load_alert: True`` when
    a same-day ride's TSS exceeds today's planned tss_estimate by >1.5×
    (Foster 1998 session-load spike). Read-only — does not mutate the plan.
    """
    # The sync fetches; this handler adapts, once (it did twice: inside the
    # sync and again below, the second finding the day already stamped).
    if force:
        result = _sync_icu_rides_and_wellness(force=True)
    else:
        result = _sync_icu_activities(force=False)
    # Cache invalidation on new rides happens inside _sync_icu_activities (one
    # source of truth, so the lazy-sync path is covered too — v2.2.6).
    # Always compute the alert post-sync so the dashboard can toast even
    # when the throttle returned status="throttled" (a recently-finished
    # ride still posts via a separate ICU push).
    result["plan_load_alert"] = _detect_plan_load_alert()
    # v1.0.3 — best-effort auto-reforecast on new rides. Helper swallows
    # all exceptions. plan_adapted names what it wrote (None: nothing), so
    # the dashboard knows to repaint the cards that read the plan.
    result["plan_adapted"] = _maybe_auto_reforecast("default", result.get("added", 0))
    return result


@app.get("/api/rides")
def api_rides(limit: int = Query(0)):
    """List all saved rides as a single flat array sorted newest-first.

    v4.4.0: now also surfaces ICU-synced activities (from the local
    ``~/.domestique/rides/icu/`` cache) alongside FIT imports + legacy
    JSON rides. Each entry carries ``source`` ("icu"|"fit"|"json") and a
    stable ``ride_id`` ("icu_<id>"|"fit_<stem>"|legacy id).

    FIX-CONTRACT C3 (v4.3.x): payload remains a flat array (not a
    {json, fit} envelope) so existing UI guards keep working.
    """
    import ride_storage

    # ICU + FIT merged set from ride_storage (handles dedupe internally).
    merged = _load_all_rides_safe()

    # Legacy JSON rides (from the pre-v4 live session path) are still
    # browseable. Append them after the merged ICU/FIT list and re-sort.
    try:
        legacy = ride_storage.list_rides()
    except Exception as e:
        _log.warning(f"ride_storage.list_rides failed: {e}")
        legacy = []
    for r in legacy:
        r.setdefault("ride_id", r.get("id", ""))
        r.setdefault("source", "json")
    merged = list(merged) + list(legacy)

    def _sort_key(r: dict) -> str:
        return str(r.get("started_at") or r.get("mtime") or "")

    merged.sort(key=_sort_key, reverse=True)

    if isinstance(limit, int) and limit > 0:
        merged = merged[:limit]

    return merged


# v1.8.3 BUG-E — interval label override for misclassified RECOVERY rows.
# ICU's auto-detection labels long flat segments "RECOVERY" regardless of
# actual power. Domestique used to display the label verbatim, so the user
# saw "RECOVERY" rows in the 23 INTERVALS table for segments averaging
# 189-297W (Z3-Z5+ on a 248W FTP). We now compute the zone from avg power
# and override the displayed name when ICU's label disagrees.
_RECOVERY_LABEL_TOKEN = "RECOVERY"
# Structured ICU labels look like "302s@243w91rpm" or "11s@396w88rpm" —
# group_id strings encoding duration+watts+cadence. Preserve those verbatim.
_STRUCTURED_NAME_RE = re.compile(r"\d+\s*w", re.IGNORECASE)


def _zone_from_ftp_pct(pct: float) -> str:
    """Return the Z1..Z7 band for a %FTP value (Coggan 7-zone)."""
    if pct >= 151:
        return "Z7"
    if pct >= 121:
        return "Z6"
    if pct >= 106:
        return "Z5"
    if pct >= 91:
        return "Z4"
    if pct >= 76:
        return "Z3"
    if pct >= 56:
        return "Z2"
    return "Z1"


def _display_interval_name(row: dict, ftp: int | float | None) -> str:
    """Override ICU's interval name when it disagrees with the computed zone.

    Rules (in order):
    1. Structured ICU labels (containing a "<watts>w" pattern like
       "302s@243w91rpm") are always preserved.
    2. If we have no FTP or no avg_power, fall back to ICU's raw name
       (or an em-dash placeholder when it's blank).
    3. If ICU's name is exactly "RECOVERY" but avg_power yields Z2+,
       replace with "Z<n> <watts>W".
    4. Otherwise keep ICU's name (with a zone+watts fallback when blank).
    """
    icu_name = (row.get("name") or "").strip()
    try:
        avg_w = int(row.get("avg_power_w") or 0)
    except (TypeError, ValueError):
        avg_w = 0
    try:
        ftp_v = float(ftp or 0)
    except (TypeError, ValueError):
        ftp_v = 0.0

    # Rule 1: preserve structured names (e.g. "302s@243w91rpm").
    if icu_name and _STRUCTURED_NAME_RE.search(icu_name):
        return icu_name

    # Rule 2: missing FTP or avg_power → no override possible.
    if avg_w <= 0 or ftp_v <= 0:
        return icu_name or "—"

    pct = avg_w / ftp_v * 100.0
    zone = _zone_from_ftp_pct(pct)

    # Rule 3: override misclassified RECOVERY.
    if icu_name.upper() == _RECOVERY_LABEL_TOKEN and zone != "Z1":
        return f"{zone} {avg_w}W"

    # Rule 4: keep ICU's name (or synthesise a zone+watts fallback).
    return icu_name or f"{zone} {avg_w}W"


def _project_intervals_for_display(rec: dict) -> dict:
    """Apply the v1.8.3 BUG-E name override to each interval row.

    Mutates the record's ``intervals`` list in place (each row gets its
    ``name`` rewritten where Rule 3 applies). Safe on records with no
    intervals or no FTP.
    """
    if not isinstance(rec, dict):
        return rec
    intervals = rec.get("intervals")
    if not isinstance(intervals, list) or not intervals:
        return rec
    ftp = rec.get("ftp_at_ride") or getattr(config, "ATHLETE_FTP_W", None)
    for row in intervals:
        if isinstance(row, dict):
            row["name"] = _display_interval_name(row, ftp)
    return rec


@app.get("/api/ride/{ride_id}/detail")
def api_ride_full_detail(ride_id: str, include: str = Query("")):
    """v4.4.0 — return the full normalized ride record (MASTER §3 schema).

    ``ride_id`` accepts the prefixed forms ``icu_<external_id>`` or
    ``fit_<stem>``, plus legacy ride ids (assumed JSON archive).
    Optional ``?include=samples`` adds a decimated 1Hz time-series block.

    v1.0.6 IMPL-3D-DASHBOARD: also exposes the Belastingscore (XSS)
    decomposition under ``summary.xss_*`` (xss_total, xss_cp,
    xss_w_prime, xss_pmax). None when ride has no power data — the
    field is added unconditionally so consumers can rely on its
    presence (TSS-primary contract preserved; tss / np_w / if_pct
    untouched).
    """
    if not isinstance(ride_id, str) or not ride_id or len(ride_id) > 80:
        return JSONResponse({"error": "bad ride_id"}, 400)
    if not re.match(r"^[\w\-]+$", ride_id):
        return JSONResponse({"error": "bad ride_id"}, 400)

    want_samples = "samples" in (include or "").split(",")

    def _attach_xss_summary(rec_dict: dict) -> dict:
        """v1.0.6 — surface xss_* on rec.summary so the Belastingscore card
        always finds the keys (None when 3D model has not run on this ride).

        v1.0.7 IMPL-NP-ALT — also surfaces sr_avg_w / sr_if (strain-rate
        watt-equivalent, NP-alternative lens) so the "Two lenses" comparison
        block on the ride-detail modal renders Coggan NP/IF/TSS vs Kontro
        SR_avg/SR_IF/Belastingscore side-by-side. Both fields are None when
        CP/W'/Pmax aren't calibrated — dashboard greys out the right column.
        """
        if not isinstance(rec_dict, dict):
            return rec_dict
        summary = rec_dict.get("summary")
        if not isinstance(summary, dict):
            summary = {}
            rec_dict["summary"] = summary
        # Read from the ride's stored summary cache; IMPL-3D-INGEST writes
        # these after strain-score integration. Fallback to top-level rec
        # fields when caller stored xss_* there directly.
        summary.setdefault("xss_total", rec_dict.get("xss_total"))
        summary.setdefault("xss_cp", rec_dict.get("xss_cp"))
        summary.setdefault("xss_w_prime", rec_dict.get("xss_w_prime"))
        summary.setdefault("xss_pmax", rec_dict.get("xss_pmax"))
        summary.setdefault("sr_avg_w", rec_dict.get("sr_avg_w"))
        summary.setdefault("sr_if", rec_dict.get("sr_if"))
        # v1.0.7 IMPL-DFA: surface DFA α1 series + status field so the
        # ride-detail panel + the v1.0.7 IMPL-HRV-PROMPT toast both find
        # the keys (None when sensor didn't emit RR-intervals).
        summary.setdefault("dfa_alpha1_avg", rec_dict.get("dfa_alpha1_avg"))
        summary.setdefault("dfa_alpha1_status", rec_dict.get("dfa_alpha1_status"))
        # v1.8.14 — also surface the per-window α1 series + LT1 minutes +
        # raw beat count so the activity modal can draw the DFA α1-over-time
        # chart (the actual visualisation, not just the scalar average).
        summary.setdefault("dfa_alpha1_series", rec_dict.get("dfa_alpha1_series") or [])
        summary.setdefault("dfa_alpha1_lt1_minutes", rec_dict.get("dfa_alpha1_lt1_minutes"))
        summary.setdefault("rr_intervals_count", rec_dict.get("rr_intervals_count"))

        # v1.0.7 IMPL-TAU-FIT-WIRING: surface is_race + activity_id from the
        # SQLite activities table so the dashboard "🏁 Mark as race" checkbox
        # can reflect current state. Look up by external_id (ICU rides) or
        # the bare ride id (legacy/FIT). Gracefully None when the activity
        # row doesn't exist or the column is missing (pre-migration DB).
        ext = rec_dict.get("external_id") or rec_dict.get("id") or ""
        is_race_val = None
        try:
            if ext:
                conn = db.get_db()
                cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(activities)"
                ).fetchall()]
                if "is_race" in cols:
                    row = conn.execute(
                        "SELECT is_race FROM activities WHERE id = ?",
                        (str(ext),),
                    ).fetchone()
                    if row is not None and row[0] is not None:
                        is_race_val = bool(row[0])
        except Exception as e:
            _log.debug(f"_attach_xss_summary: is_race lookup failed: {e}")
            is_race_val = None
        rec_dict["is_race"] = is_race_val
        rec_dict["activity_id"] = str(ext) if ext else None
        return rec_dict

    # ── ICU rides ──────────────────────────────────────────────────────────
    if ride_id.startswith("icu_"):
        import ride_storage as _rs
        rec = _rs.get_icu_ride(ride_id)
        if rec is None:
            _log.info("ride_detail 404 ride_id=%s", ride_id)
            return JSONResponse({"error": "Ride not found"}, 404)
        # v4.5.5 IMPL-DETAIL-SERVER: lazy-fetch ICU detail when the cached
        # record is missing zones / hr-zones / intervals (older sync). One
        # extra round-trip on first detail-modal click; subsequent requests
        # come from the persisted normalized record.
        rec = _maybe_enrich_icu_record(rec)
        # v1.8.3 BUG-E: rewrite "RECOVERY" labels that disagree with the
        # computed zone-from-power before the modal renders them.
        rec = _project_intervals_for_display(rec)
        if want_samples:
            rec["samples"] = _build_icu_samples(rec)
        return _attach_xss_summary(rec)

    # ── FIT rides ──────────────────────────────────────────────────────────
    if ride_id.startswith("fit_"):
        stem = ride_id[4:]
        fit_path = _rides_fit_dir() / f"{stem}.fit"
        if not fit_path.exists():
            _log.info("ride_detail 404 ride_id=%s", ride_id)
            return JSONResponse({"error": "Ride not found"}, 404)
        rec = _build_fit_normalized(fit_path, ride_id)
        rec = _project_intervals_for_display(rec)
        if want_samples:
            rec["samples"] = _build_fit_samples(fit_path)
        return _attach_xss_summary(rec)

    # ── Bare-id legacy rides (v1.8.8 Bug 1 fallback) ───────────────────────
    # Homepages built before the prefix scheme pass the bare ICU external_id.
    # Try ICU lookup first (covers the most common legacy case), then the
    # legacy JSON archive. Logs the 404 so the next bug-report tells us the
    # exact id format that failed.
    import ride_storage as _rs2
    rec = _rs2.get_icu_ride(ride_id)
    if rec is not None:
        rec = _maybe_enrich_icu_record(rec)
        rec = _project_intervals_for_display(rec)
        if want_samples:
            rec["samples"] = _build_icu_samples(rec)
        return _attach_xss_summary(rec)

    # ── Legacy JSON rides ──────────────────────────────────────────────────
    legacy = _rs2.get_ride(ride_id)
    if not legacy:
        _log.info("ride_detail 404 ride_id=%s", ride_id)
        return JSONResponse({"error": "Ride not found"}, 404)
    rec = _legacy_ride_to_normalized(legacy, ride_id)
    rec = _project_intervals_for_display(rec)
    if want_samples:
        rec["samples"] = _legacy_ride_samples(legacy)
    return _attach_xss_summary(rec)


def _maybe_enrich_icu_record(rec: dict) -> dict:
    """v4.5.5 — lazy-fetch ICU detail when cached record lacks zones/intervals.

    On first detail-modal click for an older ICU ride, the persisted
    normalized record may have empty time-in-zone (all zeros) and empty
    intervals (older sync paths didn't fetch the /intervals subpath).
    We hit ``/api/v1/activity/<id>`` + ``/intervals`` once, re-normalize,
    and persist back. Subsequent requests hit the cache. Failures degrade
    to whatever the cached record had — the modal still renders.

    Also adds the polarization block when the cached record was written
    before v4.5.5 (and therefore lacks ``polarization``).
    """
    try:
        ext = rec.get("external_id") or ""
        if not ext:
            return rec

        tiz = rec.get("time_in_zone") or {}
        tiz_total = sum(int(v or 0) for k, v in tiz.items()
                        if isinstance(k, str) and k.startswith("z"))
        intervals = rec.get("intervals") or []
        hr_tiz = rec.get("hr_time_in_zone")
        polarization = rec.get("polarization")

        needs_zones = tiz_total == 0
        # v3.7.0 — a record written before laps carried their position on the
        # ride clock needs one re-fetch to gain it (block grading is silent
        # without it). The normalizer always writes the key now, even when ICU
        # had no offset to give, so a re-fetch that comes back without one
        # still settles instead of re-firing on every open.
        needs_intervals = not intervals or all(
            isinstance(iv, dict) and "start_s" not in iv for iv in intervals)
        needs_hr = hr_tiz is None
        # v4.5.5: also recompute when the cached polarization predates
        # REFINE-CLASSIFY (i.e. is missing the new "confidence" key).
        needs_polarization = (
            polarization is None
            or (isinstance(polarization, dict) and "confidence" not in polarization)
        )

        if not (needs_zones or needs_intervals or needs_hr or needs_polarization):
            return rec

        if needs_zones or needs_intervals or needs_hr:
            # Fetch fresh from ICU and re-persist.
            try:
                import training as _training
                full = _training.fetch_activity_full(str(ext))
            except Exception as e:
                _log.debug(f"_maybe_enrich_icu_record({ext}) fetch failed: {e}")
                full = None
            if full:
                try:
                    import ride_storage as _rs
                    # carry_hydrated=False: this endpoint IS the per-ride
                    # refresh — a fresh detail fetch must be able to replace
                    # stale carried streams/efforts (ICU re-processed the
                    # activity), not be setdefault-blocked by them.
                    new_path = _rs.persist_icu_activity(full, carry_hydrated=False)
                    if new_path is not None:
                        new_rec = _rs.get_icu_ride(str(ext))
                        if new_rec:
                            return new_rec
                except Exception as e:
                    _log.debug(f"_maybe_enrich_icu_record({ext}) persist failed: {e}")

        # Fall through: at minimum, attach a polarization block computed
        # from whatever zones the cached record already had.
        if needs_polarization:
            try:
                from analytics import compute_polarization_block
                rec["polarization"] = compute_polarization_block(tiz)
            except Exception:
                pass
        return rec
    except Exception as e:
        _log.debug(f"_maybe_enrich_icu_record failed: {e}")
        return rec


def _build_icu_samples(rec: dict) -> dict:
    """Pull streams from ICU + decimate to ≤1800 points.

    Returns the canonical samples dict per §3 (or empty arrays when the
    fetch fails). We never block the modal on this — failure means the UI
    shows the stats grid without sparklines.
    """
    ext = rec.get("external_id") or ""
    streams: dict = {}
    if ext:
        try:
            import training as _training
            streams = _training.fetch_activity_streams(str(ext)) or {}
        except Exception as e:
            _log.debug(f"_build_icu_samples({ext}) failed: {e}")
            streams = {}

    pwr = streams.get("watts") or streams.get("power") or []
    hr = streams.get("heartrate") or streams.get("hr") or []
    cad = streams.get("cadence") or []
    elev = streams.get("altitude") or streams.get("elevation") or []
    n = max(len(pwr), len(hr), len(cad), len(elev))
    if n == 0:
        return {"t_s": [], "power_w": [], "hr_bpm": [], "cadence_rpm": [], "elevation_m": []}
    return _decimate_samples(
        list(range(n)),
        list(pwr) + [None] * (n - len(pwr)),
        list(hr) + [None] * (n - len(hr)),
        list(cad) + [None] * (n - len(cad)),
        list(elev) + [None] * (n - len(elev)),
    )


def _build_fit_normalized(fit_path: Path, ride_id: str) -> dict:
    """Build the §3 normalized record from a FIT file by parsing it once."""
    stats = _parse_fit_stats(fit_path)
    # v1.0.7 — compute DFA α1 from the FIT's HrvMessage records (chest-strap
    # rides only). compute_dfa_alpha1_for_fit returns a dict with
    # ``dfa_alpha1_status`` always set; status='no_rr_data' when the ride
    # had no RR-intervals (optical-wrist HR or HRV-recording-off Garmin).
    dfa_avg = None
    dfa_series: list = []
    dfa_lt1_min = None
    dfa_status = "no_rr_data"
    dfa_confidence = "low"  # K1
    rr_count = 0
    try:
        from analytics import compute_dfa_alpha1_for_fit
        # K1: gate by the SOURCE sport — a running .fit caps DFA confidence at
        # medium (footstrike RR jitter), never disabled. Read once, best-effort.
        try:
            from fit_activity import read_session_sport
            _src_sport = read_session_sport(fit_path)
        except Exception:
            _src_sport = None
        dfa = compute_dfa_alpha1_for_fit(fit_path, sport=_src_sport)
        if isinstance(dfa, dict):
            dfa_avg = dfa.get("dfa_alpha1_avg")
            dfa_series = dfa.get("dfa_alpha1_series") or []
            dfa_lt1_min = dfa.get("dfa_alpha1_lt1_minutes")
            dfa_status = dfa.get("dfa_alpha1_status") or "no_rr_data"
            dfa_confidence = dfa.get("dfa_alpha1_confidence") or "low"
            rr_count = int(dfa.get("rr_intervals_count") or 0)
    except Exception as e:
        _log.debug(f"_build_fit_normalized DFA α1 compute failed: {e}")

    # v1.0.7 IMPL-HRV-PROMPT — read FIT FileIdMessage so the home-page toast
    # can name the rider's specific Garmin device. Failure is non-fatal.
    device_manufacturer = None
    device_product_name = None
    device_product_id = None
    try:
        import ride_storage as _rs_dev
        dev = _rs_dev.summarise_fit_device(fit_path)
        if isinstance(dev, dict):
            device_manufacturer = dev.get("device_manufacturer")
            device_product_name = dev.get("device_product_name")
            device_product_id = dev.get("device_product_id")
    except Exception as e:
        _log.debug(f"_build_fit_normalized device-info read failed: {e}")

    # v3.6.0 — a FIT rider's own session-RPE lives in the load sidecar (there is
    # no ICU record to hold it). Without this the rating saved fine and then read
    # back blank on the one screen that renders the control, which to the rider
    # is indistinguishable from it never having saved.
    rider_input = {}
    try:
        import ride_storage as _rs
        side = _rs.read_fit_load_sidecar(fit_path) or {}
        for k in ("rpe", "rpe_at", "rpe_scale"):
            if side.get(k) is not None:
                rider_input[k] = side[k]
    except Exception as e:  # noqa: BLE001 — never break a ride view for this
        _log.debug(f"fit rider-input read skipped: {e}")

    return {
        **rider_input,
        "ride_id": ride_id,
        "source": "fit",
        "external_id": None,
        "name": "",
        "started_at": stats.get("start_time") or "",
        "duration_s": int(stats.get("duration_sec") or 0),
        "moving_s": int(stats.get("duration_sec") or 0),
        "distance_km": float(stats.get("distance_km") or 0),
        "elevation_m": None,
        "avg_power_w": int(stats.get("avg_power") or 0) or None,
        "np_w": None,
        "if_pct": None,
        "tss": stats.get("tss"),
        "kj": stats.get("total_kj"),
        "kj_above_ftp": None,
        "kcal": None,
        "avg_hr": int(stats.get("avg_hr") or 0) or None,
        "hr_max": int(stats.get("max_hr") or 0) or None,
        "avg_cadence": int(stats.get("avg_cadence") or 0) or None,
        "weight_kg": None,
        "ftp_at_ride": getattr(config, "ATHLETE_FTP_W", None),
        "eftp_at_ride": getattr(config, "ATHLETE_FTP_W", None),
        "decoupling_pct": None,
        "dfa_alpha1": dfa_avg,
        "dfa_alpha1_avg": dfa_avg,
        "dfa_alpha1_series": dfa_series,
        "dfa_alpha1_lt1_minutes": dfa_lt1_min,
        "dfa_alpha1_status": dfa_status,
        "dfa_alpha1_confidence": dfa_confidence,  # K1
        "rr_intervals_count": rr_count,
        # v1.0.7 IMPL-HRV-PROMPT — device fields used by the
        # /api/wellness/hrv-recording-status endpoint to name the rider's
        # head unit in the toast / modal.
        "device_manufacturer": device_manufacturer,
        "device_product_name": device_product_name,
        "device_product_id": device_product_id,
        "time_in_zone": {f"z{i}": 0 for i in range(1, 8)},
        "intervals": [],
        "samples_url": f"/api/ride/{ride_id}/detail?include=samples",
        # Pass-through any FTP-test fields the parser produced.
        "is_ftp_test": stats.get("is_ftp_test", False),
        "ftp_test_suggestion": stats.get("ftp_test_suggestion"),
    }


def _build_fit_samples(fit_path: Path) -> dict:
    """Re-parse the FIT for raw 1Hz streams + decimate.

    We re-parse rather than caching to keep memory low; the modal call is
    rare (user-initiated click) so the cost is acceptable.
    """
    try:
        from fit_tool.fit_file import FitFile
    except Exception:
        return {"t_s": [], "power_w": [], "hr_bpm": [], "cadence_rpm": [], "elevation_m": []}

    try:
        ff = FitFile.from_file(str(fit_path))
    except Exception:
        return {"t_s": [], "power_w": [], "hr_bpm": [], "cadence_rpm": [], "elevation_m": []}

    pwr: list = []
    hr: list = []
    cad: list = []
    elev: list = []
    try:
        for rec in ff.records:
            msg = rec.message
            if type(msg).__name__ != "RecordMessage":
                continue
            try:
                pwr.append(int(_fit_val(msg, "power") or 0))
            except Exception:
                pwr.append(0)
            try:
                hr.append(int(_fit_val(msg, "heart_rate") or 0))
            except Exception:
                hr.append(0)
            try:
                cad.append(int(_fit_val(msg, "cadence") or 0))
            except Exception:
                cad.append(0)
            try:
                elev.append(float(_fit_val(msg, "altitude") or 0))
            except Exception:
                elev.append(0)
    except Exception:
        pass

    n = len(pwr)
    if n == 0:
        return {"t_s": [], "power_w": [], "hr_bpm": [], "cadence_rpm": [], "elevation_m": []}
    return _decimate_samples(list(range(n)), pwr, hr, cad, elev)


def _legacy_ride_to_normalized(ride: dict, ride_id: str) -> dict:
    """Map the legacy JSON-ride shape to the §3 normalized record."""
    summary = ride.get("summary") or {}
    zones = (ride.get("zones") or {}).get("power") or {}
    tiz = {f"z{i}": int(zones.get(f"Z{i}") or 0) for i in range(1, 8)}
    return {
        "ride_id": ride_id,
        "source": "json",
        "external_id": None,
        "name": (ride.get("metadata") or {}).get("name") or "",
        "started_at": ride.get("started_at") or "",
        "duration_s": int(summary.get("duration_sec") or 0),
        "moving_s": int(summary.get("duration_sec") or 0),
        "distance_km": float(summary.get("distance_km") or 0),
        "elevation_m": int(summary.get("elevation_gain_m") or 0) or None,
        "avg_power_w": int(summary.get("avg_power") or 0) or None,
        "np_w": int(summary.get("weighted_power") or summary.get("normalized_power") or 0) or None,
        "if_pct": (
            round(float(summary.get("intensity_factor")) * 100.0, 1)
            if summary.get("intensity_factor") is not None
            else None
        ),
        "tss": summary.get("tss"),
        "kj": summary.get("kj_mechanical") or summary.get("calories"),
        "kj_above_ftp": None,
        "kcal": None,
        "avg_hr": int(summary.get("avg_hr") or 0) or None,
        "hr_max": int(summary.get("max_hr") or 0) or None,
        "avg_cadence": int(summary.get("avg_cadence") or 0) or None,
        "weight_kg": None,
        "ftp_at_ride": getattr(config, "ATHLETE_FTP_W", None),
        "eftp_at_ride": getattr(config, "ATHLETE_FTP_W", None),
        "decoupling_pct": summary.get("decoupling_pct"),
        "dfa_alpha1": summary.get("dfa_alpha1_avg"),
        "time_in_zone": tiz,
        "intervals": [],
        "samples_url": f"/api/ride/{ride_id}/detail?include=samples",
    }


def _legacy_ride_samples(ride: dict) -> dict:
    """Build the §3 samples block from a legacy ride's columnar samples."""
    samples = ride.get("samples") or {}
    pwr = samples.get("power") or []
    hr = samples.get("hr") or samples.get("heart_rate") or []
    cad = samples.get("cadence") or []
    elev = samples.get("elevation") or samples.get("altitude") or []
    n = max(len(pwr), len(hr), len(cad), len(elev))
    if n == 0:
        return {"t_s": [], "power_w": [], "hr_bpm": [], "cadence_rpm": [], "elevation_m": []}
    return _decimate_samples(list(range(n)), pwr, hr, cad, elev)


def _decimate_samples(
    t_s: list, power_w: list, hr_bpm: list, cad_rpm: list, elev_m: list,
    target_max: int = 1800,
) -> dict:
    """Reduce 1Hz streams to ≤target_max points by simple bucketed averaging.

    Cheap and good enough for sparklines — we don't need exact peaks
    on the modal preview (the stats grid above the chart already shows
    max_power / max_hr from the parser).
    """
    import math
    n = len(t_s)
    if n <= target_max:
        return {
            "t_s": list(t_s),
            "power_w": list(power_w),
            "hr_bpm": list(hr_bpm),
            "cadence_rpm": list(cad_rpm),
            "elevation_m": list(elev_m),
        }
    # v4.4.1 FIX-SERVER: ceil(n/target_max) so output count is GUARANTEED
    # ≤ target_max. Floor-stride (n // target) overshoots when n % target != 0
    # — e.g. n=8000, target=1800 → bucket=4 → output=2000 > 1800. Ceiling
    # gives bucket=5 → output=1600 ≤ 1800. Correct contract enforcement.
    bucket = max(1, math.ceil(n / target_max))
    out_t: list = []
    out_p: list = []
    out_h: list = []
    out_c: list = []
    out_e: list = []
    for i in range(0, n, bucket):
        chunk_t = t_s[i:i + bucket]
        chunk_p = [v for v in power_w[i:i + bucket] if v is not None]
        chunk_h = [v for v in hr_bpm[i:i + bucket] if v is not None]
        chunk_c = [v for v in cad_rpm[i:i + bucket] if v is not None]
        chunk_e = [v for v in elev_m[i:i + bucket] if v is not None]
        out_t.append(chunk_t[0] if chunk_t else None)
        out_p.append(round(sum(chunk_p) / len(chunk_p), 1) if chunk_p else 0)
        out_h.append(round(sum(chunk_h) / len(chunk_h), 1) if chunk_h else 0)
        out_c.append(round(sum(chunk_c) / len(chunk_c), 1) if chunk_c else 0)
        out_e.append(round(sum(chunk_e) / len(chunk_e), 1) if chunk_e else 0)
    return {
        "t_s": out_t,
        "power_w": out_p,
        "hr_bpm": out_h,
        "cadence_rpm": out_c,
        "elevation_m": out_e,
    }


@app.get("/api/rides/legacy-envelope")
def api_rides_legacy_envelope():
    """Back-compat shim for any caller still reading the old
    ``{json: [...], fit: [...]}`` envelope. Kept so that external scripts
    (e.g. ride-report-png batch) don't break silently; the primary endpoint
    /api/rides is now a flat array per FIX-CONTRACT C3.
    """
    import ride_storage
    try:
        rides = ride_storage.list_rides()
    except Exception:
        rides = []
    for r in rides:
        r["ride_id"] = r.get("id", "")
        r["source"] = "json"
    fits: list[dict] = []
    try:
        for f in sorted(_rides_fit_dir().glob("*.fit")):
            try:
                st = f.stat()
            except OSError:
                continue
            fits.append({
                "ride_id": f.stem, "source": "fit",
                "size_bytes": st.st_size,
                "mtime": datetime.fromtimestamp(
                    st.st_mtime, timezone.utc
                ).astimezone().isoformat(),
            })
    except Exception:
        pass
    return {"json": rides, "fit": fits}


@app.get("/api/rides/{ride_id}")
def api_ride_detail(ride_id: str):
    """Get full ride detail. Tries the FIT archive first (post-pivot
    canonical path), then falls back to the legacy JSON archive.

    FIX-CONTRACT C3: FIT rides now also expose a ``summary`` block that
    mirrors the legacy JSON-ride shape (summary.is_ftp_test,
    summary.ftp_test_suggestion, summary.ftp_test_halted). U1's modal
    reads from ``summary.*`` so without this mirror the post-v4.0 FIT
    import path had no way to trigger the post-test banner.
    """
    safe_id = re.sub(r'[^\w.\-]', '_', ride_id)
    fit_path = _rides_fit_dir() / f"{safe_id}.fit"
    if fit_path.exists():
        stats = _parse_fit_stats(fit_path)
        # Build summary block from parsed_stats so UI can read either path.
        summary = {
            "duration_sec": stats.get("duration_sec"),
            "distance_km": stats.get("distance_km"),
            "avg_power": stats.get("avg_power"),
            "max_power": stats.get("max_power"),
            "avg_hr": stats.get("avg_hr"),
            "max_hr": stats.get("max_hr"),
            "avg_cadence": stats.get("avg_cadence"),
            "tss": stats.get("tss"),
            "is_ftp_test": stats.get("is_ftp_test", False),
            "ftp_test_type": stats.get("ftp_test_type"),
            "ftp_test_suggestion": stats.get("ftp_test_suggestion"),
            "ftp_test_halted": stats.get("ftp_test_halted", False),
            "ftp_test_halt_at_step": stats.get("ftp_test_halt_at_step"),
            "ftp_test_halt_step": stats.get("ftp_test_halt_step"),
            "local_eftp_suggestion": stats.get("local_eftp_suggestion"),
            # v1.0.6 IMPL-3D-DASHBOARD: Belastingscore (XSS) decomposition.
            # IMPL-3D-INGEST writes these into the ride summary cache after
            # strain-score integration. None when ride has no power data.
            "xss_total": stats.get("xss_total"),
            "xss_cp": stats.get("xss_cp"),
            "xss_w_prime": stats.get("xss_w_prime"),
            "xss_pmax": stats.get("xss_pmax"),
        }
        return {
            "ride_id": ride_id,
            "id": ride_id,
            "source": "fit",
            "started_at": stats.get("start_time"),
            "parsed_stats": stats,
            "summary": summary,
        }

    import ride_storage
    # FTP-tests IP W2c: resolve icu_ ids — the legacy get_ride only reads the
    # JSON-archive dir, so every ICU-synced ride 404'd here and the post-test
    # modal could never fire for sync-path rides. The detection keys live at
    # the top of the ICU envelope; mirror them into summary.* (the shape the
    # modal reads on every other path).
    if ride_id.startswith("icu_"):
        icu = ride_storage.get_icu_ride(ride_id)
        if not icu:
            return JSONResponse({"error": "Ride not found"}, 404)
        icu_summary = {
            "is_ftp_test": icu.get("is_ftp_test", False),
            "ftp_test_type": icu.get("ftp_test_type"),
            "ftp_test_suggestion": icu.get("ftp_test_suggestion"),
            "ftp_test_halted": icu.get("ftp_test_halted", False),
            "ftp_test_halt_step": icu.get("ftp_test_halt_step"),
            "ftp_test_review": icu.get("ftp_test_review"),
            "duration_sec": icu.get("moving_s") or icu.get("duration_s"),
            "tss": icu.get("tss"),
        }
        return {
            "ride_id": f"icu_{icu.get('external_id')}",
            "id": f"icu_{icu.get('external_id')}",
            "source": "icu",
            "started_at": icu.get("started_at"),
            "name": icu.get("name"),
            "summary": icu_summary,
        }
    ride = ride_storage.get_ride(ride_id)
    if not ride:
        return JSONResponse({"error": "Ride not found"}, 404)
    # v1.0.6 IMPL-3D-DASHBOARD: ensure xss_* fields are present in the
    # legacy/ICU ride summary block so the dashboard's Belastingscore card
    # always has a key to read (None when not computed). Preserves the
    # TSS-primary contract — does not modify TSS or any existing field.
    try:
        _summary = ride.get("summary") if isinstance(ride, dict) else None
        if isinstance(_summary, dict):
            _summary.setdefault("xss_total", _summary.get("xss_total"))
            _summary.setdefault("xss_cp", _summary.get("xss_cp"))
            _summary.setdefault("xss_w_prime", _summary.get("xss_w_prime"))
            _summary.setdefault("xss_pmax", _summary.get("xss_pmax"))
    except Exception:
        pass
    return ride


def _fit_power_series_1hz(fit_path: Path) -> list[int]:
    """1 Hz power series from a FIT (same record walk + resampling the
    detector uses) — for the FTP-test analysis view only."""
    from fit_tool.fit_file import FitFile
    ff = FitFile.from_file(str(fit_path))
    ts, pw = [], []
    for rec in ff.records:
        msg = rec.message
        if type(msg).__name__ != "RecordMessage":
            continue
        try:
            _t = _fit_val(msg, "timestamp")
            _tf = float(_t) if _t is not None else None
            ts.append((_tf / 1000.0 if _tf and _tf > 1e11 else _tf))
        except Exception:
            ts.append(None)
        try:
            pw.append(int(_fit_val(msg, "power") or 0))
        except Exception:
            pw.append(0)
    return _resample_series_1hz(ts, pw)


@app.get("/api/ride/{ride_id}/ftp-test-analysis")
def api_ride_ftp_test_analysis(ride_id: str):
    """v3.11.3 — the FTP-test analysis view: the recognised protocol, the
    1 Hz power trace, and WHERE the number came from (scored window, the
    clearing effort, the ramp halt point), so the rider sees the evidence
    behind a suggestion instead of a bare number. Read-only."""
    safe_id = re.sub(r'[^\w.\-]', '_', ride_id)
    suggestion = None
    halted = False
    halt_step = None
    series: list = []
    source = None
    fit_path = _rides_fit_dir() / f"{safe_id}.fit"
    if fit_path.exists():
        source = "fit"
        stats = _parse_fit_stats(fit_path)
        suggestion = stats.get("ftp_test_suggestion")
        halted = bool(stats.get("ftp_test_halted"))
        halt_step = stats.get("ftp_test_halt_step")
        try:
            series = _fit_power_series_1hz(fit_path)
        except Exception as e:
            _log.debug(f"ftp-test-analysis: FIT series failed: {e}")
    elif ride_id.startswith("icu_"):
        import ride_storage
        icu = ride_storage.get_icu_ride(ride_id)
        if not icu:
            return JSONResponse({"error": "Ride not found"}, 404)
        source = "icu"
        suggestion = icu.get("ftp_test_suggestion")
        halted = bool(icu.get("ftp_test_halted"))
        halt_step = icu.get("ftp_test_halt_step")
        streams = icu.get("streams") or {}
        raw = streams.get("watts") or streams.get("power") or []
        series = [int(w or 0) for w in raw] if isinstance(raw, list) else []
    else:
        return JSONResponse({"error": "Ride not found"}, 404)
    if not suggestion:
        return JSONResponse({"error": "no FTP-test suggestion on this ride"}, 404)

    total_s = len(series)
    # Downsample for the chart (≤ 1800 points), keeping seconds addressable.
    step = max(1, -(-total_s // 1800)) if total_s else 1   # ceil → ≤ 1800 points
    ds = []
    for i in range(0, total_s, step):
        chunk = series[i:i + step]
        ds.append(int(round(sum(chunk) / len(chunk))) if chunk else 0)

    windows = []
    ws, we = suggestion.get("window_start_s"), suggestion.get("window_end_s")
    kind = suggestion.get("method") or suggestion.get("type")
    if ws is not None and we is not None:
        label = {"coggan_20min": "20-min block (scored)",
                 "ramp": "best sustained minute (scored)",
                 "sixty_min": "hour plateau (scored)"}.get(kind, "scored window")
        windows.append({"kind": "scored", "start_s": int(ws), "end_s": int(we), "label": label})
    if suggestion.get("blowout_start_s") is not None:
        windows.append({"kind": "blowout", "start_s": int(suggestion["blowout_start_s"]),
                        "end_s": int(suggestion["blowout_end_s"]),
                        "label": "5-min clearing effort"})
    return {
        "ok": True, "ride_id": ride_id, "source": source,
        "suggestion": suggestion, "halted": halted, "halt_step": halt_step,
        "halt_at_s": suggestion.get("halt_at_s"),
        "total_s": total_s, "step_s": step, "series": ds, "windows": windows,
        "prior_ftp": suggestion.get("prior_ftp"),
    }


@app.post("/api/ride/{ride_id}/ftp-test-review")
async def api_ride_ftp_test_review(ride_id: str, request: Request):
    """FTP-tests IP W2c — persist the rider's verdict on a test suggestion
    (accepted / declined / edited) onto the ICU ride envelope, so the post-
    test modal never re-nags across restarts or re-syncs (the marker is on
    ride_storage's unconditional carry list, same class as RPE). FIT-archive
    rides keep the sessionStorage fast-path only — their stats are re-parsed
    from the file on every read, so there is no envelope to stamp."""
    body = await _get_json_body(request)
    action = str(body.get("action") or "").strip()
    if action not in ("accepted", "declined", "edited"):
        return JSONResponse(
            {"error": "action must be accepted, declined or edited"}, 400)
    if not ride_id.startswith("icu_"):
        return {"ok": True, "persisted": False}
    import ride_storage
    icu = ride_storage.get_icu_ride(ride_id)
    if not icu:
        return JSONResponse({"error": "Ride not found"}, 404)
    try:
        _ftp_val = int(float(body.get("ftp") or 0)) or None
    except (TypeError, ValueError):
        _ftp_val = None  # grill pipeline/F5: garbage ftp must not 500
    icu["ftp_test_review"] = {
        "action": action,
        "ftp": _ftp_val,
        "at": clock.now().isoformat(timespec="seconds"),
    }
    try:
        ext = str(icu.get("external_id"))
        path = ride_storage._icu_rides_dir() / f"{ext}.json"
        path.write_text(json.dumps(icu, indent=2), encoding="utf-8")
    except OSError as e:
        _log.warning(f"ftp-test-review persist failed: {e}")
        return JSONResponse({"detail": "persist failed"}, 500)
    return {"ok": True, "persisted": True}


@app.delete("/api/rides/{ride_id}")
def api_ride_delete(ride_id: str):
    """Delete a saved ride (FIT import or legacy JSON)."""
    safe_id = re.sub(r'[^\w.\-]', '_', ride_id)
    fit_path = _rides_fit_dir() / f"{safe_id}.fit"
    if fit_path.exists():
        try:
            fit_path.unlink()
        except OSError as e:
            _log.warning(f"ride delete failed: {e}")
            return JSONResponse({"error": "delete failed"}, 500)
        return {"ok": True}
    import ride_storage
    if ride_storage.delete_ride(ride_id):
        return {"ok": True}
    return JSONResponse({"error": "Ride not found"}, 404)


@app.get("/api/rides/{ride_id}/fit")
def api_ride_fit(ride_id: str):
    """Download a saved ride as a FIT activity file.

    FIT imports are served verbatim. Legacy JSON rides are built via
    fit_activity.build_activity_fit using the saved sample payload.
    """
    safe_id = re.sub(r'[^\w.\-]', '_', ride_id)
    fit_path = _rides_fit_dir() / f"{safe_id}.fit"
    if fit_path.exists():
        return FileResponse(
            str(fit_path),
            media_type="application/octet-stream",
            filename=f"{safe_id}.fit",
        )

    import ride_storage
    ride = ride_storage.get_ride(ride_id)
    if not ride:
        return JSONResponse({"error": "Ride not found"}, 404)

    profile_id = ride.get("profile_id") or "domestique"
    try:
        from fit_activity import build_activity_fit
        fit_bytes = build_activity_fit(ride, profile_id)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, 409)
    except Exception as e:
        _log.exception(f"ride-fit: build_activity_fit failed: {e}")
        raise HTTPException(status_code=500, detail="failed to build FIT")

    slug = (profile_id or "ride").strip() or "ride"
    filename = f"domestique-{slug}-{safe_id}.fit"
    return Response(
        content=fit_bytes,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ═══════════════════════════════════════════════════════════════════════════════
# v4.6.7 IMPL-SUM PROGRAMME-SUMMARY — end-of-plan recap
# ═══════════════════════════════════════════════════════════════════════════════

def _ride_started_iso_date(ride: dict) -> str | None:
    """Best-effort 'YYYY-MM-DD' from a list_rides() entry."""
    s = ride.get("started_at") or ""
    if isinstance(s, str) and len(s) >= 10:
        return s[:10]
    return None


def _build_programme_summary(plan: dict) -> dict:
    """Compose the §4 programme-summary JSON shape.

    Reads:
      - plan dict (current_plan.json) for window + phases + planned TSS
      - athlete_metrics ledger for FTP / VO2max snapshots at start/end
      - ride_storage for actual rides in the window (decoupling, peaks,
        zone-time, TSS aggregates)
      - daily_log for Hooper trend
      - analytics + training for polarization / monotony
    """
    weeks = plan.get("weeks") or []
    if not weeks:
        return {
            "plan_id": "current",
            "start_date": None, "end_date": None, "weeks": 0,
            # The modal keys its empty state off "rides": zero rides must read
            # as "nothing ridden yet", never as measured zeros.
            "rides": 0,
            "ftp_delta": {}, "eftp_delta": {}, "vo2max_delta": {},
            "ctl_gain": {}, "intensity_dist": {}, "pol_index": {},
            "monotony_max": None, "strain_max": None, "compliance": [],
            "mean_max_curve": {"start": [], "end": []}, "hooper_trend": [],
            "totals": {}, "decoupling_trend": [],
            "citations": ["Stöggl 2014", "Foster 1998", "Treff 2019",
                          "Hooper 1995", "Coggan/Allen TR&P"],
        }
    start_date = weeks[0].get("start") or weeks[0].get("day")
    end_date = weeks[-1].get("end") or weeks[-1].get("day")
    n_weeks = len(weeks)

    # A CONTINUOUS plan regenerates on a rolling basis, so "plan start" is
    # whenever the last regeneration happened — possibly yesterday. Summarising
    # from there produces a recap of nothing: a rider with a 213-TSS ride five
    # days ago saw zeros everywhere because the window had just reset behind
    # it. A rolling plan has no end to recap, so its summary is a trailing
    # 28-day one instead, extended to the plan window when that is longer.
    goal_type = str(((plan.get("goal") or {}).get("goal_type")) or "").lower()
    is_continuous = goal_type == "continuous" or any(
        (w.get("phase") or "") == "continuous" for w in weeks[:1])
    if is_continuous and start_date:
        from datetime import date as _date, timedelta as _td
        trailing = (clock.today() - _td(days=28)).isoformat()
        if trailing < start_date:
            start_date = trailing
        if end_date:
            end_date = min(end_date, clock.today().isoformat())

    # ── FTP / VO2max ledger lookups ────────────────────────────────────────
    def _value_at_or_before(metric: str, target_date: str) -> float | None:
        try:
            history = db.query_metric_history(metric, days=400)
        except Exception:
            return None
        best = None
        for r in history:
            if r["date"] <= target_date:
                best = r["value"]
            else:
                break
        if best is None and history:
            return float(history[0]["value"])
        return float(best) if best is not None else None

    def _value_at_or_after(metric: str, target_date: str) -> float | None:
        try:
            history = db.query_metric_history(metric, days=400)
        except Exception:
            return None
        for r in history:
            if r["date"] >= target_date:
                return float(r["value"])
        return float(history[-1]["value"]) if history else None

    ftp_start = _value_at_or_before("ftp", start_date) if start_date else None
    ftp_end = _value_at_or_after("ftp", end_date) if end_date else None
    if ftp_end is None:
        ftp_end = _value_at_or_before("ftp", end_date) if end_date else None

    vo2_start = _value_at_or_before("vo2max", start_date) if start_date else None
    vo2_end = _value_at_or_after("vo2max", end_date) if end_date else None
    if vo2_end is None:
        vo2_end = _value_at_or_before("vo2max", end_date) if end_date else None

    def _delta(a, b) -> dict:
        if a is None or b is None:
            return {"start": int(round(a)) if a else None,
                    "end": int(round(b)) if b else None,
                    "pct": None}
        try:
            pct = round(((b - a) / a) * 100.0, 1) if a else None
        except (TypeError, ZeroDivisionError):
            pct = None
        return {"start": int(round(a)), "end": int(round(b)), "pct": pct}

    def _delta_float(a, b) -> dict:
        if a is None or b is None:
            return {"start": a, "end": b, "pct": None}
        try:
            pct = round(((b - a) / a) * 100.0, 1) if a else None
        except (TypeError, ZeroDivisionError):
            pct = None
        return {"start": round(a, 1), "end": round(b, 1), "pct": pct}

    # ── Rides in window ────────────────────────────────────────────────────
    try:
        import ride_storage as _rs
        # load_all_rides, not list_rides: list_rides globs ride_*.json — FIT
        # imports only. A rider whose rides all arrive by intervals.icu sync
        # (the icu/i*.json cache) summed to zero rides here, so the recap
        # showed zeros over a window with a 213-TSS ride sitting in it.
        all_rides = _rs.load_all_rides()
    except Exception:
        all_rides = []
    # load_all_rides mixes two record shapes: FIT imports nest their numbers
    # under "summary", ICU sync records are flat. Every aggregate below reads
    # the summary shape, so a flat record gets one synthesized — otherwise an
    # ICU-only rider counts rides but sums zero km, zero hours, zero kJ.
    for _r in all_rides:
        if not _r.get("summary"):
            _r["summary"] = {
                "tss": _r.get("tss"),
                "distance_km": _r.get("distance_km"),
                "duration_sec": _r.get("duration_s") or _r.get("moving_s"),
                "kj_mechanical": _r.get("kj"),
                "elevation_gain_m": _r.get("elevation_m"),
                "decoupling_pct": _r.get("decoupling_pct"),
            }
        # Same shape trap for zone time: FIT records call it "zones", ICU
        # records "time_in_zone" — so the intensity-distribution and
        # polarization charts stayed empty for a fully-synced rider. The "ss"
        # entry is ICU's sweet-spot overlay spanning parts of z3/z4; summing
        # it alongside the real zones would double-count those seconds.
        if not _r.get("zones") and isinstance(_r.get("time_in_zone"), dict):
            _r["zones"] = {k: v for k, v in _r["time_in_zone"].items()
                           if k != "ss"}
    in_window = []
    for r in all_rides:
        d = _ride_started_iso_date(r)
        if d and start_date and end_date and start_date <= d <= end_date:
            in_window.append(r)

    # ── eFTP Δ — Coggan 95% rule on best 20-min ────────────────────────────
    def _best_20min_window(rides_subset: list[dict]) -> int | None:
        best = 0
        for r in rides_subset:
            summary = r.get("summary") or {}
            for k in ("peak_20min", "best_20min", "p20min"):
                v = summary.get(k)
                if v and v > best:
                    best = float(v)
            for eff in (r.get("efforts") or []):
                lab = (eff.get("label") or eff.get("name") or "")
                secs = eff.get("secs") or eff.get("seconds") or 0
                w = eff.get("watts") or eff.get("power") or 0
                if "20" in str(lab) and ("min" in str(lab).lower() or secs == 1200) and w > best:
                    best = float(w)
        return int(round(best)) if best > 0 else None

    half = max(1, len(in_window) // 2)
    early_rides = sorted(in_window, key=lambda r: r.get("started_at") or "")[:half]
    late_rides = sorted(in_window, key=lambda r: r.get("started_at") or "")[half:]

    eftp_start_w = None
    eftp_end_w = None
    p20_start = _best_20min_window(early_rides)
    p20_end = _best_20min_window(late_rides)
    if p20_start:
        eftp_start_w = int(round(p20_start * 0.95))
    if p20_end:
        eftp_end_w = int(round(p20_end * 0.95))
    if eftp_start_w is None:
        eftp_start_w = int(round(ftp_start)) if ftp_start else None
    if eftp_end_w is None:
        eftp_end_w = int(round(ftp_end)) if ftp_end else None

    # ── CTL gain ───────────────────────────────────────────────────────────
    # intervals.icu's CTL on the first day and on the last (today's live when
    # the window runs to today). It was a local EWMA over the FIT-only rides,
    # None at the end and a guess at the start for a rider on intervals.icu.
    import fitness
    ctl_start_val = fitness.stored_ctl_on(start_date) if start_date else None
    if end_date and end_date < clock.today().isoformat():
        ctl_end_val = fitness.stored_ctl_on(end_date)
    else:
        ctl_end_val = _fitness_state()["ctl"]

    ctl_block = {
        "start": ctl_start_val,
        "end": ctl_end_val,
        "delta": (round(ctl_end_val - ctl_start_val, 1)
                  if (ctl_start_val is not None and ctl_end_val is not None)
                  else None),
    }

    # ── Intensity distribution ─────────────────────────────────────────────
    z1z2_secs = 0
    z3_secs = 0
    z4plus_secs = 0
    for r in in_window:
        tiz = r.get("zones") or {}
        if isinstance(tiz, dict):
            try:
                z1z2_secs += int(tiz.get("z1") or 0) + int(tiz.get("z2") or 0)
                z3_secs += int(tiz.get("z3") or 0)
                z4plus_secs += (int(tiz.get("z4") or 0) + int(tiz.get("z5") or 0)
                                + int(tiz.get("z6") or 0) + int(tiz.get("z7") or 0))
            except (TypeError, ValueError):
                pass
    intensity_dist = {
        "z1z2_min": int(round(z1z2_secs / 60.0)),
        "z3_min": int(round(z3_secs / 60.0)),
        "z4plus_min": int(round(z4plus_secs / 60.0)),
    }

    # ── Polarization index ─────────────────────────────────────────────────
    from analytics import compute_polarization_block, classify_distribution
    pol_indices = []
    for r in in_window:
        block = compute_polarization_block(r.get("zones") or {})
        if block and block.get("polarization_index") is not None:
            pol_indices.append(block["polarization_index"])
    if pol_indices:
        pol_mean = round(sum(pol_indices) / len(pol_indices), 2)
        total_s = z1z2_secs + z3_secs + z4plus_secs
        if total_s > 0:
            z1z2_pct = 100.0 * z1z2_secs / total_s
            z3_pct = 100.0 * z3_secs / total_s
            z4_pct = 100.0 * z4plus_secs / total_s
            klass = classify_distribution(z1z2_pct, z3_pct, z4_pct, pol_mean)
        else:
            klass = "—"
        pol_block = {"mean": pol_mean, "class": klass}
    else:
        pol_block = {"mean": None, "class": "—"}

    # ── Monotony / strain trend per week ───────────────────────────────────
    import statistics as _stat
    monotony_max = None
    strain_max = None
    if start_date and end_date:
        try:
            import datetime as _dt
            week_start = _dt.date.fromisoformat(start_date)
            week_end_d = _dt.date.fromisoformat(end_date)
            cursor = week_start
            while cursor <= week_end_d:
                daily = {}
                for k in range(7):
                    daily[(cursor + _dt.timedelta(days=k)).isoformat()] = 0.0
                for r in in_window:
                    d = _ride_started_iso_date(r)
                    if d and d in daily:
                        tss = (r.get("summary") or {}).get("tss") or 0
                        daily[d] += float(tss)
                loads = list(daily.values())
                weekly_load = sum(loads)
                if weekly_load > 0:
                    try:
                        sd = _stat.stdev(loads)
                    except _stat.StatisticsError:
                        sd = 0
                    if sd > 0:
                        m = _stat.mean(loads) / sd
                        s = weekly_load * m
                        if monotony_max is None or m > monotony_max:
                            monotony_max = round(m, 2)
                        if strain_max is None or s > strain_max:
                            strain_max = round(s, 1)
                cursor += _dt.timedelta(days=7)
        except Exception:
            pass

    # ── Compliance per phase ───────────────────────────────────────────────
    compliance = []
    phases = plan.get("phases") or []
    for ph in phases:
        ph_start = ph.get("start")
        ph_end = ph.get("end")
        if not ph_start or not ph_end:
            continue
        weekly_tss = float(ph.get("weekly_tss") or 0)
        phase_weeks = int(ph.get("weeks") or 0) or max(
            1, sum(1 for w in weeks if w.get("phase") == ph.get("name")))
        planned = weekly_tss * phase_weeks
        actual = 0.0
        for r in in_window:
            d = _ride_started_iso_date(r)
            if d and ph_start <= d <= ph_end:
                actual += float((r.get("summary") or {}).get("tss") or 0)
        pct = int(round(100.0 * actual / planned)) if planned > 0 else 0
        compliance.append({
            "phase": ph.get("name"),
            "planned_tss": round(planned, 1),
            "actual_tss": round(actual, 1),
            "pct": pct,
        })

    # ── Mean-max power curve (first 4w vs last 4w) ─────────────────────────
    try:
        import datetime as _dt
        sd_d = _dt.date.fromisoformat(start_date) if start_date else None
        ed_d = _dt.date.fromisoformat(end_date) if end_date else None
    except (ValueError, TypeError):
        sd_d = ed_d = None
    early_4w = []
    last_4w = []
    if sd_d and ed_d:
        early_cut = (sd_d + timedelta(days=28)).isoformat()
        last_cut = (ed_d - timedelta(days=28)).isoformat()
        for r in in_window:
            d = _ride_started_iso_date(r)
            if not d:
                continue
            if d <= early_cut:
                early_4w.append(r)
            if d >= last_cut:
                last_4w.append(r)

    def _peaks(rides_subset: list[dict]) -> list[dict]:
        targets = [(5, 5), (60, 60), (300, 300), (1200, 1200), (3600, 3600)]
        out = []
        for tgt_sec, label_sec in targets:
            best = 0
            for r in rides_subset:
                summary = r.get("summary") or {}
                for k in (f"peak_{tgt_sec}s", f"best_{tgt_sec}s",
                          f"peak_{tgt_sec // 60}m", f"best_{tgt_sec // 60}m"):
                    v = summary.get(k)
                    if v and float(v) > best:
                        best = float(v)
                for eff in (r.get("efforts") or []):
                    secs = eff.get("secs") or eff.get("seconds") or 0
                    w = eff.get("watts") or eff.get("power") or 0
                    if abs(secs - tgt_sec) <= max(2, tgt_sec * 0.05) and w > best:
                        best = float(w)
            out.append({"dur": label_sec, "watts": int(round(best)) if best > 0 else None})
        return out

    mm = {"start": _peaks(early_4w), "end": _peaks(last_4w)}

    # ── Hooper trend per week ──────────────────────────────────────────────
    hooper_trend: list[dict] = []
    try:
        if sd_d and ed_d:
            total_days = (ed_d - sd_d).days + 1
            log_rows = db.query_daily_log(days=max(total_days + 7, 14))
            by_date = {row["date"]: row.get("hooper_index") for row in log_rows}
            week_idx = 0
            cur = sd_d
            while cur <= ed_d:
                vals = []
                for k in range(7):
                    di = (cur + timedelta(days=k)).isoformat()
                    v = by_date.get(di)
                    if v is not None:
                        try:
                            vals.append(float(v))
                        except (TypeError, ValueError):
                            pass
                if vals:
                    hooper_trend.append({
                        "week": week_idx + 1,
                        "mean": round(sum(vals) / len(vals), 1),
                    })
                week_idx += 1
                cur += timedelta(days=7)
    except Exception:
        hooper_trend = []

    # ── Totals ─────────────────────────────────────────────────────────────
    total_km = 0.0
    total_sec = 0
    total_kj = 0.0
    total_elev = 0.0
    for r in in_window:
        s = r.get("summary") or {}
        total_km += float(s.get("distance_km") or 0)
        total_sec += int(s.get("duration_sec") or 0)
        kj = s.get("kj_mechanical") or s.get("calories") or s.get("total_kj") or 0
        try:
            total_kj += float(kj)
        except (TypeError, ValueError):
            pass
        total_elev += float(s.get("elevation_gain_m") or 0)
    totals = {
        "km": int(round(total_km)),
        "hours": round(total_sec / 3600.0, 1),
        "kj": int(round(total_kj)),
        "elev_m": int(round(total_elev)),
    }

    # ── Decoupling trend per week ──────────────────────────────────────────
    decoupling_trend: list[dict] = []
    if sd_d and ed_d:
        cur = sd_d
        wk_idx = 0
        while cur <= ed_d:
            vals = []
            for r in in_window:
                d = _ride_started_iso_date(r)
                if not d:
                    continue
                d_iter = date.fromisoformat(d)
                if cur <= d_iter <= cur + timedelta(days=6):
                    dec = (r.get("summary") or {}).get("decoupling_pct")
                    if dec is None:
                        dec = r.get("decoupling_pct")
                    if dec is not None:
                        try:
                            vals.append(float(dec))
                        except (TypeError, ValueError):
                            pass
            if vals:
                decoupling_trend.append({
                    "week": wk_idx + 1,
                    "mean_pct": round(sum(vals) / len(vals), 1),
                })
            wk_idx += 1
            cur += timedelta(days=7)

    return {
        "plan_id": "current",
        "start_date": start_date,
        "end_date": end_date,
        "weeks": n_weeks,
        "rides": len(in_window),
        "ftp_delta": _delta(ftp_start, ftp_end),
        "eftp_delta": _delta(eftp_start_w, eftp_end_w),
        "vo2max_delta": _delta_float(vo2_start, vo2_end),
        "ctl_gain": ctl_block,
        "intensity_dist": intensity_dist,
        "pol_index": pol_block,
        "monotony_max": monotony_max,
        "strain_max": strain_max,
        "compliance": compliance,
        "mean_max_curve": mm,
        "hooper_trend": hooper_trend,
        "totals": totals,
        "decoupling_trend": decoupling_trend,
        "citations": [
            "Stöggl & Sperlich 2014 (Front Physiol)",
            "Foster 1998 (MSSE)",
            "Treff et al. 2019 (Front Physiol)",
            "Hooper & Mackinnon 1995",
            "Coggan & Allen TR&P 3rd ed.",
            "Seiler 2010 (IJSPP)",
        ],
    }


@app.get("/api/programme/summary")
def api_programme_summary(plan_id: str = Query("current")):
    """v4.6.7 IMPL-SUM — end-of-plan recap as structured JSON.

    Composes 12 literature-grounded metrics over the active plan window:
    FTP/eFTP/VO2max Δ, CTL gain, intensity distribution, polarization
    index (Treff 2019), monotony/strain (Foster 1998), compliance per
    phase, mean-max curve (start-4w vs end-4w), Hooper trend (1995),
    totals, decoupling trend.
    """
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan"}, 404)
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return JSONResponse({"error": f"plan read failed: {e}"}, 500)
    summary = _build_programme_summary(plan)
    summary["plan_id"] = plan_id
    return summary


@app.get("/api/programme/summary/png")
def api_programme_summary_png(plan_id: str = Query("current")):
    """v4.6.7 IMPL-SUM — render the programme summary as PNG (1200×1600).

    No new dependencies — Pillow is already in the runtime stack via
    ride_report_png.
    """
    json_path = _plan_dir() / "current_plan.json"
    if not json_path.exists():
        return JSONResponse({"error": "No active plan"}, 404)
    try:
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        return JSONResponse({"error": f"plan read failed: {e}"}, 500)
    summary = _build_programme_summary(plan)
    summary["plan_id"] = plan_id
    try:
        from programme_summary_png import render_programme_summary_png
        png = render_programme_summary_png(summary)
    except Exception as e:
        _log.exception(f"programme summary PNG render failed: {e}")
        return JSONResponse({"error": "render failed"}, 500)
    return Response(content=png, media_type="image/png",
                    headers={"Content-Disposition":
                             f'inline; filename="programme-summary-{plan_id}.png"'})


# ═══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTICS (v1.6.0)
# ═══════════════════════════════════════════════════════════════════════════════

# 60-second cache for the health check — cheap protection against repeated
# clicks on the Diagnostics modal hammering the disk.
_DIAG_HEALTH_CACHE: dict = {"ts": 0.0, "result": None}
_DIAG_HEALTH_CACHE_TTL = 60.0


@app.get("/api/diag/recent-errors")
def api_diag_recent_errors(
    request: Request,
    since: str | None = Query(None),
    limit: int = Query(50),
    verbose: int = Query(0),
):
    """v1.6.0 — return the last N entries from the in-process error ring.

    ``since`` is an ISO timestamp filter (return only entries with ts >
    since). ``limit`` is clamped to [1, 256]. ``verbose=1`` includes the
    full exc_msg; default truncates / strips PII-ish keys.
    """
    if not _diag_local_only(request):
        return JSONResponse({"error": "local-only"}, status_code=403)
    n = max(1, min(int(limit or 50), _DIAG_RING_MAX))
    items = _diag_ring_snapshot(limit=n, since_iso=since)
    if not verbose:
        # Strip context keys that could carry PII / big payloads.
        BAN = {"path", "body", "headers", "cookie", "authorization"}
        scrubbed: list[dict] = []
        for e in items:
            ctx = {k: v for k, v in (e.get("context") or {}).items() if k not in BAN}
            row = dict(e)
            row["context"] = ctx
            # Truncate exc_msg defensively (already capped at 500 in _log_error).
            if "exc_msg" in row and isinstance(row["exc_msg"], str):
                row["exc_msg"] = row["exc_msg"][:240]
            scrubbed.append(row)
        items = scrubbed
    return {"count": len(items), "items": items}


@app.post("/api/diag/frontend-error")
async def api_diag_frontend_error(request: Request):
    """v1.6.0 — accept structured error reports from the dashboard JS.

    Body: ``{"code": "E_FRONTEND_*", "context": {...}, "url": str, "user_agent": str}``.
    Unknown codes are coerced to ``E_FRONTEND_GENERIC`` with the original
    code preserved in context.
    """
    if not _diag_local_only(request):
        return JSONResponse({"error": "local-only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    raw_code = str(body.get("code") or "")
    if error_codes.is_valid_code(raw_code):
        code = raw_code
        ctx_extra: dict = {}
    else:
        code = error_codes.Codes.FRONTEND_GENERIC
        ctx_extra = {"original_code": raw_code or "(missing)"}
    ctx = body.get("context") or {}
    if not isinstance(ctx, dict):
        ctx = {"_raw": str(ctx)[:200]}
    ctx = dict(ctx)
    ctx.update(ctx_extra)
    if "url" in body:
        ctx["url"] = str(body["url"])[:300]
    if "user_agent" in body:
        ctx["user_agent"] = str(body["user_agent"])[:200]
    _log_error(code, **ctx)
    return {"ok": True, "code": code}


@app.get("/api/diag/health")
def api_diag_health(request: Request):
    """v1.6.0 — system-state self-check.

    Always returns 200 so the modal can reach it even when the app is
    in a degraded state. ``ok`` is False if ANY check failed; per-check
    results are in ``checks``. 60-second in-process cache.
    """
    if not _diag_local_only(request):
        return JSONResponse({"error": "local-only"}, status_code=403)
    now = time.time()
    cached = _DIAG_HEALTH_CACHE.get("result")
    if cached is not None and (now - _DIAG_HEALTH_CACHE.get("ts", 0)) < _DIAG_HEALTH_CACHE_TTL:
        return cached
    checks: dict = {}
    plan_path = _plan_dir() / "current_plan.json"
    plan_data: dict | None = None
    # plan_readable
    try:
        if not plan_path.exists():
            checks["plan_readable"] = {"ok": False, "code": error_codes.Codes.PLAN_PARSE_MISSING}
        else:
            with open(plan_path, encoding="utf-8") as f:
                plan_data = json.load(f)
            checks["plan_readable"] = {"ok": True}
    except json.JSONDecodeError as e:
        checks["plan_readable"] = {"ok": False, "code": error_codes.Codes.PLAN_PARSE_CORRUPT, "msg": str(e)[:200]}
    except OSError as e:
        checks["plan_readable"] = {"ok": False, "code": error_codes.Codes.PLAN_LOAD_OS_ERROR, "msg": str(e)[:200]}
    # v3.11.3: OAuth readiness — the build gates prove .oauth.env is IN the
    # bundle; this proves the frozen app actually LOADED it (a reporter hit
    # "token exchange failed" on 3.11.2 and the platform was unknown).
    # Booleans only — the secret never leaves the process.
    checks["icu_oauth"] = {
        "client_id": str(getattr(config, "ICU_OAUTH_CLIENT_ID", "") or ""),
        "secret_loaded": bool(getattr(config, "ICU_OAUTH_CLIENT_SECRET", "")),
        "redirect_uri": str(getattr(config, "ICU_OAUTH_REDIRECT_URI", "") or ""),
        # v3.11.4: which verifier guards the sign-in. CI asserts os-native on
        # Windows (antivirus roots OpenSSL refuses) and openssl on Linux.
        "tls_backend": tls_trust.backend_name(),
    }
    # workout_library
    try:
        lib = tp.load_workout_library()
        checks["workout_library"] = {"ok": True, "count": len(lib) if hasattr(lib, "__len__") else None}
        # Linux-IP D6: pool health — one curl diagnoses the whole class of
        # "plan degenerated to endurance-only" faults (empty library, hit-
        # only pool collapse, stale workout_dir override).
        try:
            _pi = tp._build_pool_indexes(lib)
            checks["pool_health"] = {
                "hit": len(_pi.get("hit") or []),
                "endurance": len(_pi.get("endurance") or []),
                "all_pool": len(_pi.get("all_pool") or []),
                "library": len(lib),
                "workout_dir": str(tp.WORKOUT_DIR),
                "workout_dir_is_bundled":
                    tp.WORKOUT_DIR == tp._BUNDLED_WORKOUT_DIR,
                "collapse_reason": tp._pool_collapse_reason(_pi, lib) or None,
            }
        except Exception as e:
            checks["pool_health"] = {"ok": False, "msg": str(e)[:200]}
    except Exception as e:
        checks["workout_library"] = {"ok": False, "code": error_codes.Codes.ENRICH_LIBRARY, "msg": str(e)[:200]}
    # enrich
    if plan_data is not None and isinstance(plan_data, dict):
        try:
            sample = json.loads(json.dumps(plan_data, default=str))
            _enrich_plan_for_response(sample, today_iso=clock.today().isoformat())
            # Spot-check at least one session got an enrichment field.
            ok = False
            for w in (sample.get("weeks") or []):
                for s in (w.get("sessions") or []):
                    if "card_state" in s or "card_state_v2" in s:
                        ok = True
                        break
                if ok:
                    break
            checks["enrich"] = {"ok": True} if ok else {"ok": False, "code": error_codes.Codes.ENRICH_FAILED, "msg": "no card_state on any session"}
        except Exception as e:
            checks["enrich"] = {"ok": False, "code": error_codes.Codes.ENRICH_FAILED, "msg": str(e)[:200]}
    else:
        checks["enrich"] = {"ok": True, "skipped": "no plan_data"}
    # rides_dir
    try:
        rides_dir = Path.home() / ".domestique" / "rides"
        if rides_dir.exists():
            _ = list(rides_dir.iterdir())
        checks["rides_dir"] = {"ok": True, "exists": rides_dir.exists()}
    except Exception as e:
        checks["rides_dir"] = {"ok": False, "msg": str(e)[:200]}
    # log_dir
    try:
        log_dir = Path.home() / ".domestique" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        checks["log_dir"] = {"ok": True, "writable": os.access(str(log_dir), os.W_OK)}
    except Exception as e:
        checks["log_dir"] = {"ok": False, "msg": str(e)[:200]}
    overall_ok = all(c.get("ok") for c in checks.values())
    result = {
        "ok": overall_ok,
        "checks": checks,
        "ts": clock.now(timezone.utc).isoformat(),
        "ring_size": len(_DIAG_RING),
    }
    _DIAG_HEALTH_CACHE["result"] = result
    _DIAG_HEALTH_CACHE["ts"] = now
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# HTML
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    from fastapi.responses import RedirectResponse
    from profile_manager import ProfileManager
    active_profile_id = ""
    try:
        pm = ProfileManager.get()
        # AC4 (flag-only rule, no heuristics): first-run wizard when
        #   1. there is NO active profile (fresh boot pre-migration, or
        #      delete-last), or
        #   2. the active profile was auto-created fresh by migrate_to_profiles
        #      ("bootstrapped": true in the registry) AND setup never completed.
        # The old bundle-marker fallback is gone (the repo ships
        # .setup_complete, which defeated first-run detection entirely), and
        # legacy upgrades are marked bootstrapped=false by the migration — a
        # real install provably never redirects.
        if pm.active_id is None:
            return RedirectResponse(url="/setup")
        if (_active_profile_entry(pm).get("bootstrapped")
                and not _setup_marker().exists()):
            return RedirectResponse(url="/setup")
        active_profile_id = pm.active_id or ""
        # Multiple profiles + skip_picker=false → profile picker
        profiles = pm.list_profiles()
        skip = pm._registry.get("skip_picker", True)
        if len(profiles) > 1 and not skip:
            return RedirectResponse(url="/profile-picker")
    except Exception:
        pass
    # Otherwise → dashboard. active_profile_id feeds the per-profile
    # localStorage keys (AC2d) — rendered into an inline const.
    return templates.TemplateResponse(
        request=request, name="dashboard.html",
        context={"active_profile_id": active_profile_id})


if __name__ == "__main__":
    import uvicorn
    # Pinned to single-worker mode: this app holds per-process caches
    # (profile manager, plan files, cache maps) that would diverge
    # across workers.
    _UVICORN_WORKERS = 1
    assert _UVICORN_WORKERS == 1, (
        "Domestique requires single-worker uvicorn — see launcher.py / README "
        "for the reason (per-process caches, DB sync thread)."
    )
    # Follows the launcher's choice so a standalone `python app.py` lands on
    # the same URL the desktop app uses. 22400 only applies when nothing set
    # DOMESTIQUE_PORT — i.e. app.py run directly, without the launcher.
    _port = int(os.environ.get("DOMESTIQUE_PORT") or 22400)
    print(f"Domestique Dashboard - http://127.0.0.1:{_port}")
    uvicorn.run(app, host="127.0.0.1", port=_port, log_level="warning",
                workers=_UVICORN_WORKERS)
