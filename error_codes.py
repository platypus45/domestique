"""Error-code taxonomy for Domestique (v1.6.0).

Every error path in the app emits a structured ``E_<domain>_<failure>``
code via ``_log_error`` so log scrapers can group + the diagnostics
endpoint can surface them. The ``Codes`` class holds string literals;
``REGISTRY`` is the metadata table consumed by ``/api/diag/health`` and
the Diagnostics modal in the dashboard.

Severity values:
    FATAL -- app cannot continue (process exit imminent)
    ERROR -- request failed; user-visible degraded state
    WARN  -- transient or recoverable; user may not notice
    INFO  -- informational; expected on first run / clean state

Adding a code:
    1. Add a constant on ``Codes``.
    2. Add a row in ``REGISTRY`` with severity + description + user_action.
    3. Reference it from at least one ``_log_error(Codes.X, ...)`` call site.
    The v1.6.0 test suite asserts every constant has a registry row and
    vice versa.
"""
from __future__ import annotations

from typing import TypedDict


class CodeMeta(TypedDict):
    severity: str
    description: str
    user_action: str


class Codes:
    # ---- plan / training_planner I/O ------------------------------------
    PLAN_PARSE_CORRUPT       = "E_PLAN_PARSE_CORRUPT"
    PLAN_PARSE_MISSING       = "E_PLAN_PARSE_MISSING"
    PLAN_LOAD_OS_ERROR       = "E_PLAN_LOAD_OS_ERROR"
    PLAN_GENERATE_FAILED     = "E_PLAN_GENERATE_FAILED"
    PLAN_REFORECAST_FAILED   = "E_PLAN_REFORECAST_FAILED"
    REFORECAST_DICT_TO_PW    = "E_REFORECAST_DICT_TO_PW"
    REFORECAST_TSB           = "E_REFORECAST_TSB"
    REFORECAST_AVAILABILITY  = "E_REFORECAST_AVAILABILITY"

    # ---- per-session enrichment -----------------------------------------
    ENRICH_FAILED            = "E_ENRICH_FAILED"
    ENRICH_LIBRARY           = "E_ENRICH_LIBRARY"
    ENRICH_CLASSIFICATION    = "E_ENRICH_CLASSIFICATION"
    ENRICH_PROPAGATE         = "E_ENRICH_PROPAGATE"
    ENRICH_CARD_STATE        = "E_ENRICH_CARD_STATE"

    # ---- cache wrappers --------------------------------------------------
    CACHE_TRAINING           = "E_CACHE_TRAINING"
    CACHE_SLEEP              = "E_CACHE_SLEEP"
    CACHE_WELLNESS           = "E_CACHE_WELLNESS"
    CACHE_GENERIC            = "E_CACHE_GENERIC"

    # ---- /api/calendar ---------------------------------------------------
    CALENDAR_MERGE           = "E_CALENDAR_MERGE"
    CALENDAR_ICU_SYNC        = "E_CALENDAR_ICU_SYNC"
    CALENDAR_RIDES_LOAD      = "E_CALENDAR_RIDES_LOAD"
    CALENDAR_LEGACY_RIDES    = "E_CALENDAR_LEGACY_RIDES"

    # ---- analytics / fitness --------------------------------------------
    AUGMENT_3D_FITNESS       = "E_AUGMENT_3D_FITNESS"
    READINESS_COMPUTE        = "E_READINESS_COMPUTE"

    # ---- ride parsing ----------------------------------------------------
    RIDE_PARSE_FIT           = "E_RIDE_PARSE_FIT"
    RIDE_PARSE_ICU           = "E_RIDE_PARSE_ICU"
    RIDE_PARSE_GENERIC       = "E_RIDE_PARSE_GENERIC"

    # ---- profile ---------------------------------------------------------
    PROFILE_LOAD             = "E_PROFILE_LOAD"

    # ---- diag self ------------------------------------------------------
    DIAG_HEALTH_CHECK        = "E_DIAG_HEALTH_CHECK"

    # ---- frontend (posted via /api/diag/frontend-error) ------------------
    FRONTEND_LOADHOME        = "E_FRONTEND_LOADHOME"
    FRONTEND_LOADCAL         = "E_FRONTEND_LOADCAL"
    FRONTEND_LOADPLAN        = "E_FRONTEND_LOADPLAN"
    FRONTEND_RENDER_PLAN     = "E_FRONTEND_RENDER_PLAN"
    FRONTEND_RENDER_CAL      = "E_FRONTEND_RENDER_CAL"
    FRONTEND_UNHANDLED       = "E_FRONTEND_UNHANDLED"
    FRONTEND_PROMISE_REJECT  = "E_FRONTEND_PROMISE_REJECT"
    FRONTEND_GENERIC         = "E_FRONTEND_GENERIC"


REGISTRY: dict[str, CodeMeta] = {
    Codes.PLAN_PARSE_CORRUPT: {
        "severity": "ERROR",
        "description": "current_plan.json failed to JSON-decode.",
        "user_action": "Regenerate the plan from the Plan tab.",
    },
    Codes.PLAN_PARSE_MISSING: {
        "severity": "INFO",
        "description": "No current_plan.json on disk yet (first run).",
        "user_action": "Generate a plan from the Plan tab.",
    },
    Codes.PLAN_LOAD_OS_ERROR: {
        "severity": "ERROR",
        "description": "OS error while reading current_plan.json (perms, disk).",
        "user_action": "Check ~/.domestique/plans/ permissions and disk health.",
    },
    Codes.PLAN_GENERATE_FAILED: {
        "severity": "ERROR",
        "description": "Plan generation failed inside training_planner.",
        "user_action": "Try regenerating; if persistent, check workout library.",
    },
    Codes.PLAN_REFORECAST_FAILED: {
        "severity": "ERROR",
        "description": "Plan reforecast (TSB/availability) failed.",
        "user_action": "Try regenerating the plan.",
    },
    Codes.REFORECAST_DICT_TO_PW: {
        "severity": "ERROR",
        "description": "_plan_dict_to_planned_weeks raised inside reforecast.",
        "user_action": "Check plan JSON shape; regenerate if needed.",
    },
    Codes.REFORECAST_TSB: {
        "severity": "WARN",
        "description": "TSB lookup raised inside reforecast.",
        "user_action": "Check wellness data freshness.",
    },
    Codes.REFORECAST_AVAILABILITY: {
        "severity": "WARN",
        "description": "Availability reflow raised; availability still saved.",
        "user_action": "Plan may not have been re-fitted; regenerate to refresh.",
    },
    Codes.ENRICH_FAILED: {
        "severity": "ERROR",
        "description": "_enrich_plan_for_response raised; cards may render without colours.",
        "user_action": "Regenerate plan; if persistent, content classifier may be stale.",
    },
    Codes.ENRICH_LIBRARY: {
        "severity": "WARN",
        "description": "Workout library load failed during enrichment; falling back to empty.",
        "user_action": "Check workouts/ folder is intact.",
    },
    Codes.ENRICH_CLASSIFICATION: {
        "severity": "WARN",
        "description": "Content classification load failed; falling back to filename rules.",
        "user_action": "Re-classify workouts; check workouts/.content_classification.json.",
    },
    Codes.ENRICH_PROPAGATE: {
        "severity": "WARN",
        "description": "Reforecast propagation step raised; sessions may keep stale fields.",
        "user_action": "Regenerate or reforecast plan.",
    },
    Codes.ENRICH_CARD_STATE: {
        "severity": "WARN",
        "description": "_classify_card_state_v2 raised on a session.",
        "user_action": "Check the session's day/zwo_file fields for malformed data.",
    },
    Codes.CACHE_TRAINING: {
        "severity": "WARN",
        "description": "training cache backing fn raised; returning {} for 30s.",
        "user_action": "Check ICU connectivity and credentials.",
    },
    Codes.CACHE_SLEEP: {
        "severity": "WARN",
        "description": "sleep cache backing fn raised; returning {} for 30s.",
        "user_action": "Check sleep-tracker integration.",
    },
    Codes.CACHE_WELLNESS: {
        "severity": "WARN",
        "description": "wellness cache backing fn raised; returning {} for 30s.",
        "user_action": "Check ~/.domestique/wellness/ accessibility.",
    },
    Codes.CACHE_GENERIC: {
        "severity": "WARN",
        "description": "Generic cached() fn raised; returning {} for 30s.",
        "user_action": "Check the cache key in the log context for the upstream system.",
    },
    Codes.CALENDAR_MERGE: {
        "severity": "ERROR",
        "description": "merge_plan_with_rides raised.",
        "user_action": "Check plan and ride archive integrity.",
    },
    Codes.CALENDAR_ICU_SYNC: {
        "severity": "WARN",
        "description": "Lazy ICU sync raised; calendar shows cached data.",
        "user_action": "Check ICU credentials in Settings.",
    },
    Codes.CALENDAR_RIDES_LOAD: {
        "severity": "WARN",
        "description": "_load_all_rides_safe raised; calendar shows planned only.",
        "user_action": "Check ~/.domestique/rides/ accessibility.",
    },
    Codes.CALENDAR_LEGACY_RIDES: {
        "severity": "WARN",
        "description": "Legacy ride_storage.list_rides raised.",
        "user_action": "Check ~/.domestique/profiles/<id>/rides/ for stale files.",
    },
    Codes.AUGMENT_3D_FITNESS: {
        "severity": "WARN",
        "description": "Banister 3D-fitness convolution raised.",
        "user_action": "Check wellness records for malformed dates/loads.",
    },
    Codes.READINESS_COMPUTE: {
        "severity": "WARN",
        "description": "Readiness composite computation raised; defaulting to '—'.",
        "user_action": "Check sleep + HRV data for current week.",
    },
    Codes.RIDE_PARSE_FIT: {
        "severity": "WARN",
        "description": "FIT file parse failed for one ride; ride is skipped.",
        "user_action": "Re-export the FIT file; check fit_tool warnings.",
    },
    Codes.RIDE_PARSE_ICU: {
        "severity": "WARN",
        "description": "ICU envelope JSON malformed for one ride.",
        "user_action": "Re-sync from intervals.icu.",
    },
    Codes.RIDE_PARSE_GENERIC: {
        "severity": "WARN",
        "description": "Generic ride parse raised; ride is skipped.",
        "user_action": "Inspect the ride file path in log context.",
    },
    Codes.PROFILE_LOAD: {
        "severity": "ERROR",
        "description": "Profile athlete.json load failed.",
        "user_action": "Check ~/.domestique/profiles/<id>/athlete.json.",
    },
    Codes.DIAG_HEALTH_CHECK: {
        "severity": "ERROR",
        "description": "Diagnostics health endpoint itself raised.",
        "user_action": "Inspect server log; this should not happen.",
    },
    Codes.FRONTEND_LOADHOME: {
        "severity": "ERROR",
        "description": "loadHome() threw in browser; home page may be partial.",
        "user_action": "Check browser console; reload page.",
    },
    Codes.FRONTEND_LOADCAL: {
        "severity": "ERROR",
        "description": "loadCalendar() threw in browser; calendar may not render.",
        "user_action": "Check browser console; reload page.",
    },
    Codes.FRONTEND_LOADPLAN: {
        "severity": "ERROR",
        "description": "loadPlan() threw in browser; Plan tab may be empty.",
        "user_action": "Check browser console; reload page.",
    },
    Codes.FRONTEND_RENDER_PLAN: {
        "severity": "ERROR",
        "description": "renderPlanJSON threw in browser; plan grid not painted.",
        "user_action": "Check browser console for the exception detail.",
    },
    Codes.FRONTEND_RENDER_CAL: {
        "severity": "ERROR",
        "description": "renderCalendar threw in browser; calendar not painted.",
        "user_action": "Check browser console for the exception detail.",
    },
    Codes.FRONTEND_UNHANDLED: {
        "severity": "ERROR",
        "description": "Uncaught JS error reached window.onerror.",
        "user_action": "Check browser console for full stack trace.",
    },
    Codes.FRONTEND_PROMISE_REJECT: {
        "severity": "ERROR",
        "description": "Unhandled promise rejection in browser.",
        "user_action": "Check browser console; usually a fetch() that wasn't awaited.",
    },
    Codes.FRONTEND_GENERIC: {
        "severity": "ERROR",
        "description": "Unknown frontend error code reported (coerced).",
        "user_action": "Check the original code in log context.",
    },
}


_VALID_SEVERITIES = frozenset({"FATAL", "ERROR", "WARN", "INFO"})


def is_valid_code(code: str) -> bool:
    """True iff ``code`` is a registered E_… code."""
    return isinstance(code, str) and code in REGISTRY


def all_codes() -> list[str]:
    """Sorted list of every registered code (stable order for tests)."""
    return sorted(REGISTRY.keys())


def metadata(code: str) -> CodeMeta | None:
    """Lookup a code's metadata, or None if unknown."""
    return REGISTRY.get(code)
