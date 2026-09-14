"""Logging and the error-code ring buffer.

Lifted verbatim from app.py's prelude, where every other section reached past
whatever feature banner it happened to sit under to get at it. `_log_error` is
the single funnel for failures the user never sees directly, and it is handed
to training_planner at import (`tp._set_log_error_hook`) -- so it has to be
importable without pulling FastAPI or any feature module in behind it.

Nothing here is new. The behaviour is byte-for-byte what app.py did; app.py
re-exports every name so the 60 test files that reach `app.X` keep working.
"""
import clock  # the one clock every module reads (see src/clock.py)
import collections
import logging
import threading
from datetime import datetime, timezone

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
            "ts": clock.now(timezone.utc).isoformat(),
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
