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

LOG_DIR = Path.home() / ".domestique" / "logs"

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

_DEFAULT_MAX_BYTES = 5 * 1024 * 1024
_DEFAULT_BACKUP_COUNT = 20
_DEFAULT_RIDE_LOG_KEEP = 20

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
            # v4.0.0-alpha (FIX-SERVER, MASTER §2): per-boot ISO-stamped
            # filename ``domestique_<iso>.log`` replaces the former static
            # ``domestique.log``. Before opening the new file prune older
            # ones beyond backup_count so disk doesn't grow unbounded.
            _prune_old_boot_logs(backup_count - 1)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            log_path = LOG_DIR / f"domestique_{ts}.log"
            global LOG_FILE
            LOG_FILE = log_path
            fh = logging.handlers.RotatingFileHandler(
                log_path, maxBytes=max_bytes, backupCount=backup_count,
                encoding="utf-8",
            )
            fh.setLevel(logging.DEBUG)
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


def _prune_old_app_logs(keep: int) -> None:
    try:
        files = sorted(
            LOG_DIR.glob("app_*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass


def _prune_old_boot_logs(keep: int) -> None:
    """Trim boot-time ``domestique_<iso>.log`` files beyond ``keep`` newest.

    v4.0.0-alpha (FIX-SERVER): separate glob from ``_prune_old_app_logs``
    (which handles per-session ``app_*.log`` files) so the two prune paths
    stay isolated.
    """
    try:
        files = sorted(
            LOG_DIR.glob("domestique_*.log"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for stale in files[keep:]:
            try:
                stale.unlink()
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
    """Open a dedicated log file for this app session.

    Creates ``~/.domestique/logs/app_<iso_timestamp>_<sid>.log``, attaches
    it to the root logger at INFO, and prunes oldest session logs beyond
    ``DOMESTIQUE_RIDE_LOG_KEEP`` (default 20).

    v4.0.0-alpha: the file pattern moved from ``ride_<iso>_<sid>.log`` to
    ``app_<iso>_<sid>.log`` because there is no ride-session concept in
    the post-pivot app -- just per-app-boot log rotation.
    """
    setup_logging()
    if not session_id:
        session_id = uuid.uuid4().hex[:8]

    keep = _env_int("DOMESTIQUE_RIDE_LOG_KEEP", _DEFAULT_RIDE_LOG_KEEP)
    _prune_old_app_logs(keep - 1)

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = LOG_DIR / f"app_{ts}_{session_id}.log"

    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(path, encoding="utf-8")
    except OSError as e:
        logging.getLogger().warning(
            "log_config: cannot open app log %s (%s); per-session logging disabled.",
            path, e,
        )
        return session_id

    fh.setLevel(logging.INFO)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    fh.addFilter(_SessionContextFilter(session_id))

    root = logging.getLogger()
    root.addHandler(fh)

    with _session_lock:
        _session_handlers[session_id] = fh
        _session_paths[session_id] = path

    root.info("session_log_started session_id=%s path=%s", session_id, path)
    return session_id


def get_active_log_path(session_id: str | None = None) -> str | None:
    """Return the absolute path of the currently-open per-session log file."""
    with _session_lock:
        if session_id is not None:
            p = _session_paths.get(session_id)
            return str(p) if p else None
        if len(_session_paths) == 1:
            return str(next(iter(_session_paths.values())))
        return None


def stop_session_log(session_id: str) -> None:
    """Close and detach the per-session log handler. Safe if already closed."""
    with _session_lock:
        fh = _session_handlers.pop(session_id, None)
        _session_paths.pop(session_id, None)
    if fh is None:
        return
    try:
        logging.getLogger().info("session_log_stopped session_id=%s", session_id)
        fh.flush()
        logging.getLogger().removeHandler(fh)
        fh.close()
    except Exception as e:
        logging.getLogger().debug("stop_session_log(%s) tidy failed: %s", session_id, e)


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
