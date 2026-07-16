"""Persistent logging configuration for Domestique (v4.0.0-alpha).

Logs to both console and a rotating file. The file-handler path is
``~/.domestique/logs/domestique_<iso_timestamp>.log`` per boot; the 20
newest boot logs survive on disk and older ones are pruned on the next
boot. In addition, per-app-session logs can be spawned via
``start_session_log()`` -- these live at
``~/.domestique/logs/app_<iso_timestamp>_<sid>.log`` and are rotated so
only the newest 20 survive. A background flusher wakes every ~1 s so a
SIGKILL does not lose the last few seconds of data.

Usage::

    import log_config
    log = log_config.get_logger(__name__)
    log_lib = log_config.get_logger("domestique.library")

    sid = log_config.start_session_log()   # optional, for long-running flows
    log_config.stop_session_log(sid)

Named categories (shortcuts available as ``log_app``, ``log_plan`` etc):

    domestique.app         -- app-level lifecycle + HTTP surface events
    domestique.plan        -- planner / weekly-plan writes + reforecasts
    domestique.profile     -- profile switch / migration events
    domestique.workout     -- ZWO parsing + library validation
    domestique.ride_import -- FIT upload + parse events
    domestique.library     -- workout-library browsing events
    domestique.power       -- post-ride power-math sanity warnings
    domestique.rides       -- saved-ride archive CRUD

The trainer/BLE/gate/phase/hr/ws/session category loggers that existed
in v3 were removed when the live-ride runtime was ripped out.

Env overrides (honoured at ``setup_logging()`` time):
    DOMESTIQUE_VERBOSE           -- "1" / "true" bumps root logger to DEBUG
    DOMESTIQUE_LOG_CATEGORIES    -- comma-list (e.g. "plan,profile") bumps
                                     those category loggers to DEBUG.
    DOMESTIQUE_LOG_MAX_BYTES     -- rotating file size before rollover (default 5 MB)
    DOMESTIQUE_LOG_BACKUP_COUNT  -- number of rotated files to keep (default 20)
    DOMESTIQUE_RIDE_LOG_KEEP     -- number of per-session app logs to retain (default 20)

Runtime::

    set_level("DEBUG")                        # root
    set_level("DEBUG", category="library")    # just domestique.library

Legacy ``CC_LOG_MAX_BYTES`` / ``CC_LOG_BACKUP_COUNT`` are still honoured
as a fallback; setting either emits a one-shot DeprecationWarning.
"""

import logging
import logging.handlers
import os
import threading
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

from user_home import domestique_home
LOG_DIR = domestique_home() / "logs"

# v4.0.0-alpha (FIX-SERVER): primary log file is boot-stamped
# ``domestique_<iso>.log`` (was a single ``domestique.log``). Each uvicorn
# boot writes its own file; the 20 newest survive on disk, older ones get
# pruned before the new handler opens. Per-boot isolation makes
# post-incident triage trivial ("the log for THIS crash is
# domestique_<ts>.log") and sidesteps the stale-rotation handshake that
# kept pre-pivot ``domestique.log.1..14`` files alive in older deploys.
# Keeping the ``domestique_`` prefix distinguishes the boot log from
# per-session logs (``app_<ts>_<sid>.log``) which share ``LOG_DIR``.
LOG_FILE: "Path | None" = None

# v2.2.0 — MINIMAL logging. The old per-boot timestamped logs + per-session
# app/ride logs accumulated unbounded (multi-GB) because the prune missed the
# rotation backups and a new file family spawned every launch. Now: ONE small
# capped ``domestique.log`` (≈3 MB total, forever), no per-boot/per-session files.
_DEFAULT_MAX_BYTES = 1 * 1024 * 1024   # 1 MB per file
_DEFAULT_BACKUP_COUNT = 2              # domestique.log + .1 + .2  → ~3 MB ceiling
_DEFAULT_RIDE_LOG_KEEP = 0            # per-session app logs disabled (see start_session_log)

_configured = False

# Active per-session log handlers, keyed by session_id. Writes are gated by
# ``_session_lock`` so start/stop from the HTTP side never collides with a
# concurrent flush or rotation.
_session_handlers: "dict[str, logging.Handler]" = {}
_session_paths: "dict[str, Path]" = {}
_session_lock = threading.Lock()

# Canonical list of every category logger that the post-pivot app emits on.
# Used by ``get_levels()`` (log-level endpoint payload) and by the
# ``DOMESTIQUE_LOG_CATEGORIES`` env-var parser. Keep in sync with the
# ``log_*`` shortcuts at the bottom of this file.
CATEGORY_NAMES = (
    "app", "plan", "profile", "workout", "ride_import",
    "library", "power", "rides",
)

# Background flusher: wake every ~1 s, ``handler.flush()`` every handler so
# a SIGKILL never loses more than a second of disk log. Exits automatically
# at interpreter shutdown (daemon thread) so we never delay exit.
_flusher_thread: "threading.Thread | None" = None
_flusher_stop = threading.Event()


# ── env helpers ─────────────────────────────────────────────────────────


def _env_bool(name: str) -> bool:
    raw = os.environ.get(name, "")
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


def _resolve_log_env_int(new_name: str, legacy_name: str, default: int) -> int:
    if os.environ.get(new_name) is not None:
        return _env_int(new_name, default)
    if os.environ.get(legacy_name) is not None:
        warnings.warn(
            f"{legacy_name} is deprecated; use {new_name} instead. "
            "The legacy name will be removed in a future release.",
            DeprecationWarning,
            stacklevel=2,
        )
        return _env_int(legacy_name, default)
    return default


# ── root setup ──────────────────────────────────────────────────────────


def setup_logging(level: int | None = None) -> None:
    """Configure the root logger with console + rotating file handler.

    Idempotent -- safe to call many times. ``level`` may be explicitly
    passed (tests do this) or will be read from DOMESTIQUE_VERBOSE at
    first call.
    """
    global _configured
    # v1.6.3: pin third-party noise levels on EVERY call, not just the
    # first. ``fit_tool`` writes its level lazily after its first import,
    # which sometimes happens AFTER ``setup_logging()`` returned; without
    # this re-pin, the WARNING spam returns the moment the FIT parser
    # touches a record. Idempotent — setLevel is a no-op when the level
    # is already correct.
    logging.getLogger("fit_tool").setLevel(logging.ERROR)
    if _configured:
        return
    _configured = True

    if level is None:
        level = logging.DEBUG if _env_bool("DOMESTIQUE_VERBOSE") else logging.INFO

    root = logging.getLogger()
    root.setLevel(level)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)
    root.addHandler(ch)

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        root.warning(
            "log_config: cannot create %s (%s); file logging disabled, console only.",
            LOG_DIR, e,
        )
    else:
        try:
            max_bytes = _resolve_log_env_int(
                "DOMESTIQUE_LOG_MAX_BYTES", "CC_LOG_MAX_BYTES", _DEFAULT_MAX_BYTES,
            )
            backup_count = _resolve_log_env_int(
                "DOMESTIQUE_LOG_BACKUP_COUNT", "CC_LOG_BACKUP_COUNT", _DEFAULT_BACKUP_COUNT,
            )
            # v2.2.0 — single small capped ``domestique.log``. First sweep away
            # the legacy bloat (old per-boot ``domestique_<ts>.log`` families +
            # their rotation backups, per-session ``app_*``/``ride_*`` logs,
            # legacy ``chickencycling*``) so existing installs shrink to a few
            # MB, THEN open the one rotating log we keep from now on.
            _cleanup_legacy_logs(backup_count)
            log_path = LOG_DIR / "domestique.log"
            global LOG_FILE
            LOG_FILE = log_path
            fh = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=max_bytes, backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setLevel(logging.INFO)   # was DEBUG — keep the file lean
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as e:
            root.warning(
                "log_config: cannot open boot log in %s (%s); file logging disabled, console only.",
                LOG_DIR, e,
            )

    # Quiet noisy libraries.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)
    # v1.6.3: fit_tool emits per-record WARNINGs for non-standard FIT fields
    # (e.g. "Field id: 108 is not defined for message record:20"). Garmin
    # devices stamp ~3,000 records per hour-long ride and each produces 1-2
    # warning lines. On first-boot ICU sync after install this floods the
    # log to >13,000 lines / 5 MB inside 70 s, which (a) makes triage
    # impossible and (b) blocks the request thread doing the FIT parse.
    # Promote to ERROR — we never inspect these warnings, and any genuine
    # FIT parse failure already raises an exception that's caught upstream.
    logging.getLogger("fit_tool").setLevel(logging.ERROR)

    # DOMESTIQUE_LOG_CATEGORIES: opt-in per-category DEBUG, e.g.
    # DOMESTIQUE_LOG_CATEGORIES=plan,library. Unknown tokens are ignored
    # silently so a typo never crashes boot.
    cats_env = os.environ.get("DOMESTIQUE_LOG_CATEGORIES", "").strip()
    if cats_env:
        for raw in cats_env.split(","):
            token = raw.strip().lower()
            if not token:
                continue
            if token in CATEGORY_NAMES:
                logging.getLogger(f"domestique.{token}").setLevel(logging.DEBUG)

    _start_flusher()


def get_logger(name: str) -> logging.Logger:
    """Get a named logger. Call ``setup_logging()`` first (idempotent)."""
    setup_logging()
    return logging.getLogger(name)


# ── runtime level toggle ────────────────────────────────────────────────


def set_level(level: str | int, category: str | None = None) -> str:
    """Hot-swap a log level at runtime.

    ``category`` may be:
      - None / "" / "root": set the root logger level.
      - any entry from CATEGORY_NAMES: set ``domestique.<category>``.
      - "domestique.<x>" / full logger name: set that specific logger.

    ``level`` is an int (logging.DEBUG etc.) or a case-insensitive string.
    Returns the resolved level name. Raises ValueError on a bad level or
    unknown category.
    """
    setup_logging()
    if isinstance(level, str):
        lvl = logging.getLevelName(level.upper())
        if not isinstance(lvl, int):
            raise ValueError(f"unknown level: {level!r}")
    else:
        lvl = int(level)

    target_name: str
    if category is None or category == "" or category == "root":
        logger = logging.getLogger()
        target_name = "root"
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(
                h, (logging.handlers.RotatingFileHandler, logging.FileHandler)
            ):
                h.setLevel(lvl)
    else:
        cat = category.strip().lower()
        if cat in CATEGORY_NAMES:
            target_name = f"domestique.{cat}"
        elif cat.startswith("domestique."):
            target_name = cat
        else:
            raise ValueError(f"unknown category: {category!r}")
        logger = logging.getLogger(target_name)
    logger.setLevel(lvl)
    return logging.getLevelName(lvl)


def get_levels() -> "dict[str, str]":
    """Return ``{logger_name: level_name}`` for root + every known category.

    Returns the effective level (what ``getEffectiveLevel`` would report)
    so inherited root values are visible rather than "NOTSET".
    """
    setup_logging()
    out: dict[str, str] = {}
    root = logging.getLogger()
    out["root"] = logging.getLevelName(root.getEffectiveLevel())
    for cat in CATEGORY_NAMES:
        lg = logging.getLogger(f"domestique.{cat}")
        explicit = logging.getLevelName(lg.level) if lg.level != logging.NOTSET else None
        out[f"domestique.{cat}"] = explicit or logging.getLevelName(lg.getEffectiveLevel())
    return out


# ── per-session file sink ───────────────────────────────────────────────


def _cleanup_legacy_logs(backup_count: int) -> None:
    """v2.2.0 — delete every legacy/bloat log file, keeping only the single
    capped ``domestique.log`` (+ its ``.1..`` rotation backups).

    The pre-v2.2.0 setup wrote a NEW ``domestique_<ts>.log`` family every launch
    (each up to ~100 MB across its rotation backups) plus per-session ``app_*`` /
    ``ride_*`` logs, and the prune missed the ``.N`` rotation suffixes — so the
    logs dir grew to multiple GB. This runs once at startup so existing installs
    shrink to a few MB. Best-effort: any unlink failure is ignored.
    """
    try:
        keep = {"domestique.log"} | {
            f"domestique.log.{i}" for i in range(1, max(backup_count, 0) + 1)
        }
        for p in LOG_DIR.iterdir():
            if not p.is_file() or p.name in keep:
                continue
            n = p.name
            if (n.startswith("domestique_") or n.startswith("app_")
                    or n.startswith("ride_") or n.startswith("chickencycling")
                    or ".log" in n):
                try:
                    p.unlink()
                except OSError:
                    pass
    except OSError:
        pass


class _SessionContextFilter(logging.Filter):
    """Prefix every log record with ``[SESSION <id>]`` for this handler only."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "_sid_tagged", False):
            record.msg = f"[SESSION {self.session_id}] {record.msg}"
            record._sid_tagged = True  # type: ignore[attr-defined]
        return True


def start_session_log(session_id: str | None = None) -> str:
    """v2.2.0 — NO-OP. Per-session ``app_*.log`` files were removed (they were
    part of the unbounded log bloat); everything now goes to the single capped
    ``domestique.log``. Kept as a no-op returning an id so legacy callers stay
    safe."""
    setup_logging()
    return session_id or uuid.uuid4().hex[:8]


def get_active_log_path(session_id: str | None = None) -> str | None:
    """v2.2.0 — the one capped log file (no per-session files anymore)."""
    return str(LOG_FILE) if LOG_FILE else None


def stop_session_log(session_id: str) -> None:
    """v2.2.0 — no-op (per-session logs removed)."""
    return None


def flush_all() -> None:
    """Flush every handler attached to the root logger (and every per-session
    handler). Called by the periodic background flusher so a SIGKILL never
    loses more than ~1 s of the active log."""
    for h in logging.getLogger().handlers:
        try:
            h.flush()
        except Exception:
            pass
    with _session_lock:
        handlers = list(_session_handlers.values())
    for h in handlers:
        try:
            h.flush()
        except Exception:
            pass


def _flusher_loop() -> None:
    """Daemon loop: flush every ~1 s until interpreter shutdown."""
    while not _flusher_stop.wait(1.0):
        try:
            flush_all()
        except Exception:
            # Flusher must never raise -- would tear down its own thread.
            pass


def _start_flusher() -> None:
    """Idempotently spawn the 1-Hz flusher thread (daemon)."""
    global _flusher_thread
    if _flusher_thread is not None and _flusher_thread.is_alive():
        return
    _flusher_thread = threading.Thread(
        target=_flusher_loop, name="log_config.flusher", daemon=True,
    )
    _flusher_thread.start()


def stop_flusher(join_timeout: float = 2.0) -> None:
    """Signal the flusher to exit. Optional -- tests use this to tidy up."""
    _flusher_stop.set()
    t = _flusher_thread
    if t is not None:
        try:
            t.join(timeout=join_timeout)
        except Exception:
            pass


# ── named category loggers (convenience) ────────────────────────────────


def _cat(name: str) -> logging.Logger:
    return get_logger(f"domestique.{name}")


log_app = _cat("app")
log_plan = _cat("plan")
log_profile = _cat("profile")
log_workout = _cat("workout")
log_ride_import = _cat("ride_import")
log_library = _cat("library")
log_power = _cat("power")
log_rides = _cat("rides")
