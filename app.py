"""
Domestique Dashboard v2 — local web interface.

Run: python3 app.py
Open: http://localhost:8080

Pure HTML + CSS + vanilla JS. No npm, no frameworks.
"""

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
import log_config
_log = log_config.get_logger("app")
log = log_config.get_logger(__name__)

# v4.0.0-alpha: named category loggers for the observability layer that
# survived the trainer rip. .ride_import + .library are new; the old
# .ble / .ws / .session named loggers are gone with their runtimes.
log_library = log_config.get_logger("domestique.library")
log_ride_import = log_config.get_logger("domestique.ride_import")

# v1.6.0 — error-code observability layer.
# ``_log_error`` is the single funnel for "something failed inside an
# error path that the user might never see directly". Every call emits a
# structured log line with the literal E_<domain>_<failure> code AND
# appends an entry to the in-process ring buffer that
# ``/api/diag/recent-errors`` reads.
import error_codes
_DIAG_RING_MAX = 256
_DIAG_RING: collections.deque = collections.deque(maxlen=_DIAG_RING_MAX)
_DIAG_RING_LOCK = threading.Lock()


def _log_error(code: str, exc: Exception | None = None, **context) -> None:
    """Log a structured error event under the error-code taxonomy.

    Signature: ``_log_error(Codes.X, exc=e, key=value, ...)``. The ``code``
    must be a registered string from ``error_codes.REGISTRY``; passing an
    unregistered code is allowed (best-effort) but will be tagged as
    ``unregistered`` in the ring entry. ``exc`` (optional) records type
    and message. ``context`` keys are arbitrary diagnostic breadcrumbs.

    Always non-throwing: this helper sits inside other except clauses, so
    it must never raise. Worst case it logs nothing.
    """
    try:
        meta = error_codes.metadata(code)
        severity = (meta or {}).get("severity", "ERROR")
        entry: dict = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "code": code,
            "severity": severity,
            "context": dict(context),
        }
        if meta is None:
            entry["context"]["_unregistered_code"] = True
        if exc is not None:
            entry["exc_type"] = type(exc).__name__
            entry["exc_msg"] = str(exc)[:500]
        with _DIAG_RING_LOCK:
            _DIAG_RING.append(entry)
        # Console + file via standard logger. Severity → level mapping:
        # FATAL/ERROR → ERROR, WARN → WARNING, INFO → INFO.
        if severity in ("FATAL", "ERROR"):
            level = logging.ERROR
        elif severity == "WARN":
            level = logging.WARNING
        else:
            level = logging.INFO
        ctx_repr = " ".join(f"{k}={v!r}" for k, v in entry["context"].items())
        if exc is not None:
            _log.log(level, "%s %s exc=%s:%s", code, ctx_repr,
                     entry["exc_type"], entry["exc_msg"])
        else:
            _log.log(level, "%s %s", code, ctx_repr)
    except Exception:
        # Never let observability break the host code path.
        pass


def _diag_ring_snapshot(limit: int = 50, since_iso: str | None = None) -> list[dict]:
    """Return up to ``limit`` recent ring entries newest-first, optionally
    filtered to entries with ``ts > since_iso``.
    """
    with _DIAG_RING_LOCK:
        items = list(_DIAG_RING)
    items.reverse()  # newest first
    if since_iso:
        items = [e for e in items if e.get("ts", "") > since_iso]
    return items[:limit]

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
_user_data_dir = Path.home() / ".domestique"

COURSE_DIR    = Path(__file__).parent / "courses"
_DEFAULT_PLAN_DIR = _user_data_dir / "plans"

def _plan_dir() -> Path:
    """Dynamic plan dir: uses profile-specific path after profile switch."""
    d = getattr(tp, 'PLAN_DIR', _DEFAULT_PLAN_DIR)
    d.mkdir(parents=True, exist_ok=True)
    return d


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


# Legacy alias for any direct references
PLAN_DIR = _DEFAULT_PLAN_DIR
WORKOUT_DIR   = Path(__file__).parent / "workouts"  # bundled workout files
GPX_DIR       = Path(__file__).parent / "gpx"       # bundled GPX files

# Load user path overrides — check user data dir (packaged) then project dir (dev)
for _upf in [_user_data_dir / "user_paths.json", Path(__file__).parent / "user_paths.json"]:
    if _upf.exists():
        try:
            _up = json.loads(_upf.read_text(encoding="utf-8"))
            if _up.get("workout_dir"):
                WORKOUT_DIR = Path(_up["workout_dir"])
            if _up.get("gpx_dir"):
                GPX_DIR = Path(_up["gpx_dir"])
        except Exception:
            pass
        break
CONFIG_PATH   = Path(__file__).parent / "config.py"
ROUTE_DATA    = Path(__file__).parent / "routes.json"
ROUTE_PROFILES_INDEX = Path(__file__).parent / "profiles_indexed.json"
ROUTE_PROFILES_DIR = Path(__file__).parent / "profiles"

# Single source of truth for the app version. VERSION file is the canonical
# spec (read by the PyInstaller builder and the /api/version route).
try:
    _VERSION = (Path(__file__).parent / "VERSION").read_text(encoding="utf-8-sig").strip()
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
    # Restart the DB sync thread on every profile switch so it picks up the new
    # profile's db_path. profile_manager may also call this inline during
    # switch() — a second call here is intentionally idempotent.
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

    try:
        yield
    finally:
        # ── Shutdown cleanup: best-effort, don't block teardown ──────────
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
async def _get_json_body(request) -> dict:
    try:
        return await request.json()
    except Exception as e:
        log.debug(f"JSON parse failed on {request.url.path}: {e}")
        raise HTTPException(status_code=400, detail="invalid JSON body")


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
_ALLOWED_ORIGIN_PREFIXES = (
    "http://localhost:8080",
    "http://127.0.0.1:8080",
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

# Check both locations for setup marker (user home takes priority)
SETUP_MARKER = _user_data_dir / ".setup_complete"
if not SETUP_MARKER.exists() and (Path(__file__).parent / ".setup_complete").exists():
    SETUP_MARKER = Path(__file__).parent / ".setup_complete"
DATA_DIR = _user_data_dir


# ═══════════════════════════════════════════════════════════════════════════════
# SETUP WIZARD
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    return templates.TemplateResponse(request=request, name="setup.html")


@app.get("/api/setup/status")
def setup_status():
    return {"complete": SETUP_MARKER.exists()}


@app.get("/api/setup/defaults")
def setup_defaults():
    """Return current/detected config values to pre-fill the wizard."""
    workout_dir_default = ""

    return {
        "icu_id": config.ICU_ATHLETE_ID or "",
        "workout_dir": str(WORKOUT_DIR) if WORKOUT_DIR.exists() else workout_dir_default,
        "gpx_dir": str(GPX_DIR) if GPX_DIR.exists() else "",
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
        resp = httpx.get(url, auth=("API_KEY", api_key), timeout=10)
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
            w_resp = httpx.get(w_url, auth=("API_KEY", api_key), timeout=10)
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
    today = date.today()
    oldest = (today - _td(days=42)).isoformat()
    newest = today.isoformat()
    try:
        url = (f"https://intervals.icu/api/v1/athlete/{athlete_id}/activities"
               f"?oldest={oldest}&newest={newest}")
        resp = httpx.get(url, auth=("API_KEY", api_key), timeout=15)
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


@app.get("/api/setup/pick-folder")
def setup_pick_folder():
    """Open a native OS folder picker dialog and return the selected path."""
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
        # Get recent activities to find max HR and threshold HR
        url = f"https://intervals.icu/api/v1/athlete/{athlete_id}/activities?oldest=2024-01-01&newest=2026-12-31"
        resp = httpx.get(url, auth=("API_KEY", api_key), timeout=15)
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


_SETUP_PATH_ALLOWED_BASES = [
    Path.home(),
    Path.home() / ".domestique",
    Path("/tmp"),
    Path("/private/tmp"),  # macOS symlink target
]


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
    for base in _SETUP_PATH_ALLOWED_BASES:
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

        # 1. Save ICU credentials to profile's .env
        icu_id = body.get("icu_athlete_id", "").strip()
        icu_key = body.get("icu_api_key", "").strip()
        # v4.5.3 FIX-AUTO-ATHLETE-ID: when user provides an API key but no
        # athlete ID (or wants to verify the one they typed), call /athlete/0
        # to discover the authenticated athlete. Eliminates a class of typo-
        # induced HTTP 403 errors.
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
                    if p.exists():
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

        # 2. Update athlete profile via ProfileManager
        profile = {}
        for key in ("weight", "ftp", "lthr", "max_hr"):
            if key in body:
                profile[key] = body[key]
        if body.get("weight"):
            profile["lbm"] = round(float(body["weight"]) * 0.80, 1)

        # Server-side validation: clamp/reject physiologically implausible values
        validators = {
            "ftp": (50, 600),
            "weight": (30, 200),
            "lthr": (100, 220),
            "max_hr": (120, 240),
        }
        for key, (lo, hi) in validators.items():
            if key in profile:
                try:
                    v = float(profile[key])
                except (TypeError, ValueError):
                    raise HTTPException(400, f"invalid {key}")
                if not (lo <= v <= hi):
                    raise HTTPException(400, f"{key}={v} out of range [{lo},{hi}]")
                profile[key] = v
        if profile:
            update_settings(profile)

        # 3. Update path overrides. SEC4: reject paths outside the allow-list
        # (home / ~/.domestique / /tmp) so a malicious setup-save cannot make
        # the server read/write arbitrary filesystem locations later.
        for _field in ("workout_dir", "gpx_dir"):
            _val = body.get(_field)
            if _val and not _setup_path_allowed(Path(_val)):
                raise HTTPException(
                    status_code=400,
                    detail=f"{_field} must be under $HOME, ~/.domestique, or /tmp",
                )
        if body.get("workout_dir"):
            WORKOUT_DIR = Path(body["workout_dir"])
        if body.get("gpx_dir"):
            GPX_DIR = Path(body["gpx_dir"])

        paths_file = DATA_DIR / "user_paths.json"
        paths = {}
        if body.get("workout_dir"):
            paths["workout_dir"] = body["workout_dir"]
        if body.get("gpx_dir"):
            paths["gpx_dir"] = body["gpx_dir"]
        if paths:
            paths_file.write_text(json.dumps(paths, indent=2), encoding="utf-8")

        # 4. Save training preferences to profile
        prefs = {}
        if body.get("hours_per_week"):
            prefs["hours_per_week"] = float(body["hours_per_week"])
        if body.get("available_days") is not None:
            prefs["available_days"] = [int(d) for d in body["available_days"]]
        if body.get("rest_days") is not None:
            prefs["rest_days"] = [int(d) for d in body["rest_days"]]
        if prefs:
            pm.save_prefs(prefs)

        # 5. Mark setup complete — single global marker (per-profile marker dropped to avoid drift).
        SETUP_MARKER.write_text(
            json.dumps({"completed": datetime.now().isoformat(), "version": "1.0.0"}, indent=2),
            encoding="utf-8",
        )

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

_cache = {}
_cache_ts = {}

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
        "default",
        window_days=int(window_days),
        kj_threshold=int(kj_threshold),
    )

def cached(key, fn, ttl=300):
    now = time.time()
    if key in _cache and now - _cache_ts.get(key, 0) < ttl:
        return _cache[key]
    try:
        result = fn()
    except Exception as e:
        # If API call fails (no internet, DNS error), return stale cache.
        if key in _cache:
            return _cache[key]
        # v1.6.0 — log under E_CACHE_<key>-or-GENERIC and stick the empty
        # result for only 30s so transient errors don't sit in cache for
        # the full ttl. Trick: backdate _cache_ts to (now - (ttl - 30))
        # so the staleness check ``now - ts < ttl`` flips back to False
        # after 30 wall-clock seconds.
        cache_code = {
            "training": error_codes.Codes.CACHE_TRAINING,
            "sleep": error_codes.Codes.CACHE_SLEEP,
            "wellness": error_codes.Codes.CACHE_WELLNESS,
        }.get(key, error_codes.Codes.CACHE_GENERIC)
        _log_error(cache_code, exc=e, cache_key=key)
        _cache[key] = {}
        if ttl > 30:
            _cache_ts[key] = now - (ttl - 30)
        else:
            _cache_ts[key] = now
        return {}
    _cache[key] = result
    _cache_ts[key] = now
    return result

def clear_cache():
    _cache.clear()
    _cache_ts.clear()


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
                    athlete_path = Path.home() / ".domestique" / "profiles" / pid / "athlete.json"
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
    new_id = pm.create_profile(name, color)
    return {"ok": True, "id": new_id}


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
    try:
        latest = power_curve.latest_ride_id_in_window("default", int(window_days))
    except Exception as e:
        _log.debug(f"power_curve latest_ride_id failed: {e}")
        latest = ""
    cache_key = f"power_curve_default_{int(window_days)}_{latest}"
    now = time.time()
    ttl = 24 * 3600
    if not refresh and cache_key in _cache and now - _cache_ts.get(cache_key, 0) < ttl:
        return _cache[cache_key]
    try:
        result = power_curve.aggregate_power_curve("default",
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
            acquired, _lock = power_curve.acquire_backfill_lock()
            if acquired:
                try:
                    bf = power_curve.backfill_icu_history("default",
                                                          max_per_second=1,
                                                          _skip_lock=True)
                    _log.info(
                        "power-curve self-heal backfill: "
                        f"backfilled={bf.get('backfilled')} "
                        f"already_cached={bf.get('already_cached')} "
                        f"failed={bf.get('failed')}"
                    )
                except Exception as e:
                    _log.warning(f"power-curve self-heal backfill failed: {e}")
                finally:
                    power_curve.release_backfill_lock()
                # Recompute once now that envelopes are hydrated.
                try:
                    result = power_curve.aggregate_power_curve(
                        "default", window_days=int(window_days))
                    try:
                        n_rides = int(result.get("n_rides") or 0)
                    except (TypeError, ValueError):
                        n_rides = 0
                except Exception as e:
                    _log.warning(f"power-curve recompute after backfill failed: {e}")
    # v1.8.8 Bug 2/11 — surface `needs_backfill` so the dashboard can swap
    # the "Loading…" placeholder for a "Run backfill" button when streams
    # haven't been cached yet. The legacy gate (n_rides==0) silently missed
    # the common case where rides ARE cached but none carry efforts yet, so
    # it's now True whenever the rider curve is still empty after the
    # self-heal pass above. ADD-only field — callers ignore it when absent.
    result["needs_backfill"] = bool(not (result.get("rider_curve") or []))
    # Lazy GC — prune stale power_curve_* entries with old latest_ride_ids.
    for k in list(_cache.keys()):
        if k.startswith(f"power_curve_default_{int(window_days)}_") and k != cache_key:
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


def _run_backfill_job(task_id: str) -> None:
    """Worker thread — runs the one-shot backfill and updates the task entry.

    GRILL-WAVE2A W2A-G2 fix: the lock acquired by the endpoint is held
    until this worker finishes (success or crash). Released exactly once,
    here, in the `finally`. Closes the TOCTOU window where two POSTs
    between endpoint-release and worker-acquire could spawn duplicate
    ICU workers.
    """
    import power_curve
    try:
        result = power_curve.backfill_icu_history(
            "default", max_per_second=1, _skip_lock=True
        )
        with _backfill_thread_lock:
            _backfill_tasks[task_id] = {
                "task_id": task_id,
                "state": "done",
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
    icu_dir = _user_data_dir / "rides" / "icu"
    paths: list[Path] = []
    try:
        if icu_dir.exists():
            paths = sorted(icu_dir.glob("*.json"))
    except Exception:
        paths = []
    paths = [p for p in paths if not p.name.startswith(".")]
    total = len(paths)
    sticky = {"computed", "no_rr_data", "sanity_rejected", "icu_deleted"}
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
    try:
        latest = power_curve.latest_ride_id_in_window("default",
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
    cache_key = (f"fatigue_resistance_default_{int(window_days)}_"
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
        pct = int(round(100.0 * n_with_streams / n_long)) if n_long > 0 else 0
        result["power_streams_cached_pct"] = pct
        auto_triggered = False
        if n_with_streams == 0 and n_long >= 1:
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
            prefix = (f"fatigue_resistance_default_{int(window_days)}_"
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

    v1.8.10 Bug B — also scans ``~/.domestique/rides/icu/*.json`` so ICU
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
        # Only surface rides that actually have an α1 value in the table.
        if a1 is None:
            continue
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
            "algo_version": d.get("dfa_algo_version"),
        })

    # Newest first (date string sorts lexically for ISO).
    rides.sort(key=lambda r: r.get("date") or "", reverse=True)

    # ── Aggregate: rides in the last 42 days that resolved a threshold;
    #    fall back to the last 8 resolved if <3 in the window. r²-gated.
    def _resolved(row, key, channel):
        t = row.get(key)
        if not isinstance(t, dict):
            return None
        load = t.get(channel)
        r2 = t.get("r2_hr" if channel == "hr" else "r2_power")
        if load is None or r2 is None or r2 < _AGG_R2:
            return None
        return (load, r2, row.get("date") or "")

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
                out[med_k] = round(_stats.median(vals), 1)
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

    return {
        "rides": rides,
        "aggregate": {
            "hrvt1": _aggregate("hrvt1"),
            "hrvt2": _aggregate("hrvt2"),
            "n_window": 8,
            "agg_min_r2": _AGG_R2,
        },
        "n_total": len(raw),
        "n_computed": n_computed,
        "n_stale_version": n_stale,
    }


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

    ICU sync writes flat envelopes to ``~/.domestique/rides/icu/*.json``
    (dfa_alpha1_avg, dfa_alpha1_status, rr_intervals_count at the top
    level — no ``summary`` nesting). ``ride_storage.list_rides()`` reads
    the FIT-import dir only, so any DFA scan based on it misses ICU
    rides entirely. We wrap each ICU envelope to mimic the FIT shape
    expected by callers: ``{id, started_at, summary{...}, ...}``.
    """
    import json as _json
    out: list[dict] = []
    try:
        icu_dir = _user_data_dir / "rides" / "icu"
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
                "dfa_zone_minutes": d.get("dfa_zone_minutes"),
                "dfa_algo_version": d.get("dfa_algo_version"),
            })
    except Exception:
        pass
    return out


def _recent_dfa_and_decoupling() -> tuple[list[float], float | None, str | None, str | None]:
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
    rides.extend(_iter_icu_dfa_rides())
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
        return (_date.today() - _date.fromisoformat(str(d)[:10])).days
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
                        "at": datetime.now().isoformat()}),
            encoding="utf-8",
        )
    except Exception as e:
        _log.warning(f"readiness revert flag write failed: {e}")


def _compute_local_atl(days: int = 90, tau: int = 7) -> float | None:
    """v4.4.2 §B3 — 7-day EWMA over local ride TSS (mirror of
    ride_storage.compute_local_ctl with τ=7 for ATL).

    Returns None when no usable local TSS is found so callers can decide
    whether to keep ICU values or surface ``data_status``.
    """
    try:
        import ride_storage as _rs
    except Exception:
        return None
    rides = _rs.list_rides()
    if not rides:
        return None
    cutoff_iso = (date.today() - timedelta(days=days)).isoformat()
    per_day: dict[str, float] = {}
    for r in rides:
        started = (r.get("started_at") or "")[:10]
        if not started or started < cutoff_iso:
            continue
        summary = r.get("summary") or {}
        tss = summary.get("tss") or 0
        if not tss:
            continue
        try:
            per_day[started] = per_day.get(started, 0.0) + float(tss)
        except (TypeError, ValueError):
            continue
    if not per_day:
        return None
    today = date.today()
    atl = 0.0
    d = date.fromisoformat(min(per_day.keys()))
    while d <= today:
        tss_today = per_day.get(d.isoformat(), 0.0)
        atl = atl + (tss_today - atl) / float(tau)
        d += timedelta(days=1)
    return round(atl, 1)


def _local_training_load() -> dict:
    """v4.4.2 §B3 — single helper that returns CTL/ATL/TSB derived from the
    local rides archive.

    Output shape: ``{ctl, atl, tsb, source}`` where ``source`` is always
    "local" (callers compose with ICU values to produce "icu"/"mixed"/"local").
    Any of ctl/atl/tsb may be None if local rides have no TSS values at all.
    """
    try:
        import ride_storage as _rs
        ctl = _rs.compute_local_ctl()
    except Exception:
        ctl = None
    atl = _compute_local_atl()
    tsb = None
    if ctl is not None and atl is not None:
        tsb = round(ctl - atl, 1)
    return {"ctl": ctl, "atl": atl, "tsb": tsb, "source": "local"}


def _merge_training_load(icu_t: dict | None) -> dict:
    """v4.4.2 §B3 — merge ICU-derived training metrics with local fallback.

    Rule: prefer ICU value when present; fall back to local on a per-field
    basis. Source label:
      - "icu" if all ICU fields present
      - "local" if no ICU fields and local fallback used
      - "mixed" otherwise
    """
    icu = icu_t or {}
    local = _local_training_load()
    icu_ctl = icu.get("ctl")
    icu_atl = icu.get("atl")
    icu_tsb = icu.get("tsb")
    out = {
        "ctl": icu_ctl if icu_ctl is not None else local["ctl"],
        "atl": icu_atl if icu_atl is not None else local["atl"],
        "tsb": icu_tsb if icu_tsb is not None else local["tsb"],
        "acwr": icu.get("acwr"),
        "ramp_rate": icu.get("ramp_rate"),
        "monotony": icu.get("monotony"),
        "strain": icu.get("strain"),
    }
    icu_count = sum(1 for v in (icu_ctl, icu_atl, icu_tsb) if v is not None)
    if icu_count == 3:
        out["source"] = "icu"
    elif icu_count == 0:
        out["source"] = "local" if any(v is not None for v in (out["ctl"], out["atl"], out["tsb"])) else "none"
    else:
        out["source"] = "mixed"
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
    return {
        "readiness": r,
        # v1.8.8 Bug 8 — top-level unified score fields.
        "score_0_10": s10,
        "score_0_100": s100,
        "training": merged_load,
        "sleep": {
            "sleep_h": sleep.get("sleep_h"), "sleep_score": sleep.get("sleep_score"),
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
    _log.info(f"EVENT=readiness_cap_reverted signal={_} body={body}")
    return {
        "ok": True,
        "reverted": True,
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
    from datetime import date as _date_cls
    target_iso = date or _date_cls.today().isoformat()
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
    day_iso = str(body.get("date") or date.today().isoformat()).strip()
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
        tss_per_h = tp.TSS_PER_HOUR.get(new_type, 45)
        new_tss = round(old_duration / 60 * tss_per_h, 1)
        target["session_type"] = new_type
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
                duration_min=old_duration,
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
            )
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
            "at": datetime.now().isoformat(),
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
                today_iso=date.today().isoformat(),
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
            "duration_min": old_duration,
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

    today_iso = date.today().isoformat()
    # v1.8.8 Bug 7 — `scope='day'` targets tomorrow's session.
    target_iso = (
        (date.today() + timedelta(days=1)).isoformat()
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
                    tss_per_h = tp.TSS_PER_HOUR.get(new_type, 45)
                    new_tss = round(old_duration / 60 * tss_per_h, 1)
                    target["session_type"] = new_type
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
                            duration_min=old_duration,
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
                        )
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
                        "after": {"type": new_type, "duration_min": old_duration, "tss": new_tss},
                        "rematched": rematched,
                        "zwo_cleared": zwo_cleared,
                    })
        else:  # scope == "week"
            try:
                wk_result = tp.apply_week_tier_down(working, today_iso, dry_run=dry_run)
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
    d = (body.get("date") or date.today().isoformat()).strip()
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
            cutoff = (date.today() - timedelta(days=7)).isoformat()
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
def api_wellness(days: int = Query(28)):
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
            return [_wellness_record_to_api_dict(w) for w in data]
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
            return [_wellness_record_to_api_dict(w) for w in local]
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
        return [_wellness_record_to_api_dict(w) for w in shaped]
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
                pw = msg.get_value("power")
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
    tss_accum = 0.0
    distinct_high_targets: set = set()
    has_vo2_intensity = False

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
            # Linear-ramp TSS integral (matches planner).
            tss_accum += (plo * plo + plo * phi + phi * phi) / 3 * (dur / 3600) * 100
            # v2.1.0: slice the ramp across the zones it SWEEPS, mirroring the
            # planner's load_workout_library parse (training_planner.py, v2.0.6).
            # Binning the whole duration at mean power here (while the planner
            # sliced) drifted the two scanners' zone seconds → score_sync mismatch
            # (e.g. neuromuscular_30s120s_9x_52min.zwo scored 5 vs 6).
            _RAMP_SLICES = 20
            for _i in range(_RAMP_SLICES):
                _acc_zone((plo + (phi - plo) * (_i + 0.5) / _RAMP_SLICES) * 100, dur / _RAMP_SLICES)
            _acc_structure(phi * 100)
        elif tag == "SteadyState":
            dur = int(float(seg.get("Duration", 0)))
            p = float(seg.get("Power", 0.65))
            total_sec += dur
            tss_accum += dur / 3600 * (p ** 2) * 100
            _acc_zone(p * 100, dur)
            _acc_structure(p * 100)
        elif tag == "IntervalsT":
            reps = int(seg.get("Repeat", 1))
            on_s = int(float(seg.get("OnDuration", 0)))
            off_s = int(float(seg.get("OffDuration", 0)))
            on_p = float(seg.get("OnPower", 1.0))
            off_p = float(seg.get("OffPower", 0.5))
            total_sec += reps * (on_s + off_s)
            tss_accum += reps * on_s / 3600 * (on_p ** 2) * 100
            tss_accum += reps * off_s / 3600 * (off_p ** 2) * 100
            _acc_zone(on_p * 100, reps * on_s)
            _acc_zone(off_p * 100, reps * off_s)
            _acc_structure(on_p * 100)
            _acc_structure(off_p * 100)
        elif tag == "FreeRide":
            dur = int(float(seg.get("Duration", 0)))
            total_sec += dur
            tss_accum += dur / 3600 * (0.65 ** 2) * 100
            _acc_zone(65, dur)

    if total_sec == 0:
        return None
    dur_min = total_sec / 60
    if_val = (tss_accum / (dur_min / 60)) ** 0.5 / 10 if dur_min > 0 else 0.5
    return {
        "name": name, "description": description,
        "total_sec": total_sec, "tss": tss_accum, "if_val": if_val,
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
    if not WORKOUT_DIR.exists():
        return []
    seen: set[str] = set()
    for zwo_path in WORKOUT_DIR.glob("*.zwo"):
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
        mtimes = [p.stat().st_mtime for p in WORKOUT_DIR.glob("*.zwo")]
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
    library browser. Cached in-memory; refreshes when WORKOUT_DIR mtimes
    drift.
    """
    return {"tags": _get_library_tags_cached()}


@app.get("/api/workouts")
def api_workouts(
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
      - ``search=ramp`` — substring match (case-insensitive) against
        ``Name`` or ``File``.
      - ``sort=score_desc|score_asc|duration_desc|duration_asc|name_asc|name_desc``
        Default ``score_desc``.
    """
    workouts = []
    if not WORKOUT_DIR.exists():
        return []

    filter_tags: set[str] = set()
    if tags:
        filter_tags = {t.strip().lower() for t in tags.split(",") if t.strip()}

    # v4.1.2 IMPL-CLASSIFIER: load the content-classification cache once per
    # request. Same source as training_planner so the library browser and
    # planner agree on protocol categorisation. Empty {} fallback when the
    # cache is missing — the dominant-zone heuristic kicks back in.
    _content_classifications = tp._load_content_classifications()
    _CONTENT_TO_PROTOCOL = tp._CONTENT_TO_PROTOCOL

    # v4.2.0: resolve duration window. The new ``duration_min/max`` params
    # take precedence over the legacy ``min_duration/max_duration`` aliases
    # so callers using either spelling get the same behaviour.
    eff_min_dur = duration_min if duration_min is not None else min_duration
    eff_max_dur = duration_max if duration_max is not None else max_duration

    search_lower = (search or "").strip().lower()
    has_flag_key = (has_flag or "").strip()
    content_class_lower = (content_class or "").strip().lower()

    for zwo_path in sorted(WORKOUT_DIR.glob("*.zwo")):
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

        row = {
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
        }

        if score < min_score:
            continue
        if not (eff_min_dur <= dur_min <= eff_max_dur):
            continue
        if session_type and session_type.lower() not in protocol.lower():
            continue
        if filter_tags:
            row_tags_lower = {t.lower() for t in zwo_tags}
            if not (filter_tags & row_tags_lower):
                continue
        # v4.2.0: content_class exact-match. Empty/missing classification
        # never matches a non-empty filter, so the user can't accidentally
        # surface uncategorised files via the dropdown.
        if content_class_lower:
            if (content_class_val or "").lower() != content_class_lower:
                continue
        # v4.2.0: secondary-flag boolean filter.
        if has_flag_key:
            if not bool(secondary_flags.get(has_flag_key)):
                continue
        # v4.2.0: search-by-structure substring match against Name + File.
        # v1.8.20: also match display_name so typing the VISIBLE title (which is
        # now display_name, not the <name> tag) returns hits.
        if search_lower:
            hay = f"{name.lower()} {zwo_path.name.lower()} {(content_entry.get('display_name') or '').lower()}"
            if search_lower not in hay:
                continue
        workouts.append(row)

    # v4.2.0 IMPL-LIBRARY: explicit sort enum. Legacy ``score`` / ``duration``
    # / ``tss`` aliases stay supported (callers from older clients) so we
    # don't break the planner's score-ranked iteration.
    if sort in ("score_desc", "score"):
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

    return workouts[:limit]


@app.get("/api/workout/download/{filename}")
def download_workout_by_id(filename: str):
    """Serve a ZWO workout file as a download attachment.

    v4.0.0-alpha: IMPL-B's dashboard calls this URL pattern to let the user
    grab the raw ZWO for external trainer apps (MyWhoosh, Tacx, Zwift) now
    that the live-ride runtime is gone.

    FIX-SERVER: path disambiguated from ``/api/workout/{filename}/download``
    AND registered BEFORE ``/api/workout/{category}/{filename}`` below so
    FastAPI's declaration-order match resolves ``download`` as the literal
    path segment, not as ``category=download``. Both guarantees must hold
    for the route to resolve correctly.
    """
    path = _safe_path(WORKOUT_DIR, filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    # v1.6.4: media_type "application/octet-stream" (was "application/xml")
    # so WKWebView always treats it as a download.
    return FileResponse(
        path,
        filename=filename,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/api/workout/{category}/{filename}")
def api_workout_detail(category: str, filename: str):
    """Parse a ZWO file and return segment data for visualization."""
    # Flat layout first, legacy category/file fallback
    path = _safe_path(WORKOUT_DIR, filename)
    if not path or not path.exists():
        path = _safe_path(WORKOUT_DIR, category, filename)
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

    for el in workout_el:
        tag = el.tag
        # ZWO files occasionally store Duration as a float (e.g. "600.5").
        # `int()` on a numeric float is fine, but `int("600.5")` raises
        # ValueError and 500s the whole route list. Float-first, then int.
        dur = int(float(el.get("Duration", 0) or 0))
        if tag in ("Warmup", "Cooldown", "Ramp"):
            plo = float(el.get("PowerLow", 0.5))
            phi = float(el.get("PowerHigh", 0.7))
            segments.append({
                "type": tag, "start": cursor, "duration": dur,
                "power_low": round(plo * ftp), "power_high": round(phi * ftp),
                "pct_low": round(plo * 100), "pct_high": round(phi * 100),
            })
            cursor += dur
        elif tag == "SteadyState":
            pw = float(el.get("Power", 0.65))
            segments.append({
                "type": tag, "start": cursor, "duration": dur,
                "power": round(pw * ftp), "pct": round(pw * 100),
            })
            cursor += dur
        elif tag == "IntervalsT":
            reps = int(el.get("Repeat", 1))
            on_d = int(el.get("OnDuration", 0))
            off_d = int(el.get("OffDuration", 0))
            on_p = float(el.get("OnPower", 0.95))
            off_p = float(el.get("OffPower", 0.50))
            segments.append({
                "type": tag, "start": cursor,
                "duration": reps * (on_d + off_d),
                "repeats": reps,
                "on_duration": on_d, "off_duration": off_d,
                "on_power": round(on_p * ftp), "off_power": round(off_p * ftp),
                "on_pct": round(on_p * 100), "off_pct": round(off_p * 100),
            })
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

    return {
        "name": name, "description": desc,
        "display_name": _display_name,
        "total_seconds": cursor,
        "segments": segments,
        "ftp": ftp,
    }


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
        path = _safe_path(WORKOUT_DIR, fname) if fname else None
        if (not path or not path.exists()) and cat:
            path = _safe_path(WORKOUT_DIR, cat, fname)
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

    # Map readiness to session type + fallback types
    if score >= 80:
        types = ["vo2", "helgerud", "rønnestad"]
    elif score >= 70:
        types = ["sweet spot", "threshold", "z2", "endurance"]
    elif score >= 60:
        types = ["z2", "seiler", "endurance"]
    elif score >= 40:
        types = ["z2", "recovery", "endurance"]
    else:
        # REST-level readiness: still show 3 light suggestions rather
        # than an empty card (Bug D.2).
        types = ["recovery", "z2", "endurance"]

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
    workouts: list[dict] = []
    for stype in types:
        try:
            matches = api_workouts(
                min_score=3, session_type=stype,
                max_duration=duration + 30,
                min_duration=max(0, duration - 30),
                limit=10, sort="score", tags=None,
            )
        except Exception as _e:
            _log.warning(f"picker api_workouts({stype!r}) failed: {_e}")
            matches = []
        workouts.extend(matches)
        if len(workouts) >= 5:
            break

    # If still empty, try without session type filter
    if not workouts:
        try:
            workouts = api_workouts(
                min_score=3, max_duration=duration + 30,
                min_duration=max(0, duration - 30),
                limit=5, sort="score", tags=None,
            )
        except Exception as _e:
            _log.warning(f"picker api_workouts fallback failed: {_e}")
            workouts = []

    # Last-resort widening: drop the score floor + widen the duration
    # window so fresh installs with a smaller library still see ≥3 cards.
    if len(workouts) < 3:
        try:
            extra = api_workouts(
                min_score=1, max_duration=duration + 60,
                min_duration=0, limit=10, sort="score", tags=None,
            )
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
        has_gpx = (GPX_DIR / r / gpx_name).exists()

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

def _slugify_route_name(name: str) -> str:
    """Slugify a route name using the same rule as generate_route_profiles.py."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def _route_key_for(world_slug: str, crs_filename: str) -> tuple[str, str]:
    """Derive (slug, route_key) for a route based on its CRS filename.

    Mirrors the logic in generate_route_profiles.py so the key matches what was
    written to profiles_indexed.json and profiles/<world>__<slug>.json.
    """
    stem = crs_filename[:-4] if crs_filename.lower().endswith(".crs") else crs_filename
    # Virtual-world routes embed "<world>__<slug>" in the filename; real-world
    # routes slugify the whole filename.
    slug = stem.split("__", 1)[1] if "__" in stem else _slugify_route_name(stem)
    return slug, f"{world_slug}/{slug}"


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


_ROUTES_CACHE: list[dict] = []
_ROUTES_INDEX: dict = {}
_ROUTES_MTIME: float = 0.0
_ROUTES_LOCK = threading.Lock()

# ─── Canonical surface-segment mapping (MASTER_DECISIONS §1) ───────────────
# surface_types.json is authored in lowercase today, but training_live loads
# it as UPPERCASE TACX RoadSurface tokens (_SURFACE_NAME_MAP). Anything that
# crosses a tier (HTTP / WS) MUST downshift to the canonical lowercase enum:
# asphalt | gravel | cobble | dirt | sand | unknown. Emit "unknown" for any
# value outside that enum — never drop.
_SURFACE_CANONICAL_MAP: dict[str, str] = {
    # Already-lowercase canonical forms (pass through).
    "asphalt": "asphalt",
    "gravel": "gravel",
    "cobble": "cobble",
    "dirt": "dirt",
    "sand": "sand",
    "unknown": "unknown",
    # UPPERCASE TACX tokens (from training_live._SURFACE_NAME_MAP leak paths).
    "ASPHALT": "asphalt",
    "PAVED": "asphalt",
    "TARMAC": "asphalt",
    "COBBLESTONES_HARD": "cobble",
    "COBBLESTONES_SOFT": "cobble",
    "BRICK_ROAD": "cobble",
    "CONCRETE_PLATES": "cobble",
    "GRAVEL": "gravel",
    "OFF_ROAD": "gravel",
    "DIRT": "dirt",
    "TRAIL": "dirt",
    "SAND": "sand",
    "WOODEN_BOARDS": "unknown",
    "CATTLE_GRID": "unknown",
    "ICE": "unknown",
}


def _canonical_surface(raw) -> str:
    """Map any surface token (lower/upper) to the canonical lowercase enum.
    Unknown inputs fall through to "unknown" rather than silently dropping."""
    if not raw:
        return "unknown"
    s = str(raw).strip()
    return _SURFACE_CANONICAL_MAP.get(s) or _SURFACE_CANONICAL_MAP.get(s.upper()) or _SURFACE_CANONICAL_MAP.get(s.lower(), "unknown")


_SURFACE_TYPES_CACHE: dict | None = None
_SURFACE_TYPES_MTIME: float = 0.0
_SURFACE_TYPES_LOCK = threading.Lock()


def _load_surface_types_db() -> dict:
    """Return a cached copy of surface_types.json, reloaded on mtime change.

    Shape: `{"<region>/<slug>": [{start_km, end_km, surface}, ...]}`. Returns
    an empty dict if the file is missing or malformed — callers fall through
    to a single "unknown" segment so the frontend always has renderable data.
    """
    global _SURFACE_TYPES_CACHE, _SURFACE_TYPES_MTIME
    path = Path(__file__).parent / "surface_types.json"
    if not path.exists():
        return {}
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _SURFACE_TYPES_LOCK:
        if _SURFACE_TYPES_CACHE is None or mtime != _SURFACE_TYPES_MTIME:
            try:
                _SURFACE_TYPES_CACHE = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                _log.error(f"Failed to load surface_types.json: {e}")
                _SURFACE_TYPES_CACHE = {}
            _SURFACE_TYPES_MTIME = mtime
        return _SURFACE_TYPES_CACHE or {}


def _route_surface_segments(route_id: str, distance_km: float | None = None,
                             lap_info: dict | None = None) -> list[dict]:
    """Canonical `surface_segments` for a given route id.

    Returns a list of `{start_km, end_km, surface}` dicts. Surface values are
    canonical lowercase (MASTER_DECISIONS §1). When no surface data exists
    for the route, emits a single "unknown" segment covering 0..distance_km
    (or empty list when distance is unknown). Never returns None; the
    frontend always has something to paint.

    v3.6.0-fix29 — when a route carries `lap_route.laps > 1`,
    `surface_types.json` stores only ONE base-lap's worth of segments while
    `distance_km` reflects the fully multiplied distance. Tile the base
    segments `laps` times with `base_km` offsets so lap 2+ does not fall
    through the frontend's gap-filler as implicit asphalt (bug: Cobbled
    Classic Sectors × 2, Hidden Cruise 47 × 2, and any `lap_route.laps>1`).
    """
    if not route_id:
        return []
    db = _load_surface_types_db()
    raw = db.get(route_id) or []
    if not raw:
        if distance_km and distance_km > 0:
            return [{"start_km": 0.0, "end_km": float(distance_km), "surface": "unknown"}]
        return []
    base: list[dict] = []
    for seg in raw:
        try:
            s = float(seg.get("start_km", 0.0) or 0.0)
            e = float(seg.get("end_km", 0.0) or 0.0)
        except (TypeError, ValueError):
            continue
        if e <= s:
            continue
        base.append({
            "start_km": s,
            "end_km": e,
            "surface": _canonical_surface(seg.get("surface")),
        })
    # Multi-lap tiling (fix29). Only fires when the caller passes a
    # `lap_route` dict with `laps > 1`; single-lap routes return base as-is.
    laps = 1
    base_km = 0.0
    if isinstance(lap_info, dict):
        try:
            laps = int(lap_info.get("laps", 1) or 1)
        except (TypeError, ValueError):
            laps = 1
        try:
            base_km = float(lap_info.get("base_km", 0.0) or 0.0)
        except (TypeError, ValueError):
            base_km = 0.0
    if laps <= 1 or base_km <= 0:
        return base
    # Tile stride: prefer the base segments' own max end_km over
    # `lap_route.base_km` — authored data sometimes rounds base_km to 2 dp
    # (e.g. 11.85) while the segment file carries the unrounded value
    # (11.859), which would overlap lap 1 into lap 2 if we blindly used
    # base_km as the offset. The surface data is the canonical base.
    base_end = max(s["end_km"] for s in base) if base else base_km
    stride = base_end if base_end > 0 else base_km
    tiled: list[dict] = []
    for lap_idx in range(laps):
        offset = lap_idx * stride
        for seg in base:
            tiled.append({
                "start_km": seg["start_km"] + offset,
                "end_km": seg["end_km"] + offset,
                "surface": seg["surface"],
            })
    return tiled


def _build_routes_index(routes: list[dict]) -> dict:
    """Build inverted indexes for hot filter fields."""
    by_id: dict[str, dict] = {}
    by_region: dict[str, list[int]] = {}
    by_source: dict[str, list[int]] = {}
    by_category: dict[str, list[int]] = {}
    by_primary_surface: dict[str, list[int]] = {}
    by_terrain: dict[str, list[int]] = {}
    by_finish: dict[str, list[int]] = {}
    for i, r in enumerate(routes):
        rid = r.get("id", "")
        if rid:
            by_id[rid] = r
        by_region.setdefault(r.get("region", ""), []).append(i)
        by_source.setdefault(r.get("source", ""), []).append(i)
        by_category.setdefault(r.get("category", ""), []).append(i)
        by_primary_surface.setdefault(r.get("primary_surface", ""), []).append(i)
        by_terrain.setdefault(r.get("terrain", ""), []).append(i)
        by_finish.setdefault(r.get("finish_type", ""), []).append(i)
    return {
        "by_id": by_id,
        "by_region": by_region,
        "by_source": by_source,
        "by_category": by_category,
        "by_primary_surface": by_primary_surface,
        "by_terrain": by_terrain,
        "by_finish": by_finish,
    }


def _load_routes_v2(force: bool = False) -> tuple[list[dict], dict]:
    """Load routes.json v2 with mtime-based invalidation.

    Returns (routes, index). Caches indefinitely; reloads on file mtime change.
    Thread-safe via _ROUTES_LOCK.
    """
    global _ROUTES_CACHE, _ROUTES_INDEX, _ROUTES_MTIME
    if not ROUTE_DATA.exists():
        return [], {"by_id": {}, "by_region": {}, "by_source": {}, "by_category": {},
                    "by_primary_surface": {}, "by_terrain": {}, "by_finish": {}}
    try:
        mtime = ROUTE_DATA.stat().st_mtime
    except OSError:
        mtime = 0.0
    with _ROUTES_LOCK:
        if force or not _ROUTES_CACHE or mtime != _ROUTES_MTIME:
            try:
                data = json.loads(ROUTE_DATA.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as e:
                _log.error(f"Failed to load routes.json: {e}")
                return _ROUTES_CACHE, _ROUTES_INDEX
            # Accept either flat list (v2) or legacy dict shape — normalize.
            if isinstance(data, list):
                _ROUTES_CACHE = data
            else:
                # Legacy shape: {"worlds": [...]}; flatten best-effort so the
                # cache never ends up in a broken intermediate state.
                flat = []
                for w in (data.get("worlds") if isinstance(data, dict) else []) or []:
                    for r in w.get("routes", []):
                        flat.append(r)
                _ROUTES_CACHE = flat
            # Attach canonical surface_segments to every route entry so the
            # list endpoint (/api/routes) and the detail endpoint (/api/routes/{id})
            # both return the spatial data the mini-map needs. Single point of
            # truth — delegates to `_route_surface_segments` so malformed
            # `surface_types.json` entries cannot 500 the whole /api/routes
            # response (QA-CODE #2: the inline float() parse was unguarded).
            for _r in _ROUTES_CACHE:
                rid = _r.get("id")
                if not rid:
                    continue
                _r["surface_segments"] = _route_surface_segments(
                    rid, _r.get("distance_km"), _r.get("lap_route")
                )
            _ROUTES_INDEX = _build_routes_index(_ROUTES_CACHE)
            _ROUTES_MTIME = mtime
        return _ROUTES_CACHE, _ROUTES_INDEX


# Shared climb/flat predicates so the empty-state hint counter and the main
# pipeline agree on what "climb required" / "no climbs" mean. Previously these
# were inline and drifted by ~60 routes. Changes must land here, not in two
# places.
_CLIMB_CATEGORIES = {"cat1", "cat2", "cat3", "cat4", "hc"}


def _is_climb_route(r: dict) -> bool:
    """True if the route has real climbing content (not just a single kicker).

    Canonical predicate used by both the main pipeline and the empty-state
    "would_match" counter. Categories are compared case-insensitively so
    upstream generators emitting "HC" vs "hc" both register as climbs.
    """
    cat = (r.get("category") or "").lower()
    if cat in _CLIMB_CATEGORIES:
        return True
    if (r.get("climb_count") or 0) >= 1 and (r.get("max_grade") or 0) >= 4.0:
        return True
    if r.get("terrain") == "climb":
        return True
    return False


def _is_flat_route(r: dict) -> bool:
    """True if the route is flat enough for a "no climbs today" request.
    Loosened from max_grade<5 to max_grade<8 so that flat gravel/cobble
    sportives (which carry short kickers ≥5%) can still satisfy a flat query."""
    if r.get("terrain") == "climb":
        return False
    if (r.get("max_grade") or 0) >= 8.0:
        return False
    return True


def _route_summary(r: dict) -> dict:
    """Project a route entry to the lightweight list shape (no crs_path)."""
    # Prefer the stored value when present — the prior version unconditionally
    # recomputed at 23 km/h (Z2 average), which overrode any hand-tuned
    # duration baked into routes.json. Only fall back to the flat-speed
    # estimator when the stored field is missing/invalid.
    stored = r.get("est_duration_min_z2")
    km = r.get("distance_km") or 0
    if isinstance(stored, (int, float)) and stored and stored > 0:
        est_duration_min_z2 = int(round(stored))
    elif km and km > 0:
        est_duration_min_z2 = int(round(km / 23.0 * 60))
    else:
        est_duration_min_z2 = stored
    return {
        "id": r.get("id"),
        "name": r.get("name"),
        "region": r.get("region"),
        "source": r.get("source"),
        "distance_km": r.get("distance_km"),
        "climb_m": r.get("climb_m"),
        "max_grade": r.get("max_grade"),
        "avg_grade_signed": r.get("avg_grade_signed"),
        "difficulty_score": r.get("difficulty_score"),
        "category": r.get("category"),
        "terrain": r.get("terrain"),
        "finish_type": r.get("finish_type"),
        "primary_surface": r.get("primary_surface"),
        "has_gravel": r.get("has_gravel"),
        "has_cobble": r.get("has_cobble"),
        "loop": r.get("loop"),
        "lap_route": r.get("lap_route"),
        "est_duration_min_z2": est_duration_min_z2,
        "est_tss": r.get("est_tss"),
        "tags": r.get("tags", []),
        "preview_profile": r.get("preview_profile", []),
        "climb_count": r.get("climb_count", 0),
        "surface_mix_pct": r.get("surface_mix_pct") or {},
        "surface_segments": r.get("surface_segments") or [],
        "primary_climb": r.get("primary_climb"),
    }


def _parse_csv_param(val) -> list[str]:
    """Parse a comma-separated query param into a list of lowercase tokens."""
    if not val:
        return []
    if isinstance(val, list):
        raw = ",".join(val)
    else:
        raw = str(val)
    return [t.strip().lower() for t in raw.split(",") if t.strip()]


def _apply_route_filters(
    routes: list[dict],
    *,
    source: str | None = None,
    regions: list[str] | None = None,
    surfaces: list[str] | None = None,
    terrains: list[str] | None = None,
    categories: list[str] | None = None,
    finishes: list[str] | None = None,
    min_km: float | None = None,
    max_km: float | None = None,
    loop_only: bool = False,
    lap_only: bool = False,
    max_difficulty: float | None = None,
    search: str | None = None,
) -> list[dict]:
    """Apply all filter flags; returns the filtered list (order preserved)."""
    # Derive the real-world region set from the loaded routes so new regions
    # (like Flanders) auto-register without needing a hardcoded list update.
    real_world_regions = {r.get("region") for r in routes
                          if r.get("source") == "real_world" and r.get("region")}
    # Accept BOTH the UI-facing token "__real_world__" (returned by
    # /api/routes/regions.real_world_meta.region) and the short "real_world"
    # alias for robustness.
    _REAL_WORLD_TOKENS = {"real_world", "__real_world__"}
    region_set: set[str] | None = None
    if regions:
        expanded: set[str] = set()
        for reg in regions:
            if reg in _REAL_WORLD_TOKENS:
                expanded |= real_world_regions
            else:
                expanded.add(reg)
        region_set = expanded

    surface_set = set(surfaces) if surfaces else None
    terrain_set = set(terrains) if terrains else None
    category_set = set(categories) if categories else None
    finish_set = set(finishes) if finishes else None
    search_lc = search.lower() if search else None

    out = []
    for r in routes:
        if source and source != "any":
            if r.get("source") != source:
                continue
        if region_set is not None and r.get("region") not in region_set:
            continue
        if surface_set is not None:
            ps = (r.get("primary_surface") or "").lower()
            has_g = r.get("has_gravel")
            has_c = r.get("has_cobble")
            # A route matches if its primary_surface is in the filter set OR
            # if it has gravel/cobble sectors for those tokens respectively.
            match = ps in surface_set
            if not match and "gravel" in surface_set and has_g:
                match = True
            if not match and "cobble" in surface_set and has_c:
                match = True
            if not match:
                continue
        if terrain_set is not None and (r.get("terrain") or "").lower() not in terrain_set:
            continue
        if category_set is not None and (r.get("category") or "").lower() not in category_set:
            continue
        if finish_set is not None and (r.get("finish_type") or "").lower() not in finish_set:
            continue
        if min_km is not None and (r.get("distance_km") or 0) < min_km:
            continue
        if max_km is not None and (r.get("distance_km") or 0) > max_km:
            continue
        if loop_only and not r.get("loop"):
            continue
        if lap_only and not r.get("lap_route"):
            continue
        if max_difficulty is not None and (r.get("difficulty_score") or 0) > max_difficulty:
            continue
        if search_lc and search_lc not in (r.get("name") or "").lower():
            continue
        out.append(r)
    return out


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


_CAT_WORDS = {
    "cat5": "gentle territory",
    "cat4": "hilly territory",
    "cat3": "serious climbing",
    "cat2": "big climb day",
    "cat1": "brutal territory",
    "hc": "epic mountain day",
}


def _score_route_for_suggest(
    r: dict,
    *,
    d_mid: float,
    d_half: float,
    target_diff_mid: float,
    diff_max: float | None,
    surface_filter: set[str] | None,
    finish_filter: set[str] | None,
) -> tuple[float, str]:
    """Compute match score (0..1) and a short conversational rationale.

    Improvements vs the naive v1 (per grill B):
    - Distance decay is quadratic with a 2.5 km floor on d_half so narrow
      bands still produce a usable ranking curve instead of a cliff.
    - Difficulty is rescaled against the user's cap (diff_max) and treated
      as "no preference" when cap is ≥9 so climbs and flats both surface.
    - Rationale is conversational ("Great fit …") with climb count + grade
      hints so cards feel less robotic.
    """
    actual_km = r.get("distance_km") or 0

    # --- Distance: quadratic decay, floored AND capped half-width.
    # Floor (2.5) prevents cliff on narrow bands; cap (30) prevents wide
    # bands like 10-200 km from scoring everything ≈1.0.
    d_half_eff = max(d_half, 2.5)
    d_half_eff = min(d_half_eff, 30.0)
    delta = abs(actual_km - d_mid)
    if delta >= d_half_eff * 2.0:
        distance_fit = 0.0
    else:
        # Quadratic: 1 - (delta/half)^2, so falloff is gentler near target,
        # steeper at edges. Hits 0 at 2*half (well outside the hard filter).
        t = delta / d_half_eff
        distance_fit = max(0.0, 1.0 - t * t)

    # --- Difficulty: rescaled against user cap, smooth fade toward 1.0
    # as diff_max approaches 10 ("no preference"). Avoids the hard cliff
    # that used to flip the whole ranking at diff_max=9.0.
    diff_score = r.get("difficulty_score") or 0
    if diff_max is None:
        difficulty_fit = 1.0
    else:
        band = max(1.0, float(diff_max))
        raw = max(0.0, min(1.0, 1.0 - abs(diff_score - target_diff_mid) / band))
        fade = max(0.0, min(1.0, (diff_max - 7.0) / 3.0))  # 7→0, 10→1
        difficulty_fit = raw + (1.0 - raw) * fade

    ps = (r.get("primary_surface") or "").lower()
    if surface_filter is None:
        surface_match = 1.0
    else:
        # Match if primary surface is selected, OR the route carries a
        # has_X flag for a selected surface, OR the surface mix has ≥30%
        # in any selected surface. (Previously only primary_surface was
        # checked, so a 70%-gravel asphalt-primary route scored 0.5 even
        # with surface=["gravel"].)
        mix = r.get("surface_mix_pct") or {}
        in_primary = ps in surface_filter
        in_flag = (
            ("gravel" in surface_filter and r.get("has_gravel")) or
            ("cobble" in surface_filter and r.get("has_cobble"))
        )
        in_mix = any((mix.get(s) or 0) >= 30 for s in surface_filter)
        # Pure surface match (primary=X when user picked X) outranks a mixed
        # match by 0.15 so gravel-primary routes top "has_gravel + asphalt-
        # primary" mixes on a gravel query. Below that, in_flag/in_mix still
        # rank above outright non-matches.
        if in_primary:
            surface_match = 1.0
        elif in_flag or in_mix:
            surface_match = 0.85
        else:
            surface_match = 0.4

    ft = (r.get("finish_type") or "").lower()
    if finish_filter is None:
        finish_match = 1.0
    else:
        finish_match = 1.0 if ft in finish_filter else 0.5

    score = (
        0.50 * distance_fit
        + 0.25 * difficulty_fit
        + 0.10 * surface_match
        + 0.15 * finish_match
    )

    # --- Rationale: conversational + differential
    km_txt = f"{round(actual_km)} km"
    climb_m = int(r.get("climb_m") or 0)
    climb_count = int(r.get("climb_count") or 0)
    max_grade = float(r.get("max_grade") or 0)
    cat = (r.get("category") or "").lower()
    pieces = []
    # Distance framing
    if delta <= 2.0:
        pieces.append(f"spot on your {km_txt} target")
    elif delta <= d_half_eff:
        pieces.append(f"close to your target ({km_txt})")
    else:
        pieces.append(f"{km_txt} ride")
    # Climb framing
    if climb_count >= 3 and max_grade >= 8:
        pieces.append(f"{climb_count} real climbs, max {max_grade:.0f}%")
    elif climb_m >= 300:
        pieces.append(f"{climb_m} m of climbing")
    elif cat in ("flat",) and climb_m < 150:
        pieces.append("mostly flat")
    elif cat and cat != "flat":
        # Map internal category tokens to human words so users don't see
        # "cat5 territory" on cards.
        pieces.append(_CAT_WORDS.get(cat, f"{cat} territory"))
    # Surface framing (only call out when user selected it)
    if surface_filter and ps in surface_filter:
        pieces.append(f"{ps} as requested")
    elif ps in ("gravel", "cobble"):
        pieces.append(f"{ps} surface")
    # Combine — keep it under ~14 words
    sentence = "Good fit: " + ", ".join(pieces[:3]) + "."
    # Use the SAME recomputed Z2 pace as the summary (23 km/h) so the card
    # and the rationale agree. Previously the rationale quoted the stored
    # field, which diverged from the summary's recomputed value for ~60% of
    # routes and caused "64 min" on the card next to "~81 min at Z2" in the
    # sentence. We derive here from distance rather than reading the summary
    # because the scorer predates the projection step.
    if actual_km > 0:
        recomputed_min = int(round(actual_km / 23.0 * 60))
        diff_tail = f" {diff_score:.1f}/10 difficulty · ~{recomputed_min} min at Z2."
    else:
        diff_tail = f" {diff_score:.1f}/10 difficulty."
    return round(score, 3), sentence + diff_tail


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


def _top_archetypes_for_region(routes_in_region: list[dict], n: int = 5) -> list[str]:
    """Return up to N representative tags for a region (most common first)."""
    from collections import Counter
    tag_counts: Counter[str] = Counter()
    for r in routes_in_region:
        for t in r.get("tags", []) or []:
            tag_counts[t] += 1
    return [t for t, _ in tag_counts.most_common(n)]


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
        shaped.append({
            **r,
            "world": REGION_TITLES.get(reg, reg),
            "world_slug": reg,
            "slug": (r.get("id", "").split("/", 1)[1] if "/" in r.get("id", "") else r.get("id", "")),
            "url": r.get("id", ""),
            "km": r.get("distance_km", 0),
            "climb": r.get("climb_m", 0),
            "avg_grad": r.get("avg_grade_signed", 0),
            "difficulty": str(r.get("difficulty_score", "")),
        })
    return {"worlds": world_names, "routes": shaped, "total": len(shaped)}


def _route_profile_points(lat_lon_grade: list) -> list[dict]:
    """Convert [[lat,lon,grade],...] to [{d, e, g},...] for elevProfile rendering."""
    from geodesy import haversine
    if not lat_lon_grade or len(lat_lon_grade) < 2:
        return []

    points = []
    cum_dist_km = 0.0
    cum_ele = 0.0
    for i, pt in enumerate(lat_lon_grade):
        if len(pt) < 3:
            continue
        lat, lon, grade = pt[0], pt[1], pt[2]
        grade = max(-45, min(45, grade))  # cap to realistic range
        if i > 0:
            prev = lat_lon_grade[i - 1]
            d_m = haversine((prev[0], prev[1]), (lat, lon))  # metres
            d_km = d_m / 1000.0
            cum_dist_km += d_km
            cum_ele += d_m * grade / 100.0  # elevation change in metres
        points.append({"d": round(cum_dist_km, 3), "e": round(cum_ele, 1), "g": round(grade, 1)})

    # Downsample to max 200 points
    if len(points) > 200:
        step = max(1, len(points) // 200)
        sampled = [points[i] for i in range(0, len(points), step)]
        if sampled[-1] != points[-1]:
            sampled.append(points[-1])
        return sampled
    return points


def _load_route_index() -> dict:
    """Load compact pre-indexed virtual route profiles (~1MB, loaded once)."""
    return cached("route_index", lambda: (
        json.loads(ROUTE_PROFILES_INDEX.read_text(encoding="utf-8"))
        if ROUTE_PROFILES_INDEX.exists() else {}
    ), ttl=3600)


def _load_route_detail(url: str) -> dict | None:
    """Load full route data from individual file (for detail modal).

    Profile files are named ``<world>__<slug>.json`` (see
    ``generate_route_profiles.py``). The frontend may pass several URL shapes,
    so we try them all:

    * ``<world>/<slug>``                      (new canonical form)
    * ``virtual/<world>/<file>.crs``          (CRS path for virtual worlds)
    * ``<region>/<File Name>.crs``            (CRS path for real-world rides)
    * ``/climb-portal/<slug>`` or ``/route/<world>/<slug>``  (legacy)
    * bare ``<slug>``                         (last resort)
    """
    if not url:
        return None
    # Strip query strings / fragments and sanitise
    url = url.split("?", 1)[0].split("#", 1)[0].strip()
    clean = url.replace("..", "").replace("\\", "/").strip("/")
    parts = [p for p in clean.split("/") if p]
    if not parts:
        return None

    last = parts[-1]
    # Strip file extension, if any
    last_stem = last.rsplit(".", 1)[0] if "." in last else last

    # Build candidate (world, slug) pairs to try, in priority order.
    candidates: list[tuple[str, str]] = []

    # Legacy: "climb-portal/<slug>" anywhere in path
    if "climb-portal" in parts:
        candidates.append(("climb-portal", last_stem))

    # Virtual routes: "virtual/<world>/<world>__<slug>.crs"
    if len(parts) >= 3 and parts[0] == "virtual":
        world = parts[1]
        slug = last_stem.split("__", 1)[1] if "__" in last_stem else _slugify_route_name(last_stem)
        candidates.append((world, slug))

    # Legacy "/route/<world>/<slug>"
    if "route" in parts:
        try:
            idx = parts.index("route")
            if idx + 2 < len(parts):
                candidates.append((parts[idx + 1], parts[idx + 2]))
        except ValueError:
            pass

    # New canonical form: "<world>/<slug>" (exact match to index keys)
    if len(parts) == 2:
        candidates.append((parts[0], parts[1]))

    # CRS path for real-world rides: "<region>/<File Name>.crs"
    if len(parts) >= 2:
        world = parts[-2]
        # When the filename embeds "<world>__<slug>" use that slug directly,
        # otherwise slugify the whole stem (matches generate_route_profiles.py).
        if "__" in last_stem:
            slug = last_stem.split("__", 1)[1]
        else:
            slug = _slugify_route_name(last_stem)
        candidates.append((world, slug))

    # Bare slug (no world): fall back to any file whose stem contains the slug.
    # Handled after the direct lookups below.

    seen: set[tuple[str, str]] = set()
    for world, slug in candidates:
        if not world or not slug:
            continue
        # Sanitise — files are on disk so block traversal
        world_safe = re.sub(r"[^A-Za-z0-9_\-]", "", world)
        slug_safe = re.sub(r"[^A-Za-z0-9_\-]", "", slug)
        key = (world_safe, slug_safe)
        if key in seen:
            continue
        seen.add(key)
        path = ROUTE_PROFILES_DIR / f"{world_safe}__{slug_safe}.json"
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass

    # Fallback: scan for any profile whose filename contains the last slug.
    # This handles ambiguous inputs like a bare slug with no world prefix.
    if last_stem and ROUTE_PROFILES_DIR.exists():
        # If the bare stem is itself "<world>__<slug>", split on the first
        # "__" and try that pair directly.
        if "__" in last_stem:
            world_guess, slug_guess = last_stem.split("__", 1)
            world_safe = re.sub(r"[^A-Za-z0-9_\-]", "", world_guess).replace("-", "_")
            slug_safe = re.sub(r"[^A-Za-z0-9_\-]", "", slug_guess)
            path = ROUTE_PROFILES_DIR / f"{world_safe}__{slug_safe}.json"
            if path.exists():
                try:
                    return json.loads(path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    pass
        fallback_slug = _slugify_route_name(last_stem)
        if fallback_slug:
            needle = f"__{fallback_slug}.json"
            for candidate in ROUTE_PROFILES_DIR.glob(f"*{needle}"):
                try:
                    return json.loads(candidate.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
    return None


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


def _gradient_to_power_factor(grade_pct: float) -> float:
    """Map a terrain gradient (%) to a fraction of FTP for climb-ZWO generation.

    Single source of truth — previously duplicated byte-for-byte between the CRS
    and virtual-route climb ZWO builders.
    """
    if grade_pct >= 10: return 0.95
    if grade_pct >= 7:  return 0.90
    if grade_pct >= 5:  return 0.85
    if grade_pct >= 3:  return 0.75
    if grade_pct >= 1:  return 0.68
    if grade_pct >= -2: return 0.60
    return 0.50


def _build_climb_zwo(points: list[dict], course_name: str, warmup_min: int = 10) -> str:
    """Generate ZWO XML string from profile points. Shared by CRS and virtual routes."""
    ftp = config.ATHLETE_FTP_W
    total_dist = points[-1]["d"] if points else 0
    total_climb = sum(max(0, points[i]["e"] - points[i-1]["e"]) for i in range(1, len(points)))

    segments_xml = ""
    if warmup_min > 0:
        segments_xml += f'    <Warmup Duration="{warmup_min * 60}" PowerLow="0.45" PowerHigh="0.65"/>\n'

    num_segs = min(40, max(10, len(points) // 5))
    seg_step = max(1, len(points) // num_segs)

    for i in range(0, len(points) - seg_step, seg_step):
        j = min(i + seg_step, len(points) - 1)
        dist_km = points[j]["d"] - points[i]["d"]
        if dist_km <= 0:
            continue
        avg_grad = sum(points[k]["g"] for k in range(i, j + 1)) / (j - i + 1)

        power_pct = _gradient_to_power_factor(avg_grad)

        if avg_grad >= 5:    speed_kmh = max(8, 20 - avg_grad * 1.2)
        elif avg_grad >= 0:  speed_kmh = 25
        else:                speed_kmh = min(45, 25 - avg_grad * 2)

        duration_sec = max(30, int(dist_km / speed_kmh * 3600))
        segments_xml += f'    <SteadyState Duration="{duration_sec}" Power="{power_pct:.2f}"/>\n'

    segments_xml += '    <Cooldown Duration="300" PowerLow="0.40" PowerHigh="0.60"/>\n'

    from xml.sax.saxutils import escape as xml_escape
    desc = f"Climb simulation: {course_name}. {total_dist:.1f}km, {total_climb:.0f}m elevation."
    if warmup_min > 0:
        desc = f"{warmup_min}min warmup + {desc}"

    return f"""<?xml version='1.0' encoding='utf-8'?>
<workout_file>
  <author>Domestique</author>
  <name>{xml_escape(course_name)}</name>
  <description>{xml_escape(desc)}</description>
  <sportType>bike</sportType>
  <workout>
{segments_xml}  </workout>
</workout_file>"""


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

def _safe_path(base: Path, *parts: str) -> Path | None:
    """Resolve path and verify it's inside the base directory (prevent traversal + symlink escape)."""
    try:
        path = base.joinpath(*parts).resolve()
        base_resolved = base.resolve()
        # Use is_relative_to (Python 3.9+) for robust check
        if hasattr(path, 'is_relative_to'):
            if not path.is_relative_to(base_resolved):
                return None
        else:
            # Fallback: string prefix with trailing separator
            if not (str(path) + "/").startswith(str(base_resolved) + "/"):
                return None
        return path
    except (ValueError, OSError):
        return None


def _wrap_zwo_outdoor(xml_text: str, transit_min: int, spin_min: int) -> str:
    """G1 (v2.1) — frame a prescribed indoor block inside a real OUTDOOR ride:
    prepend a flat transit warmup and append a spin-home cooldown. The prescribed
    body is passed through UNCHANGED (string-level insert, not re-serialized, so
    the middle is byte-identical). Export-only: this is the download path, so it
    never touches the planner's accounted/weekly TSS — the transit + spin-home are
    OFF-PLAN additional easy minutes by construction."""
    t_s = max(0, int(transit_min)) * 60
    s_s = max(0, int(spin_min)) * 60
    out = xml_text
    if t_s and "<workout>" in out:
        out = out.replace(
            "<workout>",
            f'<workout>\n        <Warmup Duration="{t_s}" PowerLow="0.40" '
            f'PowerHigh="0.60"/>  <!-- G1 transit to climb (off-plan) -->', 1)
    if s_s and "</workout>" in out:
        out = out.replace(
            "</workout>",
            f'        <Cooldown Duration="{s_s}" PowerLow="0.50" PowerHigh="0.40"/>'
            f'  <!-- G1 spin home (off-plan) -->\n    </workout>', 1)
    return out


def _zwo_download_response(path, filename: str, outdoor: int,
                          transit_min: int, spin_min: int):
    """Shared ZWO download: plain FileResponse, or (G1) an outdoor-wrapped copy."""
    plain = FileResponse(
        path, filename=filename, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'})
    if not outdoor:
        return plain
    try:
        wrapped = _wrap_zwo_outdoor(path.read_text(encoding="utf-8"),
                                    transit_min, spin_min)
    except Exception:
        return plain
    out_name = filename.rsplit(".", 1)[0] + "_outdoor.zwo"
    return Response(
        wrapped, media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{out_name}"'})


@app.get("/api/download/zwo/{filename}")
def download_zwo_flat(filename: str, outdoor: int = Query(0),
                      transit_min: int = Query(10), spin_min: int = Query(20)):
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
    """
    path = _safe_path(WORKOUT_DIR, filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    return _zwo_download_response(path, filename, outdoor, transit_min, spin_min)


@app.get("/api/download/zwo/{category}/{filename}")
def download_zwo(category: str, filename: str, outdoor: int = Query(0),
                 transit_min: int = Query(10), spin_min: int = Query(20)):
    """v1.6.4: see download_zwo_flat docstring for media-type + Disposition change.
    G1 (v2.1): ``outdoor=1`` adds an off-plan transit warmup + spin-home cooldown."""
    # Flat layout first, legacy category/file fallback
    path = _safe_path(WORKOUT_DIR, filename)
    if not path or not path.exists():
        path = _safe_path(WORKOUT_DIR, category, filename)
    if not path or not path.exists():
        return JSONResponse({"error": "not found"}, 404)
    return _zwo_download_response(path, filename, outdoor, transit_min, spin_min)


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
    segments_xml += '    <Cooldown Duration="300" PowerLow="0.40" PowerHigh="0.60"/>\n'

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
    path = _safe_path(GPX_DIR, region, filename)
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
    log_path = Path.home() / ".domestique" / "logs" / "domestique.log"
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
        "version": _VERSION,
        "python": platform.python_version(),
        "frozen": getattr(sys, "frozen", False),
        "data_dir": str(Path.home() / ".domestique"),
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
    Anything else (Linux, BSD) → no asset; banner falls back to release_url.
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
        body["cache_written_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
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


@app.get("/api/update/check")
def api_update_check():
    """Live GitHub-Releases poll with 6h cache + platform-specific asset.

    Always returns 200; on upstream failure, populates `error` and returns
    the last-good cache so the dashboard banner can still render.
    """
    import httpx
    from packaging.version import parse as _vparse, InvalidVersion

    plat = sys.platform
    now = datetime.now(timezone.utc)
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

    if _cache_is_fresh(cache, now):
        out = {k: cache.get(k) for k in (
            "current", "latest", "update_available", "release_url",
            "download_url", "asset_name", "platform", "checked_at", "error",
            "release_body",
        )}
        out["cached"] = True
        return _overlay_live_current(out)

    try:
        resp = httpx.get(_GITHUB_RELEASES_LATEST_URL, timeout=10)
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
    h_zones = _hr_zones(pm.lthr, pm.max_hr)

    return {
        "weight": config.ATHLETE_WEIGHT_KG,
        "lbm": config.ATHLETE_LBM_KG,
        "ftp": config.ATHLETE_FTP_W,
        "eftp": eftp,
        "lthr": config.ATHLETE_LTHR,
        "max_hr": config.ATHLETE_MAX_HR,
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
        pm.save_athlete(athlete_updates)
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


def _power_zones(ftp):
    # Delegates to zones.power_zones (Coggan/Allen 7-zone model).
    # Adapts Zone(low, high, name) → dict shape required by clients
    # (dashboard.html / training.html expect zone, name, low, high).
    return [
        {"zone": f"Z{i + 1}", "name": z.name, "low": z.low, "high": z.high}
        for i, z in enumerate(_zones_mod.power_zones(ftp))
    ]

def _hr_zones(lthr, max_hr):
    # Delegates to zones.hr_zones (LTHR-anchored hybrid model).
    # Adapts Zone(low, high, name) → dict shape required by clients.
    return [
        {"zone": f"Z{i + 1}", "name": z.name, "low": z.low, "high": z.high}
        for i, z in enumerate(_zones_mod.hr_zones(lthr, max_hr))
    ]


# ═══════════════════════════════════════════════════════════════════════════════
# SYNC APIs
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/sync")
def api_sync():
    """Manually trigger a sync from Intervals.icu."""
    result = db.run_sync(days=90)
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
    dt = body.get("date", date.today().isoformat())
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
    dt = body.get("date", date.today().isoformat())
    try:
        entry = db.upsert_daily_log(
            dt=dt,
            sleep_quality=int(body.get("sleep_quality", 4)),
            fatigue=int(body.get("fatigue", 4)),
            soreness=int(body.get("soreness", 4)),
            stress=int(body.get("stress", 4)),
            mood=int(body.get("mood", 4)),
            notes=body.get("notes"),
        )
    except (ValueError, TypeError) as e:
        return JSONResponse({"error": str(e)}, 400)
    return {"ok": True, "entry": entry}


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
    dt = body.get("date", date.today().isoformat())
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
    today = date.today()
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
    path = _safe_path(GPX_DIR, region, gpx_name)
    if not path or not path.exists():
        path = _safe_path(GPX_DIR, region, filename)
    if (not path or not path.exists()) and (GPX_DIR / region).is_dir():
        # Try fuzzy match — find GPX with same stem prefix
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        for gpx_file in (GPX_DIR / region).glob("*.gpx"):
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

@app.get("/api/weekly-plan")
def api_weekly_plan(week_offset: int = Query(0)):
    """Generate or retrieve weekly mesocycle plan.
    week_offset=0 → current week, -1 → last week, 1 → next week.
    """

    training = cached("training", get_today_metrics)
    current_ctl = training.get("ctl") or 30

    # Check for active plan to get current phase
    current_phase = None
    json_path = _plan_dir() / "current_plan.json"
    if json_path.exists():
        try:
            with open(json_path, encoding="utf-8") as f:
                plan = json.load(f)
            today = date.today() + timedelta(weeks=week_offset)
            today_str = today.isoformat()
            for p in plan.get("phases", []):
                if p.get("start", "") <= today_str <= p.get("end", ""):
                    current_phase = tp.Phase(
                        name=p["name"], start=date.fromisoformat(p["start"]),
                        end=date.fromisoformat(p["end"]), weeks=p.get("weeks", 1),
                        focus=p.get("focus", ""), weekly_tss_target=p.get("weekly_tss", current_ctl * 7),
                        z2_pct=70, hit_per_week=p.get("hit_per_week", 2),
                        session_types=p.get("session_types", ["z2", "threshold", "vo2max", "sweetspot", "overunder", "tempo", "sprint"]),
                    )
                    break
        except (json.JSONDecodeError, OSError, KeyError):
            pass

    # Load goal from current_plan.json if available
    goal = None
    try:
        if json_path.exists():
            with open(json_path, encoding="utf-8") as f:
                plan_data = json.load(f)
            g = plan_data.get("goal", {})
            # P5 (v4.1.0): restore available_days + daily_max_hours from the
            # persisted plan if present. Fall back to [0..6]-minus-rest_days
            # when missing (pre-v4.1 plans) to avoid the Mon-drop bug where
            # the old default [1..6] silently turned Monday into a rest day
            # that wasn't in the user's rest_days list.
            rest_days_val = g.get("rest_days", [0])
            raw_available = g.get("available_days")
            if raw_available is not None:
                available_days_val = list(raw_available)
            else:
                available_days_val = [d for d in range(7) if d not in rest_days_val]
            raw_daily = g.get("daily_max_hours") or {}
            daily_max_val = {}
            for k, v in raw_daily.items():
                try:
                    daily_max_val[int(k)] = float(v)
                except (TypeError, ValueError):
                    continue
            goal = tp.Goal(
                goal_type=g.get("type", g.get("goal_type", "general")),  # JSON saves as "type"
                hours_per_week=g.get("hours_per_week", 8.0),
                max_weekday_hours=g.get("max_weekday_hours", 2.0),
                max_weekend_hours=g.get("max_weekend_hours", 3.5),
                rest_days=rest_days_val,
                available_days=available_days_val,
                daily_max_hours=daily_max_val,
                plan_weeks=g.get("plan_weeks", 0),
                longest_ride_h_90d=g.get("longest_ride_h_90d"),
                last_ftp_test_date=g.get("last_ftp_test_date"),
            )
        else:
            goal = tp.Goal(goal_type="general", hours_per_week=8.0)
    except Exception:
        goal = tp.Goal(goal_type="general", hours_per_week=8.0)
    # v4.6.7 IMPL-CAP: auto-populate endurance baseline if missing.
    if goal.longest_ride_h_90d is None:
        goal.longest_ride_h_90d = _longest_ride_h_90d()

    # P1 (v4.1.0): feed cross-week used_names from the persisted plan so
    # /api/weekly-plan picks up the same 6-week sliding-window dedupe that
    # generate_plan uses. Without this, the simple weekly planner gets a
    # fresh empty set on every request → the UI weekly card was handing
    # the user the same threshold workout week after week.
    cross_week_used_names: set[str] = set()
    try:
        if json_path.exists():
            with open(json_path, encoding="utf-8") as f:
                _persist = json.load(f)
            today = date.today() + timedelta(weeks=week_offset)
            today_str = today.isoformat()
            # Window: sessions from the last 6 weeks of the stored plan
            # (mirrors generate_plan's sliding-window ≥6 stale threshold).
            window_start = (today - timedelta(weeks=6)).isoformat()
            for w_json in _persist.get("weeks", []):
                if w_json.get("end", "") < window_start:
                    continue
                if w_json.get("start", "") > today_str:
                    continue
                for s_json in w_json.get("sessions", []):
                    nm = s_json.get("zwo_name") or ""
                    if nm:
                        cross_week_used_names.add(nm)
    except Exception as _e:
        _log.debug(f"weekly-plan used_names rollup failed: {_e}")

    week = tp.generate_weekly_plan(
        goal=goal, current_phase=current_phase,
        current_ctl=current_ctl,
        used_names=cross_week_used_names,
    )

    # Match ZWO files for every non-rest session
    try:
        library = tp.load_workout_library()
        for i, s in enumerate(week.sessions):
            if s.session_type == "rest" or getattr(s, "zwo_file", ""):
                continue
            try:
                tp.match_zwo(
                    s, library,
                    week_num=week.week_num, day_idx=i,
                    used_names=cross_week_used_names,
                )
            except Exception:
                pass
    except Exception:
        pass

    # fix26 §6.4/§6.8/§6.12 — merge status fields from stored current_plan.json.
    # /api/weekly-plan regenerates the week on-the-fly from the goal/phase, but
    # user-moved slots / done statuses / dismissed flags live in the stored
    # plan JSON. Without this merge the UI would show the regen'd week without
    # any of the user's persisted edits and the drag-to-move would appear to
    # have no effect.
    stored_by_day: dict[str, dict] = {}
    try:
        json_path_sess = _plan_dir() / "current_plan.json"
        if json_path_sess.exists():
            with open(json_path_sess, encoding="utf-8") as f:
                stored_plan = json.load(f)
            # P4 (v4.1.0) — merge by DATE OVERLAP (not just ISO-week match).
            # The stored plan's weeks may start on Sat (legacy) or Mon (new
            # plans). Our weekly-plan reply is always Mon–Sun. Matching on
            # ISO-week alone silently drops 5 of 7 days of merge coverage
            # when stored weeks started on Sat — because week 1 Sat is in
            # ISO week N, but week 2 Mon–Sun is in ISO week N+1. Iterate
            # EVERY stored session and key by day ISO directly — that guarantees
            # the user_moved/done/dismissed fields round-trip regardless of
            # the stored-week boundary convention.
            week_start_iso = week.start.isoformat()
            week_end_iso = week.end.isoformat()
            for w_json in stored_plan.get("weeks", []):
                for s_json in w_json.get("sessions", []):
                    day_iso = s_json.get("day", "")
                    if not day_iso:
                        continue
                    if week_start_iso <= day_iso <= week_end_iso:
                        stored_by_day[day_iso] = s_json
    except Exception as _e:
        _log.debug(f"weekly-plan stored merge failed: {_e}")

    # v4.1.1 FIX-PLANNER B: build a ZWO→metadata index once so every session
    # can surface zone_dist + score without re-parsing the library.
    _lib_by_file: dict[str, dict] = {}
    try:
        for _w in tp.load_workout_library():
            _fname = _w.get("File")
            if _fname:
                _lib_by_file[_fname] = _w
    except Exception:
        _lib_by_file = {}

    # v1.0.4 IMPL-WIRING: load content classifications once so every session
    # can surface display_name (Layer 3) without re-reading the JSON.
    try:
        _classifications = tp._load_content_classifications() or {}
    except Exception:
        _classifications = {}

    def _session_out(s):
        day_iso = s.day.isoformat()
        stored = stored_by_day.get(day_iso) or {}
        zwo_file = stored.get("zwo_file") or getattr(s, "zwo_file", "")
        # v1.0.4 IMPL-WIRING — resolve canonical title + actual library duration.
        display_name, zwo_duration_min = _session_naming_lookup(
            zwo_file, _classifications, _lib_by_file,
        )
        out = {
            "day": day_iso,
            "day_name": s.day_name,
            "session_type": stored.get("session_type") or s.session_type,
            "duration_min": stored.get("duration_min", s.duration_min),
            "tss_estimate": stored.get("tss_estimate", s.tss_estimate),
            "description": stored.get("description") or s.description,
            "zwo_file": zwo_file,
            "zwo_name": stored.get("zwo_name") or getattr(s, "zwo_name", ""),
            "display_name": display_name,
            "zwo_duration_min": zwo_duration_min,
            # fix26 §6 — status + move + completion round-trips
            "status": stored.get("status", "pending"),
            "user_moved": stored.get("user_moved", False),
            "moved_from": stored.get("moved_from", ""),
            "completion_matches": stored.get("completion_matches") or None,
            "dismissed_at": stored.get("dismissed_at", ""),
        }
        # v4.1.1 FIX-PLANNER B: per-session zone_dist from the ACTUAL ZWO.
        meta = _lib_by_file.get(zwo_file) if zwo_file else None
        if meta:
            out["zone_dist"] = {
                "z1": meta.get("Z1%", 0), "z2": meta.get("Z2%", 0),
                "z3": meta.get("Z3%", 0), "z4": meta.get("Z4%", 0),
                "z5": meta.get("Z5%", 0), "z6": meta.get("Z6%", 0),
            }
            out["score"] = meta.get("Score")
            out["protocol"] = meta.get("Protocol")
        else:
            out["zone_dist"] = None
            out["score"] = None
        return out

    result = {
        "week_num": week.week_num,
        "start": week.start.isoformat(),
        "end": week.end.isoformat(),
        "phase": week.phase,
        "tss_target": week.tss_target,
        "is_stepback": week.is_stepback,
        "sessions": [_session_out(s) for s in week.sessions],
    }

    # Add event readiness + eFTP drift if plan exists
    try:
        json_path = _plan_dir() / "current_plan.json"
        if json_path.exists():
            with open(json_path, encoding="utf-8") as f:
                plan = json.load(f)
            g = plan.get("goal", {})
            if g.get("event_date"):
                plan_goal = tp.Goal(
                    goal_type=g.get("type", "general"),
                    target_date=date.fromisoformat(g["event_date"]),
                    event_name=g.get("event_name", ""),
                    event_km=g.get("event_km", 0),
                    event_climb_m=g.get("event_climb", 0),
                    event_type=g.get("event_type", "granfondo"),
                    hours_per_week=g.get("hours_per_week", 8),
                    longest_ride_h_90d=g.get("longest_ride_h_90d"),
                    last_ftp_test_date=g.get("last_ftp_test_date"),
                )
                # v4.6.7 IMPL-CAP: auto-populate endurance baseline if missing.
                if plan_goal.longest_ride_h_90d is None:
                    plan_goal.longest_ride_h_90d = _longest_ride_h_90d()
                result["event_readiness"] = tp.compute_event_readiness(plan_goal, current_ctl)

        # eFTP drift detection (+ F5 auto-apply after 7+ days)
        wellness = cached("wellness_7", lambda: fetch_wellness(7))
        # For the 7-day-streak auto-apply we actually need 14 days so a drift
        # ending yesterday doesn't fall out of the window; fetch a wider slice.
        wellness_14 = cached("wellness_14", lambda: fetch_wellness(14))
        if wellness:
            for w in reversed(wellness):
                si = w.get("sportInfo", [])
                if si and len(si) > 0 and si[0].get("eftp"):
                    eftp = round(si[0]["eftp"])
                    drift = tp._check_eftp_drift(eftp)
                    if drift:
                        result["eftp_drift"] = drift
                    break
        # F5 (v4.1.0): 7+-day sustained >3% up-drift triggers auto-apply.
        if wellness_14:
            try:
                auto = tp.check_and_auto_apply_eftp(wellness_14)
                if auto:
                    result["eftp_auto_applied"] = auto
                    clear_cache()
            except Exception as _e:
                _log.debug(f"eftp auto-apply skipped: {_e}")
    except Exception as _e:
        import logging
        logging.getLogger(__name__).warning("event_readiness failed: %s", _e)

    return result


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


_CYCLING_SPORTS = frozenset({"Ride", "VirtualRide", "EBikeRide"})


# Map planned session_type → expected exposure band (used by /api/week-summary
# to render planned-vs-actual exposure bars). Anything not in this map (e.g.
# rest) contributes no minutes to any band.
_SESSION_TYPE_TO_BAND = {
    "recovery":       "low_aerobic",
    "z2":             "low_aerobic",
    "long_z2":        "low_aerobic",
    "tempo":          "mid_aerobic",
    "sweetspot":      "high_aerobic",
    "threshold":      "high_aerobic",
    "vo2max":         "anaerobic",
    "overunder":      "anaerobic",
}


def _matches_planned(activities: list, session_type: str) -> bool:
    """A planned cycling workout is DONE only if a cycling activity was
    actually performed that day. Cross-sport doesn't count — the user still
    owes the specific session.
    """
    return any((a.get("sport") or "") in _CYCLING_SPORTS for a in activities)


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
        tz = ZoneInfo(tz_name) if tz_name else datetime.now().astimezone().tzinfo
    except Exception:
        tz = datetime.now().astimezone().tzinfo
    today = datetime.now(tz).date()
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
        tss_adherence_pct = 0

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

    for a in week_activities:
        band, signal = _classify_exposure_with_signal(a, lthr)
        dur = int(round(a.get("duration_min") or 0))
        if signal == "inferred":
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
        # Strict past: today and future days aren't done/missed yet.
        if day_d >= today:
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
        else:
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

    # Planned exposure-minutes: attribute every planned session's duration to
    # its expected band so the rollup can render a "Planned vs Actual" bar.
    # Sessions outside _SESSION_TYPE_TO_BAND (e.g. rest) contribute nothing.
    exposure_minutes_planned = {
        "low_aerobic": 0, "mid_aerobic": 0, "high_aerobic": 0, "anaerobic": 0,
    }
    for s in sessions:
        st = (s.get("session_type") or "").lower()
        band = _SESSION_TYPE_TO_BAND.get(st)
        if not band:
            continue
        dur = int(round(s.get("duration_min") or 0))
        exposure_minutes_planned[band] = exposure_minutes_planned.get(band, 0) + dur

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
    if high_zone_planned == 0 and high_zone_done >= 30:
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

        today_iso = date.today().isoformat()
        with open(json_path, encoding="utf-8") as f:
            plan = json.load(f)

        found = False
        for w in plan.get("weeks", []):
            for s in w.get("sessions", []):
                if s.get("day") == today_iso:
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

    # Get weekly plan
    week_data = api_weekly_plan(week_offset=0)
    today_str = date.today().isoformat()
    planned_data = next(
        (s for s in week_data["sessions"] if s["day"] == today_str),
        None,
    )
    if not planned_data:
        return {"planned": None, "adjusted": None, "reason": "No session planned today"}

    planned = tp.PlannedSession(
        day=date.today(), day_name=planned_data["day_name"],
        session_type=planned_data["session_type"],
        duration_min=planned_data["duration_min"],
        tss_estimate=planned_data["tss_estimate"],
        description=planned_data["description"],
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
    yesterday = (date.today() - timedelta(days=1)).isoformat()
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

    yesterday_planned = next(
        (s["tss_estimate"] for s in week_data["sessions"]
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
    _cutoff_4d = (date.today() - timedelta(days=4)).isoformat()
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
    adjusted, reason = tp.adjust_today_session(
        planned=planned, readiness=r,
        hrv_streak_below_swc=hrv_streak,
        yesterday_tss_ratio=yesterday_tss_ratio,
        rides_recent=rides_recent,
        daily_log_today=daily_log_today,
    )

    # v4.6.0 IMPL-HOMEPAGE-CONSISTENCY §3 Pillar D: surface adjustment_reason
    # explicitly so the homepage chip can render "Adjusted to {type} due to
    # {reason}" without re-parsing the legacy arrow-string. Empty when no
    # adjustment ran; otherwise mirrors `reason` (kept for back-compat).
    adjustment_reason = reason if reason else ""
    return {
        "planned": {
            "session_type": planned.session_type,
            "duration_min": planned.duration_min,
            "tss_estimate": planned.tss_estimate,
            "description": planned.description,
        },
        "adjusted": {
            "session_type": adjusted.session_type,
            "duration_min": adjusted.duration_min,
            "tss_estimate": adjusted.tss_estimate,
            "description": adjusted.description,
        },
        "reason": reason,
        "adjustment_reason": adjustment_reason,
        "readiness": r.get("score"),
        "hrv_streak_below_swc": hrv_streak,
        "was_modified": reason != "",
        "hr_target": _session_hr_target(adjusted.session_type),
        "power_target": _session_power_target(adjusted.session_type),
        # F1/F2 (v4.1.0): expose DFA cap + decoupling advisory so the UI
        # can surface a "Why Z2?" tooltip on the today card.
        "dfa_cap": r.get("dfa_cap"),
        "decoupling_advisory": r.get("decoupling_advisory"),
    }


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
    if not components:
        return None
    # Use the MIN (worst) so any single bad component drags the score —
    # matches Hooper's "any limb weak" intent better than averaging.
    return min(components)


def _session_hr_target(session_type: str) -> dict:
    """Return HR zone target for a session type."""
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
    """Return power zone target for a session type."""
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
                    plan_data, today_iso=date.today().isoformat(),
                )
            except Exception as _e:
                _log_error(
                    error_codes.Codes.ENRICH_FAILED, exc=_e,
                    endpoint="/api/plan",
                    week_count=len(plan_data.get("weeks", [])) if isinstance(plan_data, dict) else 0,
                )
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

    # For event-prep, plan_weeks is auto-computed from event_date; UI
    # disables the input but we recompute server-side as defence in depth
    # so a stale/forced-edit value can't desync the right panel from the
    # generated grid.
    if goal in ("event", "event_preparation") and target_date is not None:
        days_to_event = (target_date - date.today()).days
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
    )

    try:
        training = cached("training", get_today_metrics)
        current_ctl = float(training.get("ctl") or 37.0)
    except Exception:
        current_ctl = 37.0

    phases = tp.generate_phases(g, current_ctl)
    return {
        "ok": True,
        "plan_weeks": plan_weeks,
        "goal": goal_type,
        "event_date": target_date.isoformat() if target_date else None,
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

    target_date = None
    if g.get("event_date"):
        try:
            target_date = date.fromisoformat(g["event_date"])
        except ValueError:
            target_date = None

    goal = tp.Goal(
        goal_type=g.get("type", "event"),
        target_date=target_date,
        event_name=g.get("event_name", ""),
        event_km=g.get("event_km", 0),
        event_climb_m=g.get("event_climb", 0),
        event_type=g.get("event_type", "granfondo"),
        hours_per_week=g.get("hours_per_week", 8),
        longest_ride_h_90d=g.get("longest_ride_h_90d"),
        last_ftp_test_date=g.get("last_ftp_test_date"),
    )

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
        current_ctl = float(training.get("ctl") or 50.0)
    except Exception:
        current_ctl = 50.0

    # Best-efforts aggregate for CP/W' Monod fit (only when we have ≥5 rides
    # in the last 30 days — gate prevents fitting on stale data).
    best_efforts: dict = {}
    try:
        if rides:
            cutoff_30 = (date.today() - timedelta(days=30)).isoformat()
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
        cutoff_iso = (date.today() - timedelta(days=12 * 7)).isoformat()
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
            target = date.today() - timedelta(weeks=wk_back)
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

        goal = tp.Goal(
            goal_type=body.get("goal", "general"),
            target_date=target_date,
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
            distribution=body.get("distribution", _prefs.get("distribution", "polarized")),
            # F1 (v2.1): opt-in block periodization (default off).
            block_periodization=bool(body.get("block_periodization", _prefs.get("block_periodization", False))),
            events=_events_from_dicts(body.get("events")),  # F7 (A + optional B/C)
        )
        # v4.6.7 IMPL-CAP: auto-populate endurance baseline if missing.
        if goal.longest_ride_h_90d is None:
            goal.longest_ride_h_90d = _longest_ride_h_90d()

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
        json_path = _plan_dir() / "current_plan.json"
        availability_overrides: dict[str, float] = {}
        old_availability_full: dict = {}  # v1.8.21 — keep type info for block-preserve
        if json_path.exists():
            try:
                with open(json_path, encoding="utf-8") as f:
                    _existing_plan = json.load(f)
                old_availability_full = _existing_plan.get("availability", {}) or {}
                availability_overrides = {
                    day_iso: float(entry["hours"])
                    for day_iso, entry in old_availability_full.items()
                    if isinstance(entry, dict) and "hours" in entry
                }
            except (json.JSONDecodeError, OSError, KeyError, ValueError, TypeError):
                availability_overrides = {}
                old_availability_full = {}

        # v1.11.0 — thread athlete (ftp + weight) so the event-demand planner
        # can compute event targets. Mirrors /api/event/projection's assembly;
        # only pass a real dict when ftp+weight are present, else athlete=None
        # (training_planner no-ops on a missing/empty athlete for non-event
        # goals and for events without a real ftp/weight).
        athlete = None
        try:
            from profile_manager import ProfileManager
            _pm = ProfileManager.get()
            # pm.ftp / pm.weight_kg are default-backed (200 / 70.0), so probe the
            # raw athlete store to tell a genuinely-set value from a fabricated
            # default — a brand-new user with no ftp/weight must yield athlete=None.
            _raw = getattr(_pm, "_athlete", {}) or {}
            if "ftp" in _raw and "weight_kg" in _raw and _raw.get("ftp") and _raw.get("weight_kg"):
                athlete = {"ftp": _pm.ftp, "weight_kg": _pm.weight_kg}
        except Exception:
            athlete = None

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
            current_ctl = training.get("ctl")
        except Exception:
            current_ctl = None
        try:
            import ride_storage as _rs
            recent_weekly_tss = _rs.recent_mean_weekly_tss()
        except Exception:
            recent_weekly_tss = None

        phases, weeks = tp.generate_plan(
            goal, seed_salt=seed_salt,
            availability_overrides=availability_overrides or None,
            athlete=athlete,
            current_ctl=current_ctl,
            recent_weekly_tss=recent_weekly_tss,
        )
        plan_path = tp.export_plan_md(goal, phases, weeks)

        # Also save structured JSON
        plan_dict = {
            "goal": {
                "type": goal.goal_type,
                "event_date": goal.target_date.isoformat() if goal.target_date else None,
                "event_name": goal.event_name,
                "event_km": goal.event_km,
                "event_climb": goal.event_climb_m,
                "event_type": goal.event_type,
                "hours_per_week": goal.hours_per_week,
                "max_weekday_hours": goal.max_weekday_hours,
                "max_weekend_hours": goal.max_weekend_hours,
                "rest_days": goal.rest_days,
                # P5 (v4.1.0): persist available_days + daily_max_hours so
                # /api/weekly-plan reconstruction doesn't silently fall back to
                # [1..6] (dropping Monday). Without this, a user who picked
                # rest_days=[2,3] would see a phantom Monday rest because the
                # Goal reconstructor defaulted available_days to [1..6].
                "available_days": list(goal.available_days or []),
                "daily_max_hours": {str(k): float(v) for k, v in (goal.daily_max_hours or {}).items()},
                "plan_weeks": goal.plan_weeks,
                # v4.6.7 IMPL-CAP: persist capability-projection inputs.
                "longest_ride_h_90d": goal.longest_ride_h_90d,
                "last_ftp_test_date": goal.last_ftp_test_date,
                # J1 (v2.1.0): persist the chosen distribution so recalc/refit
                # rebuild with the same model (else they'd revert to polarized).
                "distribution": getattr(goal, "distribution", "polarized"),
                "block_periodization": getattr(goal, "block_periodization", False),  # F1
                "events": _events_to_dicts(getattr(goal, "events", [])),  # F7
            },
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
            "weeks": [
                {
                    "week_num": w.week_num,
                    "start": w.start.isoformat() if hasattr(w.start, "isoformat") else str(w.start),
                    "end": w.end.isoformat() if hasattr(w.end, "isoformat") else str(w.end),
                    "phase": w.phase,
                    "tss_target": w.tss_target,
                    "is_stepback": w.is_stepback,
                    "sessions": [
                        {
                            "day": s.day.isoformat() if hasattr(s.day, "isoformat") else str(s.day),
                            "day_name": s.day_name,
                            "session_type": s.session_type,
                            "duration_min": s.duration_min,
                            "tss_estimate": s.tss_estimate,
                            "description": s.description,
                            "zwo_file": s.zwo_file,
                            "zwo_name": s.zwo_name,
                        }
                        for s in w.sessions
                    ],
                }
                for w in weeks
            ],
            "generated": datetime.now().isoformat(),
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
        _EXPLICIT_BLOCK = {"holiday", "injury", "illness", "unavailable"}
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

        tp.atomic_write_plan(json_path, plan_dict)

        # v1.4.0 — single enrichment helper (replaces the inline duplicate).
        # Adds card_state / card_state_v2 / content_class / display_name /
        # zone_dist / score / protocol / zwo_duration_min per session.
        # v1.6.0 — surface failure under E_ENRICH_FAILED.
        try:
            _enrich_plan_for_response(plan_dict, today_iso=date.today().isoformat())
        except Exception as _e:
            _log_error(
                error_codes.Codes.ENRICH_FAILED, exc=_e,
                endpoint="/api/plan/generate",
                week_count=len(plan_dict.get("weeks", [])) if isinstance(plan_dict, dict) else 0,
            )

        return {"ok": True, "plan_json": plan_dict, "plan_file": str(plan_path)}

    except Exception:
        # SEC3: don't leak exception text to the client. Log the full
        # traceback server-side; return a generic detail for the UI.
        _log.exception("Plan generate failed")
        return JSONResponse({"detail": "Plan update failed"}, status_code=500)


def _maybe_auto_reforecast(profile_id: str, new_rides: int) -> None:
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
    """
    try:
        if new_rides <= 0:
            return
        json_path = _plan_dir() / "current_plan.json"
        if not json_path.exists():
            return

        with tp.plan_write_lock():
            # Re-read inside the lock so a concurrent writer's fresher state
            # (reforecast_date / gap_regen_latch) is respected — the latch +
            # debounce are read-modify-write on the plan dict (grill risk 6).
            with open(json_path, encoding="utf-8") as f:
                plan = json.load(f)

            activities = db.query_activities(days=120)
            training = cached("training", get_today_metrics)

            plan_dict, action, _info, _status = _apply_plan_update(
                plan,
                training=training,
                activities=activities,
                today=date.today(),
                allow_regen=True,
                gap_debounce=True,
                reforecast_min_interval_iso=plan.get("reforecast_date"),
            )
            if action == "skipped":
                return  # reforecast tier debounced (<5 min) — nothing to write

            tp.atomic_write_plan(json_path, plan_dict)
    except Exception as e:  # noqa: BLE001
        _log.warning(f"auto-adapt skipped: {e}")


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
        today_str = date.today().isoformat()
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
        current_ctl = training.get("ctl") or 30
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
            today_d = date.today()
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
            # J1 (v2.1.0): align the breach gate with the plan's chosen
            # distribution model (also sets the active model for this recalc's
            # budget lookups) so a pyramidal/threshold plan isn't judged against
            # the polarized ceiling. Default polarized → unchanged.
            tp.set_active_distribution(
                (plan.get("goal", {}) or {}).get("distribution", "polarized"))
            _model_targets = tp.get_active_polarized_targets()
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
        plan["reforecast_date"] = datetime.now().isoformat()
        plan["last_reforecast_info"] = reforecast_info
        tp.atomic_write_plan(json_path, plan)

        # Also detect gaps (tiered absence detection). v1.5.0: rebuild
        # pw_list from the (now-mutated) plan dict — the previous local
        # pw_list was inlined into tp.reforecast_dict.
        gaps_pw_list = tp._plan_dict_to_planned_weeks(plan)
        gaps = tp.detect_plan_gaps(gaps_pw_list, activities, current_ctl)
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
            # the dict (no fall-through to live ICU).
            tsb_series: "dict | None" = None
            try:
                training_snap = cached("training", get_today_metrics)
                current_tsb = training_snap.get("tsb")
            except Exception:
                current_tsb = None
            if current_tsb is not None:
                tsb_series = {}
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
    # Reconstruct Goal
    g = plan.get("goal", {})
    target_date = date.fromisoformat(g["event_date"]) if g.get("event_date") else date.today() + timedelta(weeks=12)
    goal = tp.Goal(
        goal_type=g.get("type", "general"),
        target_date=target_date,
        event_name=g.get("event_name", ""),
        event_km=g.get("event_km", 0),
        event_climb_m=g.get("event_climb", 0),
        event_type=g.get("event_type", "granfondo"),
        hours_per_week=g.get("hours_per_week", 8),
        max_weekday_hours=g.get("max_weekday_hours", 2.0),
        max_weekend_hours=g.get("max_weekend_hours", 3.5),
        rest_days=g.get("rest_days", [0]),
        longest_ride_h_90d=g.get("longest_ride_h_90d"),
        last_ftp_test_date=g.get("last_ftp_test_date"),
    )
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
        sessions = [_planned_session_from_json(s) for s in w.get("sessions", [])]
        old_weeks.append(tp.PlannedWeek(
            week_num=w["week_num"], start=date.fromisoformat(w["start"]),
            end=date.fromisoformat(w["end"]), phase=w.get("phase", ""),
            tss_target=w.get("tss_target", 0), is_stepback=w.get("is_stepback", False),
            sessions=sessions,
        ))

    unavailable = plan.get("unavailable_periods", [])

    # v1.11.0 — thread athlete (ftp + weight) so the event-demand planner can
    # compute event targets on regen too. No ProfileManager is in scope here
    # (this core is shared by the auto-on-sync + manual regen paths), so
    # assemble it the same way /api/event/projection does; only a real dict
    # when ftp+weight are present, else athlete=None.
    athlete = None
    try:
        from profile_manager import ProfileManager
        _pm = ProfileManager.get()
        # pm.ftp / pm.weight_kg are default-backed (200 / 70.0), so probe the raw
        # athlete store to tell a genuinely-set value from a fabricated default —
        # a brand-new user with no ftp/weight must yield athlete=None.
        _raw = getattr(_pm, "_athlete", {}) or {}
        if "ftp" in _raw and "weight_kg" in _raw and _raw.get("ftp") and _raw.get("weight_kg"):
            athlete = {"ftp": _pm.ftp, "weight_kg": _pm.weight_kg}
    except Exception:
        athlete = None

    # Regenerate (seed_salt forces shuffle variance per call — B3)
    _regen_kwargs = dict(
        goal=goal, old_plan_weeks=old_weeks,
        current_ctl=current_ctl,
        unavailable_periods=unavailable,
        activities=activities,
        seed_salt=seed_salt,
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
    plan_dict["weeks"] = [
        {
            "week_num": w.week_num,
            "start": w.start.isoformat(), "end": w.end.isoformat(),
            "phase": w.phase, "tss_target": w.tss_target,
            "is_stepback": w.is_stepback,
            "sessions": [_planned_session_to_json(s) for s in w.sessions],
        }
        for w in all_weeks
    ]
    plan_dict["regenerated"] = datetime.now().isoformat()
    plan_dict["regen_info"] = regen_info
    # v4.3.0 B3: persist the per-regen entropy salt so external readers
    # (UI, tests) can detect that a fresh regen happened.
    plan_dict["last_regen_at"] = seed_salt
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


def _events_to_dicts(events) -> list:
    """F7 (v2.1): serialize Goal.events (TargetEvent list) into the saved goal block."""
    out = []
    for e in events or []:
        d = getattr(e, "date", None)
        out.append({
            "date": d.isoformat() if hasattr(d, "isoformat") else (d or None),
            "priority": getattr(e, "priority", "B"),
            "name": getattr(e, "name", ""),
            "event_type": getattr(e, "event_type", "granfondo"),
            "event_km": getattr(e, "event_km", 0),
            "event_climb_m": getattr(e, "event_climb_m", 0),
        })
    return out


def _events_from_dicts(raw) -> list:
    """F7 (v2.1): rebuild Goal.events (TargetEvent list) from the saved goal block
    or the plan-form POST. Skips entries without a parseable date."""
    out = []
    for e in raw or []:
        ds = e.get("date")
        if not ds:
            continue
        try:
            d = date.fromisoformat(ds) if isinstance(ds, str) else ds
        except (TypeError, ValueError):
            continue
        climb = e.get("event_climb_m")
        if climb is None:
            climb = e.get("event_climb")
        out.append(tp.TargetEvent(
            date=d,
            priority=e.get("priority", "B"),
            name=e.get("name", "") or "",
            event_type=e.get("event_type", "granfondo"),
            event_km=e.get("event_km", 0) or 0,
            event_climb_m=climb or 0,
        ))
    return out


def _goal_from_plan_dict(g: dict) -> "tp.Goal":
    """Reconstruct a full scheduling Goal from a persisted plan's ``goal`` block.

    Mirrors the P5 (v4.1.0) reconstruction in api_plan_generate: restores
    available_days / rest_days / daily_max_hours / max_*_hours so the sampler
    sees the rider's real weekly shape (not Goal defaults). Used by the
    missed-hard refit tier (the sampler reads all of these).
    """
    rest_days_val = g.get("rest_days", [0])
    raw_available = g.get("available_days")
    if raw_available is not None:
        available_days_val = list(raw_available)
    else:
        available_days_val = [d for d in range(7) if d not in rest_days_val]
    raw_daily = g.get("daily_max_hours") or {}
    daily_max_val: dict = {}
    for k, v in raw_daily.items():
        try:
            daily_max_val[int(k)] = float(v)
        except (TypeError, ValueError):
            continue
    # B1 (v2.1.0): the saved goal block persists the event, but this
    # reconstruction used to DROP target_date + every event_* field, so any
    # recalc/refit/reforecast lost the event entirely — which also starved the
    # F4 race-eve taper guard (it keys on goal.target_date). Restore them.
    ev = g.get("event_date")
    target_date_val = None
    if ev:
        try:
            target_date_val = date.fromisoformat(ev)
        except (TypeError, ValueError):
            target_date_val = None
    return tp.Goal(
        goal_type=g.get("type", g.get("goal_type", "general")),
        target_date=target_date_val,
        event_name=g.get("event_name", ""),
        event_km=g.get("event_km", 0),
        event_climb_m=g.get("event_climb", 0),  # persisted as "event_climb"
        event_type=g.get("event_type", "granfondo"),
        hours_per_week=g.get("hours_per_week", 8.0),
        max_weekday_hours=g.get("max_weekday_hours", 2.0),
        max_weekend_hours=g.get("max_weekend_hours", 3.5),
        rest_days=rest_days_val,
        available_days=available_days_val,
        daily_max_hours=daily_max_val,
        plan_weeks=g.get("plan_weeks", 0),
        longest_ride_h_90d=g.get("longest_ride_h_90d"),
        last_ftp_test_date=g.get("last_ftp_test_date"),
        distribution=g.get("distribution", "polarized"),  # J1
        block_periodization=bool(g.get("block_periodization", False)),  # F1
        events=_events_from_dicts(g.get("events")),  # F7
    )


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
            dto_weeks.append(tp.PlannedWeek(
                week_num=w["week_num"], start=date.fromisoformat(w["start"]),
                end=date.fromisoformat(w["end"]), phase=w.get("phase", ""),
                tss_target=w.get("tss_target", 0),
                is_stepback=w.get("is_stepback", False),
                sessions=[_planned_session_from_json(s) for s in w.get("sessions", [])],
            ))
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

    _, refit_info = tp.refit_remaining_week(
        goal, dto_weeks, today, seed_salt=seed_salt,
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
    now_iso = datetime.now().isoformat()
    current_ctl = training.get("ctl") or 30
    current_tsb = training.get("tsb")

    # J1 (v2.1.0): pin the active intensity-distribution model to the plan's
    # persisted choice so every tier (rebuild / missed-hard refit / reforecast)
    # rebuilds with the same model rather than reverting to polarized.
    tp.set_active_distribution((plan.get("goal", {}) or {}).get("distribution", "polarized"))

    # v1.8.25 — RECONCILE FIRST. Mark the current week's sessions done/missed
    # from actual activities BEFORE adapting, so this happens automatically on
    # every sync / Update-plan (no manual "Reconcile Week" click). Idempotent
    # (dedup by activity_id). Runs before regen/reforecast so the regen path's
    # v1.8.20 preservation carries the freshly-marked done/missed statuses.
    try:
        _reconcile_current_week(plan, today)
    except Exception:  # noqa: BLE001 — reconcile is best-effort; never block adapt
        _log.exception("auto-reconcile skipped")

    # Reconstruct PlannedWeek list for gap detection (full-field round-trip so
    # past statuses are visible to detect_plan_gaps' actual-TSS sum).
    old_weeks = []
    for w in plan.get("weeks", []):
        try:
            old_weeks.append(tp.PlannedWeek(
                week_num=w["week_num"], start=date.fromisoformat(w["start"]),
                end=date.fromisoformat(w["end"]), phase=w.get("phase", ""),
                tss_target=w.get("tss_target", 0),
                is_stepback=w.get("is_stepback", False),
                sessions=[_planned_session_from_json(s) for s in w.get("sessions", [])],
            ))
        except (KeyError, ValueError, TypeError):
            continue

    gaps = tp.detect_plan_gaps(old_weeks, activities or [], current_ctl)
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
            if (datetime.now() - last_dt).total_seconds() < 300:
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
    plan, _modified, reforecast_info = tp.reforecast_dict(
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
    status = "Plan rebalanced to today's fitness."
    plan["last_update_info"] = {"action": "rebalanced", "message": status, "at": now_iso}
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
        current_ctl = training.get("ctl") or 30

        activities = db.query_activities(days=120)

        plan_dict, regen_info = _regenerate_plan_dict(
            plan, current_ctl=current_ctl, activities=activities, seed_salt=seed_salt,
        )

        tp.atomic_write_plan(json_path, plan_dict)

        return {"ok": True, "plan_json": plan_dict, "regen_info": regen_info}

    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, 500)


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
                today=date.today(),
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
            _enrich_plan_for_response(plan_dict, today_iso=date.today().isoformat())
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

@app.get("/api/export/fit-workout")
def api_export_fit_workout(
    session_type: str = Query("z2"),
    duration_min: int = Query(75),
    name: str = Query("Workout"),
    zwo_file: str | None = Query(None),
):
    """Generate a FIT binary workout file for Garmin Edge / Wahoo ELEMNT / Hammerhead Karoo.

    v1.0.1 fix: session_type is normalised before dispatch — both the planner's
    canonical form ("sweetspot", "overunder") AND the dashboard <select> values
    ("sweet_spot", "over_under") now route to the same elif branch. Pre-fix,
    the snake_case forms fell through to the else branch (a generic Z2 block),
    which silently produced a wrong workout instead of the requested intervals.

    v1.0.3 fix-forward(workout-detail UX): when ``zwo_file`` is supplied, the
    server parses that exact ZWO file out of WORKOUT_DIR and transcodes its
    blocks into FIT workout steps — so the FIT body matches the ZWO content
    (was previously a generic block keyed only on session_type+duration). If
    the named file is missing the route 404s loudly rather than silently
    falling back to a wrong workout. When ``zwo_file`` is None or empty, the
    pre-existing generic generator path is preserved (used by the no-library
    fallback at L10185 in dashboard.html).
    """
    try:
        from fit_tool.fit_file_builder import FitFileBuilder  # verify fit_tool is available

        ftp = config.ATHLETE_FTP_W

        if zwo_file:
            # v1.0.3 fix-forward: ZWO-content path — mirror exactly what the
            # rider sees in MyWhoosh / Tacx / Zwift.
            zwo_path = _safe_path(WORKOUT_DIR, zwo_file)
            if not zwo_path or not zwo_path.exists():
                return JSONResponse({"error": f"ZWO file not found: {zwo_file}"}, 404)
            fit_data = _build_fit_workout_from_zwo(name, zwo_path, ftp)
        else:
            # v1.0.1: normalise to lowercase + strip "_" / "-" so "sweet_spot",
            # "Sweet-Spot", and "sweetspot" all map to the same key.
            st_norm = (session_type or "").lower().replace("_", "").replace("-", "")

            # Build power blocks (reuse existing logic)
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

            # Build FIT file manually (lightweight binary format)
            # FIT header + data records + CRC
            # For compatibility, generate a minimal FIT workout that Garmin can read
            fit_data = _build_fit_workout(name, blocks, ftp)

        from fastapi.responses import Response
        # Sanitize aggressively — any char outside [A-Za-z0-9_.-] becomes '_'.
        # The prior quote-only escape accepted CR/LF, which would let a user
        # inject extra Content-Disposition headers into the response.
        safe_name = re.sub(r'[^\w.\-]', '_', name) or "workout"
        return Response(
            content=fit_data,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{safe_name}.fit"'},
        )

    except ImportError as ie:
        return JSONResponse({"error": f"fit_tool not installed. Run: pip install fit_tool. Detail: {ie}"}, 500)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse({"error": str(e)}, 500)


def _build_fit_workout(name: str, blocks: list[dict], ftp: int) -> bytes:
    """Build a proper FIT binary workout file using fit_tool.

    Produces a valid .fit file readable by Garmin Edge, Wahoo ELEMNT, and other ANT+ devices.
    Workout steps use absolute power targets in watts (calculated from %FTP).
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
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = 12345
    # time_created is optional for workout files — skip to avoid epoch issues
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

    for i, b in enumerate(blocks):
        step = WorkoutStepMessage()
        step.message_index = i
        step.duration_type = WorkoutStepDuration.TIME
        step.duration_value = b["min"] * 60 * 1000  # milliseconds
        step.target_type = WorkoutStepTarget.POWER
        step.intensity = INTENSITY_MAP.get(b.get("intensity", "active"), Intensity.ACTIVE)

        # Power targets in watts (FIT uses absolute watts + 1000 offset for custom targets)
        power_low = round(ftp * b["pctLow"] / 100) + 1000
        power_high = round(ftp * b["pctHigh"] / 100) + 1000
        step.custom_target_power_low = power_low
        step.custom_target_power_high = power_high
        step.target_value = 0  # 0 = custom target (not a zone)

        builder.add(step)

    # Build and return binary FIT data
    fit_file = builder.build()
    return fit_file.to_bytes()


def _build_fit_workout_from_zwo(name: str, zwo_path: Path, ftp: int) -> bytes:
    """v1.0.3 fix-forward(workout-detail UX): transcode a ZWO file directly
    into a FIT workout. The previous code path keyed only on
    ``(session_type, duration_min)`` so the FIT body could be a generic Tempo
    block while the ZWO download was the matched library file (a ladder, an
    over-under, etc.). When both files exist for the same session they MUST
    represent the same workout.

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

    tree = ET.parse(zwo_path)
    root = tree.getroot()
    workout_el = root.find("workout")

    # (intensity, duration_seconds, power_low_pct_fraction, power_high_pct_fraction)
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
                # No target in ZWO; default to 50% FTP so the head unit isn't blank.
                raw_steps.append(("active", dur, 0.5, 0.5))
            # Anything else (e.g. <textevent>) is ignored.

    INTENSITY_MAP = {
        "active": Intensity.ACTIVE,
        "rest": Intensity.REST,
        "warmup": Intensity.WARMUP,
        "cooldown": Intensity.COOLDOWN,
    }

    builder = FitFileBuilder()
    file_id = FileIdMessage()
    file_id.type = FileType.WORKOUT
    file_id.manufacturer = Manufacturer.DEVELOPMENT.value
    file_id.product = 0
    file_id.serial_number = 12345
    builder.add(file_id)

    workout = WorkoutMessage()
    workout.workout_name = name
    workout.sport = Sport.CYCLING
    workout.num_valid_steps = len(raw_steps)
    builder.add(workout)

    for i, (intensity_kind, dur_s, p_lo, p_hi) in enumerate(raw_steps):
        step = WorkoutStepMessage()
        step.message_index = i
        step.duration_type = WorkoutStepDuration.TIME
        step.duration_value = dur_s * 1000  # milliseconds
        step.target_type = WorkoutStepTarget.POWER
        step.intensity = INTENSITY_MAP.get(intensity_kind, Intensity.ACTIVE)
        # FIT uses absolute watts + 1000 offset for custom targets.
        step.custom_target_power_low = round(ftp * p_lo) + 1000
        step.custom_target_power_high = round(ftp * p_hi) + 1000
        step.target_value = 0  # 0 = custom target (not a zone)
        builder.add(step)

    fit_file = builder.build()
    return fit_file.to_bytes()


# ═══════════════════════════════════════════════════════════════════════════════
# ROLLING PLAN AUTO-RECALCULATION
# ═══════════════════════════════════════════════════════════════════════════════

# v1.8.20 — canonical PlannedSession ↔ JSON round-trip (single source of truth).
# Pre-v1.8.20 several writers (notably /api/plan/regenerate) hand-listed ~8 of
# the dataclass's 22 fields, silently DROPPING user_moved / status / moved_from /
# completion_matches / dismissed_at / adapted / am_or_pm on every rebuild — so an
# auto-regen wiped the rider's dragged + dismissed sessions. These helpers derive
# the field list from ``dataclasses.fields`` so no field can ever silently
# regress, and pass through the two JSON-only keys the dataclass doesn't carry
# (``variation`` + ``adapted_reason``, written by accept-redraw — variation drives
# redraw-seed reproducibility).
import dataclasses as _dataclasses

_PS_JSON_ONLY_KEYS = ("variation", "adapted_reason")


def _planned_session_from_json(s: dict) -> "tp.PlannedSession":
    """Reconstruct a PlannedSession from stored JSON, preserving ALL fields."""
    kwargs = {}
    for f in _dataclasses.fields(tp.PlannedSession):
        if f.name == "day":
            kwargs["day"] = date.fromisoformat(s["day"])
            continue
        if f.name in s:
            kwargs[f.name] = s[f.name]
    ps = tp.PlannedSession(**kwargs)
    # Carry JSON-only keys (not dataclass fields) as dynamic attrs so they
    # survive the round-trip on preserved sessions.
    for k in _PS_JSON_ONLY_KEYS:
        if k in s:
            try:
                setattr(ps, k, s[k])
            except Exception:
                pass
    return ps


def _planned_session_to_json(s: "tp.PlannedSession") -> dict:
    """Serialize a PlannedSession to JSON, emitting ALL fields."""
    out = {}
    for f in _dataclasses.fields(tp.PlannedSession):
        v = getattr(s, f.name, f.default)
        if f.name == "day":
            out["day"] = v.isoformat() if hasattr(v, "isoformat") else v
        else:
            out[f.name] = v
    for k in _PS_JSON_ONLY_KEYS:
        if hasattr(s, k):
            out[k] = getattr(s, k)
    return out


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
            sessions = [_planned_session_from_json(s) for s in w.get("sessions", [])]
            pw = tp.PlannedWeek(
                week_num=w["week_num"], start=w_start, end=w_end,
                phase=w["phase"], tss_target=w["tss_target"],
                is_stepback=w.get("is_stepback", False),
                sessions=sessions,
            )
            return pw, i
    return None, -1


def _collect_week_activities(current_week, today: date, include_today: bool = False):
    """Gather actual activities within [week_start, today) (or today+1 if include_today).

    Dedups by (date, rounded TSS / 5). Returns dicts rich enough for the
    rematch classifier (intensity_factor, duration_min, id passed through).
    """
    week_start_iso = current_week.start.isoformat()
    upper_iso = (today + timedelta(days=1)).isoformat() if include_today else today.isoformat()
    actual = []
    seen_keys = set()

    def _add(a: dict):
        d = (a.get("date") or a.get("start_date_local", "") or "")[:10]
        if not (week_start_iso <= d < upper_iso):
            return
        tss = float(a.get("tss") or a.get("icu_training_load") or 0)
        if tss <= 0:
            return
        key = (d, round(tss / 5) * 5)
        if key in seen_keys:
            return
        seen_keys.add(key)
        actual.append({
            "date": d,
            "tss": tss,
            "duration_min": float(a.get("duration_min") or (a.get("moving_time", 0) or 0) / 60 or 0),
            "intensity_factor": a.get("intensity_factor") or a.get("icu_intensity"),
            "id": a.get("id") or a.get("icu_id"),
            "sport": a.get("sport", ""),
        })

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

        today = date.today()
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
        # UI renders the week as Mon-Sun (``tp.generate_weekly_plan``).
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

        src_week = weeks_data[src_week_idx]
        dst_week = weeks_data[dst_week_idx]
        src_sessions = src_week.get("sessions", [])
        dst_sessions = dst_week.get("sessions", [])
        src_session = next((s for s in src_sessions if s.get("day") == src_iso), None)
        dst_session = next((s for s in dst_sessions if s.get("day") == dst_iso), None)
        if not src_session:
            # v4.3.0 B1 — same 422 contract as the missing-week case so the
            # frontend toast handler has a single error code to react on.
            return JSONResponse(
                {"error": "source_session_not_found", "date": src_iso}, 422
            )

        moved_payload = {k: v for k, v in src_session.items()}
        moved_payload["day"] = dst_iso
        moved_payload["day_name"] = (
            dst_session["day_name"] if dst_session else dst_d.strftime("%a")
        )
        moved_payload["user_moved"] = True
        moved_payload["moved_from"] = src_iso
        if moved_payload.get("status", "pending") == "pending":
            moved_payload["status"] = "pending"

        # Rewrite source week: replace src slot with a rest stub.
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
                # Same stored week — dst slot overwritten with moved payload.
                new_src.append(moved_payload)
            else:
                new_src.append(s)
        if src_week_idx == dst_week_idx:
            # Dst was in same week; ensure it's actually present.
            if not any(s.get("day") == dst_iso for s in new_src):
                new_src.append(moved_payload)
            src_week["sessions"] = new_src
        else:
            src_week["sessions"] = new_src
            # Rewrite dst week separately.
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

        plan["last_move"] = {"date": src_iso, "new_date": dst_iso, "at": datetime.now().isoformat()}

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

        goal = plan.get("goal", {}) or {}
        rest_days_idx = set(int(d) for d in goal.get("rest_days", []) or [])
        avail_days_raw = goal.get("available_days")
        if avail_days_raw is None:
            avail_days_idx = set(d for d in range(7) if d not in rest_days_idx)
        else:
            avail_days_idx = set(int(d) for d in avail_days_raw)
        availability = plan.get("availability", {}) or {}

        # v1.0.4 IMPL-WIRING — surface display_name + zwo_duration_min on each
        # suggestion so the missed-session reschedule banner can render the
        # canonical title (Layer 3) instead of the planner's session_type.
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

        today = date.today()

        # Index every session by ISO date for the rule §2 check.
        sess_by_day: dict[str, dict] = {}
        for w in plan.get("weeks", []) or []:
            for s in w.get("sessions", []) or []:
                day_iso = s.get("day", "")
                if day_iso:
                    sess_by_day[day_iso] = s

        def _is_available_slot(d_iso: str, missed_iso: str) -> bool:
            """Apply the locked six-rule "available slot" predicate."""
            try:
                d = date.fromisoformat(d_iso)
            except ValueError:
                return False
            # Rule 1: D >= today
            if d < today:
                return False
            # Rule 5: D is not the missed_date itself
            if d_iso == missed_iso:
                return False
            # Rule 6: same ISO Mon-Sun week
            try:
                missed_d = date.fromisoformat(missed_iso)
            except ValueError:
                return False
            if d.isocalendar()[:2] != missed_d.isocalendar()[:2]:
                return False
            # Rule 3: weekday in available_days AND not in rest_days
            wd = d.weekday()
            if wd not in avail_days_idx:
                return False
            if wd in rest_days_idx:
                return False
            # Rule 4: availability entry not "unavailable"
            entry = availability.get(d_iso)
            if isinstance(entry, dict) and entry.get("type") == "unavailable":
                return False
            # Rule 2: stored session at D must be rest + clean status
            sess = sess_by_day.get(d_iso)
            if sess is None:
                return False
            if sess.get("session_type") != "rest":
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

        # Collect missed sessions with date metadata, sorted ascending
        # by missed_date for greedy first-fit.
        misses: list[dict] = []
        for w in plan.get("weeks", []) or []:
            for s in w.get("sessions", []) or []:
                if (s.get("status") or "") != "missed":
                    continue
                d_iso = s.get("day", "")
                if not d_iso:
                    continue
                misses.append(s)
        misses.sort(key=lambda s: s.get("day", ""))

        used: set[str] = set()
        suggestions: list[dict] = []

        for miss in misses:
            missed_iso = miss.get("day", "")
            try:
                missed_d = date.fromisoformat(missed_iso)
            except ValueError:
                continue
            iso_year, iso_week = missed_d.isocalendar()[:2]

            # Walk all sessions in the same ISO week, ordered by date.
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
                if not _is_available_slot(cand_iso, missed_iso):
                    continue
                chosen = cand_iso
                cand_sess = sess_by_day.get(cand_iso, {})
                chosen_reason = "rest_slot" if cand_sess.get("session_type") == "rest" else "unfilled_available_day"
                break

            if chosen is None:
                continue

            used.add(chosen)
            try:
                suggested_d = date.fromisoformat(chosen)
                suggested_day_name = suggested_d.strftime("%a")
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
            # v1.0.4 IMPL-WIRING — pull canonical title for the missed session.
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

        return {"suggestions": suggestions}

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
    # 3D mirrors (None when 3D unavailable; v1.0.6)
    "wprime_tss", "pmax_tss", "cp_tss",
    "wprime_xss", "pmax_xss", "cp_xss",
    # synthetic history shells (calendar payload only; v4.3.0)
    "_synthetic_history",
    # match_zwo redraw seeds
    "profile_id",
    # enriched (added by _enrich_plan_for_response)
    "card_state", "card_state_v2", "content_class", "display_name",
    "zone_dist", "score", "protocol", "zwo_duration_min",
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
    "content_class", "card_state", "card_state_v2",
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
            else:
                s.setdefault("zone_dist", None)
                s.setdefault("score", None)
                s.setdefault("content_class", "")
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
    """Fill ``planned_ctl_eow`` for each week using forecast_ctl over the
    daily TSS estimates pulled from the plan. Mutates ``weeks`` in place.
    """
    if not plan or not plan.get("weeks"):
        return
    # Resolve a starting CTL: prefer ICU wellness, fallback to local archive,
    # final fallback to 37.0 (matches generate_plan).
    try:
        import ride_storage as _rs
        start_ctl = _rs.compute_local_ctl()
    except Exception:
        start_ctl = None
    if start_ctl is None:
        start_ctl = float(getattr(config, "CURRENT_CTL", 37.0) or 37.0)

    plan_weeks = plan.get("weeks", [])
    daily_tss: list[float] = []
    week_end_index: dict[str, int] = {}  # iso start_date → end-of-week idx
    cursor = 0
    for pw in plan_weeks:
        for s in (pw.get("sessions") or []):
            try:
                daily_tss.append(float(s.get("tss_estimate") or 0))
            except (TypeError, ValueError):
                daily_tss.append(0.0)
            cursor += 1
        # End-of-week CTL is at index cursor (forecast_ctl prepends start).
        week_end_index[pw.get("start") or ""] = cursor

    if not daily_tss:
        return
    series = tp.forecast_ctl(start_ctl, daily_tss)  # length = len(daily_tss)+1

    for w in weeks:
        sd = w.get("start_date") or ""
        idx = week_end_index.get(sd)
        if idx is None or idx >= len(series):
            continue
        w["planned_ctl_eow"] = float(series[idx])


def _polarized_actual_from_rides(
    rides: list[dict], today: date, last_n_days: int = 7
) -> dict:
    """v4.4.0 — compute actual polarized split (Z1+Z2 / Z3 / Z4+) over the
    last N days of rides.

    Buckets:
      - Z1+Z2 = z1+z2 (low aerobic)
      - Z3    = z3    (tempo / SS)
      - Z4+   = z4+z5+z6+z7 (threshold + above)

    Returns ``{"z1z2_pct": int, "z3_pct": int, "z4plus_pct": int}``. Empty
    rides → all zeros.
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
        z12 += float((tiz.get("z1") or 0) + (tiz.get("z2") or 0))
        z3  += float(tiz.get("z3") or 0)
        z4p += float(
            (tiz.get("z4") or 0) + (tiz.get("z5") or 0)
            + (tiz.get("z6") or 0) + (tiz.get("z7") or 0)
        )
    total = z12 + z3 + z4p
    if total <= 0:
        return {"z1z2_pct": 0, "z3_pct": 0, "z4plus_pct": 0}
    return {
        "z1z2_pct": int(round(z12 / total * 100)),
        "z3_pct":   int(round(z3  / total * 100)),
        "z4plus_pct": int(round(z4p / total * 100)),
    }


def _planned_ctl_today(plan: dict, today: date) -> float | None:
    """End-of-today planned CTL via forecast_ctl over daily TSS."""
    if not plan or not plan.get("weeks"):
        return None
    try:
        import ride_storage as _rs
        start_ctl = _rs.compute_local_ctl()
    except Exception:
        start_ctl = None
    if start_ctl is None:
        start_ctl = 37.0

    # Walk plan sessions chronologically, picking up daily TSS by day-iso.
    daily_tss: list[float] = []
    days: list[str] = []
    for w in plan.get("weeks", []):
        for s in (w.get("sessions") or []):
            day = s.get("day") or ""
            if not day:
                continue
            try:
                t = float(s.get("tss_estimate") or 0)
            except (TypeError, ValueError):
                t = 0.0
            days.append(day)
            daily_tss.append(t)

    if not days:
        return None
    series = tp.forecast_ctl(start_ctl, daily_tss)
    today_iso = today.isoformat()
    # forecast_ctl prepends start_ctl, so series[i+1] = end of days[i].
    for i, d in enumerate(days):
        if d == today_iso:
            return float(series[i + 1])
        if d > today_iso:
            return float(series[i])  # prior day's end-of-day CTL
    return float(series[-1])


def _actual_ctl_today(rides: list[dict], today: date) -> float | None:
    """Best-effort actual CTL: prefer ICU wellness, fall back to local.

    v1.3.3 perf: route through ``cached()`` (5-min TTL) instead of calling
    ``fetch_wellness`` directly. Pre-fix every /api/calendar served the
    homepage triggered a 200-400ms ICU HTTP round-trip here AND another in
    ``_hrv_trend_score``, blocking the dashboard's main paint. The cache
    key matches the one ``/api/wellness?days=7`` already uses so the two
    paths share a single TTL window.
    """
    try:
        wellness = cached("wellness_7", lambda: fetch_wellness(7))
        if wellness:
            for w in reversed(wellness):
                if w.get("ctl") is not None:
                    return float(w["ctl"])
    except Exception:
        pass
    try:
        import ride_storage as _rs
        v = _rs.compute_local_ctl()
        if v is not None:
            return float(v)
    except Exception:
        pass
    return None


def _intensity_dist_match(actual: dict, target: dict) -> float:
    """Score 0..100 measuring how close ``actual`` polarized split matches
    ``target``. Lower L1 distance → higher score. Perfect match = 100.
    """
    if not actual or not target:
        return 0.0
    keys = ("z1z2_pct", "z3_pct", "z4plus_pct")
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


def _monotony_score(rides: list[dict], today: date) -> float | None:
    """Foster monotony score per CONCEPT-SCI §1. 100 = monotony in [1.2, 1.8]."""
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
    if sum(daily_tss) <= 0:
        return None
    mean = sum(daily_tss) / 7.0
    var = sum((v - mean) ** 2 for v in daily_tss) / 7.0
    sd = var ** 0.5
    if sd <= 0:
        return 0.0  # all-equal = max monotony
    monotony = mean / sd
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
    target_pol = tp.PHASE_POLARIZED_TARGETS.get(
        cur_phase.lower() if cur_phase else "",
        tp.PHASE_POLARIZED_TARGETS["history"],
    )

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
    today = date.today()
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
    plan_weeks = plan.get("weeks", []) if plan else []
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
        # Duplicate ISO key → prefer the one with planned content.
        existing = deduped[existing_idx]
        existing_has_planned = any(
            (s.get("zwo_file") or "") and not s.get("_synthetic_history")
            for s in (existing.get("sessions") or [])
        )
        cand_has_planned = any(
            (s.get("zwo_file") or "") and not s.get("_synthetic_history")
            for s in (w.get("sessions") or [])
        )
        if cand_has_planned and not existing_has_planned:
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
                # v1.0.4 IMPL-WIRING — display_name + zwo_duration_min on every
                # calendar planned cell so the dashboard cascade can pick the
                # canonical title and the actual library duration.
                _zwo = sess.get("zwo_file") or ""
                _dn, _zdur = _session_naming_lookup(_zwo, classifications, lib_by_file)
                planned_payload = {
                    "session_type": sess.get("session_type") or "",
                    "content_class": (
                        (lib_by_file.get(_zwo) or {}).get("content_class")
                        or sess.get("content_class")
                        or ""
                    ),
                    "name": sess.get("zwo_name") or sess.get("description") or "",
                    "display_name": _dn,
                    "zwo_duration_min": _zdur,
                    "duration_min": sess.get("duration_min") or 0,
                    "tss": sess.get("tss_estimate") or 0,
                    "score": sess.get("score"),
                    "zwo_file": _zwo,
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
        target_polarized = tp.PHASE_POLARIZED_TARGETS.get(
            phase_name, tp.PHASE_POLARIZED_TARGETS["history"]
        )

        out_weeks.append({
            "iso_year": wy,
            "iso_week": wk,
            "start_date": w_start.isoformat(),
            "end_date": w_end.isoformat(),
            "phase": w.get("phase") or "",
            "is_stepback": bool(w.get("is_stepback")),
            "is_current": is_current,
            "planned_tss": round(planned_tss, 1),
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
                "today": date.today().isoformat(),
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
            "today": date.today().isoformat(),
        }


def _apply_rematch_preview_to_plan(plan: dict, week_idx: int, preview: dict) -> int:
    """Apply a rematch ``preview`` (from tp.rematch_week) onto the plan's
    week ``week_idx`` sessions — set status + dedup'd completion_matches.
    Mutates ``plan`` in place. Returns the count of sessions whose status
    actually changed. Idempotent (dedup by activity_id) so it's safe to run on
    every sync / Plan-tab open.
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
                "applied_at": datetime.now().isoformat(),
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
    return changed


def _reconcile_current_week(plan: dict, today: date) -> "tuple[int, dict | None]":
    """v1.8.25 — mark the CURRENT week's sessions done/missed/ambiguous from
    actual activities (the /api/plan/rematch?apply=1 logic), idempotently.
    Mutates ``plan`` in place; returns (sessions_changed, preview|None).

    This is what makes reconciliation AUTOMATIC: the auto-adapt path
    (_apply_plan_update, fired by ride-sync) calls it so completed rides are
    matched to planned sessions on sync — no manual "Reconcile Week" click.
    """
    current_week, week_idx = _load_current_week_dto(plan, today)
    if not current_week:
        return 0, None
    actual = _collect_week_activities(current_week, today, include_today=True)
    preview = tp.rematch_week(current_week, actual, today)
    n = _apply_rematch_preview_to_plan(plan, week_idx, preview)
    if n:
        plan["last_rematch"] = datetime.now().isoformat()
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

        today = date.today()
        current_week, week_idx = _load_current_week_dto(plan, today)
        if not current_week:
            return {"action": "no_current_week"}

        actual = _collect_week_activities(current_week, today, include_today=True)
        preview = tp.rematch_week(current_week, actual, today)

        if not apply:
            return {"ok": True, "apply": False, **preview}

        _apply_rematch_preview_to_plan(plan, week_idx, preview)
        plan["last_rematch"] = datetime.now().isoformat()
        tp.atomic_write_plan(json_path, plan)

        return {"ok": True, "apply": True, **preview}

    except Exception:
        _log.exception("Plan rematch failed")
        return JSONResponse({"detail": "Rematch failed"}, 500)


@app.post("/api/plan/re-draw")
async def api_plan_re_draw(request: Request):
    """FIX-CONTRACT C4 — per-day workout re-draw via JSON body.

    UI U8 contract: body ``{date: "YYYY-MM-DD"}`` (or legacy ``{day: int}``
    as day-of-week index resolved against the current plan start).
    Thin wrapper around :func:`api_plan_rematch_day` which already does the
    real work — kept as its own route so the UI primary path ends at ``200
    {zwo_file, zwo_name, variation}`` instead of 404-ing into the week-level
    classifier fallback.
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
                    today = date.today()
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
    # Delegate to the existing path-param form — same semantics.
    return await api_plan_rematch_day(day_iso)


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
        tp.match_zwo(
            cand, library,
            week_num=week_num + variation * 100,
            day_idx=day_idx,
            used_names=same_week | hard_exclude,
            raise_on_empty=True,
            exact_duration=True,  # closest-duration tier (v1.8.24)
        )
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

    target["zwo_file"] = str(candidate.get("zwo_file") or "")
    target["zwo_name"] = str(candidate.get("zwo_name") or candidate.get("zwo_file") or "")
    target["variation"] = int(candidate.get("variation") or 0)
    target["status"] = "pending"
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
        "at": datetime.now().isoformat(),
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
            today_iso=date.today().isoformat(),
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
                # v1.8.24 — closest-duration match on reshuffle (see helper).
                exact_duration=True,
            )
        except tp.NoCandidateWorkoutError:
            return {"ok": False, "action": "no_candidate", "day": day}

        if not planned.zwo_file:
            return {"ok": False, "action": "no_candidate", "day": day}

        target_session["zwo_file"] = planned.zwo_file
        target_session["zwo_name"] = planned.zwo_name
        target_session["variation"] = variation
        target_session["status"] = "pending"
        plan["last_rematch_day"] = {"date": day, "at": datetime.now().isoformat(),
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

        found = False
        for w in plan.get("weeks", []):
            for s in w.get("sessions", []):
                if s.get("day") == d_iso:
                    if undo:
                        s["status"] = "pending"
                        s["dismissed_at"] = ""
                    else:
                        s["status"] = "dismissed"
                        s["dismissed_at"] = datetime.now().isoformat()
                    found = True
                    break
            if found:
                break
        if not found:
            return JSONResponse({"error": f"no session at {d_iso}"}, 404)

        tp.atomic_write_plan(json_path, plan)

        return {"ok": True, "dismissed": not undo}

    except Exception:
        _log.exception("Plan dismiss-session failed")
        return JSONResponse({"detail": "Dismiss failed"}, 500)


@app.get("/api/plan/auto-recalc")
def api_plan_auto_recalc():
    """Auto-recalculate plan if >7 days since last recalc. Called on tab load."""

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
                # subtraction with `datetime.now()` is always homogeneous.
                if last_dt.tzinfo is not None:
                    last_dt = last_dt.replace(tzinfo=None)
                days_since = (datetime.now() - last_dt).days
                if days_since < 7:
                    # Still fresh — return event readiness only
                    training = cached("training", get_today_metrics)
                    current_ctl = training.get("ctl") or 30
                    g = plan.get("goal", {})
                    readiness = {}
                    if g.get("event_date"):
                        goal = tp.Goal(
                            goal_type=g.get("type", "general"),
                            target_date=date.fromisoformat(g["event_date"]),
                            event_name=g.get("event_name", ""),
                            event_km=g.get("event_km", 0),
                            event_climb_m=g.get("event_climb", 0),
                            event_type=g.get("event_type", "granfondo"),
                            hours_per_week=g.get("hours_per_week", 8),
                            longest_ride_h_90d=g.get("longest_ride_h_90d"),
                            last_ftp_test_date=g.get("last_ftp_test_date"),
                        )
                        if goal.longest_ride_h_90d is None:
                            goal.longest_ride_h_90d = _longest_ride_h_90d()
                        readiness = tp.compute_event_readiness(goal, current_ctl)
                    return {"action": "fresh", "days_since_recalc": days_since, "event_readiness": readiness}
            except (ValueError, TypeError):
                pass

        # Recalc needed — rebuild plan
        training = cached("training", get_today_metrics)
        current_ctl = training.get("ctl") or 30

        g = plan.get("goal", {})
        target_date = date.fromisoformat(g["event_date"]) if g.get("event_date") else None
        if not target_date:
            # Non-event goals (FTP, general): use target_date or default 12 weeks out
            target_date = date.fromisoformat(g["target_date"]) if g.get("target_date") else (date.today() + timedelta(weeks=12))

        goal = tp.Goal(
            goal_type=g.get("type", "general"),
            target_date=target_date,
            event_name=g.get("event_name", ""),
            event_km=g.get("event_km", 0),
            event_climb_m=g.get("event_climb", 0),
            event_type=g.get("event_type", "granfondo"),
            hours_per_week=g.get("hours_per_week", 8),
            max_weekday_hours=g.get("max_weekday_hours", 2.0),
            max_weekend_hours=g.get("max_weekend_hours", 3.5),
            rest_days=g.get("rest_days", [0]),
            longest_ride_h_90d=g.get("longest_ride_h_90d"),
            last_ftp_test_date=g.get("last_ftp_test_date"),
        )
        # v4.6.7 IMPL-CAP: auto-populate endurance baseline if missing.
        if goal.longest_ride_h_90d is None:
            goal.longest_ride_h_90d = _longest_ride_h_90d()

        # Reconstruct plan weeks
        old_weeks = []
        for w in plan.get("weeks", []):
            sessions = []
            for s in w.get("sessions", []):
                sessions.append(tp.PlannedSession(
                    day=date.fromisoformat(s["day"]), day_name=s.get("day_name", ""),
                    session_type=s.get("session_type", "rest"),
                    duration_min=s.get("duration_min", 0),
                    tss_estimate=s.get("tss_estimate", 0),
                    description=s.get("description", ""),
                ))
            old_weeks.append(tp.PlannedWeek(
                week_num=w["week_num"], start=date.fromisoformat(w["start"]),
                end=date.fromisoformat(w["end"]), phase=w.get("phase", ""),
                tss_target=w.get("tss_target", 0), is_stepback=w.get("is_stepback", False),
                sessions=sessions,
            ))

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
        # Assembled exactly like the regen caller: probe the raw athlete store
        # so a brand-new user with default-backed ftp/weight yields None.
        recalc_athlete = None
        try:
            from profile_manager import ProfileManager
            _pm = ProfileManager.get()
            _raw = getattr(_pm, "_athlete", {}) or {}
            if "ftp" in _raw and "weight_kg" in _raw and _raw.get("ftp") and _raw.get("weight_kg"):
                recalc_athlete = {"ftp": _pm.ftp, "weight_kg": _pm.weight_kg}
        except Exception:
            recalc_athlete = None

        new_phases, all_weeks, recalc_info = tp.recalculate_plan(
            goal=goal, current_plan_weeks=old_weeks,
            current_ctl=current_ctl, current_eftp=eftp,
            athlete=recalc_athlete,
        )

        if recalc_info.get("action") == "no_change":
            return {"action": "no_change", **recalc_info}

        # Save updated plan
        plan_dict = {
            "goal": plan.get("goal", {}),
            "phases": [
                {"name": p.name, "weeks": p.weeks,
                 "start": p.start.isoformat(), "end": p.end.isoformat(),
                 "weekly_tss": p.weekly_tss_target, "focus": p.focus}
                for p in new_phases
            ] if new_phases else plan.get("phases", []),
            "weeks": [
                {"week_num": w.week_num, "start": w.start.isoformat(), "end": w.end.isoformat(),
                 "phase": w.phase, "tss_target": w.tss_target, "is_stepback": w.is_stepback,
                 "sessions": [
                     {"day": s.day.isoformat(), "day_name": s.day_name,
                      "session_type": s.session_type, "duration_min": s.duration_min,
                      "tss_estimate": s.tss_estimate, "description": s.description,
                      "zwo_file": getattr(s, "zwo_file", ""), "zwo_name": getattr(s, "zwo_name", "")}
                     for s in w.sessions
                 ]}
                for w in all_weeks
            ],
            "generated": plan.get("generated", ""),
            "recalc_date": datetime.now().isoformat(),
            "recalc_info": recalc_info,
            "availability": plan.get("availability", {}),
            "unavailable_periods": plan.get("unavailable_periods", []),
        }

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


def _rides_fit_dir() -> Path:
    """Directory for raw FIT imports. Shared across profiles by design:
    the imported FIT contains its own athlete/device metadata and the
    per-profile saved-ride archive lives elsewhere (ride_storage)."""
    d = _user_data_dir / "rides"
    d.mkdir(parents=True, exist_ok=True)
    return d


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

    try:
        for rec in ff.records:
            msg = rec.message
            mtype = type(msg).__name__
            if mtype == "RecordMessage":
                sample_count += 1
                p_val = 0
                try:
                    pw = msg.get_value("power")
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
                    hr = msg.get_value("heart_rate")
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
                    cad = msg.get_value("cadence")
                    if cad is not None:
                        cad_sum += int(cad)
                        cad_n += 1
                        c_val = int(cad)
                except Exception:
                    pass
                cadence_series.append(c_val)
                try:
                    sp = msg.get_value("speed")
                    if sp is not None and float(sp) > speed_max:
                        speed_max = float(sp)
                except Exception:
                    pass
                try:
                    d = msg.get_value("distance")
                    if d is not None:
                        distance_m = max(distance_m, float(d))
                except Exception:
                    pass
            elif mtype == "SessionMessage":
                try:
                    v = msg.get_value("total_timer_time")
                    if v is not None:
                        duration_s = int(round(float(v)))
                except Exception:
                    pass
                try:
                    v = msg.get_value("total_distance")
                    if v is not None:
                        distance_m = max(distance_m, float(v))
                except Exception:
                    pass
                try:
                    v = msg.get_value("total_work")
                    if v is not None:
                        total_kj = float(v) / 1000.0
                except Exception:
                    pass
                try:
                    v = msg.get_value("training_stress_score")
                    if v is not None:
                        tss = float(v)
                except Exception:
                    pass
                try:
                    sport = str(msg.get_value("sport") or "")
                except Exception:
                    sport = None
                try:
                    v = msg.get_value("start_time")
                    if v is not None:
                        start_time = str(v)
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
        test_kind = _fe.detect_ftp_test_shape(
            power_series, filename_hint=fit_path.name,
        )
        # FIX-CONTRACT M3: carry prior_ftp / pct_delta / method on the
        # suggestion payload so U1's banner renders the delta line correctly
        # ("was 250W · +3.2%") instead of defaulting to 0W/0%.
        _prior_ftp = int(config.ATHLETE_FTP_W or 0)
        if test_kind == "coggan_20min":
            suggest = _fe.coggan_20min_ftp(power_series)
            if suggest:
                is_ftp_test = True
                ftp_test_type = "coggan_20min"
                _val = int(suggest["value"])
                _delta = (
                    round((_val - _prior_ftp) / _prior_ftp * 100.0, 1)
                    if _prior_ftp > 0 else 0.0
                )
                ftp_test_suggestion = {
                    "type": suggest["type"],
                    "method": "coggan_20min",
                    "ftp": _val,
                    "value": _val,
                    "prior_ftp": _prior_ftp,
                    "pct_delta": _delta,
                    "source": "tested_coggan_20min",
                    "formula_used": suggest["formula_used"],
                    "best_20min": suggest.get("best_20min"),
                }
        elif test_kind == "ramp":
            suggest = _fe.ramp_test_ftp(power_series)
            if suggest:
                is_ftp_test = True
                ftp_test_type = "ramp"
                _val = int(suggest["value"])
                _delta = (
                    round((_val - _prior_ftp) / _prior_ftp * 100.0, 1)
                    if _prior_ftp > 0 else 0.0
                )
                ftp_test_suggestion = {
                    "type": suggest["type"],
                    "method": "ramp",
                    "ftp": _val,
                    "value": _val,
                    "prior_ftp": _prior_ftp,
                    "pct_delta": _delta,
                    "source": "tested_ramp",
                    "formula_used": suggest["formula_used"],
                    "best_60s": suggest.get("best_60s"),
                }
                halt = _fe.detect_ramp_halt(power_series, cadence_series)
                if halt and halt.get("halted"):
                    ftp_test_halted = True
                    ftp_test_halt_at_step = halt.get("halt_at_step")
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
    now = datetime.now(timezone.utc).astimezone()
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

# Module-level state file for last-sync-at marker. Sits next to the rides dir
# so a profile reset clears it naturally.
def _icu_sync_state_path() -> Path:
    return _user_data_dir / "rides" / "icu" / ".last_sync_at"


def _icu_wellness_sync_state_path() -> Path:
    """v4.5.0 — separate marker for last wellness sync (rides + wellness
    have different cadence requirements: rides matter ASAP after a ride,
    wellness is daily)."""
    return _user_data_dir / "wellness" / ".last_sync_at"


def _icu_credentials_present() -> bool:
    """True iff ICU_ATHLETE_ID + ICU_API_KEY are both set in config."""
    return bool(
        getattr(config, "ICU_ATHLETE_ID", None)
        and getattr(config, "ICU_API_KEY", None)
    )


def _read_last_sync_at() -> float | None:
    p = _icu_sync_state_path()
    if not p.exists():
        return None
    try:
        return float(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_last_sync_at(ts: float) -> None:
    p = _icu_sync_state_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(str(ts), encoding="utf-8")
    except OSError as e:
        _log.debug(f"_write_last_sync_at failed: {e}")


def _read_last_wellness_sync_at() -> float | None:
    p = _icu_wellness_sync_state_path()
    if not p.exists():
        return None
    try:
        return float(p.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_last_wellness_sync_at(ts: float) -> None:
    p = _icu_wellness_sync_state_path()
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
    for rec in records or []:
        if not isinstance(rec, dict):
            continue
        path = _rs.persist_wellness(rec)
        if path is not None:
            added += 1

    _write_last_wellness_sync_at(time.time())
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

    if not force:
        last = _read_last_sync_at()
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

    try:
        activities = _training.fetch_recent_activities(days=90)
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
    added_paths: list[Path] = []
    icu_dir = _rs._icu_rides_dir()
    # v2.0.5 — bound the TOTAL augment time for this foreground sync. The augment
    # runs synchronously on the request thread the Plan-tab catch-up overlay
    # awaits; without a ceiling a batch of slow records (e.g. first sync after a
    # break) blows past the UI's 40s step timeout and strands the overlay,
    # skipping the reconcile that adapts the week.
    _aug_deadline = time.monotonic() + _SYNC_AUGMENT_BUDGET_S
    _aug_deferred = 0
    for a in activities or []:
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
            continue
        existed = (icu_dir / f"{ext}.json").exists()
        path = _rs.persist_icu_activity(a)
        if path is None:
            continue
        if existed:
            updated += 1
        else:
            added += 1
            added_paths.append(path)
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

    if _aug_deferred:
        _log.info(
            f"_sync_icu_activities: DFA augment budget ({_SYNC_AUGMENT_BUDGET_S}s) "
            f"reached — deferred {_aug_deferred} record(s) to next sync"
        )
    now = time.time()
    _write_last_sync_at(now)
    total = len(_load_all_rides_safe())
    # v1.0.3 — best-effort auto-reforecast on new rides. Helper swallows
    # all exceptions so the sync result stays clean.
    _maybe_auto_reforecast("default", added)
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


def _load_all_rides_safe() -> list[dict]:
    """Defensive wrapper around ride_storage.load_all_rides()."""
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
    cutoff = (date.today() - timedelta(days=90)).isoformat()
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
    # v1.3.0 default mode: delegate to power_curve.
    import power_curve
    full = power_curve.aggregate_power_curve(profile_id, window_days=90)
    return {pt["duration_s"]: pt["watts"] for pt in full["rider_curve"]
            if pt["duration_s"] in target_durations}


def _pr_toast_queue_path() -> Path:
    """v1.3.0 IMPL-PR-DETECTION: per-version toast queue file.

    Stores ``[{"ride_id": ..., "started_at": ..., "n_prs": int}]`` for rides
    that landed major PRs since the last dashboard open. Drained by the GET
    /api/ride/.../prs/toast-queue endpoint.
    """
    return Path.home() / ".domestique" / "last_pr_toast.json"


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
            today_iso = date.today().isoformat()
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
        today_iso = date.today().isoformat()
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
    if force:
        result = _sync_icu_rides_and_wellness(force=True)
    else:
        result = _sync_icu_activities(force=False)
    # Always compute the alert post-sync so the dashboard can toast even
    # when the throttle returned status="throttled" (a recently-finished
    # ride still posts via a separate ICU push).
    result["plan_load_alert"] = _detect_plan_load_alert()
    # v1.0.3 — best-effort auto-reforecast on new rides. Helper swallows
    # all exceptions so the sync response stays unchanged.
    _maybe_auto_reforecast("default", result.get("added", 0))
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
        needs_intervals = not intervals
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
                    new_path = _rs.persist_icu_activity(full)
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

    return {
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
                pwr.append(int(msg.get_value("power") or 0))
            except Exception:
                pwr.append(0)
            try:
                hr.append(int(msg.get_value("heart_rate") or 0))
            except Exception:
                hr.append(0)
            try:
                cad.append(int(msg.get_value("cadence") or 0))
            except Exception:
                cad.append(0)
            try:
                elev.append(float(msg.get_value("altitude") or 0))
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
        all_rides = _rs.list_rides()
    except Exception:
        all_rides = []
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
    try:
        import ride_storage as _rs
        ctl_end_val = _rs.compute_local_ctl()
    except Exception:
        ctl_end_val = None
    ctl_start_val = None
    try:
        import datetime as _dt
        if start_date:
            start_d = _dt.date.fromisoformat(start_date)
            per_day: dict[str, float] = {}
            for r in all_rides:
                d = _ride_started_iso_date(r)
                if not d or d > start_date:
                    continue
                tss = (r.get("summary") or {}).get("tss") or 0
                if not tss:
                    continue
                per_day[d] = per_day.get(d, 0.0) + float(tss)
            if per_day:
                ctl = 0.0
                d_iter = _dt.date.fromisoformat(min(per_day.keys()))
                while d_iter <= start_d:
                    tss_today = per_day.get(d_iter.isoformat(), 0.0)
                    ctl = ctl + (tss_today - ctl) / 42.0
                    d_iter += _dt.timedelta(days=1)
                ctl_start_val = round(ctl, 1)
    except Exception:
        ctl_start_val = None

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


def _diag_local_only(request: Request) -> bool:
    """True iff the request is from localhost. Domestique listens only on
    127.0.0.1, so any non-local client is suspicious. Returns True when
    ``request.client`` is None or the host is the FastAPI TestClient
    sentinel, so tests pass without special-casing.
    """
    client = getattr(request, "client", None)
    if client is None or client.host is None:
        return True
    return client.host in ("127.0.0.1", "localhost", "::1", "testclient")


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
    # workout_library
    try:
        lib = tp.load_workout_library()
        checks["workout_library"] = {"ok": True, "count": len(lib) if hasattr(lib, "__len__") else None}
    except Exception as e:
        checks["workout_library"] = {"ok": False, "code": error_codes.Codes.ENRICH_LIBRARY, "msg": str(e)[:200]}
    # enrich
    if plan_data is not None and isinstance(plan_data, dict):
        try:
            sample = json.loads(json.dumps(plan_data, default=str))
            _enrich_plan_for_response(sample, today_iso=date.today().isoformat())
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
        "ts": datetime.now(timezone.utc).isoformat(),
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
    registry_path = Path.home() / ".domestique" / "profiles.json"
    # 1. No profiles.json at all → first-time setup
    if not registry_path.exists() and not SETUP_MARKER.exists():
        return RedirectResponse(url="/setup")
    # 2. Multiple profiles + skip_picker=false → profile picker
    try:
        pm = ProfileManager.get()
        profiles = pm.list_profiles()
        skip = pm._registry.get("skip_picker", True)
        if len(profiles) > 1 and not skip:
            return RedirectResponse(url="/profile-picker")
    except Exception:
        pass
    # 3. Otherwise → dashboard
    return templates.TemplateResponse(request=request, name="dashboard.html")


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
    print("Domestique Dashboard - http://localhost:8080")
    uvicorn.run(app, host="127.0.0.1", port=8080, log_level="warning",
                workers=_UVICORN_WORKERS)
