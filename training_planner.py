"""
Training Planner — evidence-based periodization engine.

Takes a goal + time budget → generates a full training plan with:
  - Phase structure (base → build → peak → taper)
  - Weekly TSS targets per phase
  - Specific workouts from the 2,459 ZWO library
  - Daily adaptation via readiness/HRV
  - Reforecasting when actual ≠ planned
  - Push to Intervals.icu calendar or local export

Research base: 60+ papers (see RESEARCH_TRAINING_PLANNER.md)

Usage:
  # Define a goal interactively
  python3 training_planner.py

  # Gran Fondo target
  python3 training_planner.py --goal event --event-date 2026-07-15 \\
    --event-km 150 --event-climb 4300 --hours-per-week 8

  # FTP target
  python3 training_planner.py --goal ftp --target-ftp 270 --target-date 2026-08-01

  # General improvement
  python3 training_planner.py --goal general --hours-per-week 8

  # Push to Intervals.icu
  python3 training_planner.py --goal event --event-date 2026-07-15 \\
    --event-km 150 --event-climb 4300 --push-icu

  # Reforecast (after deviations)
  python3 training_planner.py --reforecast
"""

import argparse
import hashlib
import json
import logging
import math
import shutil
import sys
import threading
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)

# v1.6.1 — observability hook. ``app.py``'s ``_log_error`` is registered via
# ``_set_log_error_hook`` immediately after ``import training_planner`` so
# planner-internal failures land in the same diag ring buffer as the rest
# of the app. The hook is optional: when running training_planner stand-
# alone (CLI / tests that don't import app), ``_tp_log_error`` falls back
# to stdlib logging so observability still gets a record. The indirection
# avoids a circular import (app -> tp -> app).
import error_codes  # leaf module — no circular risk
_LOG_ERROR_HOOK = None


def _set_log_error_hook(fn) -> None:
    """Register the app-level _log_error funnel for planner observability."""
    global _LOG_ERROR_HOOK
    _LOG_ERROR_HOOK = fn


def _tp_log_error(code: str, exc: Exception | None = None, **context) -> None:
    """Emit a structured E_<code> via the registered hook (or stdlib log).

    Always non-throwing — used inside except clauses.
    """
    try:
        if _LOG_ERROR_HOOK is not None:
            _LOG_ERROR_HOOK(code, exc=exc, **context)
            return
        # Fallback path — stdlib logger only. The diag ring is unreachable
        # without the hook, but the line still appears in the boot log.
        meta = error_codes.metadata(code)
        severity = (meta or {}).get("severity", "ERROR")
        ctx_repr = " ".join(f"{k}={v!r}" for k, v in context.items())
        if severity == "WARN":
            level = logging.WARNING
        elif severity == "INFO":
            level = logging.INFO
        else:
            level = logging.ERROR
        if exc is not None:
            log.log(level, "%s %s exc=%s:%s", code, ctx_repr,
                    type(exc).__name__, str(exc)[:300])
        else:
            log.log(level, "%s %s", code, ctx_repr)
    except Exception:
        pass


from contextlib import contextmanager
from dataclasses import dataclass, field, asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from training import get_today_metrics, fetch_wellness, fetch_activities
import config

# Workout library — flat directory of .zwo files (metadata extracted by parsing XML)
WORKOUT_DIR = Path(__file__).parent / "workouts"
# Allow user override via user_paths.json (matches app.py behavior)
for _upf in [Path.home() / ".domestique" / "user_paths.json",
             Path(__file__).parent / "user_paths.json"]:
    if _upf.exists():
        try:
            _up = json.loads(_upf.read_text(encoding="utf-8"))
            if _up.get("workout_dir"):
                WORKOUT_DIR = Path(_up["workout_dir"])
        except Exception:
            pass
        break
# Plans must be written to user data dir (not the read-only app bundle)
PLAN_DIR = Path.home() / ".domestique" / "plans"
# NOTE: PLAN_DIR creation deferred to first write so the v3 data-dir
# migration in profile_manager._maybe_migrate_data_dir can race-free
# detect a fresh install vs. a pre-existing legacy dir. Callers that
# write into PLAN_DIR should mkdir(parents=True, exist_ok=True) first.

# Cache for load_workout_library(): key=str(WORKOUT_DIR),
# value=((classifier_version, zwo_count, max_mtime), list_of_workout_dicts).
# classifier_version is a manual bump — increment it whenever _classify_protocol
# changes semantics so old cached entries don't survive across restarts. The
# in-process cache is keyed by (count, mtime) alone, so the version bump is
# only belt-and-braces vs a persisted cache, but it documents the invalidation
# intent unambiguously.
_CLASSIFIER_VERSION = 3  # v4.1.2 IMPL-CLASSIFIER: content-based 12-rule cascade replaces filename heuristic
# v1.10.1 SPEED-INDEX: consolidated on-disk row index (workouts/.library_index.json).
# Stores every row load_workout_library() builds, so a cold call can skip the
# 4,198-file XML sweep (~3s) and instead do one JSON read (~0.2s). The on-disk
# header pins (schema_version, classifier_version, count, max_mtime) — the SAME
# (count, max .zwo mtime) signal the in-process slow-path cache uses, which is
# immune to the index file's own writes (a dotfile, not a *.zwo). The loader
# trusts the index only when classifier_version + count + max_mtime all match the
# live workouts dir, and falls back to the XML parse (self-healing: it rewrites
# the index) on any mismatch. The builder lives in
# scripts/classify_library_content.py (run via --all). Bump _INDEX_SCHEMA_VERSION
# whenever the row shape changes so stale on-disk indexes are rejected.
_INDEX_SCHEMA_VERSION = 2  # v2.0.6: ramp zone-integration fix changed stored Z% data → invalidate cached indexes
_LIBRARY_INDEX_FILENAME = ".library_index.json"
_WORKOUT_LIB_CACHE: dict[str, tuple] = {}
# v1.8.1 SPEED-A: fast hot-path validator keyed by str(WORKOUT_DIR).
# Stores (dir_mtime, classifier_version) — a single os.stat(WORKOUT_DIR)
# replaces the per-file glob+stat sweep on cache-hit. Directory mtime changes
# when files are added/removed/renamed (covers the library-edit case); pure
# in-place content edits to an existing file are not detected by this fast
# check, but the underlying library is treated as append-only in normal use.
# A persistent change to library shape still invalidates via the slow-path
# (zwo_count, max_mtime) tuple stored in _WORKOUT_LIB_CACHE.
_WORKOUT_LIB_FAST_VALIDATOR: dict[str, tuple] = {}

# Cache for the content-based classifier output
# (workouts/.content_classification.json, produced by
#  scripts/classify_library_content.py). Populated lazily on first use.
# Maps basename → {primary, confidence, secondary_flags, features}.
_CONTENT_CLASSIFICATION_CACHE: dict[str, dict] | None = None
_CONTENT_CLASSIFICATION_HASH: str | None = None
# Mapping from content-classifier primary → existing Protocol enum strings.
# vo2_short maps to VO2max; secondary_flags carry the sub-type info.
_CONTENT_TO_PROTOCOL = {
    "recovery": "Recovery",
    "endurance": "Endurance",
    "endurance_intervals": "Endurance + Strides",
    "tempo": "Tempo",
    "tempo_intervals": "Tempo Intervals",
    "tempo_ladder": "Tempo Ladder",
    "sweet_spot": "Sweet Spot",
    "sweet_spot_ladder": "Sweet Spot Ladder",
    "threshold": "Threshold",
    "threshold_ladder": "Threshold Ladder",
    "over_under": "Over-Unders",
    "vo2max": "VO2max",
    "vo2_ladder": "VO2 Ladder",
    "vo2_short": "VO2max",
    "anaerobic": "Anaerobic",
    "neuromuscular": "Sprint",
    "ftp_test": "FTP Test",
}


def _load_content_classifications() -> dict[str, dict]:
    """Lazy-load the content-classification cache produced by
    ``scripts/classify_library_content.py``. Returns {} if the cache is
    missing — in that case ``_classify_protocol`` falls back to the
    filename-based heuristic without complaint. Logs a one-shot warning so
    the user knows to run the script after a workout-library change.
    """
    global _CONTENT_CLASSIFICATION_CACHE
    if _CONTENT_CLASSIFICATION_CACHE is not None:
        return _CONTENT_CLASSIFICATION_CACHE
    cache_path = WORKOUT_DIR / ".content_classification.json"
    if not cache_path.exists():
        log.warning(
            "content_classification cache missing — run "
            "`python3 scripts/classify_library_content.py --all` to enable "
            "content-based protocol classification (falling back to "
            "filename heuristic for now)"
        )
        _CONTENT_CLASSIFICATION_CACHE = {}
        return _CONTENT_CLASSIFICATION_CACHE
    try:
        with cache_path.open(encoding="utf-8") as f:
            payload = json.load(f)
        # Compare workouts dir hash; if drifted, log a warning but still use
        # what we have (the planner shouldn't auto-run a 30-second classifier
        # pass on every boot — the user must rerun explicitly).
        try:
            current_hash = _compute_workouts_dir_hash()
            cached_hash = payload.get("workouts_dir_hash")
            if cached_hash and cached_hash != current_hash:
                log.warning(
                    "content_classification cache stale (workouts dir has "
                    "changed since last classification) — rerun "
                    "`python3 scripts/classify_library_content.py --all`"
                )
        except Exception:
            pass
        _CONTENT_CLASSIFICATION_CACHE = payload.get("classifications", {})
    except (OSError, json.JSONDecodeError) as e:
        log.warning("content_classification cache load failed: %s", e)
        _CONTENT_CLASSIFICATION_CACHE = {}
    return _CONTENT_CLASSIFICATION_CACHE


def _compute_workouts_dir_hash() -> str:
    """SHA-256 over (filename, mtime) tuples for *.zwo in WORKOUT_DIR."""
    h = hashlib.sha256()
    if not WORKOUT_DIR.exists():
        return ""
    for p in sorted(WORKOUT_DIR.glob("*.zwo")):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0
        h.update(f"{p.name}:{mtime}\n".encode())
    return h.hexdigest()


def _read_library_index(count: int, max_mtime: float) -> list[dict] | None:
    """v1.10.1 SPEED-INDEX: try the consolidated on-disk row index.

    Returns the list of pre-parsed library rows iff
    ``workouts/.library_index.json`` exists AND its header
    (schema_version, classifier_version, count, max_mtime) matches the live
    workouts dir. Returns ``None`` on any miss (absent / stale / malformed) so
    the caller falls back to the XML-parse path. ``count`` (number of *.zwo)
    and ``max_mtime`` (newest *.zwo mtime) are the SAME slow-path validators
    ``load_workout_library`` already computes for its in-process cache, so the
    on-disk index invalidates on exactly the same events (add/remove/rename/
    in-place edit of any *.zwo bumps one of the two). Crucially this signal is
    immune to writing the index file itself — it is a dotfile, not a *.zwo, so
    self-healing the index never invalidates it.

    The rows are returned verbatim from JSON. They are byte-identical in
    content to the XML-parse path because the builder serializes that path's
    own output; JSON round-trips every field cleanly (Tags=list[str],
    SecondaryFlags=dict, all other fields str/int/float).
    """
    index_path = WORKOUT_DIR / _LIBRARY_INDEX_FILENAME
    if not index_path.exists():
        return None
    try:
        with index_path.open(encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        log.warning("library_index load failed (%s) — falling back to XML parse", e)
        return None
    if (
        payload.get("schema_version") != _INDEX_SCHEMA_VERSION
        or payload.get("classifier_version") != _CLASSIFIER_VERSION
        or payload.get("count") != count
        or payload.get("max_mtime") != max_mtime
    ):
        # Stale (library changed since the index was built) or wrong schema.
        # Silent — the XML path will rebuild and self-heal the index.
        return None
    rows = payload.get("rows")
    if not isinstance(rows, list):
        return None
    return rows


def _write_library_index(rows: list[dict], count: int, max_mtime: float) -> None:
    """v1.10.1 SPEED-INDEX: persist ``rows`` to workouts/.library_index.json.

    Called after a full XML parse so the index self-heals when missing/stale.
    Best-effort: a read-only workouts dir (e.g. inside the notarized app
    bundle) just means the next cold call re-parses — never fatal. Written via
    a tmp file + atomic rename so a concurrent reader never sees a half file.
    """
    index_path = WORKOUT_DIR / _LIBRARY_INDEX_FILENAME
    payload = {
        "schema_version": _INDEX_SCHEMA_VERSION,
        "classifier_version": _CLASSIFIER_VERSION,
        "count": count,
        "max_mtime": max_mtime,
        "rows": rows,
    }
    try:
        tmp_path = index_path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f)
        tmp_path.replace(index_path)
    except OSError as e:
        log.debug("library_index write skipped (%s)", e)


# ── Plan-write serialization (PL3) ───────────────────────────────────────────
# Six FastAPI endpoints in app.py all write current_plan.json via the
# `tmp_path = json_path.with_suffix('.tmp')` → `tmp_path.rename(json_path)`
# pattern. Without serialization, concurrent daily-adapt + auto-recalc can
# silently drop adaptations (the last rename wins). Endpoints use
# `with training_planner.plan_write_lock(): ...` around the tmp-write + rename.
# Helper `atomic_write_plan(json_path, plan)` wraps the full write for callers
# who don't want to manage the tmp path themselves.
#
# v1.6.2 — ``_plan_write_lock`` is an ``RLock`` so callers already inside
# ``plan_write_lock()`` can safely call ``atomic_write_plan`` without
# deadlocking.
_plan_write_lock = threading.RLock()

PLAN_BACKUP_DEPTH = 7  # v1.6.2: keep .bak through .bak7 (7 snapshots).


@contextmanager
def plan_write_lock():
    """Context manager over the module-level plan-write lock.

    Use around paired tmp-write + rename (or any atomic plan mutation) to
    serialize writes across the plan endpoints in app.py.
    """
    with _plan_write_lock:
        yield


def _rotate_plan_backups(plan_path: "Path") -> None:
    """v1.6.2 — shift backups down: .bak6→.bak7, ..., .bak→.bak2, then live→.bak.

    Best-effort: per-file failures (perm, disk) are logged and swallowed so
    the upcoming write still proceeds. The live file is COPIED (not moved)
    to .bak so the about-to-be-replaced file stays intact until the atomic
    rename swaps it. Caller MUST hold ``_plan_write_lock``.
    """
    p = Path(plan_path)
    # Drop the oldest if at depth (unlink only used here — guarded by tests).
    oldest = p.with_suffix(p.suffix + f".bak{PLAN_BACKUP_DEPTH}")
    if oldest.exists():
        try:
            oldest.unlink()
        except OSError as e:
            log.debug(f"_rotate_plan_backups: drop oldest failed: {e}")
    # Shift bak{n} → bak{n+1}, downward from N-1 to 1.
    for n in range(PLAN_BACKUP_DEPTH - 1, 0, -1):
        src = p.with_suffix(p.suffix + (f".bak{n}" if n > 1 else ".bak"))
        dst = p.with_suffix(p.suffix + f".bak{n+1}")
        if src.exists():
            try:
                src.replace(dst)
            except OSError as e:
                log.debug(f"_rotate_plan_backups: shift {src.name}→{dst.name} failed: {e}")
    # Snapshot live → .bak (copy so the original remains for the atomic
    # rename of the new tmp file to overwrite).
    bak = p.with_suffix(p.suffix + ".bak")
    try:
        shutil.copy2(p, bak)
    except OSError as e:
        log.debug(f"_rotate_plan_backups: copy live→.bak failed: {e}")


def atomic_write_plan(json_path: "Path | str", plan: dict) -> None:
    """Atomically write ``plan`` to ``json_path`` under the plan-write lock.

    v1.6.2:
      - Refuses to write an empty / non-dict plan (guards the bug where a
        mutation produced ``{}`` or ``None`` and silently nuked the live file).
      - Rotates existing live → .bak → .bak2 ... → .bak7 BEFORE writing, so
        a crash mid-write still leaves at least 7 prior snapshots intact.
      - Writes to `<json_path>.tmp` and atomic-renames on success. If the
        write or rotation raises, the live file is unchanged.
    """
    if not isinstance(plan, dict) or not plan:
        raise ValueError(
            "atomic_write_plan: refusing to write empty / non-dict plan "
            f"(got {type(plan).__name__})"
        )
    p = Path(json_path)
    tmp = p.with_suffix('.tmp')
    with _plan_write_lock:
        if p.exists():
            _rotate_plan_backups(p)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(plan, f, indent=2, default=str)
        tmp.replace(p)


# v4.1.1 FIX-PLANNER A: auto-rewrite stale-classified sessions in a stored plan.
# Bug A cause: _classify_protocol missed six prefix families (vo2_, over_under_,
# sprints_, anaerobic_, sweet_spot_, pyramid_), so ~30% of sessions ended up
# with session_type≠zwo_file prefix (e.g. type=tempo + zwo=vo2_…zwo). After
# extending the classifier we still need to REWRITE plans that were saved
# before the fix — users won't regen manually. This walks the plan and
# re-matches any session whose zwo_file's actual category disagrees with its
# session_type, using the planner's normal match_zwo path. Called from app.py
# at startup (best-effort — failures never block boot).
_SESSION_TYPE_PREFIXES = {
    # session_type → acceptable zwo filename prefixes for the boot-time
    # staleness check. STRICT for intervals (tempo / vo2max / sweetspot /
    # overunder / sprint) because the user's visible symptom is "I clicked
    # tempo but got a VO2 ZWO" — we want those rewritten. RELAXED for
    # easy-endurance types (z2/long_z2/recovery) where match_zwo's
    # explicit fallback_cats legitimately surfaces recovery_/endurance_
    # swaps when no exact-category file fits the duration bucket (e.g. a
    # 90-min z2 slot with only recovery_spin at that duration).
    "vo2max":    ("vo2max_", "vo2_"),
    "threshold": ("threshold_", "supra_threshold"),
    "sweetspot": ("sweetspot_", "sweet_spot_"),
    "tempo":     ("tempo_",),
    "recovery":  ("recovery_", "warmup_", "z2_", "endurance_"),
    "z2":        ("z2_", "endurance_", "recovery_"),
    "long_z2":   ("z2_", "endurance_", "recovery_"),
    "overunder": ("over_under_", "supra_threshold"),
    "sprint":    ("sprints_",),
    "ftp_test":  ("ftp_test_",),
}


def _session_is_stale(session_type: str, zwo_file: str,
                      library_files: "set[str] | None" = None) -> bool:
    """Return True if this session's ``zwo_file`` must be re-matched.

    v1.8.18 (grill B2) — the staleness test is now **resolves-on-disk**, NOT
    the old session_type↔prefix heuristic. The prefix test was unreliable:
    ``match_zwo`` legitimately resolves a sweetspot session to an
    ``over_under_*`` file via its fallback categories, which FAILS a prefix
    check and re-heals forever (infinite churn). Existence is the only sound
    signal. A session is stale iff its ``zwo_file`` is non-empty / non-rest AND
    either contains a path separator (an external scrape slug like
    ``ftp-builder/week-6-day-3.zwo``) OR its basename is not in the local flat
    library. When ``library_files`` is None we can't check existence → treat as
    not-stale (don't touch).
    """
    if not zwo_file or session_type == "rest":
        return False
    if library_files is None:
        return False
    if "/" in zwo_file or "\\" in zwo_file:
        return True  # external subdir slug — never a local flat file
    import os as _os
    return _os.path.basename(zwo_file) not in library_files


def rewrite_stale_plan_classifications(plan_path: "Path | str") -> int:
    """Rewrite stale classifications in an existing stored plan (best-effort).

    Returns the count of sessions rewritten. No-ops if the plan is absent or
    malformed. On any exception, logs and returns 0 — never raises. Only
    sessions with session_type≠zwo_prefix are touched; user_moved and
    done/dismissed flags are preserved.
    """
    try:
        p = Path(plan_path)
        if not p.exists():
            return 0
        with open(p, encoding="utf-8") as f:
            plan = json.load(f)
        weeks = plan.get("weeks", [])
        if not weeks:
            return 0
        library = load_workout_library()
        if not library:
            return 0
        # v1.8.18 (grill B2) — set of real local library basenames for the
        # resolves-on-disk staleness test.
        library_files = {row.get("File") for row in library if row.get("File")}
        # v1.8.18 (grill B1) — the seed anchor MUST be the plan's stable birth
        # date so match_zwo is deterministic across launches. The live plan key
        # is ``generated`` (the old code read ``generated_at`` → always fell to
        # date.today() → the seed drifted daily → every re-matched session
        # re-rolled to a different file on each launch). Read both keys.
        plan_start = None
        for k in ("generated", "generated_at"):
            v = plan.get(k)
            if v:
                try:
                    plan_start = date.fromisoformat(str(v)[:10])
                    break
                except Exception:
                    pass
        if plan_start is None:
            plan_start = date.today()
        # v1.8.18 (grill B7) — FREEZE THE PAST. Healing a session dated before
        # today would silently rewrite the user's training history (what was
        # planned/done on a past day). Only future sessions are re-matched.
        today = date.today()

        rewritten = 0
        # Rolling used_names window — mirrors generate_plan's sliding-window
        # dedupe so re-matches don't collide on a workout already placed.
        used_names: set[str] = set()
        wrote_backup = False
        for w_json in weeks:
            week_num = w_json.get("week_num", 1)
            for idx, s_json in enumerate(w_json.get("sessions", [])):
                st = s_json.get("session_type") or ""
                zwo = s_json.get("zwo_file") or ""
                # Seed dedupe from every session (incl. frozen past + already-
                # valid) so future heals don't reuse an existing file's name.
                if not _session_is_stale(st, zwo, library_files):
                    if s_json.get("zwo_name"):
                        used_names.add(s_json["zwo_name"])
                    continue
                # Freeze the past: never mutate a session before today.
                day_str = s_json.get("day") or ""
                try:
                    if day_str and date.fromisoformat(day_str[:10]) < today:
                        if s_json.get("zwo_name"):
                            used_names.add(s_json["zwo_name"])
                        continue
                except Exception:
                    pass  # unparseable day → treat as future, allow heal
                # Re-match this future, unresolvable session.
                try:
                    ps = PlannedSession(
                        day=date.fromisoformat(day_str[:10]) if day_str else plan_start,
                        day_name=s_json.get("day_name", ""),
                        session_type=st,
                        duration_min=int(s_json.get("duration_min") or 0),
                        tss_estimate=float(s_json.get("tss_estimate") or 0),
                        description=s_json.get("description") or "",
                    )
                    match_zwo(
                        ps, library,
                        week_num=week_num, day_idx=idx,
                        used_names=used_names,
                        plan_start_date=plan_start,
                    )
                    new_zwo = getattr(ps, "zwo_file", "") or ""
                    # One-time pre-migration snapshot before the FIRST mutation,
                    # named so it survives the 7-deep .bak rotation (grill B5).
                    if not wrote_backup:
                        snap = p.with_suffix(p.suffix + ".premigration-v1818")
                        if not snap.exists():
                            try:
                                import shutil as _sh
                                _sh.copy2(p, snap)
                            except Exception:
                                log.debug("premigration snapshot failed", exc_info=True)
                        wrote_backup = True
                    if new_zwo and new_zwo in library_files and new_zwo != zwo:
                        s_json["zwo_file"] = new_zwo
                        s_json["zwo_name"] = getattr(ps, "zwo_name", "") or s_json.get("zwo_name", "")
                        if getattr(ps, "description", None):
                            s_json["description"] = ps.description
                        rewritten += 1
                    elif not new_zwo:
                        # No candidate found → clear the ghost to an honest empty
                        # (a synthesised session) so it stops 404-ing + is no
                        # longer stale (idempotent).
                        if zwo:
                            s_json["zwo_file"] = ""
                            rewritten += 1
                except Exception:
                    log.debug("rewrite_stale: session skip", exc_info=True)
                if s_json.get("zwo_name"):
                    used_names.add(s_json["zwo_name"])

        if rewritten > 0:
            atomic_write_plan(p, plan)
        return rewritten
    except Exception:
        log.debug("rewrite_stale_plan_classifications failed", exc_info=True)
        return 0


# ── Intensity ladder (PL1 / PL4) ─────────────────────────────────────────────
# One-step de-escalation applied when TSB is deeply negative or actuals show
# the athlete is running out of road. Ordering matches Seiler's HIT taxonomy:
# VO2max → threshold → over-under → sweetspot → tempo → endurance → recovery.
# v1.8.3 — add `sprint` at the top of the ladder. Sprint / neuromuscular
# sessions ARE in `_HARD_SESSION_TYPES` (so the tier-down candidate
# filter accepts them) but pre-v1.8.3 the ladder didn't include them,
# so `_drop_intensity("sprint")` returned "sprint" unchanged → the
# auto-adjust week walker silently skipped the session and reported
# `actions=[]`. Sprint is the highest-intensity bucket in Seiler's
# polarized model; one-step drop goes to vo2max.
_INTENSITY_LADDER = (
    "sprint", "vo2max", "threshold", "overunder", "sweetspot",
    "tempo", "z2", "long_z2", "recovery",
)


def _drop_intensity(level: str) -> str:
    """Return the next-easier session type in the Seiler-style ladder.

    Unknown session types (rest, ftp_test) pass through unchanged.
    Already-at-the-bottom recovery stays at recovery.
    """
    try:
        i = _INTENSITY_LADDER.index(level)
    except ValueError:
        return level  # unknown (rest, ftp_test) — no-op
    return _INTENSITY_LADDER[min(i + 1, len(_INTENSITY_LADDER) - 1)]


# v4.6.6 IMPL-B INJURY-GATES helpers — signatures locked in MASTER_DECISIONS §4.

def _hooper_index_today() -> int:
    """G6 input — Hooper composite (sleep+fatigue+stress+soreness, 1-7 each).
    Hooper & Mackinnon 1995 — index >=18 = significant accumulated fatigue.
    Returns 0 when daily_log is missing/incomplete (safe default).

    v4.6.6 WAVE-4-FIX: polarity matches db.py:583 + dashboard form (1=best,
    7=worst for ALL fields including sleep_quality). Direct sum, no inversion.
    Previously inverted sleep_quality via `8 - sleep_q`, producing hooper=12
    for the "well-slept but stressed" tuple (sleep=7,fat=3,str=4,sor=4) when
    UI/db both compute 18 → planner missed the gate. Single source of truth.
    """
    try:
        import db as _db
        log_row = _db.get_daily_log_today()
    except Exception:  # noqa: BLE001
        return 0
    if not log_row:
        return 0
    # Prefer the persisted hooper_index column (canonical, written by
    # db.upsert_daily_log) — single source of truth across UI/db/planner.
    persisted = log_row.get("hooper_index")
    if isinstance(persisted, int) and 4 <= persisted <= 28:
        return persisted
    sleep_q = log_row.get("sleep_quality")
    fatigue = log_row.get("fatigue")
    stress = log_row.get("stress")
    soreness = log_row.get("soreness")
    if None in (sleep_q, fatigue, stress, soreness):
        return 0
    return int(sleep_q) + int(fatigue) + int(stress) + int(soreness)


def _last_48h_z5plus_min(rides: list[dict]) -> float:
    """G2 input — rolling 48h sum of minutes in Z5/Z6/Z7 across all sports.
    Hulin 2014 — >=25min/48h forces today -> Z2 (cycling INCLUDED in v4.6.6).
    """
    if not rides:
        return 0.0
    cutoff = datetime.now() - timedelta(hours=48)
    total_seconds = 0.0
    for r in rides:
        start_str = r.get("start_date_local") or r.get("date") or ""
        if not start_str:
            continue
        try:
            if "T" in start_str:
                dt = datetime.fromisoformat(start_str.replace("Z", "+00:00").split("+")[0])
            else:
                dt = datetime.fromisoformat(start_str + "T00:00:00")
        except ValueError:
            continue
        if dt < cutoff:
            continue
        tiz = r.get("time_in_zone")
        if isinstance(tiz, dict) and tiz:
            total_seconds += float(
                (tiz.get("z5") or 0) + (tiz.get("z6") or 0) + (tiz.get("z7") or 0)
            )
            continue
        raw = {}
        rj = r.get("raw_json")
        if isinstance(rj, str) and rj:
            try:
                raw = json.loads(rj)
            except (json.JSONDecodeError, TypeError):
                raw = {}
        elif isinstance(rj, dict):
            raw = rj
        zp = (raw.get("zones") or {}).get("power") or {}
        if zp:
            total_seconds += float(
                (zp.get("Z5") or 0) + (zp.get("Z6") or 0) + (zp.get("Z7") or 0)
            )
            continue
        hr_zones = raw.get("icu_hr_zone_times") or []
        if isinstance(hr_zones, list) and len(hr_zones) >= 5:
            total_seconds += float(sum(hr_zones[4:]))
    return total_seconds / 60.0


def _last_3d_mean_feel(rides: list[dict]) -> float | None:
    """G7 input — mean of `feel`/`perceived_exertion` over last 3 days.
    Foster 1998 session-RPE. Returns None when no signal exists in window.
    `feel` (ICU 1-5) rescaled to 1-10 axis via *2.
    """
    if not rides:
        return None
    today = date.today()
    cutoff_iso = (today - timedelta(days=3)).isoformat()
    samples: list[float] = []
    for r in rides:
        d = r.get("date") or ""
        if not d or d < cutoff_iso:
            continue
        feel = r.get("feel")
        rpe = r.get("perceived_exertion")
        if feel is None and rpe is None:
            raw = {}
            rj = r.get("raw_json")
            if isinstance(rj, str) and rj:
                try:
                    raw = json.loads(rj)
                except (json.JSONDecodeError, TypeError):
                    raw = {}
            elif isinstance(rj, dict):
                raw = rj
            feel = raw.get("feel") if feel is None else feel
            rpe = raw.get("perceivedExertion") if rpe is None else rpe
        per_ride: list[float] = []
        if feel is not None:
            try:
                per_ride.append(float(feel) * 2.0)
            except (TypeError, ValueError):
                pass
        if rpe is not None:
            try:
                per_ride.append(float(rpe))
            except (TypeError, ValueError):
                pass
        if per_ride:
            samples.append(sum(per_ride) / len(per_ride))
    if not samples:
        return None
    return sum(samples) / len(samples)


def _polarization_breach(actual_pol: dict | None, target_pol: dict | None) -> bool:
    """G3 input — Seiler 2010 / Stöggl 2014 / Treff 2019.
    Breach when actual.z4plus_pct > target+8 OR actual.z1z2_pct < target-10.
    Empty inputs -> False (safe default).
    """
    if not actual_pol or not target_pol:
        return False
    try:
        a_z4 = int(actual_pol.get("z4plus_pct") or 0)
        t_z4 = int(target_pol.get("z4plus_pct") or 0)
        a_z12 = int(actual_pol.get("z1z2_pct") or 0)
        t_z12 = int(target_pol.get("z1z2_pct") or 0)
    except (TypeError, ValueError):
        return False
    if a_z4 > t_z4 + 8:
        return True
    if a_z12 < t_z12 - 10:
        return True
    return False


# ── Constants from research ───────────────────────────────────────────────────

# Phase durations (weeks) — adjustable based on available time
MIN_BASE_WEEKS   = 4
MIN_BUILD_WEEKS  = 4
MIN_PEAK_WEEKS   = 2
TAPER_DAYS       = 12    # Mujika 2003: 8-14 days optimal
STEP_BACK_EVERY  = 4     # Rønnestad: 3 load + 1 recovery
# v2.1.0 (E1) — acute:chronic workload upper bound (Gabbett 2016: sweet spot
# 0.8-1.3, >1.5 doubles injury risk). Caps the generation-time weekly volume
# at ≤1.3× the rider's recent mean weekly TSS so a fresh plan ramps from real
# recent load rather than from the sum of daily availability.
ACWR_CEILING     = 1.3
# v2.1.0 (F4) — no HARD session in the final N days before an A event. A taper
# keeps SOME intensity earlier (Mujika), but VO2max/threshold intervals on the
# event eve leave the legs flat — the last days must be easy openers. The day-3+
# sharpener is still allowed (only days within this window are demoted).
# v2.2.14 — final light/opener days before the A race. Bumped 2→3: the 12-day
# taper phase already cuts volume (intensity kept, Mujika/Bosquet), and the
# evidence (PLOS 2023 review; Mujika) is that the last ~2-3 days should be LIGHT
# (short easy + openers), never complete rest — so the legs are fresh but not
# detrained. _demote_hit_window keeps these days easy without zeroing them.
EVENT_EVE_EASY_DAYS = 3
# v1.9.2 — sanity ceiling for an availability-driven single session. The
# availability calendar applies the user's per-day hours literally (bidirectional),
# but caps here so a typo/extreme value (e.g. 10h) can't create an absurd session
# the workout library can't serve. 6h covers real long endurance / gran-fondo rides.
MAX_AVAIL_SESSION_MIN = 360

# CTL ramp rates (CTL points/week)
RAMP_CONSERVATIVE = 3
RAMP_MODERATE     = 5
RAMP_AGGRESSIVE   = 7

# TSS per hour by session type (for budget calculations)
TSS_PER_HOUR = {
    "recovery":  30,
    "z2":        45,
    "tempo":     65,
    "sweetspot":  80,
    "threshold":  90,
    "vo2max":     75,
    "overunder":  85,
    "sprint":    57,  # v2.0.6: neuromuscular = max efforts with FULL recovery →
                      # moderate aggregate load (IF ~0.75). Was 95 (≈IF 0.97),
                      # which sized 90-min sprint slots at ~142 TSS — a target only
                      # the IF>0.82-mislabeled "neuromuscular" files could hit. Now
                      # matches what the _SPRINT_SLOT_IF_CEILING (0.82) pool delivers.
    # v1.1.0 IMPL-NORWEGIAN-HR: AM+PM sub-LT2 threshold pair. Per-half ~85
    # (slightly under threshold because the HR ceiling caps glycolytic load
    # — Stöggl & Sperlich 2014). Total day = 2× this when both halves run.
    "double_threshold": 85,
}

# ── SAFETY: per-session-type duration ceiling (planner FIX-2) ──────────────────
# A library file's full duration is clamped to the DAY's available minutes by
# the sampler, but a 2 h weekday slot would still let a VO2max file render as a
# 120-min session ("VO2 every day" safety bug). Each hard type has a
# physiologically sane maximum total session length; the sampler clamps to
# min(day_cap, ceiling) and scales TSS proportionally. Endurance types
# (z2/long_z2/recovery) have no ceiling — they are intentionally day-capped.
TYPE_CEILING = {
    "vo2max":    75,
    "vo2_short": 60,
    "anaerobic": 50,
    "neuromuscular": 45,
    "sprint":    45,
    "overunder": 90,
    "threshold": 90,
    "sweetspot": 120,
    "tempo":     120,
}

# ── v2.0.6: sprint/neuromuscular LOAD ceiling ─────────────────────────────────
# The content classifier tags by STRUCTURE (many short max-effort segments, high
# peak watts, micro-interval pattern) and ignores aggregate load — so ~29% of the
# files it calls "neuromuscular" are really threshold/anaerobic by IF: short
# recovery (16–30s) between efforts keeps average power near threshold (IF
# 0.86–1.04). A genuine neuromuscular/sprint session is LOW-IF — maximal efforts
# with FULL recovery so each rep is quality, not a sustained grind. Reject
# over-cooked candidates from sprint slots so a 90-min slot can't render as a
# ~140-TSS threshold day mislabeled "neuromuscular". Files at IF ≤ this stay
# eligible (≈200 of 283 'neuromuscular' files); the ≈84 above it are excluded
# from sprint slots (they remain available to their proper vo2/threshold slots).
_SPRINT_SLOT_IF_CEILING = 0.82

# ── B5: easy-slot LOAD ceiling ────────────────────────────────────────────────
# The content classifier (and the filename fallback) tag by STRUCTURE, so an
# interval-structured file named "Endurance 20s/2min 6x" lands in the endurance
# class despite short max-effort bursts that push its IF to 0.81–0.84. The
# easy-slot Z3-6 TIME-% gate misses these (spiky efforts raise IF, not zone
# time), so a Z2/recovery slot could match a 196-TSS interval file (the tester's
# bug). Genuine easy files cap at IF ≈ 0.71; reject anything above this ceiling
# from z2 / long_z2 / recovery slots. Mirrors _SPRINT_SLOT_IF_CEILING.
_EASY_SLOT_IF_CEILING = 0.78

# ── v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE) ──────────────────────
# Parallel rough estimates for W'-load (kJ above CP) and Pmax-load (kJ PCr)
# per hour, by session type. Values are advisory ONLY: when consumed they
# augment the TSS-driven path, never replace it.
WPRIME_PER_HOUR = {
    "recovery":   0,
    "z2":         0,
    "tempo":      2,
    "sweetspot":  5,
    "threshold": 12,
    "vo2max":    50,
    "overunder": 35,
    "sprint":    25,
}

PMAX_PER_HOUR = {
    "recovery":   0,
    "z2":         0,
    "tempo":      0,
    "sweetspot":  1,
    "threshold":  2,
    "vo2max":     8,
    "overunder":  6,
    "sprint":    35,
}

# CTL needed for events (from CTS/TrainingPeaks data)
EVENT_CTL_TARGETS = {
    # Cycling events
    "century":      {"min": 50, "strong": 70,  "competitive": 90},
    "granfondo":    {"min": 70, "strong": 85,  "competitive": 100},
    "ultra":        {"min": 90, "strong": 110, "competitive": 130},
    "crit":         {"min": 40, "strong": 60,  "competitive": 80},
    "sportive":     {"min": 50, "strong": 70,  "competitive": 85},
}

# ── v4.6.7 IMPL-CAP: Capability projection constants ────────────────────────
#
# Allen & Coggan, *Training and Racing with a Power Meter* 3rd ed. (2019),
# Table 7.4 — sustainable Intensity Factor (IF = NP / FTP) by event duration.
# A 1h all-out effort sits on FTP by definition; 4h granfondo riders sustain
# 0.75; 12h ultra is gut-of-the-curve at 0.62.
AC_IF_BY_DURATION: list[tuple[int, float]] = [
    (60,  0.95),
    (120, 0.85),
    (180, 0.80),
    (300, 0.75),
    (480, 0.70),
    (720, 0.62),
]

# Pinot & Grappe (2011) *Int J Sports Med* 32:839-844 — Record Power Profile
# (RPP) by duration tier. Sustainable W/kg at the upper bound of the
# "amateur trained" category (Table 2 90th percentile).
PG_RPP_W_PER_KG: list[tuple[int, float]] = [
    (5,    7.5),   # 5s sprint
    (60,   5.5),   # 1min
    (300,  4.5),   # 5min (VO2max)
    (1200, 3.7),   # 20min (sustained climb)
    (3600, 3.2),   # 60min (≈FTP for trained amateur)
]

# 1h of climb at 100m elevation gain ≈ 1.5km of flat-equivalent road
# distance (Bassett & Howley 2000, *Med Sci Sports Exerc* 32:70-84).
CLIMB_TO_FLAT_KM_PER_100M: float = 1.5

# Default cruising speed (km/h) for an "average" granfondo on rolling terrain
# at the IF derived from AC_IF_BY_DURATION.
DEFAULT_CRUISING_KMH: float = 28.0


def _interp_if_by_duration(duration_min: float) -> float:
    """Linear interpolation on AC_IF_BY_DURATION (Allen & Coggan TR&P 3rd ed.)."""
    if duration_min <= AC_IF_BY_DURATION[0][0]:
        return AC_IF_BY_DURATION[0][1]
    if duration_min >= AC_IF_BY_DURATION[-1][0]:
        return AC_IF_BY_DURATION[-1][1]
    for i in range(len(AC_IF_BY_DURATION) - 1):
        d0, if0 = AC_IF_BY_DURATION[i]
        d1, if1 = AC_IF_BY_DURATION[i + 1]
        if d0 <= duration_min <= d1:
            t = (duration_min - d0) / (d1 - d0) if d1 > d0 else 0.0
            return if0 + t * (if1 - if0)
    return AC_IF_BY_DURATION[-1][1]


def _interp_pg_w_per_kg(duration_s: float) -> float:
    """Linear interpolation on PG_RPP_W_PER_KG (Pinot & Grappe 2011)."""
    if duration_s <= PG_RPP_W_PER_KG[0][0]:
        return PG_RPP_W_PER_KG[0][1]
    if duration_s >= PG_RPP_W_PER_KG[-1][0]:
        return PG_RPP_W_PER_KG[-1][1]
    for i in range(len(PG_RPP_W_PER_KG) - 1):
        d0, w0 = PG_RPP_W_PER_KG[i]
        d1, w1 = PG_RPP_W_PER_KG[i + 1]
        if d0 <= duration_s <= d1:
            t = (duration_s - d0) / (d1 - d0) if d1 > d0 else 0.0
            return w0 + t * (w1 - w0)
    return PG_RPP_W_PER_KG[-1][1]


# IF tier ceilings by event_type (Allen & Coggan TR&P 3rd ed. Table 7.4 +
# audit /tmp/audit_capability.md §3 step 1). Used in addition to the
# duration-based AC lookup so a "century" event_type doesn't fall to ultra
# IF just because the projected finish-time is long.
EVENT_TYPE_IF: dict[str, float] = {
    "crit":      0.95,
    "sportive":  0.80,
    "century":   0.78,
    "granfondo": 0.74,
    "ultra":     0.62,
}


def _project_event_capability(
    goal: "Goal",
    athlete: dict,
    fitness_state: dict,
    best_efforts_90d: dict | None = None,
) -> dict:
    """Project event finish time + power gap from goal + athlete state.

    Implements the literature-backed event-prep capability model (audit
    /tmp/audit_capability.md §3):

      Step 1  Flat-equivalent km = event_km + (event_climb_m / 100) * 1.5
              (Bassett & Howley 2000 climbing-distance heuristic).
      Step 2  Projected average speed: derived from FTP × W/kg × CTL mult.
      Step 3  Allen-Coggan IF lookup (AC_IF_BY_DURATION) — linear interp,
              blended with EVENT_TYPE_IF tier ceiling.
      Step 4  predicted_NP = IF × FTP; predicted_TSS = duration_h × IF² × 100.
      Step 5  Climb gate: required W/kg from VAM heuristic + Pinot-Grappe
              60-min RPP floor.
      Step 6  gap_endurance_h = required_h − goal.longest_ride_h_90d.

    When ``best_efforts_90d`` is supplied, fitness_estimation.compute_cp_wprime()
    runs a Monod fit and CP refines the W/kg baseline. Falls back to FTP
    when the fit fails (insufficient points / R² < 0.90).

    Returns:
        Dict with the locked field-name shape from
        /tmp/MASTER_DECISIONS_v467.md §4.

    References:
        Allen & Coggan, *Training and Racing with a Power Meter* 3rd ed. (2019).
        Pinot J & Grappe F (2011). Int J Sports Med 32:839-844.
        Bassett DR & Howley ET (2000). Med Sci Sports Exerc 32:70-84.
        Monod H & Scherrer J (1965). Ergonomics 8:329-338.
    """
    event_km = float(getattr(goal, "event_km", 0) or 0)
    event_climb_m = float(getattr(goal, "event_climb_m", 0) or 0)
    ftp = int(athlete.get("ftp", 200) or 200)
    weight_kg = float(athlete.get("weight_kg", 70.0) or 70.0)
    current_ctl = float(fitness_state.get("current_ctl", 50.0) or 50.0)

    # Optional CP/W' refinement: Monod fit on supplied 90d best efforts.
    cp_w: float | None = None
    if best_efforts_90d:
        try:
            from fitness_estimation import compute_cp_wprime
            fit = compute_cp_wprime(best_efforts_90d)
            if fit is not None:
                cp_candidate, _wprime = fit
                # CP should sit within ±15% of FTP for the same rider.
                if 0.85 * ftp <= cp_candidate <= 1.15 * ftp:
                    cp_w = cp_candidate
        except Exception as _e:
            log.debug(f"_project_event_capability: CP fit skipped ({_e})")
    sustainable_w = cp_w if cp_w is not None else float(ftp)

    # Step 1 — flat-equivalent km
    flat_eq_km = event_km + (event_climb_m / 100.0) * CLIMB_TO_FLAT_KM_PER_100M

    # Step 2 — projected average speed from FTP × W/kg × CTL multiplier.
    # Speed model fitted to Strava 2022 segment data: 4 W/kg amateur on
    # rolling terrain at IF=0.78 cruises at ~32 km/h; 3 W/kg at ~28 km/h.
    sustainable_w_per_kg = sustainable_w / weight_kg if weight_kg > 0 else 3.0
    w_per_kg_at_ftp = ftp / weight_kg if weight_kg > 0 else 3.0
    base_speed = DEFAULT_CRUISING_KMH * (0.65 + 0.135 * sustainable_w_per_kg)
    ctl_mult = max(0.85, min(1.15, 1.0 + 0.005 * (current_ctl - 50.0)))
    projected_avg_speed = base_speed * ctl_mult

    # Provisional duration → IF lookup (one Newton iteration).
    provisional_h = flat_eq_km / projected_avg_speed if projected_avg_speed > 0 else 4.0
    intensity = _interp_if_by_duration(provisional_h * 60.0)
    speed_refined = base_speed * ctl_mult * (intensity / 0.78)
    duration_h = flat_eq_km / speed_refined if speed_refined > 0 else provisional_h

    # Step 3 — refine IF by duration AND blend with event_type tier ceiling.
    # The duration-IF is the actual sustainable IF the rider will hit; the
    # tier is the ceiling implied by event_type. Blending ensures a 200km
    # century sits in the AC century band (0.78-0.84) even if the duration
    # interpolation alone would land lower.
    duration_if = _interp_if_by_duration(duration_h * 60.0)
    event_type = (getattr(goal, "event_type", "granfondo") or "granfondo").lower()
    tier_if = EVENT_TYPE_IF.get(event_type, 0.74)
    if duration_h <= 5.0:
        intensity = 0.25 * duration_if + 0.75 * tier_if
    elif duration_h <= 10.0:
        intensity = 0.4 * duration_if + 0.6 * tier_if
    else:
        intensity = 0.7 * duration_if + 0.3 * tier_if

    # Step 4 — predicted NP, TSS
    predicted_np = int(round(intensity * ftp))
    predicted_tss = round(duration_h * (intensity ** 2) * 100.0, 1)

    # Step 5 — climb-power gate.
    # Required W/kg derived from VAM + Pinot-Grappe RPP floor. None when
    # the event is essentially flat (event_climb_m ≤ 100m).
    if event_km > 0 and event_climb_m > 100:
        # m climbed per km — a 200km/3000m event = 15 m/km.
        # Required W/kg = climb_per_km * 0.013 + PG 60-min RPP floor * 0.6.
        # 0.013 W/kg per m/km is an empirical fit: a 50m/km climb (5%
        # average grade across the event) requires ~0.65 W/kg above floor.
        # Combined with the 1.92 W/kg floor (60-min RPP * 0.6) → ~2.6 W/kg
        # for a "rolling" event, ~5.0 W/kg for a Mont Ventoux-style stage.
        climb_ratio = event_climb_m / event_km
        climb_w_per_kg_required = climb_ratio * 0.013 + _interp_pg_w_per_kg(3600) * 0.6
        # Clamp to physiological range.
        climb_w_per_kg_required = max(2.0, min(climb_w_per_kg_required, 7.0))
    else:
        climb_w_per_kg_required = None

    climb_w_per_kg_current = w_per_kg_at_ftp if w_per_kg_at_ftp > 0 else None

    # Step 6 — endurance gap
    longest = goal.longest_ride_h_90d
    longest_completed_ride_h = float(longest) if longest is not None else None
    if longest_completed_ride_h is not None:
        gap_endurance_h = max(0.0, duration_h - longest_completed_ride_h)
    else:
        gap_endurance_h = duration_h  # full gap when baseline is missing

    if climb_w_per_kg_required is not None and climb_w_per_kg_current is not None:
        gap_power_w_per_kg = max(0.0, climb_w_per_kg_required - climb_w_per_kg_current)
    else:
        gap_power_w_per_kg = None

    # Climb readiness 0..100. 100 = athlete >= required; 0 = required is 2x.
    if climb_w_per_kg_required is not None and climb_w_per_kg_current is not None:
        if climb_w_per_kg_current >= climb_w_per_kg_required:
            climb_readiness_pct = 100
        else:
            ratio = climb_w_per_kg_current / climb_w_per_kg_required
            climb_readiness_pct = max(0, min(100, int(round(ratio * 100))))
    else:
        climb_readiness_pct = 100

    if goal.target_date:
        days_to_event = (goal.target_date - date.today()).days
        weeks_to_event = max(0, days_to_event // 7)
    else:
        weeks_to_event = 0

    return {
        "predicted_finish_h":          round(duration_h, 2),
        "predicted_np":                predicted_np,
        "predicted_tss":               predicted_tss,
        "climb_w_per_kg_required":     round(climb_w_per_kg_required, 2) if climb_w_per_kg_required is not None else None,
        "climb_w_per_kg_current":      round(climb_w_per_kg_current, 2) if climb_w_per_kg_current is not None else None,
        "longest_completed_ride_h":    round(longest_completed_ride_h, 2) if longest_completed_ride_h is not None else None,
        "longest_required_h":          round(duration_h, 2),
        "weeks_to_event":              weeks_to_event,
        "gap_endurance_h":             round(gap_endurance_h, 2),
        "gap_power_w_per_kg":          round(gap_power_w_per_kg, 2) if gap_power_w_per_kg is not None else None,
        "climb_readiness_pct":         climb_readiness_pct,
        "model_citations":             ["Allen & Coggan TR&P 3rd ed.", "Pinot & Grappe 2011"],
    }


# ── Goal types ────────────────────────────────────────────────────────────────

@dataclass
class TargetEvent:
    """F7 (v2.1) — one event in a multi-event Goal. The A event mirrors the Goal's
    canonical target_date + event_* scalars; B/C are intermediate races that get a
    proportionate mini-taper (never a full taper). priority ∈ {"A","B","C"}."""
    date: date
    priority: str = "B"
    name: str = ""
    event_type: str = "granfondo"
    event_km: float = 0
    event_climb_m: float = 0


@dataclass
class Goal:
    goal_type: str       # event, ftp, ctl, endurance, general, weight, vo2max, ftp_vo2max
    target_date: date | None = None

    # Event-specific
    event_name: str = ""
    event_km: float = 0
    event_climb_m: float = 0
    event_type: str = "granfondo"   # century, granfondo, ultra, crit, sportive

    # FTP target
    target_ftp: int | None = None

    # CTL target
    target_ctl: float | None = None

    # Endurance target
    target_distance_km: float | None = None
    target_duration_h: float | None = None

    # v4.6.7 IMPL-CAP: capability projection inputs.
    # Auto-populated from the last 90 days of rides at the Goal build site
    # when None — the projection helper uses this to compute gap_endurance_h
    # against the event's required duration. last_ftp_test_date drives the
    # "stale FTP" warning in the UI (FTP older than 8 weeks shrinks the
    # confidence band on the predicted_finish_h estimate).
    longest_ride_h_90d: float | None = None
    last_ftp_test_date: str | None = None

    # Weight target
    target_weight_kg: float | None = None

    # Time budget
    hours_per_week: float = 8.0
    max_weekday_hours: float = 2.0
    max_weekend_hours: float = 3.5
    available_days: list = field(default_factory=lambda: [1, 2, 3, 4, 5, 6])  # Mon=0..Sun=6
    rest_days: list = field(default_factory=lambda: [0])  # Monday
    daily_max_hours: dict = field(default_factory=dict)  # {0: 0, 1: 1.0, 2: 1.5, ...} per-day limits
    plan_weeks: int = 0
    # J1 (v2.1.0): intensity-distribution model is a USER CHOICE, not forced.
    # "polarized" (Seiler, default) | "pyramidal" | "threshold". Selects which
    # per-phase IntensityBudget table the planner uses (see BUDGETS_BY_MODEL).
    distribution: str = "polarized"
    # F1 (v2.1): OPT-IN block periodization (default OFF). When True the planner
    # concentrates each build/peak phase on ONE focus quality per ≤4-week block
    # (VO2 block → threshold block) instead of the weekly-mixed default. Default
    # False = today's behaviour, byte-for-byte (the default-off-parity contract).
    block_periodization: bool = False
    # F7 (v2.1): intermediate B/C races (additive). The A event stays the canonical
    # target_date + event_* scalars; B/C entries here get a proportionate mini-taper.
    # Empty = today's single-A behaviour, unchanged.
    events: list = field(default_factory=list)
    # FS1 (IP_PLANNER_MODES): plan CONSTRUCTION mode.
    #   "auto"       — the score-weighted content sampler (default, unchanged).
    #   "fixed_core" — blueprint engine: 1 HIT type/week, reps progress, Z2 core scales.
    #   "template"   — blueprint loaded from a shipped preset (see PLANNER_TEMPLATES).
    # "auto" everywhere it isn't set keeps every existing plan byte-for-byte.
    plan_mode: str = "auto"
    template_id: str = ""   # only when plan_mode == "template"
    # v2.3.0: CUSTOM intensity distribution. When distribution == "custom" this
    # holds the user's hard-work split as percentages summing to ~100:
    # {"tempo_ss": int, "threshold": int, "vo2": int, "sprint": int}. vo2+sprint
    # both map to the engine's z5plus zone (Z5/Z6); the easy/Z1-Z2 volume stays at
    # the science baseline (parity with polarized/pyramidal/threshold). Empty ⇒
    # not a custom plan.
    custom_bands: dict = field(default_factory=dict)

    def max_hours_for_day(self, weekday: int) -> float:
        """Get max training hours for a specific weekday (0=Mon..6=Sun)."""
        if self.daily_max_hours and weekday in self.daily_max_hours:
            return self.daily_max_hours[weekday]
        # Fallback to aggregate max
        return self.max_weekend_hours if weekday >= 5 else self.max_weekday_hours

    def weeks_available(self) -> int:
        if self.plan_weeks > 0:
            return self.plan_weeks
        if self.target_date is not None:
            return max(1, (self.target_date - date.today()).days // 7)
        return 16  # default


@dataclass
class Phase:
    name: str           # base, build1, build2, peak, taper, recovery
    start: date
    end: date
    weeks: int
    focus: str          # description
    weekly_tss_target: float
    z2_pct: float       # target zone distribution
    hit_per_week: int   # max HIT sessions per week
    session_types: list  # preferred session types — kept for backward compat;
                         # primary driver is now IntensityBudget below.
    # ── v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE) ──────────────────
    # Optional W'/Pmax mirrors of weekly_tss_target. None ⇒ TSS-only path.
    weekly_wprime_target: float | None = None
    weekly_pmax_target: float | None = None


@dataclass
class IntensityBudget:
    """Per-phase weekly volume + intensity budget (v4.5.0 IMPL-PLANNER).

    Drives the new ``sample_week_workouts`` sampler. Replaces the rigid
    ``Phase.session_types: list[str]`` + handwritten HIT_VARIANTS as the
    primary selector for which library workouts land on which slot. Phase
    keeps its session_types field for backward compat (read by the legacy
    ``_pick_session``) — in v4.5 the sampler is the source of truth, but
    daily-adapt + reforecast paths still inspect session_types.
    """
    z1z2_minutes_per_week: int
    z3_minutes_per_week: int
    z4_minutes_per_week: int
    z5plus_minutes_per_week: int
    tss_per_week: int
    hit_count_min: int           # min hard sessions per week
    hit_count_max: int           # max hard sessions per week
    rest_days_per_week: int      # default 2
    polarized_target: dict       # mirror of PHASE_POLARIZED_TARGETS row
    # ── v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE) ──────────────────
    # Optional W'/Pmax weekly budgets. None ⇒ TSS-only path.
    wprime_per_week: int | None = None
    pmax_per_week: int | None = None


@dataclass
class PlannedWeek:
    week_num: int
    start: date
    end: date
    phase: str
    tss_target: float
    is_stepback: bool
    sessions: list       # list of PlannedSession
    # ── v4.6.6 IMPL-A G4 (ACWR weekly scaling) ─────────────────────────────
    # Mirrored from Phase.hit_per_week so reforecast()/recalculate_plan() can
    # mutate per-week without rebuilding the full Phase tree (Gabbett 2016).
    # Defaults to 0; populated by callers that already track HIT count.
    hit_per_week: int = 0
    # True once the ACWR gate has scaled this week's tss_target ×0.85 for
    # injury-prevention. Read by the dashboard to render an "ACWR-scaled"
    # chip so the user knows why next week is lighter.
    auto_acwr_scaled: bool = False
    # ── v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE) ──────────────────
    # Optional W'/Pmax weekly mirrors. None ⇒ TSS-only path.
    wprime_target: float | None = None
    pmax_target: float | None = None
    # ── F1 (v2.1) block periodization ──────────────────────────────────────
    # The concentrated focus content_class for this week's block (e.g. "vo2max"),
    # or None for the default weekly-mixed plan. Set ONLY when
    # goal.block_periodization is on; None keeps the block plug-ins dormant.
    block_focus: "str | None" = None


@dataclass
class PlannedSession:
    day: date
    day_name: str
    session_type: str    # rest, z2, sweetspot, vo2max, threshold, overunder, long_z2, recovery, tempo, sprint, ftp_test
    duration_min: int
    tss_estimate: float
    description: str
    zwo_file: str = ""      # matched ZWO workout file
    zwo_name: str = ""
    nutrition_note: str = ""
    matched: bool = True    # False if match_zwo couldn't find a library entry
    adapted: bool = False   # True once daily-adapt rewrites this session in-place
    # ── fix26 §6: daily-adapt redesign ──────────────────────────────────
    # status tracks the lifecycle of a prescription independently of the
    # calendar date. Values:
    #   pending      — not yet executed, still owed
    #   done         — matched to an actual activity (3/3 classifier axes)
    #   done_partial — matched loosely (2/3 axes; user reviewed via rematch)
    #   missed       — past & no matching activity; stays "missed" until
    #                  explicitly rescheduled or dismissed at week end
    #   moved_from:<iso-date> — session was user-moved FROM this source date
    #   dismissed    — user dismissed prescription (stays visible greyed)
    #   ambiguous    — rematch classifier saw 2/3 axes; awaits user decision
    status: str = "pending"
    user_moved: bool = False  # True if user dragged this session — never auto-repositioned by regen
    moved_from: str = ""      # ISO date string of original slot, set when user_moved=True
    # v2.3.0: True if the user manually swapped this day to a different training
    # TYPE (Swap-type button). Pins type+duration: reforecast/refit must NOT
    # re-sample or auto-downshift it (the user's deliberate choice wins).
    user_swapped: bool = False
    completion_matches: list = None  # list of {activity_id, tss, duration_min, match_score, match_axes}
    dismissed_at: str = ""    # ISO timestamp when user dismissed this prescription
    # ── v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE) ──────────────────
    # Optional W'/Pmax mirrors of tss_estimate. None ⇒ TSS-only path.
    wprime_estimate: float | None = None
    pmax_estimate: float | None = None
    # ── v1.1.0 IMPL-NORWEGIAN-HR (HR-only Norwegian Method) ────────────────
    # All four fields nullable / None-default ⇒ preserves v1.0.6 behaviour.
    # When `hr_ceiling_pct` is set, prescription is dual-target (% FTP AND
    # HR ≤ pct × max_hr). When `is_double_threshold_pair` is True, this
    # session is half of an AM+PM same-day pair sharing
    # `double_threshold_partner_id`. `am_or_pm` records which half.
    hr_ceiling_pct: float | None = None              # 0.88 = "stay below 88% HR_max"
    is_double_threshold_pair: bool = False
    double_threshold_partner_id: str | None = None
    am_or_pm: str | None = None                      # "am" or "pm"
    # v2.2.14 (issue #7) — this day IS a race (A target event or a B/C event).
    # Set by _mark_race_days() AFTER taper passes: the day's training slot is
    # replaced by the race itself (no stray endurance ride on race day), and the
    # UI renders it distinctly. `race` carries {name, km, climb_m, type, priority}.
    is_race: bool = False
    race: dict | None = None
    # F2b (v2.5.0) — this session is a deliberate race-week OPENER (short ride
    # with 2-3×~1min race-pace touches placed at T-1 / B-1). Whitelisted in
    # _demote_hit_window + _enforce_weekly_hit_cap and round-tripped through the
    # plan dict (E7) so the eve-guard / caps / reforecast never flatten it.
    is_opener: bool = False


# ── v4.4.0 — phase targets (CONCEPT-SCI §1, §5) ───────────────────────────────

# Weekly volume + intensity targets per phase, for a trained age-group endurance
# cyclist with ~10h/wk capacity. Ranges synthesised from Seiler 2010, Mujika
# 2010, Rønnestad 2014, Coggan/Allen (TR&P 3rd ed.).
PHASE_TARGETS: dict[str, dict[str, float]] = {
    "base":          {"z1z2_hrs": 9.5, "z3z4_min": 45,  "z5plus_min": 5,   "tss_per_week": 425},
    "build1":        {"z1z2_hrs": 7.5, "z3z4_min": 120, "z5plus_min": 45,  "tss_per_week": 600},
    "build2":        {"z1z2_hrs": 7.5, "z3z4_min": 120, "z5plus_min": 45,  "tss_per_week": 600},
    "peak":          {"z1z2_hrs": 6.0, "z3z4_min": 90,  "z5plus_min": 80,  "tss_per_week": 650},
    "taper":         {"z1z2_hrs": 4.0, "z3z4_min": 30,  "z5plus_min": 22,  "tss_per_week": 275},
    # v1.0.0: consolidation = mini-taper at the END of non-event goals
    # (FTP / VO2max / hybrid / general). 1 week, ~50% of peak TSS, Z2-only.
    # Mujika 2010 *Sports Med* — 7-14 day reduced-load period after a build
    # block lets fatigue drop and supercompensation peak. Without this the
    # plan ends abruptly at peak with elevated fatigue, the athlete tries
    # to FTP-test on residual fatigue and gets a false-low result.
    "consolidation": {"z1z2_hrs": 5.5, "z3z4_min": 20, "z5plus_min": 0,    "tss_per_week": 240},
    "history":       {"z1z2_hrs": 8.0, "z3z4_min": 45,  "z5plus_min": 10,  "tss_per_week": 400},
}

# Intensity-distribution targets per phase (Seiler 2006/Stöggl 2014 polarised
# model). Adherence "broken" if Z1+Z2 falls below ~75% or Z4+ above ~25%.
PHASE_POLARIZED_TARGETS: dict[str, dict[str, int]] = {
    "base":          {"z1z2_pct": 88, "z3_pct": 8, "z4plus_pct": 4},
    "build1":        {"z1z2_pct": 78, "z3_pct": 6, "z4plus_pct": 16},
    "build2":        {"z1z2_pct": 75, "z3_pct": 5, "z4plus_pct": 20},
    "peak":          {"z1z2_pct": 72, "z3_pct": 4, "z4plus_pct": 24},
    "taper":         {"z1z2_pct": 80, "z3_pct": 5, "z4plus_pct": 15},
    # v1.0.0: consolidation = recovery-week shape, 90% Z1+Z2 (Mujika 2010).
    "consolidation": {"z1z2_pct": 92, "z3_pct": 6, "z4plus_pct": 2},
    "history":       {"z1z2_pct": 80, "z3_pct": 5, "z4plus_pct": 15},
}


# v4.5.0 IMPL-PLANNER: per-phase intensity budgets driving the new sampler.
# Numbers locked by /tmp/MASTER_DECISIONS_v45.md §3 Pillar A. Derived from
# PHASE_TARGETS (z1z2_hrs × 60 = z1z2_min; z3z4_min split 75/25 between Z3 and
# Z4 in build/peak, 80/20 in base/taper; z5plus_min direct).
BUDGETS: dict[str, "IntensityBudget"] = {
    "base":    IntensityBudget(
        z1z2_minutes_per_week=540, z3_minutes_per_week=45,
        z4_minutes_per_week=10, z5plus_minutes_per_week=5,
        tss_per_week=425, hit_count_min=1, hit_count_max=1, rest_days_per_week=2,
        polarized_target=PHASE_POLARIZED_TARGETS["base"],
    ),
    "build1":  IntensityBudget(
        z1z2_minutes_per_week=420, z3_minutes_per_week=120,
        z4_minutes_per_week=60, z5plus_minutes_per_week=45,
        tss_per_week=600, hit_count_min=2, hit_count_max=3, rest_days_per_week=2,
        polarized_target=PHASE_POLARIZED_TARGETS["build1"],
    ),
    "build2":  IntensityBudget(
        z1z2_minutes_per_week=400, z3_minutes_per_week=120,
        z4_minutes_per_week=60, z5plus_minutes_per_week=45,
        tss_per_week=600, hit_count_min=2, hit_count_max=3, rest_days_per_week=2,
        polarized_target=PHASE_POLARIZED_TARGETS["build2"],
    ),
    "peak":    IntensityBudget(
        z1z2_minutes_per_week=360, z3_minutes_per_week=90,
        z4_minutes_per_week=80, z5plus_minutes_per_week=80,
        tss_per_week=650, hit_count_min=3, hit_count_max=3, rest_days_per_week=2,
        polarized_target=PHASE_POLARIZED_TARGETS["peak"],
    ),
    "taper":   IntensityBudget(
        z1z2_minutes_per_week=240, z3_minutes_per_week=30,
        z4_minutes_per_week=20, z5plus_minutes_per_week=22,
        tss_per_week=275, hit_count_min=1, hit_count_max=1, rest_days_per_week=3,
        polarized_target=PHASE_POLARIZED_TARGETS["taper"],
    ),
    "consolidation": IntensityBudget(
        z1z2_minutes_per_week=330, z3_minutes_per_week=20,
        z4_minutes_per_week=0, z5plus_minutes_per_week=0,
        tss_per_week=240, hit_count_min=0, hit_count_max=0, rest_days_per_week=3,
        polarized_target=PHASE_POLARIZED_TARGETS["consolidation"],
    ),
    "history": IntensityBudget(
        z1z2_minutes_per_week=480, z3_minutes_per_week=45,
        z4_minutes_per_week=10, z5plus_minutes_per_week=10,
        tss_per_week=400, hit_count_min=1, hit_count_max=2, rest_days_per_week=2,
        polarized_target=PHASE_POLARIZED_TARGETS["history"],
    ),
}


# ── J1 (v2.1.0): selectable intensity-distribution model ──────────────────────
# The complaint: polarized was FORCED. The model is now a user choice (default
# polarized). pyramidal/threshold are derived from the polarized base by
# redistributing only the HARD minutes (z3+z4+z5plus) of the work phases —
# total load, TSS, HIT count, rest days and easy (z1z2) volume are preserved, so
# only the *kind* of intensity changes, never the dose. base/taper/consolidation/
# history stay polarized (foundation, recovery and taper are model-agnostic).
def _reallocate_hard(b: "IntensityBudget", z3w: float, z4w: float, z5w: float) -> "IntensityBudget":
    hard = b.z3_minutes_per_week + b.z4_minutes_per_week + b.z5plus_minutes_per_week
    tot = (z3w + z4w + z5w) or 1.0
    z3 = round(hard * z3w / tot)
    z4 = round(hard * z4w / tot)
    z5 = max(0, hard - z3 - z4)  # remainder keeps the sum exact
    wk = (b.z1z2_minutes_per_week + hard) or 1
    tgt = {
        "z1z2_pct": round(100 * b.z1z2_minutes_per_week / wk),
        "z3_pct": round(100 * z3 / wk),
        "z4plus_pct": round(100 * (z4 + z5) / wk),
    }
    return replace(b, z3_minutes_per_week=z3, z4_minutes_per_week=z4,
                   z5plus_minutes_per_week=z5, polarized_target=tgt)


def _model_budgets(z3w: float, z4w: float, z5w: float) -> "dict[str, IntensityBudget]":
    d = dict(BUDGETS)  # reuse polarized objects for the model-agnostic phases
    for ph in ("build1", "build2", "peak"):
        d[ph] = _reallocate_hard(BUDGETS[ph], z3w, z4w, z5w)
    return d


BUDGETS_BY_MODEL: dict[str, "dict[str, IntensityBudget]"] = {
    "polarized": BUDGETS,                       # Seiler — easy + very-hard, little threshold
    "pyramidal": _model_budgets(60, 28, 12),    # threshold-led, descending z3>z4>z5
    "threshold": _model_budgets(78, 16, 6),     # sweet-spot/at-FTP, minimal VO2/anaerobic
}

_ACTIVE_DISTRIBUTION = "polarized"
# v2.3.0: per-phase budget table for the "custom" distribution, built on demand
# by set_active_distribution from goal.custom_bands. None ⇒ no custom plan active.
_ACTIVE_CUSTOM_BUDGETS: "dict[str, IntensityBudget] | None" = None


def _custom_model_budgets(bands: dict) -> "dict[str, IntensityBudget]":
    """Build a per-phase budget table from a user hard-work split (v2.3.0).

    ``bands`` percentages (need not be normalized): tempo_ss→Z3, threshold→Z4,
    vo2+sprint→Z5+. Reuses _model_budgets/_reallocate_hard so easy volume, total
    hard minutes, TSS, HIT count and rest days are preserved exactly as in the
    polarized base — only the *kind* of hard work changes (parity with the
    pyramidal/threshold models)."""
    z3w = float(bands.get("tempo_ss", 0) or 0)
    z4w = float(bands.get("threshold", 0) or 0)
    z5w = float(bands.get("vo2", 0) or 0) + float(bands.get("sprint", 0) or 0)
    if (z3w + z4w + z5w) <= 0:
        z3w, z4w, z5w = 34.0, 33.0, 33.0  # safe default if the user zeroed it
    return _model_budgets(z3w, z4w, z5w)


def set_active_distribution(model: "str | None", custom_bands: "dict | None" = None) -> str:
    """Set the active intensity-distribution model for budget lookups (J1).

    Called at plan generation + recalc from ``goal.distribution``. Unknown or
    None falls back to ``polarized`` so the default path is byte-for-byte
    unchanged and the model is never hard-forced. ``model == "custom"`` with a
    non-empty ``custom_bands`` builds an on-demand budget table (v2.3.0).
    """
    global _ACTIVE_DISTRIBUTION, _ACTIVE_CUSTOM_BUDGETS
    if model == "custom" and custom_bands:
        try:
            _ACTIVE_CUSTOM_BUDGETS = _custom_model_budgets(custom_bands)
            _ACTIVE_DISTRIBUTION = "custom"
            return "custom"
        except Exception:
            _ACTIVE_CUSTOM_BUDGETS = None  # fall through to polarized on bad input
    _ACTIVE_CUSTOM_BUDGETS = None
    _ACTIVE_DISTRIBUTION = model if model in BUDGETS_BY_MODEL else "polarized"
    return _ACTIVE_DISTRIBUTION


def get_active_distribution() -> str:
    return _ACTIVE_DISTRIBUTION


def _active_budget_table() -> "dict[str, IntensityBudget]":
    if _ACTIVE_DISTRIBUTION == "custom" and _ACTIVE_CUSTOM_BUDGETS:
        return _ACTIVE_CUSTOM_BUDGETS
    return BUDGETS_BY_MODEL.get(_ACTIVE_DISTRIBUTION, BUDGETS)


def get_active_polarized_targets() -> "dict[str, dict]":
    """Per-phase polarization targets for the ACTIVE model (J1) so the recalc
    breach gate judges a non-polarized plan against its own target, not the
    polarized ceiling."""
    return {ph: b.polarized_target for ph, b in _active_budget_table().items()}


def get_budget_for_phase(phase_name: str) -> "IntensityBudget":
    """Return the IntensityBudget for a phase, defaulting to ``base``.

    Honors the active distribution model (J1; default polarized → unchanged;
    v2.3.0 custom supported).
    """
    table = _active_budget_table()
    return table.get(phase_name, table["base"])


# v2.3.0: map a session_type → one of the 5 user-facing intensity bands, for
# computing/displaying the realized training-type distribution of a plan.
_SESSION_TYPE_TO_BAND: dict[str, str] = {
    "recovery": "easy", "z2": "easy", "long_z2": "easy", "endurance": "easy",
    "endurance_intervals": "easy",
    "tempo": "tempo_ss", "tempo_intervals": "tempo_ss", "tempo_ladder": "tempo_ss",
    "sweetspot": "tempo_ss", "sweet_spot": "tempo_ss", "sweet_spot_ladder": "tempo_ss",
    "threshold": "threshold", "threshold_ladder": "threshold",
    "overunder": "threshold", "over_under": "threshold",
    "vo2max": "vo2", "vo2_short": "vo2", "vo2_ladder": "vo2", "anaerobic": "vo2",
    "sprint": "sprint", "neuromuscular": "sprint",
}

# Band order for stable display (easy → hardest).
BAND_ORDER: tuple = ("easy", "tempo_ss", "threshold", "vo2", "sprint")

# Typical realized distribution per intensity-distribution model (v2.3.0). Shown
# as a pre-generation hint; labelled "typical" because the realized split depends
# on phase mix + library availability. The post-generation readout computes the
# ACTUAL split via realized_band_distribution.
PRESET_TYPICAL_BANDS: dict[str, dict] = {
    "polarized": {"easy": 80, "tempo_ss": 4, "threshold": 6, "vo2": 8, "sprint": 2},
    "pyramidal": {"easy": 78, "tempo_ss": 13, "threshold": 6, "vo2": 3, "sprint": 0},
    "threshold": {"easy": 78, "tempo_ss": 16, "threshold": 5, "vo2": 1, "sprint": 0},
}


def realized_band_distribution(plan: dict) -> dict:
    """Compute the ACTUAL training-type distribution of a generated plan as %
    of total non-rest session minutes across the 5 bands (v2.3.0). Honest
    (computed, not hard-coded) — used to show the user what they actually got."""
    mins = {b: 0.0 for b in BAND_ORDER}
    for w in (plan.get("weeks") or []):
        for s in (w.get("sessions") or []):
            st = (s.get("session_type") or "").strip()
            if st in ("", "rest", "ftp_test"):
                continue
            band = _SESSION_TYPE_TO_BAND.get(st)
            if not band:
                continue
            try:
                mins[band] += float(s.get("duration_min") or 0)
            except (TypeError, ValueError):
                continue
    total = sum(mins.values()) or 1.0
    return {b: round(100 * mins[b] / total) for b in BAND_ORDER}


# ── v4.4.0 — composite "on-track" score helpers (CONCEPT-SCI §6) ──────────────

def compliance_band(pct: float | None) -> str:
    """Return ``green`` / ``amber`` / ``red`` per MASTER §3 thresholds.

    ``pct`` is a 0..N ratio (1.0 = 100%). ``None`` falls through to ``red``
    so an empty week with no plan-completion data renders as off-track.
    """
    if pct is None:
        return "red"
    p = float(pct) * 100.0 if pct <= 5 else float(pct)  # accept fraction or %
    if p < 50.0 or p > 135.0:
        return "red"
    if 80.0 <= p <= 115.0:
        return "green"
    return "amber"


def on_track_score(
    *,
    tss_compliance: float | None,
    intensity_dist_match: float | None,
    ctl_ramp_in_band: float | None,
    hrv_trend_ok: float | None = None,
    monotony_ok: float | None = None,
) -> int:
    """Composite 0-100 on-track score per CONCEPT-SCI §6.

    Each component should be a 0..100 score. ``None`` for any component
    drops it from the weighted sum and renormalizes the remaining weights.
    Returns 0 when *all* components are None (e.g. brand-new profile).
    """
    weights = {
        "tss":       (tss_compliance,       0.35),
        "intensity": (intensity_dist_match, 0.25),
        "ctl":       (ctl_ramp_in_band,     0.20),
        "hrv":       (hrv_trend_ok,         0.10),
        "monotony":  (monotony_ok,          0.10),
    }
    parts = [(v, w) for (v, w) in weights.values() if v is not None]
    if not parts:
        return 0
    total_w = sum(w for _, w in parts)
    if total_w <= 0:
        return 0
    score = sum(max(0.0, min(100.0, float(v))) * w for v, w in parts) / total_w
    return int(round(score))


def on_track_band(score: int) -> str:
    """Map a 0-100 ``on_track_score`` to a traffic-light band per §6."""
    s = int(score or 0)
    if s >= 80:
        return "green"
    if s >= 60:
        return "amber"
    return "red"


# ── CTL forecasting ───────────────────────────────────────────────────────────

def forecast_ctl(current_ctl: float, daily_tss: list[float]) -> list[float]:
    """Simulate CTL trajectory given a sequence of daily TSS values."""
    ctls = [current_ctl]
    ctl = current_ctl
    for tss in daily_tss:
        ctl = ctl + (tss - ctl) / 42.0
        ctls.append(round(ctl, 1))
    return ctls


def required_weekly_tss(current_ctl: float, target_ctl: float, weeks: int) -> float:
    """Calculate average weekly TSS needed to reach target CTL."""
    # CTL converges to daily_avg_tss over time
    # Simplified: weekly_tss ≈ target_ctl * 7 when at steady state
    # For ramp: need to overshoot slightly
    if weeks <= 0:
        return target_ctl * 7
    daily_tss = max(0, 2 * (target_ctl - 0.5 * current_ctl))
    return round(daily_tss * 7, 0)


def safe_ramp_rate(current_ctl: float) -> float:
    """Couzens' rule: scale ramp by relative fitness."""
    return round(min(7, max(3, 5 * (current_ctl / 80))), 1)


def target_ctl_for_event(goal: Goal, difficulty: float | None = None) -> float:
    """Determine target CTL for an event.

    v1.11.0 IMPL-EVENT — research (RESEARCH_event_plan_method.md): no platform
    derives CTL from the event. TrainingPeaks ATP sets the target from rider
    history (best-ever × ~1.1) and FEASIBILITY-caps it by ramp rate; intervals.icu
    /WKO5/Xert forecast CTL forward from prescribed load. Event SIZE drives
    long-ride duration + specificity, NOT CTL. So the EVENT_CTL_TARGETS band stays
    the anchor, the feasibility cap in generate_phases (``max_achievable``) IS the
    ATP feasibility test, and we replace ONLY the two discrete +5 steps with a small
    smooth duration nudge (±6%) so event size is continuous, not a step function.
    ``difficulty`` is 0..1 from event DURATION (km+climb fold into finish_h).
    """
    base = EVENT_CTL_TARGETS.get(goal.event_type, EVENT_CTL_TARGETS["granfondo"])["strong"]
    if difficulty is not None:
        d = max(0.0, min(1.0, difficulty))
        return base * (0.94 + 0.12 * d)   # ±6% — minor; the long ride is the lever
    # Legacy fallback (no demand model available): keep the old discrete steps.
    return base + (5 if goal.event_climb_m > 3000 else 0) + (5 if goal.event_km > 180 else 0)


# v1.11.0 IMPL-EVENT — event demand → plan targets.
LONG_RIDE_CAP_MIN = 300      # 5h ceiling (Friel/CTS 4-6h); don't match >5h events.
LONG_RIDE_FLOOR_H = 1.5      # start the long-ride ramp from at least 1.5h.
LONG_RIDE_STEP_MIN = 25      # +25 min/week absolute ("+10%/wk" is research-debunked).
STEPBACK_LONG_RIDE_CAP_MIN = 150  # B4: a deload's long ride stays ≤2.5h (matches the
                                  # step-back picker's own weekend min(max_min, 150)).


def _event_demand_targets(goal: "Goal", athlete: dict | None,
                          fitness_state: dict | None) -> dict | None:
    """Turn an event goal into plan-ready targets by wrapping the EXISTING
    `_project_event_capability` (no Martin solver, no kJ — duration is the only
    demand number the plan needs). Returns None for non-event goals OR on any
    missing/absurd input, so every event-wiring site no-ops and non-event plans
    stay byte-identical (the v1.11.0 invariant).

    Returns: difficulty (0..1, from finish_h), long_target_h, long_start_h,
    climbing_bias, + passthrough predicted_finish_h / predicted_tss / gap_endurance_h.
    """
    if getattr(goal, "goal_type", "") != "event":
        return None
    event_km = float(getattr(goal, "event_km", 0) or 0)
    if not (1.0 <= event_km <= 1200.0):              # missing or absurd → legacy
        return None
    weight = float((athlete or {}).get("weight_kg", 0) or 0)
    ftp = float((athlete or {}).get("ftp", 0) or 0)
    if not (30.0 <= weight <= 200.0) or ftp <= 0:    # need real athlete for the demand model
        return None
    if goal.target_date and (goal.target_date - date.today()).days < 0:
        return None                                  # event in the past → legacy
    try:
        cap = _project_event_capability(goal, athlete, fitness_state or {})
    except Exception as e:                            # never let demand math break plan gen
        log.debug(f"_event_demand_targets: capability failed ({e})")
        return None
    finish_h = float(cap.get("predicted_finish_h") or 0)
    if finish_h <= 0:
        return None
    difficulty = max(0.0, min(1.0, (finish_h - 2.0) / 6.0))
    long_target_h = min(0.8 * finish_h, LONG_RIDE_CAP_MIN / 60.0)   # weekend-hours cap applied in post-pass
    _start = goal.longest_ride_h_90d
    long_start_h = max(float(_start) if _start else LONG_RIDE_FLOOR_H, LONG_RIDE_FLOOR_H)
    # Climbing specificity keys on the EVENT's climbing density (m gained per km),
    # not a rider power-gap: a climby route warrants climbing-specific work
    # regardless of rider strength. >12 m/km ≈ a hilly+ route (rolling is 5-15).
    climb_density = float(getattr(goal, "event_climb_m", 0) or 0) / event_km
    return {
        "difficulty": difficulty,
        "long_target_h": long_target_h,
        "long_start_h": long_start_h,
        "climbing_bias": climb_density > 12.0,
        "predicted_finish_h": finish_h,
        "predicted_tss": float(cap.get("predicted_tss") or 0),
        "gap_endurance_h": float(cap.get("gap_endurance_h") or 0),
    }


# ── Phase generator (backwards periodization) ────────────────────────────────

def generate_phases(goal: Goal, current_ctl: float,
                    event_targets: dict | None = None,
                    recent_weekly_tss: float | None = None) -> list[Phase]:
    """Generate training phases working backwards from the target date.

    v1.11.0: ``event_targets`` (from `_event_demand_targets`, None for non-event)
    feeds the event difficulty into the CTL band as a small ±6% nudge. Non-event
    callers pass None → identical behavior.

    v2.1.0 (E1): ``recent_weekly_tss`` (rider's recent mean weekly TSS from the
    full ride archive) sets a LOAD-based weekly volume ceiling instead of the
    availability sum. None → fall back to the legacy ``hours_per_week×65`` cap."""
    total_weeks = goal.weeks_available()
    target_date = goal.target_date or (date.today() + timedelta(weeks=16))

    # Determine target CTL based on goal type
    if goal.target_ctl:
        target = goal.target_ctl
    elif goal.goal_type == "event":
        target = target_ctl_for_event(
            goal, difficulty=(event_targets or {}).get("difficulty"))
    elif goal.goal_type == "ftp":
        # FTP improvement: moderate CTL increase, emphasis on quality not volume
        target = min(90, current_ctl + safe_ramp_rate(current_ctl) * min(total_weeks, 12))
    elif goal.goal_type in ("vo2max",):
        # VO2max: similar CTL but with more intense HIT sessions
        target = min(85, current_ctl + safe_ramp_rate(current_ctl) * min(total_weeks, 12))
    elif goal.goal_type in ("ftp_vo2max", "hybrid"):
        # Hybrid: balanced volume + intensity
        target = min(95, current_ctl + safe_ramp_rate(current_ctl) * min(total_weeks, 12))
    else:
        # General / CTL / Endurance: progressive improvement
        ramp = safe_ramp_rate(current_ctl)
        target = current_ctl + ramp * min(total_weeks, 12)

    # Clamp target to what's achievable
    max_ramp = safe_ramp_rate(current_ctl)
    max_achievable = current_ctl + max_ramp * max(0, total_weeks - 2)  # minus taper
    target = min(target, max_achievable)

    # Weekly TSS at target CTL
    peak_weekly_tss = target * 7

    # v2.1.0 (E1) — LOAD-based weekly ceiling. The old cap was the sum of daily
    # availability (hours_per_week×65), so a rider with generous availability
    # got a ~24.5h/1592-TSS week regardless of what they'd actually been
    # training — "starts like post-winter". The authoritative volume is now
    # what's SMART after recent load: bounded by an ACWR-safe ramp over the
    # rider's recent mean weekly TSS (Gabbett 2016: acute:chronic ≤~1.3 keeps
    # injury risk low). target×7 stays the maintenance/aspiration cap.
    # Availability remains a per-DAY session-length ceiling only (the
    # authoritative per-day clamp at the end of _build_weeks) — it no longer
    # drives the weekly TOTAL. When there's no ride history (recent_weekly_tss
    # is None) we fall back to the legacy availability cap so existing
    # flows/tests are unchanged.
    if recent_weekly_tss and recent_weekly_tss > 0:
        gabbett_safe = recent_weekly_tss * ACWR_CEILING
        peak_weekly_tss = min(peak_weekly_tss, gabbett_safe)
    else:
        max_tss_from_hours = goal.hours_per_week * 65
        peak_weekly_tss = min(peak_weekly_tss, max_tss_from_hours)

    # ── Allocate phases backwards from target date ────────────────────────

    phases = []
    cursor = target_date

    # TAPER: 10-14 days (Mujika & Padilla 2003: 8-14 days optimal)
    # Only create taper for event/ctl goals — not for general, ftp, vo2max, etc.
    taper_weeks = 0
    if goal.goal_type in ("event", "ctl"):
        # F4c (v2.5.0, D4): a runway under 14 days cannot fit ANY build+taper
        # structure — the old path emitted a degenerate 1-2 day "peak" (or a
        # coverage hole before a backward-anchored taper) and trained THROUGH
        # the final days. Emit ONE race-week micro-phase covering start..target:
        # rest/openers/race arrive via the standard post-passes (race-week
        # shape + eve guard + clip emission), and generate_plan caps the whole
        # micro-plan at a single hard touch. Kept under the "taper" name so
        # every taper-keyed guard (budget table, stepback skip, eve guard,
        # FC2a shrink order) applies unchanged.
        _micro_start = getattr(goal, "_phase_start_override", None) or date.today()
        _runway_days = (target_date - date.today()).days
        if 0 < _runway_days < 14 and _micro_start <= target_date:
            _span = (target_date - _micro_start).days + 1
            return [Phase(
                name="taper",
                start=_micro_start,
                end=target_date,
                weeks=max(1, -(-_span // 7)),
                focus=(f"Race-week micro-plan — {goal.event_name or 'event'} in "
                       f"{_runway_days}d: rest, openers, race. Too close for a "
                       "training block."),
                weekly_tss_target=round(peak_weekly_tss * TAPER_FRACS[-1]),
                z2_pct=80,
                hit_per_week=1,
                session_types=["z2", "recovery", "rest"],
            )]
        # FC1-CLIP (v2.5.0, D2/D3/SM2): the taper ends ON the target date (race
        # day belongs to the plan; the emitters clip the final week at the phase
        # end instead of spilling to target+1), and Phase.weeks is the ceil of
        # the ACTUAL day-span (was hardcoded 2 — lied for sub-week runways).
        taper_start = max(date.today(), cursor - timedelta(days=TAPER_DAYS))
        _taper_span = (cursor - taper_start).days + 1
        taper_weeks = max(1, -(-_taper_span // 7))
        phases.append(Phase(
            name="taper",
            start=taper_start,
            end=cursor,
            weeks=taper_weeks,
            focus=f"Volume -40%, maintain intensity. Target: fresh for {goal.event_name or 'event'}",
            weekly_tss_target=round(peak_weekly_tss * 0.60),  # Mujika: 40-60% reduction, favor conservative end
            z2_pct=70,
            hit_per_week=1,
            session_types=["z2", "threshold", "vo2max", "sprint", "recovery"],
        ))
        cursor = taper_start

    # v1.0.0: reserve 1 week for the consolidation phase appended at the end
    # of non-event goals. Subtracting from remaining_weeks here means peak/
    # build2/build1 absorb the 1-week reduction (instead of the plan ending
    # 1 week later than the user requested). Event/ctl goals get taper instead
    # so consolidation = 0 for them.
    consolidation_weeks = (
        1 if goal.goal_type in ("ftp", "vo2max", "ftp_vo2max", "hybrid",
                                "general", "endurance", "weight") else 0
    )
    remaining_weeks = max(0, total_weeks - taper_weeks - consolidation_weeks)

    # Distribute remaining weeks across phases
    if remaining_weeks >= 14:
        # Full program: base(4+) + build1(4) + build2(4) + peak(2+)
        peak_weeks = min(3, max(2, remaining_weeks // 7))
        build2_weeks = min(4, remaining_weeks - peak_weeks - 8)
        build1_weeks = min(4, remaining_weeks - peak_weeks - build2_weeks - 4)
        base_weeks = remaining_weeks - peak_weeks - build2_weeks - build1_weeks
    elif remaining_weeks >= 10:
        # Compressed: base(2) + build1(3) + build2(3) + peak(2)
        peak_weeks = 2
        build2_weeks = 3
        build1_weeks = 3
        base_weeks = remaining_weeks - peak_weeks - build2_weeks - build1_weeks
    elif remaining_weeks >= 6:
        # Minimal: build1(3) + build2(2) + peak(1)
        peak_weeks = 1
        build2_weeks = 2
        build1_weeks = 2
        base_weeks = remaining_weeks - peak_weeks - build2_weeks - build1_weeks
    else:
        # Crisis: just build + peak
        peak_weeks = 1
        build2_weeks = 0
        build1_weeks = remaining_weeks - peak_weeks
        base_weeks = 0

    # Calculate progressive TSS ramp (must be monotonically increasing)
    base_tss   = round(current_ctl * 7 * 1.05)  # slightly above maintenance
    build1_tss = round(peak_weekly_tss * 0.70)
    build2_tss = round(peak_weekly_tss * 0.85)
    peak_tss   = round(peak_weekly_tss * 1.00)
    # Ensure progressive overload: base <= build1 <= build2 <= peak
    base_tss = min(base_tss, build1_tss)

    # ── GOAL-SPECIFIC PHASE DEFINITIONS ──────────────────────────────────
    # FTP: emphasise sweet spot + threshold (91-105% FTP, Ronnestad 2014)
    # VO2max: emphasise VO2max intervals (106-120% FTP, Helgerud 2007)
    # Hybrid: alternate blocks (2wk threshold → 2wk VO2max, Neal 2013 polarized)
    # Event: standard periodization (base → build → peak → taper)
    # General: balanced (same as event without specific target)

    goal_type = goal.goal_type

    if goal_type == "ftp":
        # FTP-focused: Rønnestad 30/15s + Seiler 4×8min are #1 and #2 FTP builders
        # Research: Rønnestad 2014 — 30/15s micro-intervals +12% FTP in 10 weeks
        # Seiler 2013 — 4×8min @106% FTP = +16% threshold power
        # Stöggl 2014 — raising VO2max ceiling raises FTP (polarized > threshold-only)
        # v4.1.1 FIX-PLANNER B: base adds "sweetspot" (Seiler base-mid: 80% Z2 +
        # tempo/sweet spot mix, not tempo-only) to break the "every HIT slot
        # picks tempo" identical-weeks pattern. build1 adds "overunder" to give
        # the HIT picker a 5th type to rotate through.
        phase_defs = []
        if base_weeks > 0:
            phase_defs.append(("base", base_weeks, base_tss,
                f"Aerobic base + tempo introduction. CTL {current_ctl:.0f} → {current_ctl + max_ramp * base_weeks:.0f}",
                85, 1, ["z2", "long_z2", "recovery", "tempo", "sweetspot"]))
        if build1_weeks > 0:
            phase_defs.append(("build1", build1_weeks, build1_tss,
                "Sweet spot + Seiler threshold: 3×15min SS + 3×8min @106% FTP (Seiler 2013).",
                70, 2, ["z2", "sweetspot", "threshold", "vo2max", "overunder", "long_z2"]))
        if build2_weeks > 0:
            phase_defs.append(("build2", build2_weeks, build2_tss,
                "Rønnestad micro-intervals + Seiler 4×8: #1 and #2 FTP builders. Breakthrough phase.",
                65, 2, ["z2", "vo2max", "threshold", "overunder", "sweetspot", "sprint", "long_z2"]))
        if peak_weeks > 0:
            phase_defs.append(("peak", peak_weeks, peak_tss,
                "FTP consolidation: Rønnestad peak + threshold endurance.",
                70, 2, ["z2", "vo2max", "threshold", "overunder", "sprint"]))

    elif goal_type == "vo2max":
        # VO2max-focused: maximize time at >90% VO2max per session
        # Seiler 2013: 4×8min @106% FTP = +11.4% VO2max, +16% threshold (7 weeks)
        # Rønnestad 2020: 30/15s = 12-15min at VO2max per session (elite cyclists)
        # Bossi 2020: alternating power intervals = +43% time at VO2max
        # Helgerud 2007: 4×4min = +7.2% VO2max (8 weeks, moderately trained)
        # v4.1.1 FIX-PLANNER B: base adds "sweetspot"; build1 adds "threshold"
        # for cross-week variety (previously only z2/vo2max/sweetspot/long_z2).
        phase_defs = []
        if base_weeks > 0:
            phase_defs.append(("base", base_weeks, base_tss,
                f"Aerobic base: high volume Z2 + Helgerud introduction. CTL {current_ctl:.0f} → {current_ctl + max_ramp * base_weeks:.0f}",
                85, 1, ["z2", "long_z2", "recovery", "tempo", "sweetspot"]))
        if build1_weeks > 0:
            phase_defs.append(("build1", build1_weeks, build1_tss,
                "VO2max build: Seiler 4×8min @106% FTP + Helgerud 4×4min. 10-14min above 90% VO2max/session.",
                70, 2, ["z2", "vo2max", "sweetspot", "threshold", "long_z2"]))
        if build2_weeks > 0:
            phase_defs.append(("build2", build2_weeks, build2_tss,
                "VO2max peak: Rønnestad 30/15s (12-15min @VO2max) + Bossi alternating intervals. Maximum stimulus.",
                65, 2, ["z2", "vo2max", "overunder", "threshold", "sprint", "long_z2"]))
        if peak_weeks > 0:
            phase_defs.append(("peak", peak_weeks, peak_tss,
                "VO2max consolidation: Seiler 4×8 + Rønnestad 30/15s. Break through plateau.",
                70, 2, ["z2", "vo2max", "threshold", "overunder", "sprint"]))

    elif goal_type in ("ftp_vo2max", "hybrid"):
        # Hybrid FTP+VO2max: pyramidal-to-polarized sequencing
        # Stöggl 2014: POL improved BOTH VO2max +11.7% AND threshold +8.1%
        # Neal 2013: POL 80/0/20 beat threshold 57/43/0 on ALL metrics
        # Rønnestad: block periodization +8.8% VO2max + +22% threshold power
        # Pyramidal→polarized sequence = best overall (16-week runner study)
        # Strategy: Phase 1 pyramidal (threshold emphasis + VO2max intro)
        #           Phase 2 polarized (VO2max emphasis + threshold maintain)
        # v4.1.1 FIX-PLANNER B: base adds "sweetspot"; build1 adds "overunder".
        phase_defs = []
        if base_weeks > 0:
            phase_defs.append(("base", base_weeks, base_tss,
                f"Aerobic base: high volume Z2 + tempo. Foundation for dual adaptation. CTL {current_ctl:.0f} → {current_ctl + max_ramp * base_weeks:.0f}",
                85, 1, ["z2", "long_z2", "recovery", "tempo", "sweetspot"]))
        if build1_weeks > 0:
            phase_defs.append(("build1", build1_weeks, build1_tss,
                "Pyramidal: threshold focus (3×15min @95-100% FTP) + VO2max intro (5×4min @106%). 75/15/10 distribution.",
                70, 2, ["z2", "sweetspot", "threshold", "vo2max", "overunder", "long_z2"]))
        if build2_weeks > 0:
            phase_defs.append(("build2", build2_weeks, build2_tss,
                "Polarized: VO2max focus (Seiler 4×8 + Rønnestad 30/15) + threshold maintenance (2×20min). 80/5/15 distribution.",
                65, 2, ["z2", "vo2max", "threshold", "overunder", "sweetspot", "sprint", "long_z2"]))
        if peak_weeks > 0:
            phase_defs.append(("peak", peak_weeks, peak_tss,
                "Peak consolidation: 1×VO2max + 1×threshold/week. Anchor both adaptations.",
                70, 2, ["z2", "vo2max", "threshold", "overunder", "sprint"]))

    else:
        # Event / General / CTL / Endurance — standard periodization
        # v4.1.1 FIX-PLANNER B: base adds "sweetspot"; build1 adds "overunder"
        # for cross-week variety.
        phase_defs = []
        if base_weeks > 0:
            phase_defs.append(("base", base_weeks, base_tss,
                f"Aerobic foundation. Z2 focus, 80/20 distribution. CTL {current_ctl:.0f} → {current_ctl + max_ramp * base_weeks:.0f}",
                85, 1, ["z2", "long_z2", "recovery", "tempo", "sweetspot"]))
        if build1_weeks > 0:
            phase_defs.append(("build1", build1_weeks, build1_tss,
                "Sweet spot + threshold introduction. Climbing prep.",
                70, 2, ["z2", "sweetspot", "threshold", "overunder", "long_z2"]))
        if build2_weeks > 0:
            phase_defs.append(("build2", build2_weeks, build2_tss,
                "VO2max intervals, over-unders. Peak training stress.",
                65, 2, ["z2", "vo2max", "overunder", "sweetspot", "sprint", "long_z2"]))
        if peak_weeks > 0:
            phase_defs.append(("peak", peak_weeks, peak_tss,
                "Race-specific. Climbing repeats, threshold sustain.",
                70, 2, ["z2", "threshold", "vo2max", "overunder", "sprint"]))

    # F2d (v2.5.0, SM1): a >20-week monolithic base (52w runway → 39 base weeks)
    # has no intermediate structure. Split it into repeating ≤4-week blocks that
    # ride the EXISTING 3-load+1-stepback cadence (STEP_BACK_EVERY is keyed on
    # the global week counter, so the deload rhythm is unchanged); each block is
    # its own Phase row, so week_in_phase-driven variety resets per block and
    # the phase panel shows the block structure instead of one 39-week slab.
    if base_weeks > 20:
        _split_defs = []
        for _pd in phase_defs:
            if _pd[0] != "base":
                _split_defs.append(_pd)
                continue
            _name, _wks, _tss, _focus, _z2, _hit, _types = _pd
            _n_blocks = -(-_wks // 4)  # ceil
            for _bi in range(_n_blocks):
                _blk = min(4, _wks - 4 * _bi)
                _split_defs.append((_name, _blk, _tss,
                                    f"{_focus} [block {_bi + 1}/{_n_blocks}]",
                                    _z2, _hit, _types))
        phase_defs = _split_defs

    # v1.0.0: append a 1-week CONSOLIDATION phase after peak for non-event
    # goals (FTP / VO2max / hybrid / general / endurance / weight). Mujika 2010
    # Sports Med review: 7-14 day reduced-load period after a build block lets
    # fatigue dissipate and supercompensation peak. Without this the plan ends
    # abruptly at peak with elevated fatigue — the athlete attempts an FTP
    # test on residual fatigue and gets a false-low result that under-sets
    # the next cycle. Consolidation is Z2-only (~50% of peak TSS), no HIT,
    # explicit prompt at end-of-week to FTP-test before generating the next
    # cycle. event/ctl goals already have a true taper and skip this.
    if goal_type in ("ftp", "vo2max", "ftp_vo2max", "hybrid", "general",
                     "endurance", "weight") and phase_defs:
        phase_defs.append(("consolidation", 1, 240,
            "Consolidation week: ~50% peak TSS, Z2 only, no HIT. Lets fatigue "
            "drop and supercompensation crystallise (Mujika 2010 Sports Med). "
            "FTP test recommended at end of this week before starting your "
            "next training cycle.",
            92, 0, ["z2", "long_z2", "recovery"]))

    # Build phases forward (respect override for post-recovery start)
    cursor_fwd = getattr(goal, "_phase_start_override", None) or date.today()
    for name, weeks, tss, focus, z2, hit, types in phase_defs:
        end = cursor_fwd + timedelta(weeks=weeks) - timedelta(days=1)
        phases.insert(-1 if taper_weeks > 0 else len(phases), Phase(  # insert before taper (or append if no taper)
            name=name,
            start=cursor_fwd,
            end=end,
            weeks=weeks,
            focus=focus,
            weekly_tss_target=tss,
            z2_pct=z2,
            hit_per_week=hit,
            session_types=types,
        ))
        cursor_fwd = end + timedelta(days=1)

    # ── Reconcile forward/backward cursors ──────────────────────────────────
    # The taper phase was built backward from target_date, while all other
    # phases were built forward from today. Without coordination this causes
    # either a gap (forward phases end before taper_start) or an overlap
    # (forward phases end after taper_start). Fix by adjusting the last
    # non-taper phase so it ends exactly at taper_start - 1 day.
    if taper_weeks > 0:
        required_end = taper_start - timedelta(days=1)
        # Work from the last non-taper phase backward; if a phase would
        # shrink to 0 weeks, remove it and retry with the previous one.
        while True:
            # Find the last non-taper phase in insertion order
            last_idx = None
            for i in range(len(phases) - 1, -1, -1):
                if phases[i].name != "taper":
                    last_idx = i
                    break
            if last_idx is None:
                break  # no non-taper phases left; nothing to reconcile
            last = phases[last_idx]
            if last.end == required_end:
                break  # already aligned
            new_duration_days = (required_end - last.start).days + 1
            if new_duration_days <= 0:
                # Truncation would zero-out this phase; drop it and retry.
                phases.pop(last_idx)
                continue
            last.end = required_end
            # Recalculate weeks from actual duration (round to nearest, min 1)
            last.weeks = max(1, round(new_duration_days / 7))
            break

    # Sort by start date
    phases.sort(key=lambda p: p.start)

    # SM5 (v2.5.0): non-event goals with a target_date used to stop up to 6
    # days short of it (weeks_available floor-division: 110d → 15 weeks →
    # last covered day = target-6). Extend the FINAL phase to the EVE of the
    # target so the plan covers the days before the user's chosen date; the
    # date itself carries no prescription (nothing to place on it — event/ctl
    # goals get the taper-ends-at-target treatment instead). Exact-multiple
    # runways already end at target-1 → untouched (keeps pinned non-event
    # plans byte-identical). Gap >6d means plan_weeks deliberately stops the
    # plan early → untouched too.
    if goal.target_date and taper_weeks == 0 and phases:
        _sm5_eve = goal.target_date - timedelta(days=1)
        _sm5_gap = (_sm5_eve - phases[-1].end).days
        if 0 < _sm5_gap <= 6:
            _last = phases[-1]
            _last.end = _sm5_eve
            _last.weeks = max(1, round(((_last.end - _last.start).days + 1) / 7))

    # ── Safety check: no overlaps, no gaps > 1 day ──────────────────────────
    for i in range(len(phases) - 1):
        cur, nxt = phases[i], phases[i + 1]
        gap = (nxt.start - cur.end).days
        assert gap == 1, (
            f"Phase boundary error: '{cur.name}' ends {cur.end}, "
            f"'{nxt.name}' starts {nxt.start} (gap={gap} days, expected 1)"
        )

    return phases


# ── Weekly planner ────────────────────────────────────────────────────────────

def plan_week(
    week_num: int,
    start: date,
    phase: Phase,
    goal: Goal,
    is_stepback: bool,
    prev_week_sessions: list | None = None,
    seed_salt: int = 0,
) -> PlannedWeek:
    """Generate a specific week's training schedule.

    Args:
        prev_week_sessions: Sessions from the immediately preceding week. Used
            to enforce the 48h HIT-gap across week boundaries (PL2). Without
            this, a Sunday vo2max + Monday vo2max pair slipped through because
            the gap check only saw the current week's `sessions_so_far`.
        seed_salt: v4.3.0 B3 — entropy salt forwarded into _pick_session so
            HIT-variant selection differs across regenerations.
    """
    tss_target = phase.weekly_tss_target
    if is_stepback:
        # Issurin 2010 (Block Periodization): recovery/unloading weeks should cut
        # load by ~20-30%, not 40-60%. A 45% drop forces excessive detraining and
        # stalls adaptation. 0.72 = 28% reduction, midpoint of the recommended band.
        tss_target = round(tss_target * 0.72)

    sessions = []
    tss_allocated = 0

    for day_offset in range(7):
        d = start + timedelta(days=day_offset)
        weekday = d.weekday()  # 0=Monday
        day_name = d.strftime("%a")

        # Rest day
        if weekday in goal.rest_days or weekday not in goal.available_days:
            sessions.append(PlannedSession(
                day=d, day_name=day_name, session_type="rest",
                duration_min=0, tss_estimate=0,
                description="Rest — recovery takes priority",
            ))
            continue

        # Determine max duration — use per-day availability if set
        is_weekend = weekday >= 5
        max_hours = goal.max_hours_for_day(weekday)
        max_min = int(max_hours * 60)

        # Determine session type based on phase + day position
        remaining_tss = tss_target - tss_allocated
        remaining_days = sum(
            1 for i in range(day_offset + 1, 7)
            if (start + timedelta(days=i)).weekday() not in goal.rest_days
            and (start + timedelta(days=i)).weekday() in goal.available_days
        )

        session = _pick_session(
            phase=phase,
            is_weekend=is_weekend,
            is_stepback=is_stepback,
            max_min=max_min,
            remaining_tss=remaining_tss,
            remaining_days=remaining_days,
            day_in_week=day_offset,
            sessions_so_far=sessions,
            week_num=week_num,
            prev_week_sessions=prev_week_sessions,
            seed_salt=seed_salt,
        )
        tss_allocated += session.tss_estimate
        session.day = d
        session.day_name = day_name

        # Add nutrition note by phase
        session.nutrition_note = _nutrition_note(phase.name, session.session_type)

        sessions.append(session)

    return PlannedWeek(
        week_num=week_num,
        start=start,
        end=start + timedelta(days=6),
        phase=phase.name,
        tss_target=tss_target,
        is_stepback=is_stepback,
        sessions=sessions,
    )


def _pick_session(
    phase: Phase,
    is_weekend: bool,
    is_stepback: bool,
    max_min: int,
    remaining_tss: float,
    remaining_days: int,
    day_in_week: int,
    sessions_so_far: list,
    week_num: int = 0,
    prev_week_sessions: list | None = None,
    seed_salt: int = 0,
) -> PlannedSession:
    """Pick the best session type for this day.

    Args:
        prev_week_sessions: Sessions from the preceding week. Consulted by the
            48h HIT-gap check (PL2) so that a Sunday hard session blocks a
            Monday one across the week boundary.
        seed_salt: v4.3.0 B3 — entropy salt mixed (mod 7919) into the HIT-variant
            shuffle seed so consecutive ``/api/plan/regenerate`` calls produce
            visibly different HIT picks. Default 0 = legacy deterministic mode.
    """

    # Count HIT sessions already planned this week
    hit_types = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}
    hit_count = sum(1 for s in sessions_so_far if s.session_type in hit_types)

    # Check if yesterday was a HIT (need 48h gap = at least 1 day between).
    # On the first day of the week, "yesterday" lives in prev_week_sessions.
    last_session_hit = (
        sessions_so_far and sessions_so_far[-1].session_type in hit_types
    )
    if not sessions_so_far and prev_week_sessions:
        # First training day of the new week — look at Sunday of the prior week.
        last_prev = prev_week_sessions[-1] if prev_week_sessions else None
        if last_prev is not None and last_prev.session_type in hit_types:
            last_session_hit = True

    # Step-back weeks: Z2 or recovery only.
    # v4.1.1 FIX-PLANNER B: vary the stepback pattern across weeks so W4, W8,
    # W12, W16, W20 don't all render identically (rec60/lon150/lon150/rec60/…).
    # Rotation by week_num % 3 gives three distinct stepback flavours:
    #   0 → classic Issurin unload: recovery + long_z2
    #   1 → easy tempo spice: one tempo-easy midweek, still Z2 weekend
    #   2 → all-Z2: short Z2 instead of recovery spin, still easy weekend
    # The TSS budget is unchanged (plan_week still enforces 72% unload budget);
    # this only affects the chosen session_type so the visual mini-graphs and
    # downstream ZWO match get variety across stepbacks.
    if is_stepback:
        flavour = week_num % 3
        if is_weekend:
            dur = min(max_min, 150)
            return PlannedSession(
                day=date.today(), day_name="", session_type="long_z2",
                duration_min=dur, tss_estimate=dur / 60 * TSS_PER_HOUR["z2"],
                description=f"Step-back: lang Z2 ({dur}min), HR <156 bpm",
            )
        # Weekdays — rotate flavour across stepbacks.
        if flavour == 1:
            # First weekday stepback gets easy tempo; rest remain recovery.
            # hit_count==0 + day_in_week<=2 means it's the first training day.
            if hit_count == 0 and day_in_week <= 2:
                dur = min(max_min, 60)
                return PlannedSession(
                    day=date.today(), day_name="", session_type="tempo",
                    duration_min=dur,
                    tss_estimate=round(dur / 60 * TSS_PER_HOUR.get("tempo", 75) * 0.7),
                    description=f"Step-back easy tempo ({dur}min), HR 146-156 bpm",
                )
        elif flavour == 2:
            dur = min(max_min, 75)
            return PlannedSession(
                day=date.today(), day_name="", session_type="z2",
                duration_min=dur, tss_estimate=round(dur / 60 * TSS_PER_HOUR["z2"]),
                description=f"Step-back Z2 spin ({dur}min), HR 142-156 bpm",
            )
        dur = min(max_min, 60)
        return PlannedSession(
            day=date.today(), day_name="", session_type="recovery",
            duration_min=dur, tss_estimate=dur / 60 * TSS_PER_HOUR["recovery"],
            description=f"Step-back: recovery spin ({dur}min), HR <130 bpm",
        )

    # Weekend long ride — scale duration to fit TSS budget
    if is_weekend and "long_z2" in phase.session_types:
        # Budget-aware: don't exceed remaining TSS
        ideal_tss = remaining_tss / max(1, remaining_days + 1) * 1.5  # weekends get 1.5x share
        ideal_dur = int(ideal_tss / TSS_PER_HOUR["z2"] * 60)
        dur = min(max_min, ideal_dur, 180)
        dur = max(60, dur)  # minimum 1h
        tss = dur / 60 * TSS_PER_HOUR["z2"]
        # In build/peak phases, add sweet spot block at end
        if phase.name in ("build2", "peak") and dur >= 120:
            ss_min = 30
            tss += ss_min / 60 * (TSS_PER_HOUR["sweetspot"] - TSS_PER_HOUR["z2"])
            desc = f"Long ride: {dur-ss_min}min Z2 + {ss_min}min sweet spot (fatigue resistance)"
        else:
            desc = f"Lange Z2 rit ({dur}min), HR 142-156 bpm — key session of the week"
        return PlannedSession(
            day=date.today(), day_name="", session_type="long_z2",
            duration_min=dur, tss_estimate=round(tss),
            description=desc,
        )

    # HIT session (if allowed by phase and not maxed out)
    # Scale HIT budget by available training days — ensures 50%+ training days are Z2.
    # Fix: prevents 3-day weeks from becoming 67% HIT + 33% long_z2 (zero pure Z2).
    # Count total non-rest days this week from sessions_so_far + estimated remaining
    planned_training_days = sum(1 for s in sessions_so_far if s.session_type != "rest")
    total_training_days = planned_training_days + max(1, remaining_days) + 1  # +1 for this session
    effective_hit_cap = min(phase.hit_per_week, max(1, (total_training_days - 1) // 2))
    can_hit = hit_count < effective_hit_cap

    # Check 48h gap from last HIT — rolling 2-day calendar window across week
    # boundaries (PL2). Scan prev_week_sessions + sessions_so_far for the most
    # recent HIT with a set `.day` and compare against "today" by calendar
    # offset. Works whether the boundary falls mid-week (regenerate) or Monday
    # (full plan), so Sun VO2max → Mon VO2max is now correctly blocked.
    if can_hit:
        # Anchor "today" on the last stamped session's day + 1, else on the
        # last prev_week session's day + 1, else fall back to the legacy
        # within-week index path.
        base_ord: int | None = None
        anchor_src = None
        if sessions_so_far:
            anchor_src = next(
                (s for s in reversed(sessions_so_far) if getattr(s, "day", None)),
                None,
            )
        if anchor_src is None and prev_week_sessions:
            anchor_src = next(
                (s for s in reversed(prev_week_sessions) if getattr(s, "day", None)),
                None,
            )
            # prev_week's last day is Sunday; "today" is Monday = +1 day plus
            # however many session-less rest days we've skipped. sessions_so_far
            # is empty here so the +1 captures just the Sunday→Monday hop.
        if anchor_src is not None:
            base_ord = anchor_src.day.toordinal() + 1

        combined = list(prev_week_sessions or []) + list(sessions_so_far)
        for s in reversed(combined):
            if s.session_type not in hit_types:
                continue
            sd = getattr(s, "day", None)
            if sd is not None and base_ord is not None:
                if (base_ord - sd.toordinal()) < 2:
                    can_hit = False
            else:
                # Legacy path: same-week index-based gap
                if s in sessions_so_far:
                    idx = sessions_so_far.index(s)
                    if (day_in_week - idx) < 2:
                        can_hit = False
            break

    # Block HIT if yesterday was also HIT (48h rule) — defense-in-depth in
    # case last_session_hit was set from prev_week_sessions (week boundary).
    if can_hit and last_session_hit:
        can_hit = False

    if can_hit:
        # Randomized HIT selector — seeded RNG for variety across weeks
        # (Tønnessen 2024: few session models → stagnation; diverse stimuli > repetition)
        hit_done_types = [s.session_type for s in sessions_so_far if s.session_type in hit_types]

        # Build candidate list — evidence-based protocols per phase (8+ per phase)
        # Rønnestad 2014/2020: 30/15s micro-intervals +12% FTP, +12% 40min power
        # Seiler 2013: 4×8min @106% FTP = +16% threshold power (best long interval)
        # Helgerud 2007: 4×4min @90-95% HRmax = +5.5 ml/kg/min VO2max
        # Stöggl 2014: polarized (Z2 + VO2max) beats threshold-only for BOTH metrics
        # Rønnestad 2020: 30/15s vs 30/30s — both effective, variety prevents plateau
        # Billat 2001: 30/30s @vVO2max — classic VO2max accumulation protocol
        # Laursen 2002: different interval durations target different adaptations
        HIT_VARIANTS = {
            "base": [
                ("tempo", "Tempo steady ({dur}min) — 30min sustained Z3, HR 156-165 bpm"),
                ("tempo", "Tempo intervals ({dur}min) — 3×12min Z3 @76-85% FTP, 3min Z1 recovery"),
                ("sweetspot", "Sweet spot intro ({dur}min) — 2×15min @88-93% FTP, 5min recovery"),
                ("sweetspot", "Sweet spot ramp ({dur}min) — 3×10min @85→93% FTP, 4min recovery"),
                ("tempo", "Tempo progressive ({dur}min) — 20min @75% → 80% → 85% FTP, continuous"),
                ("tempo", "Tempo criss-cross ({dur}min) — 4×8min alternating 80/85% FTP, 2min Z1"),
                ("sweetspot", "Sweet spot over-geared ({dur}min) — 2×12min @88% FTP, 60rpm, strength focus"),
                ("tempo", "Tempo endurance ({dur}min) — 2×20min @78% FTP, 5min Z1 — long tempo block"),
            ],
            "build1": [
                ("sweetspot", "Sweet spot 3×15min @88-93% FTP — threshold preparation"),
                ("sweetspot", "Sweet spot progressive ({dur}min) — 3×12min @88→93→95% FTP, 4min recovery"),
                ("threshold", "Seiler 3×8min @103-106% FTP, 2min recovery — threshold build (Seiler 2013)"),
                ("threshold", "Threshold cruise ({dur}min) — 2×20min @95-100% FTP, 5min recovery"),
                ("overunder", "Over-unders 4×(3min @105% + 2min @90%) — lactate clearance"),
                ("overunder", "Over-unders short ({dur}min) — 6×(2min @108% + 1min @88%) — fast clearance"),
                ("vo2max", "Rønnestad micro: 3×(10×30s ON/15s OFF) @115% FTP, 3min rest (Rønnestad 2014)"),
                ("vo2max", "Helgerud 4×4min @90-95% HRmax, 3min active recovery (Helgerud 2007)"),
                ("tempo", "Tempo long ({dur}min) — 2×20min Z3 @80-85% FTP, 5min recovery"),
                ("sweetspot", "Sweet spot cadence ({dur}min) — 3×10min @90% FTP alternating 70/100rpm"),
            ],
            "build2": [
                ("vo2max", "Rønnestad micro: 3×(13×30s ON/15s OFF) @120% FTP, 3min rest — #1 FTP builder"),
                ("vo2max", "Rønnestad 30/30: 3×(10×30s ON/30s OFF) @130% FTP, 5min rest (Rønnestad 2020)"),
                ("vo2max", "Helgerud 4×4min @106-115% FTP, 3min Z1 recovery — VO2max ceiling"),
                ("vo2max", "VO2max 5×3min @115-120% FTP, 3min recovery — sustained VO2max time"),
                ("threshold", "Seiler 4×8min @105-108% FTP, 2min recovery — maximum threshold stimulus"),
                ("threshold", "Threshold sustained ({dur}min) — 2×20min @100-105% FTP — race power"),
                ("overunder", "Over-unders 2×15min: 3min @95% + 2min @108%, 5min rest — race toughness"),
                ("overunder", "Over-unders surge ({dur}min) — 5×(2min @110% + 2min @85%), 3min rest"),
                ("sweetspot", "Sweet spot progressive 3×20min @88→93% FTP — volume accumulation"),
                ("sprint", "Sprint power ({dur}min) — 8×30s max @150%+ FTP, 4.5min Z1 recovery — neuromuscular"),
            ],
            "peak": [
                ("vo2max", "Rønnestad peak: 3×(13×30s/15s) @125% FTP — maximum FTP stimulus"),
                ("vo2max", "VO2max 6×2min @120-130% FTP, 2min recovery — race-intensity VO2max"),
                ("vo2max", "Billat 30/30s: 2×(12×30s @vVO2max / 30s float), 5min rest — accumulation"),
                ("threshold", "Race tempo 2×15min @100-105% FTP — specific sustained power"),
                ("threshold", "Threshold surge ({dur}min) — 3×10min @FTP with 30s surge @120% each 3min"),
                ("overunder", "Over-unders 5×(2min @108% + 1min @90%) — race simulation"),
                ("overunder", "Over-unders attack ({dur}min) — 4×(1min @115% + 2min @95% + 1min @110%)"),
                ("sprint", "Sprint repeats ({dur}min) — 6×20s max + 3×1min @120% FTP — race finishing kicks"),
            ],
            "taper": [
                ("threshold", "Openers: 3×5min @FTP + 5×30s @120% — keep legs fresh"),
                ("vo2max", "Sharpener: 5×1min @120% FTP, 4min Z1 — maintain top-end, minimal fatigue"),
                ("sprint", "Activation sprints ({dur}min) — 4×15s max, 5min Z1 — neuromuscular prime"),
            ],
        }

        candidates = HIT_VARIANTS.get(phase.name, HIT_VARIANTS.get("build1", []))
        # Filter to session types allowed by phase
        candidates = [(t, d) for t, d in candidates if t in phase.session_types]

        # PL5: local RNG (same approach as match_zwo). Seeding the global
        # `random` module here polluted every other consumer — any code pulling
        # from the module default during plan generation got deterministic
        # output keyed on the last HIT-variant shuffle.
        # v4.1.1 FIX-PLANNER B: mix phase name into the seed. Previously the
        # seed only depended on (week_num, day_in_week, hit_done_types_len),
        # which collapsed Build1 W11 and W13 to the same session-type sequence
        # because the two weeks had the same (day, hit_done) state — only
        # week_num differed and that shifted the shuffle by a single multiply.
        # Also factor the phase-specific candidate count in: phases with tighter
        # lists are otherwise likelier to repeat.
        import random as _random
        _phase_hash = (abs(hash(phase.name)) & 0xFFFF) if phase.name else 0
        # v4.3.0 B3: mix seed_salt (% 7919) so each regeneration shifts
        # which HIT variant lands on each day.
        _salt_mix = (int(seed_salt) % 7919) if seed_salt else 0
        _hit_seed = (
            week_num * 1000
            + day_in_week * 7
            + len(hit_done_types) * 13
            + _phase_hash
            + len(candidates) * 31
            + _salt_mix
        )
        _hit_rng = _random.Random(_hit_seed)
        _hit_rng.shuffle(candidates)

        # Remove candidates whose session TYPE matches any HIT already done this week
        # (ensures no two consecutive HIT sessions use the same type within one week)
        if hit_done_types:
            filtered = [(t, d) for t, d in candidates if t not in hit_done_types]
            if filtered:
                candidates = filtered
            else:
                # All types used — at least avoid the most recent one
                candidates = [(t, d) for t, d in candidates if t != hit_done_types[-1]] or candidates

        if candidates:
            hit_type, desc_template = candidates[0]
            # v4.5.0 IMPL-PLANNER: drop hardcoded 75-min HIT cap. The new
            # sampler (sample_week_workouts) overwrites this slot via
            # generate_plan/regenerate_from_today, so this duration is now
            # only consulted as a structural skeleton hint by daily-adapt /
            # legacy callers. Use max_min (clamped to sane HIT range) so
            # those callers don't accidentally produce a 30-min VO2 slot.
            dur = max(45, min(max_min, 90))
            desc = desc_template.replace("{dur}", str(dur))
            return PlannedSession(
                day=date.today(), day_name="", session_type=hit_type,
                duration_min=dur, tss_estimate=round(dur / 60 * TSS_PER_HOUR.get(hit_type, 75)),
                description=desc,
            )

    # Default: Z2 endurance — scale to available time. v4.5.0 IMPL-PLANNER:
    # the sampler overwrites this in the main flow; this remains as a fallback
    # skeleton hint. Use available time (no 150-min cap) so legacy callers
    # see a duration consistent with the time budget.
    dur = max(45, min(max_min, 180))
    return PlannedSession(
        day=date.today(), day_name="", session_type="z2",
        duration_min=dur, tss_estimate=round(dur / 60 * TSS_PER_HOUR["z2"]),
        description=f"Z2 endurance ({dur}min), HR <LTHR. {'Long session — key training session of the week.' if dur >= 120 else ''}",
    )


def _nutrition_note(phase_name: str, session_type: str) -> str:
    """Nutrition guidance per phase + session type (Impey 2018, Stellingwerff 2019)."""
    if session_type == "rest":
        return "High protein, lower carbohydrates (3g/kg)"
    if phase_name == "base":
        if session_type in ("z2", "long_z2"):
            return "Train-low option: fasted or low-carb Z2 (fat oxidation)"
        return "Normally fueled (4g/kg carbs)"
    if phase_name in ("build1", "build2"):
        if session_type in ("vo2max", "threshold", "overunder", "sweetspot", "sprint"):
            return "Fuel the work: 6-7g/kg carbs, fueled before the session"
        return "Moderate carbs (4-5g/kg)"
    if phase_name == "peak":
        return "High carbs (6-8g/kg) — practice race nutrition"
    if phase_name == "taper":
        return "High carbs — glycogen loading"
    return ""


# ── ZWO matching ──────────────────────────────────────────────────────────────

def score_workout(zwo_data: dict) -> float:
    """Single source of truth for ZWO quality score (1.0–10.0).

    v4.2.0 IMPL-LIBRARY: closes the v4.1.1 Bug C PARTIAL — score sync drift
    between training_planner.load_workout_library and app.py /api/workouts.
    Both call sites now build the same structural input dict and route it
    through this helper, so the library browser and the planner rank
    workouts identically.

    Formula (per MASTER_DECISIONS_v42.md §3):
        raw = TSS×0.6/20 (capped 10)              # tss_factor
            + above_z2_pct × 4                     # structure_factor (40%)
            + variety_bonus  ∈ [0, 2]              # distinct above-Z2 power targets
            + vo2_bonus      ∈ {0, 1}              # any segment >105% FTP
            + aerobic_bonus  ∈ {0, 0.5}            # ≥50% Z2 + dur≥75min
        score = clamp(raw, 1, 10) rounded to int  # legacy filter semantics

    Tier mapping (canonical, MASTER §3):
        low    = 1.0–3.99
        medium = 4.0–6.99
        good   = 7.0–10.0

    Args:
        zwo_data: parsed structural metrics. Required keys:
            tss              (float) — TSS accumulator
            total_sec        (int)   — total workout seconds (>0)
            z1_sec..z6_sec   (int)   — per-zone seconds
            distinct_high_targets (set or int) — count of distinct above-Z2
                                  power targets (rounded to 1% FTP bins).
                                  Pass the *set* for full fidelity OR an
                                  int approximation.
            has_vo2_intensity (bool) — any segment >105% FTP

    Returns:
        float in [1.0, 10.0].  Callers may int() it for legacy display.

    Raises:
        Never. Missing keys default to 0/empty/False.
    """
    tss = float(zwo_data.get("tss", 0.0))
    total_sec = int(zwo_data.get("total_sec", 0))
    z1 = int(zwo_data.get("z1_sec", 0))
    z2 = int(zwo_data.get("z2_sec", 0))
    z3 = int(zwo_data.get("z3_sec", 0))
    z4 = int(zwo_data.get("z4_sec", 0))
    z5 = int(zwo_data.get("z5_sec", 0))
    z6 = int(zwo_data.get("z6_sec", 0))

    if total_sec <= 0:
        return 1.0

    dur_min = total_sec / 60.0

    # 60% TSS factor — 200 TSS → 10.0 (relaxed v4.1.1 from 250→10).
    tss_factor = min(10.0, tss / 20.0)

    # 40% above-Z2 fraction × 10 (Z3+Z4+Z5+Z6 / total).
    above_z2_pct = (z3 + z4 + z5 + z6) / total_sec
    structure_factor = above_z2_pct * 10.0

    # Variety bonus: distinct above-Z2 power targets (set), capped at 4 → +[0..2].
    # Accept either a set (proper) or an int (legacy approximation).
    dht = zwo_data.get("distinct_high_targets", 0)
    if isinstance(dht, (set, list, tuple)):
        variety_n = min(len(dht), 4)
    else:
        variety_n = min(int(dht), 4)
    variety_bonus = 2.0 * variety_n / 4.0

    # VO2 bonus: any segment >105% FTP.
    vo2_bonus = 1.0 if zwo_data.get("has_vo2_intensity", False) else 0.0

    # Aerobic stimulus bonus: long Z2 endurance.
    z2_fraction = z2 / total_sec if total_sec > 0 else 0.0
    aerobic_bonus = 0.5 if (z2_fraction >= 0.5 and dur_min >= 75) else 0.0

    raw = (
        tss_factor * 0.6
        + structure_factor * 0.4
        + variety_bonus
        + vo2_bonus
        + aerobic_bonus
    )
    return float(max(1.0, min(10.0, raw)))


def _classify_protocol(
    z1_sec: float, z2_sec: float, z3_sec: float,
    z4_sec: float, z5_sec: float, z6_sec: float,
    max_power: float, filename: str,
) -> str:
    """Classify a workout into a Protocol category.

    v4.1.2 IMPL-CLASSIFIER: PREFERS the content-based 12-rule cascade
    (scripts/classify_library_content.py) over the filename-prefix heuristic.
    The cascade applies Coggan zones + Seiler/Billat/Allen/Coggan dose
    minima to the actual ZWO power profile (see /tmp/research_workout_classification.md
    §5/§7 for citations). Filename heuristic remains as a fallback when:
        * The content cache is missing
        * A specific file isn't in the cache (e.g. just-added workout)
        * The cascade returned low confidence (<0.6)
    """
    # 1. Content-based cache (preferred). Populated by running
    #    `python3 scripts/classify_library_content.py --all`. Confidence
    #    threshold of 0.6 gates against barely-meets-dose matches; below
    #    that we trust the filename more.
    content_cache = _load_content_classifications()
    content_entry = content_cache.get(filename) if content_cache else None
    if content_entry and content_entry.get("confidence", 0) >= 0.6:
        primary = content_entry.get("primary", "mixed")
        protocol = _CONTENT_TO_PROTOCOL.get(primary)
        if protocol:
            return protocol

    # 2. Filename prefix heuristic (fallback).
    # v4.1.1 FIX-PLANNER A: extended with the 6 prefix families that were
    # falling through to the dominant-zone heuristic (vo2_, over_under_,
    # sprints_, anaerobic_, sweet_spot_, pyramid_). Previously 30%+ of plan
    # sessions showed e.g. type=tempo with zwo=vo2_…zwo because the heuristic
    # mis-classified a VO2max workout as Tempo (warmup+rest zones dominated
    # by time). ORDER MATTERS — vo2max_ must stay ABOVE vo2_ because string
    # prefix matching: "vo2max_foo" also satisfies startswith("vo2_").
    fname = filename.lower()
    if fname.startswith("vo2max_"):
        return "VO2max"
    if fname.startswith("vo2_"):
        return "VO2max"
    if fname.startswith("threshold_"):
        return "Threshold"
    if fname.startswith("supra_threshold"):
        return "Threshold"
    if fname.startswith("sweetspot_"):
        return "Sweet Spot"
    if fname.startswith("sweet_spot_"):
        return "Sweet Spot"
    if fname.startswith("over_under_"):
        return "Over-Unders"
    if fname.startswith("sprints_"):
        return "Sprint"
    if fname.startswith("anaerobic_"):
        return "Anaerobic"
    if fname.startswith("pyramid_"):
        # Multi-zone protocols — treat as "Mixed" so match_zwo's fallback
        # routing can surface them for sweetspot/threshold/vo2max slots.
        return "Mixed"
    if fname.startswith("ftp_test_"):
        return "FTP Test"
    if fname.startswith("tempo_"):
        return "Tempo"
    if fname.startswith("recovery_"):
        return "Recovery"
    if fname.startswith("z2_") or fname.startswith("endurance_"):
        return "Endurance"
    if fname.startswith("ramp_"):
        return "Threshold"
    if fname.startswith("warmup_"):
        return "Recovery"
    if fname.startswith("intervals_"):
        # Distinguish Anaerobic / VO2max / Threshold by peak power.
        # NOTE: must test higher threshold first — 1.15 also passes 1.30.
        if max_power >= 1.30:
            return "Anaerobic"
        if max_power >= 1.15:
            return "VO2max"
        if max_power >= 0.95:
            return "Threshold"
        if max_power >= 0.85:
            return "Sweet Spot"
        return "Mixed"

    # Fallback: zone-based classification (matches app.py /api/workouts logic)
    zones = [z1_sec, z2_sec, z3_sec, z4_sec, z5_sec, z6_sec]
    dom_idx = zones.index(max(zones)) if any(zones) else 1
    protocol_map = {
        0: "Recovery", 1: "Endurance", 2: "Tempo",
        3: "Sweet Spot", 4: "VO2max", 5: "Anaerobic",
    }
    return protocol_map.get(dom_idx, "Mixed")


def load_workout_library() -> list[dict]:
    """Scan WORKOUT_DIR (flat) and extract metadata by parsing each ZWO XML.

    Returns list of dicts shaped like the legacy rows so that match_zwo()
    continues to work without changes (the library is now the flat ZWO
    directory; no workout_analysis.csv exists):
      Name, Category, File, Duration(min), TSS, IF, Score, Protocol,
      Z1%..Z6%, Notes.

    Zone-bucket convention: all boundaries use half-open intervals of the form
    ``[low, high)`` expressed as percent of FTP, i.e. Z1=[0,56), Z2=[56,76),
    Z3=[76,91), Z4=[91,106), Z5=[106,121), Z6=[121,inf). A sample at exactly
    the boundary (e.g. 76%) lands in the HIGHER zone. Identical in app.py.
    """
    # Module-level cache keyed by str(WORKOUT_DIR). Value is
    # (mtime_hash, list_of_workouts). Re-parsing 3,000+ ZWO files on every call
    # is ~600ms+ of disk I/O, and the library changes rarely.
    #
    # v1.8.1 SPEED-A: two-tier cache validation.
    #   * Fast tier: a single os.stat(WORKOUT_DIR) on the directory itself.
    #     The directory's st_mtime ticks whenever a file is added/removed/
    #     renamed — sufficient for the common case (library is append-only
    #     in normal use). On match, return the cached list with NO per-file
    #     stat calls. Hot-call cost drops from ~100ms (glob+stat over 3k
    #     files) to <5ms.
    #   * Slow tier: when the fast validator misses, fall through to the
    #     full glob+max-mtime sweep that catches in-place content edits.
    global _WORKOUT_LIB_CACHE, _WORKOUT_LIB_FAST_VALIDATOR
    cache_key = str(WORKOUT_DIR)

    if not WORKOUT_DIR.exists():
        return []

    # Fast-path: single stat on the workouts directory itself. If it matches
    # the validator stored alongside the cache entry, the cached list is fresh.
    try:
        dir_mtime = WORKOUT_DIR.stat().st_mtime
    except OSError:
        dir_mtime = 0.0
    fast_key = (_CLASSIFIER_VERSION, dir_mtime)
    cached_fast = _WORKOUT_LIB_FAST_VALIDATOR.get(cache_key)
    cached = _WORKOUT_LIB_CACHE.get(cache_key)
    if cached and cached_fast == fast_key:
        return cached[1]

    # Slow-path: enumerate files to compute the precise validator. This
    # catches edits that don't tick the directory's mtime (in-place rewrites
    # of an existing file).
    zwo_paths = sorted(WORKOUT_DIR.glob("*.zwo"))
    # Hash the (count, max_mtime) — fast and sufficient to detect edits/adds/removes.
    try:
        max_mtime = max((p.stat().st_mtime for p in zwo_paths), default=0.0)
    except OSError:
        max_mtime = 0.0
    mtime_hash = (_CLASSIFIER_VERSION, len(zwo_paths), max_mtime)

    if cached and cached[0] == mtime_hash:
        # Slow-path confirmed the cache is still valid (directory's mtime
        # ticked but no .zwo file actually changed). Refresh the fast
        # validator so the next call short-circuits.
        _WORKOUT_LIB_FAST_VALIDATOR[cache_key] = fast_key
        return cached[1]

    # v1.10.1 SPEED-INDEX: before the 4,198-file XML sweep (~3s cold), try the
    # consolidated on-disk row index. One JSON read (~0.2s) yields rows that are
    # byte-identical to the XML path (the builder serialized that path's own
    # output). Validated against the SAME (count, max_mtime) signal as mtime_hash
    # above, so a stale index can never feed the planner old rows — it just falls
    # through to the parse below. Populate both caches on a hit so repeat calls
    # stay at 0.000s exactly as before.
    indexed_rows = _read_library_index(len(zwo_paths), max_mtime)
    if indexed_rows is not None:
        _WORKOUT_LIB_CACHE[cache_key] = (mtime_hash, indexed_rows)
        _WORKOUT_LIB_FAST_VALIDATOR[cache_key] = fast_key
        return indexed_rows

    workouts: list[dict] = []
    for zwo_path in zwo_paths:
        try:
            tree = ET.parse(zwo_path)
        except (ET.ParseError, OSError):
            continue
        root = tree.getroot()
        name = (root.findtext("name") or zwo_path.stem).strip()
        description = (root.findtext("description") or "").strip()
        workout_el = root.find("workout")
        if workout_el is None:
            continue
        # T5 (v4.1.0): pick up <tags><tag name="…"/></tags> so we can skip
        # ftp_test-tagged workouts from normal selection (they shouldn't
        # land on a random Tuesday).
        zwo_tags: list[str] = []
        tags_el = root.find("tags")
        if tags_el is not None:
            for tag_el in tags_el.findall("tag"):
                tnm = tag_el.get("name")
                if tnm:
                    zwo_tags.append(tnm.strip())

        total_sec = 0
        z1_sec = z2_sec = z3_sec = z4_sec = z5_sec = z6_sec = 0
        tss_accum = 0.0
        max_power = 0.0
        # FIX-CONTRACT C8: structure-bonus inputs. Collect the distinct
        # above-Z2 power targets (rounded to 1% FTP bins) so the score
        # formula can reward "real" interval variety over a single
        # hammered SteadyState. Same-power repeats (e.g. 5x3min @ 110%)
        # count as ONE distinct target — it's variety we're pricing,
        # not reps, because the TSS factor already values volume. We
        # also track whether any segment breaches 105% FTP (VO2 floor)
        # for the +1 VO2 bonus.
        distinct_high_targets: set = set()
        has_vo2_intensity = False

        def _acc_zone(power_pct: float, dur_s: int):
            # Half-open buckets: [low, high). Value at boundary → next zone up.
            nonlocal z1_sec, z2_sec, z3_sec, z4_sec, z5_sec, z6_sec
            if power_pct < 56: z1_sec += dur_s
            elif power_pct < 76: z2_sec += dur_s
            elif power_pct < 91: z3_sec += dur_s
            elif power_pct < 106: z4_sec += dur_s
            elif power_pct < 121: z5_sec += dur_s
            else: z6_sec += dur_s

        def _acc_structure(power_pct: float):
            # FIX-CONTRACT C8: track distinct above-Z2 targets + VO2 floor.
            nonlocal has_vo2_intensity
            if power_pct > 75:  # above-Z2 (≥76% FTP)
                distinct_high_targets.add(round(power_pct))
            if power_pct > 105:  # VO2 floor (Coggan Z5 edge)
                has_vo2_intensity = True

        for seg in workout_el:
            tag = seg.tag
            if tag in ("Warmup", "Cooldown", "Ramp"):
                dur = int(float(seg.get("Duration", 0)))
                plo = float(seg.get("PowerLow", 0.5))
                phi = float(seg.get("PowerHigh", 0.7))
                total_sec += dur
                # TSS for a linear power ramp from a→b over duration T (hours):
                #   TSS = (1/T) ∫₀ᵀ (a + (b-a)·t/T)² · 100 dt · T
                #       = (a² + a·b + b²)/3 · T · 100
                # For a constant (a==b) this reduces to a²·T·100 as expected.
                # Using mean² underestimates when a≠b (e.g. 0.5→0.9 gives
                # mean²=0.49 but true integral 0.5033 → ~2.7% high on TSS).
                tss_accum += (plo * plo + plo * phi + phi * phi) / 3 * (dur / 3600) * 100
                max_power = max(max_power, plo, phi)
                # v2.0.6 — integrate the ramp across the zones it SWEEPS, not one
                # avg-power bucket. Binning the whole duration at mean power dumped
                # a 50%→100% ramp's Z3/Z4 time into Z2, so ramp-heavy workouts read
                # ~95% easy — e.g. a 33%-Z3 file passed the recovery 25%-Z3 ceiling
                # and landed on a recovery day. Slice the linear ramp; bin each
                # slice at its local power so per-zone seconds reflect reality.
                _RAMP_SLICES = 20
                for _i in range(_RAMP_SLICES):
                    _acc_zone((plo + (phi - plo) * (_i + 0.5) / _RAMP_SLICES) * 100, dur / _RAMP_SLICES)
                # Warmup/Ramp peaks contribute to structure + VO2 detection
                # (ramp-test workouts genuinely hit VO2 at the top step).
                _acc_structure(phi * 100)
            elif tag == "SteadyState":
                dur = int(float(seg.get("Duration", 0)))
                p = float(seg.get("Power", 0.65))
                total_sec += dur
                tss_accum += dur / 3600 * (p ** 2) * 100
                max_power = max(max_power, p)
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
                max_power = max(max_power, on_p, off_p)
                _acc_zone(on_p * 100, reps * on_s)
                _acc_zone(off_p * 100, reps * off_s)
                _acc_structure(on_p * 100)
                _acc_structure(off_p * 100)
            elif tag == "FreeRide":
                dur = int(float(seg.get("Duration", 0)))
                total_sec += dur
                # Assume ~Z2 effort for FreeRide
                tss_accum += dur / 3600 * (0.65 ** 2) * 100
                _acc_zone(65, dur)

        dur_min = total_sec / 60
        if dur_min == 0:
            continue

        if_val = (tss_accum / (dur_min / 60)) ** 0.5 / 10 if dur_min > 0 else 0.5
        zp = lambda s: round(s / total_sec * 100, 1) if total_sec else 0.0

        protocol = _classify_protocol(
            z1_sec, z2_sec, z3_sec, z4_sec, z5_sec, z6_sec,
            max_power, zwo_path.name,
        )
        # v4.1.1 FIX-PLANNER A: tag override. If the author explicitly
        # marks a ZWO as ftp_test, force Protocol="FTP Test" regardless of
        # whatever the filename / zone heuristic inferred. Some ftp_test
        # workouts lack the `ftp_test_` filename prefix but carry the tag.
        _tag_names = {t.lower() for t in zwo_tags}
        if "ftp_test" in _tag_names:
            protocol = "FTP Test"

        # Score rubric (fix v4.1.0 FIX-CONTRACT C8, rebalanced v4.1.1
        # FIX-PLANNER C for a ~30/50/20 low/med/good distribution).
        #   * TSS factor (60%) — sustained volume signal. v4.1.1: divisor
        #     relaxed from 25→20 so 200 TSS → 10.0 (was 250→10). Most of
        #     the library sits at 50–150 TSS, so the old normalisation
        #     pushed the top bucket far out of reach (<1% scored ≥9).
        #   * Above-Z2 time factor (40%) — fraction of session spent above
        #     Z2 (Z3+Z4+Z5+Z6). Captures "meaningful time in meaningful
        #     zones" without needing segment-level parse.
        # Plus three additive bonuses (not weighted, applied AFTER the 60/40
        # blend and BEFORE the final [1,10] clamp):
        #   * Structure variety bonus — count of distinct above-Z2 power
        #     targets, capped at 4, mapped to +[0..2]. A workout with
        #     5x3min@110 has 1 distinct target (+0.5 after cap math); a
        #     proper over-unders (2x10' @ 90/105/90/105) has 2 (+1.0);
        #     a 5-zone progression (Z2 pyramid) has 4+ (+2.0). Rewards
        #     "real" variety, not rep count — volume is already in TSS.
        #   * VO2 bonus (+1) — any segment >105% FTP (Coggan Z5 floor).
        #   * Aerobic stimulus bonus (v4.1.1) — a long Z2 session with
        #     substantial Z2 time (≥50% Z2 AND total dur ≥75min) gets
        #     +0.5 so a 90-min steady endurance ride doesn't sink below
        #     score 5 just because it lacks structure variety. Mirrors
        #     Seiler: Z2 volume IS a training quality signal, not noise.
        # Output clamped to [1, 10] so existing filters (Score ≥ 3 in
        # match_zwo, min_score query param in /api/workouts) keep their
        # semantics.
        # v4.2.0 IMPL-LIBRARY: route through shared score_workout helper so
        # /api/workouts and the planner rank workouts identically (closes
        # v4.1.1 Bug C PARTIAL — distinct_high_targets vs zone-count drift).
        score = max(1, min(10, int(round(score_workout({
            "tss": tss_accum,
            "total_sec": total_sec,
            "z1_sec": z1_sec, "z2_sec": z2_sec, "z3_sec": z3_sec,
            "z4_sec": z4_sec, "z5_sec": z5_sec, "z6_sec": z6_sec,
            "distinct_high_targets": distinct_high_targets,
            "has_vo2_intensity": has_vo2_intensity,
        })))))

        # v4.1.2 IMPL-CLASSIFIER: surface content-classification fields on the
        # library row. These are looked up from the on-disk cache populated
        # by scripts/classify_library_content.py. If the cache is missing
        # (e.g. fresh checkout, user hasn't run the script), the fields are
        # populated with neutral defaults so downstream consumers don't crash.
        _content_entry = (_load_content_classifications() or {}).get(zwo_path.name) or {}
        content_class = _content_entry.get("primary") or ""
        content_confidence = _content_entry.get("confidence") or 0.0
        secondary_flags = _content_entry.get("secondary_flags") or {}

        workouts.append({
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
            # T5 (v4.1.0): surface tag list for the planner's test-skip logic.
            "Tags": zwo_tags,
            # v4.1.2 IMPL-CLASSIFIER: content-based primary type + per-rule
            # confidence + secondary hybrid flags (has_threshold_work,
            # has_vo2_work, has_sprints, has_sweet_spot_work,
            # pattern_over_under, pattern_microinterval, polarized_consistent,
            # pyramidal_consistent). Empty when the content cache is absent.
            "ContentClass": content_class,
            "ContentConfidence": content_confidence,
            "SecondaryFlags": secondary_flags,
        })

    _WORKOUT_LIB_CACHE[cache_key] = (mtime_hash, workouts)
    # v1.8.1 SPEED-A: store the fast-path validator alongside the cache so
    # subsequent calls can short-circuit on a single dir-stat.
    _WORKOUT_LIB_FAST_VALIDATOR[cache_key] = fast_key
    # v1.10.1 SPEED-INDEX: self-heal the on-disk index after a full parse so the
    # NEXT cold process skips the XML sweep. Best-effort (read-only dir is fine).
    # Keyed by the same (count, max_mtime) we just validated, so it stays fresh
    # until a *.zwo actually changes. Writing this dotfile does NOT invalidate
    # the index (the validator is over *.zwo, not the dir mtime).
    _write_library_index(workouts, len(zwo_paths), max_mtime)
    return workouts


class NoCandidateWorkoutError(ValueError):
    """Raised by match_zwo when the library has no workout for the slot.

    PL6: replaces the old silent fall-through to ``zwo_file=""``. Callers that
    want the previous behaviour (mark session as unmatched and continue) pass
    ``raise_on_empty=False`` (the bulk planner does this; the aggregate count
    is surfaced in one warning at the end of ``generate_plan``). Callers that
    treat an empty pool as a user-visible problem (daily-adapt rematch, UI-
    triggered single-session swap) leave the default and catch the exception.
    """


def match_zwo(
    session: PlannedSession, library: list[dict],
    week_num: int = 0, day_idx: int = 0, used_names: set = None,
    plan_start_date: date | None = None,
    raise_on_empty: bool = False,
    seed_salt: int = 0,
    exact_duration: bool = False,
    hr_bias: bool = False,
) -> PlannedSession:
    """Find a ZWO workout matching this session, rotating for variety.

    hr_bias (v2.5.0 W5): soft preference for HR-guidable files when the
    athlete trains by heart rate (no power meter). Micro-interval/sprint
    classes (vo2_short/anaerobic/neuromuscular) degrade to all-RPE on a head
    unit, so they take a small score penalty — ONLY when the pool has ≥3
    guidable alternatives (a penalty, never a filter; a slot still fills).
    Callers thread the athlete's target_mode from app.py — match_zwo itself
    never reads profile state (stays a pure function of its args). Default
    False = power mode = bit-identical behaviour.

    Args:
        session: The planned session to match.
        library: Rows from load_workout_library() (flat ZWO library — was
            previously workout_analysis.csv, now parsed directly from the
            .zwo files on disk).
        week_num: Current week number (rotation seed for variety).
        day_idx: Day index within the week (secondary seed).
        used_names: Set of workout names already used this plan — avoids repeats.
        plan_start_date: Anchor date for the deterministic RNG seed so that
            re-matching the same session (e.g. a regenerate-from-today pass)
            always returns the same workout. If None, falls back to the
            session's own date, which is also plan-relative and stable.
        raise_on_empty: If True and no candidate matches, raise
            NoCandidateWorkoutError instead of silently returning with
            ``zwo_file=""``. Default False preserves bulk-plan behaviour where
            ``generate_plan`` surfaces an aggregate warning; ad-hoc rematch
            paths (daily-adapt, reforecast swap-in) set this to True so the UI
            can show a clear error instead of a blank workout.
        seed_salt: v4.3.0 B3 fix — extra entropy mixed into the seed so that
            ``/api/plan/regenerate`` produces a genuinely different ZWO pick
            each time. Defaults to 0 (deterministic mode for testing). When
            non-zero, also shuffles the top-50 score-weighted candidate pool
            so ranked-equal workouts don't always tie-break the same way.
        exact_duration: v1.8.24 — reshuffle/rematch mode. When True the ±25%
            hard duration gate is dropped and the scored candidate pool is
            collapsed to the single CLOSEST-duration tier before the variety
            pick, so the returned workout is the closest the library offers to
            the slot (diff 0 when a same-duration file exists) — never a far
            one. The category/type gate and Score≥3 filter still apply (we
            widen duration only, never workout type). Default False keeps the
            bulk-generation behaviour (±25% gate + score-weighted pick) and the
            v4.5.0 variety/distribution contract byte-for-byte.
    """
    if session.session_type == "rest":
        return session

    if used_names is None:
        used_names = set()

    # v1.8.25 — match on the canonical CONTENT class (content-based since v4.1.2),
    # not the Protocol zone-heuristic string. The Protocol bucket and ContentClass
    # disagree on hundreds of files (e.g. 280 Sprint-protocol files are content
    # neuromuscular, 120 VO2max-protocol are vo2_short); bucketing on Protocol
    # mis-scored those. `cat` is now `_content_class_for_row(w)` (ContentClass with
    # filename fallback — never empty), aligning match_zwo with the v4.5 sampler.
    # Map planner session_types → primary CONTENT class.
    type_to_category = {
        "z2":         "endurance",
        "long_z2":    "endurance",
        "recovery":   "recovery",
        "sweetspot":  "sweet_spot",
        "threshold":  "threshold",
        "vo2max":     "vo2max",
        "overunder":  "over_under",
        "tempo":      "tempo",
        "sprint":     "neuromuscular",
    }
    # Fallback content classes (incl. the matching *_ladder variants so ladder
    # sessions stay reachable). The Score + duration filters + the easy-slot Z3
    # gate (below) keep the wrong ones out.
    type_to_fallback = {
        "z2":         ["endurance", "recovery"],
        "long_z2":    ["endurance"],
        "recovery":   ["recovery", "endurance"],
        "sweetspot":  ["sweet_spot", "sweet_spot_ladder", "threshold", "tempo"],
        "threshold":  ["threshold", "threshold_ladder", "sweet_spot", "over_under"],
        "vo2max":     ["vo2max", "vo2_short", "vo2_ladder", "anaerobic"],
        "overunder":  ["over_under", "threshold"],
        "tempo":      ["tempo", "tempo_intervals", "tempo_ladder", "sweet_spot"],
        "sprint":     ["neuromuscular", "anaerobic", "sprint"],
    }

    primary_cat = type_to_category.get(session.session_type, "endurance")
    fallback_cats = type_to_fallback.get(session.session_type, [primary_cat])
    # Easy-slot grey-zone ceiling (Z3+Z4+Z5+Z6 %): a z2/recovery slot must not
    # pull a file with a tempo/SS finisher (over-cooks the easy day, breaks
    # polarization). Mirrors the sampler's hard zone gate. None = no gate.
    _easy_z345_ceiling = {"recovery": 25.0, "z2": 40.0, "long_z2": 40.0}.get(
        session.session_type)
    target_dur = session.duration_min

    # Build scored candidate pool from ALL matching workouts
    candidates = []
    seen_names = {}  # deduplicate: keep best score per workout name
    # T5 (v4.1.0): skip ftp_test-tagged workouts from normal weekly selection.
    # The ZWO library ships explicit `<tag name="ftp_test"/>` on both Coggan
    # 20-min and Ramp test files. Without this guard those tests can land on
    # any Tuesday via the dominant-zone classifier (Coggan-20 reads as
    # Sweet Spot / Threshold). Planner types "ftp_test" explicitly opt in.
    want_test = session.session_type == "ftp_test"
    for w in library:
        try:
            cc_row = _content_class_for_row(w)
            # v2.0.6 — sprint/neuromuscular LOAD gate: keep threshold/anaerobic-
            # load files (IF > ceiling) out of sprint slots. See _SPRINT_SLOT_IF_CEILING.
            if session.session_type == "sprint" and float(w.get("IF") or 0) > _SPRINT_SLOT_IF_CEILING:
                continue
            # B5 — easy-slot LOAD gate: keep interval-structured files (IF above
            # the easy ceiling, e.g. an "Endurance 20s/2min 6x") out of
            # z2/long_z2/recovery slots even when the classifier filed them as
            # endurance. The Z3-6 time-% gate below misses spiky efforts.
            if (session.session_type in ("z2", "long_z2", "recovery")
                    and float(w.get("IF") or 0) > _EASY_SLOT_IF_CEILING):
                continue
            # v1.8.25 — class-aware Score floor. Endurance/recovery are low
            # intensity ⇒ low Score BY CONSTRUCTION (the rubric weights TSS +
            # above-Z2 time), so the blanket Score≥3 gate hid ~475 of them from
            # the reshuffle/fallback path. Admit them at Score≥1 BUT require
            # Duration≥20min so the tiny steady stubs (8-18min, ~empty content,
            # ContentClass-empty → filename fallback "endurance") stay excluded —
            # else exact_duration's closest-tier collapse would prefer a 10-min
            # stub on a short slot. All other classes keep the Score≥3 bar.
            if cc_row in ("endurance", "recovery"):
                if w["Score"] < 1 or (w["Duration(min)"] or 0) < 20:
                    continue
            elif w["Score"] < 3:
                continue
            tags_lower = {t.lower() for t in (w.get("Tags") or [])}
            if "ftp_test" in tags_lower and not want_test:
                continue
            if want_test and "ftp_test" not in tags_lower:
                continue
            dur_diff = abs(w["Duration(min)"] - target_dur)
        except (KeyError, TypeError) as _e:
            # v1.6.1 — library entry has malformed metadata (e.g. missing
            # Score / Duration field). Skip + log; the rest of the loop
            # continues so one bad row doesn't tank the whole match.
            _tp_log_error(error_codes.Codes.MATCH_ZWO_MALFORMED_META, exc=_e,
                          file=(w or {}).get("File", "?"))
            continue
        # PL7: duration bucket uses <= at the 120-min boundary so a 120-min
        # workout picks up the wider (60-min) tolerance. The previous
        # `target_dur >= 120` vs strict `<` cousin step caused a 120-min
        # target to admit a 180-min workout but a 119-min target to reject
        # it — a jumpy discontinuity right at the base/long-ride transition.
        # Keeping `>=` for the 120+ bucket ensures inclusion at exactly 120.
        # v1.8.18 follow-up — tighten the hard duration gate from a loose flat
        # ±40/±60 min to ±25% of the target (floor 15 min for short slots).
        # The flat gate let an 82-min slot admit a 120-min file and a 120-min
        # slot a 175-min file; the score-weighted top-50 random pick then still
        # surfaced those far files. A relative gate BOUNDS the worst-case
        # mismatch to ~25% of the slot regardless of the random draw, while
        # leaving ample candidates for common type+duration combos (the
        # coverage fallback below handles any genuinely sparse band).
        max_diff = max(15.0, target_dur * 0.25)
        # v1.8.24 — exact_duration (reshuffle) admits ALL durations here and
        # collapses to the closest tier after scoring (see below). The ±25%
        # hard gate is bulk-generation only; the reshuffle UI must return the
        # closest-duration workout the library offers, never a far one.
        if not exact_duration and dur_diff > max_diff:
            continue

        # v1.8.25 — match on the content class computed above, not the Protocol
        # zone-heuristic string (which disagreed with ContentClass on hundreds
        # of files and mis-scored them).
        cat = cc_row

        # Score: category match + evidence score + duration proximity
        score = float(w["Score"])

        # v1.3.4 fix: ftp_test category check is unreliable — the ftp_test
        # ZWOs (Coggan-20, Ramp) have varying Protocol values that don't
        # consistently match "Endurance"/"Threshold"/etc, so the category
        # gate dropped all candidates. The ftp_test tag filter above is
        # sufficient identification; skip the category gate here for tests.
        if want_test:
            score += 5
        elif cat == primary_cat:
            score += 5  # primary category match
        elif cat in fallback_cats:
            score += 2  # fallback match
        else:
            continue  # skip non-matching categories

        # v2.2.12 — content-fit penalty for PURE high-intensity slots
        # (sprint / VO2max). Some library files carry a hard label but are
        # mostly tempo/threshold with little top-end — e.g. a "neuromuscular
        # 4×10s" file that's 40s of sprint over ~50min of sweet-spot — and were
        # winning sprint slots over genuine high-intensity files. SOFT penalty,
        # not exclusion: a cleaner hard file (real Z5/Z6 content, or not
        # mid-dominated) outranks it, but if it's the only candidate that fits
        # the duration the slot still fills. Threshold/SS/tempo/over-under slots
        # are untouched — mid IS the point there.
        if session.session_type in ("sprint", "vo2max") and not want_test:
            _mid_pct = float(w.get("Z3%", 0) or 0) + float(w.get("Z4%", 0) or 0)
            _top_pct = float(w.get("Z5%", 0) or 0) + float(w.get("Z6%", 0) or 0)
            if _mid_pct >= 40 and _top_pct < 10:
                score -= 5  # mid-dominated, low top-end → poor fit for a hard slot

        # v1.8.25 — easy-slot grey-zone HARD gate (mirrors the sampler). A
        # z2/recovery slot must NOT admit a file with a tempo/SS finisher
        # (z345 over the ceiling) — that over-cooks an easy day and breaks
        # polarization. Replaces the toothless soft −3 for easy slots (the
        # soft −3 below still applies to non-easy slots).
        if _easy_z345_ceiling is not None and not want_test:
            z345 = (float(w.get("Z3%", 0) or 0) + float(w.get("Z4%", 0) or 0)
                    + float(w.get("Z5%", 0) or 0) + float(w.get("Z6%", 0) or 0))
            if z345 >= _easy_z345_ceiling:
                continue
            # v1.9.2 — z345% alone misses Z2-DOMINANT files that EMBED structured
            # intensity (e.g. an "endurance" 90-min ride with 6×2min @ FTP +
            # VO2 microbursts reads ~29% above-Z2 but is NOT an easy day). The
            # content classifier's secondary flags catch these precisely; reject
            # any structured FTP / VO2 / sprint / over-under work on an easy
            # slot. Brief AEROBIC surges (Z2+85-90%) don't set these flags, so
            # genuine endurance-with-surges files are still admitted.
            _sf = w.get("SecondaryFlags") or {}
            if any(_sf.get(k) for k in
                   ("has_threshold_work", "has_vo2_work", "has_sprints", "pattern_over_under")):
                continue

        # v1.8.18 follow-up — duration proximity penalty. The old absolute
        # ``dur_diff / 10`` was too gentle vs the +5 category bonus: a 37-min
        # gap cost only 3.7, so a wrong-duration primary-category file beat a
        # closer fallback (e.g. an 82-min slot resolving to a 120-min file).
        # Use a RELATIVE penalty (gap as a fraction of target) so it scales
        # with slot length and reliably outweighs the category bonus once the
        # gap is large. K=14: a 20% gap ≈ 2.8, a 45% gap ≈ 6.3 (> the +5
        # primary bonus → a closer fallback wins). 5% gap ≈ 0.7 (negligible).
        score -= (dur_diff / max(target_dur, 30.0)) * 14.0
        if w.get("Z3%", 0) > 40:
            score -= 3  # penalize heavy grey zone

        # Soft penalty for recently used (no hard exclusion during build)
        if w["Name"] in used_names:
            score -= 15

        # Deduplicate by name: keep only the highest-scoring variant
        name = w["Name"]
        if name in seen_names:
            if score > seen_names[name][0]:
                seen_names[name] = (score, w)
        else:
            seen_names[name] = (score, w)

    candidates = list(seen_names.values())

    # v2.2.13 — STRUCTURAL duration match (applies to BULK generation, not just
    # reshuffle). Previously the closest-duration collapse below was gated on
    # `exact_duration`, so bulk plan generation skipped it: the score-weighted
    # top-50 random draw could surface a far-duration file inside the ±25% gate
    # (a 45-min sprint slot resolving to a 57-min file), which made the title,
    # the Duration stat and the power chart disagree. We now ALWAYS restrict the
    # variety pool to files whose duration genuinely matches the slot, so the
    # matched file ≈ the slot and the prescription/file decoupling disappears at
    # the source (no display band-aids needed):
    #   1. prefer files within ~8% of the slot duration (keeps variety among
    #      genuinely-close files);
    #   2. if none that close, fall back to the single closest-duration tier
    #      (+0.5 min epsilon to tie whole-minute-equal files).
    # exact_duration's only remaining job is widening the hard ±25% gate above
    # (line ~3029) so reshuffle can reach further when the band is sparse; the
    # collapse itself is now unconditional.
    if candidates:
        _tight = max(target_dur * 0.08, 3.0)
        _near = [
            c for c in candidates
            if abs(c[1]["Duration(min)"] - target_dur) <= _tight
        ]
        if _near:
            candidates = _near
        else:
            best_diff = min(abs(c[1]["Duration(min)"] - target_dur) for c in candidates)
            candidates = [
                c for c in candidates
                if abs(c[1]["Duration(min)"] - target_dur) <= best_diff + 0.5
            ]

    if not candidates:
        # v1.3.4 fix: when target_dur exceeds library coverage (e.g. a 222-min
        # vo2max slot from heavy weekend availability — library tops out at
        # 150min vo2max), DON'T leave zwo_file="". The user's symptom was
        # yellow ⚠ on every long-duration cell; zwo_file="" is the only path
        # `_classify_card_state` uses to flag missing_workout. Instead, fall
        # back to the LONGEST workout in the right category and let the
        # dashboard surface "extend on trainer" via the existing showGap
        # banner. The session keeps matched=True because the content is
        # correct — only the duration is short.
        coverage_pool: list = []
        for w in library:
            # v1.8.25 — same class-aware floor + ContentClass basis as the main
            # loop, so the fallback doesn't re-introduce the Protocol drift / the
            # endurance-recovery exclusion it was meant to bypass.
            cc_row = _content_class_for_row(w)
            # v2.0.6 — same sprint/neuromuscular LOAD gate as the main loop, so the
            # coverage fallback can't re-admit an over-cooked sprint candidate.
            if session.session_type == "sprint" and float(w.get("IF") or 0) > _SPRINT_SLOT_IF_CEILING:
                continue
            if cc_row in ("endurance", "recovery"):
                if w["Score"] < 1 or (w["Duration(min)"] or 0) < 20:
                    continue
            elif w["Score"] < 3:
                continue
            tags_lower = {t.lower() for t in (w.get("Tags") or [])}
            if "ftp_test" in tags_lower and not want_test:
                continue
            if want_test and "ftp_test" not in tags_lower:
                continue
            cat = cc_row
            # v1.3.4 fix: ftp_test bypasses the category gate (the tag filter
            # alone identifies tests).
            if want_test or cat == primary_cat or cat in fallback_cats:
                coverage_pool.append(w)
        if coverage_pool:
            if exact_duration:
                # v1.8.24 — reshuffle: closest-duration in-type, not longest.
                coverage_pool.sort(
                    key=lambda x: abs(float(x.get("Duration(min)", 0) or 0) - target_dur)
                )
            else:
                coverage_pool.sort(key=lambda x: -float(x.get("Duration(min)", 0) or 0))
            picked = coverage_pool[0]
            session.zwo_file = picked.get("File", "") or ""
            session.zwo_name = picked.get("Name", "") or ""
            try:
                session.matched = True  # type: ignore[attr-defined]
            except Exception:
                pass
            log.info(
                "match_zwo: target_dur=%smin exceeds library for %s — "
                "fell back to longest %s file (%smin)",
                target_dur, session.session_type,
                primary_cat, picked.get("Duration(min)"),
            )
            # v1.6.1 — coverage-pool fallback fired (primary pool empty).
            # WARN severity: recoverable degradation; the picked file is
            # the longest in the right category but may not match duration.
            _tp_log_error(error_codes.Codes.MATCH_ZWO_ALL_FILTERED,
                          session_type=session.session_type,
                          target_dur=target_dur,
                          picked_file=session.zwo_file,
                          picked_dur=picked.get("Duration(min)"))
            return session
        # Nothing in any acceptable category — preserve old behaviour.
        log.warning(
            "match_zwo: no candidates for session_type=%s duration=%smin "
            "target_if≈%s primary_cat=%s fallbacks=%s library_size=%d",
            session.session_type, target_dur,
            getattr(session, "target_if", None),
            primary_cat, fallback_cats, len(library),
        )
        # v1.6.1 — both pools empty: nothing in the library for this slot.
        _tp_log_error(error_codes.Codes.MATCH_ZWO_NO_CANDIDATES,
                      session_type=session.session_type,
                      target_dur=target_dur,
                      primary_cat=primary_cat,
                      library_size=len(library))
        if raise_on_empty:
            raise NoCandidateWorkoutError(
                f"No candidate workouts for duration={target_dur}min "
                f"intensity={session.session_type} "
                f"(primary_cat={primary_cat}, library_size={len(library)})"
            )
        session.zwo_file = ""
        # Use dataclass attribute if present; fall back to setattr for tolerance.
        try:
            session.matched = False  # type: ignore[attr-defined]
        except Exception:
            pass
        return session

    # v2.5.0 W5 — hr-mode soft bias: RPE-heavy classes (no usable bpm target
    # on a head unit) get a small penalty when >=3 guidable alternatives
    # exist. Rebuilds the LOCAL (score, w) tuples only — library row dicts are
    # shared via the module cache and must never be mutated here.
    if hr_bias and candidates:
        _RPE_HEAVY = {"vo2_short", "anaerobic", "neuromuscular"}
        _n_guidable = sum(1 for _s, _w in candidates
                          if str(_w.get("ContentClass") or "") not in _RPE_HEAVY)
        if _n_guidable >= 3:
            candidates = [
                ((s - 3, w) if str(w.get("ContentClass") or "") in _RPE_HEAVY
                 else (s, w))
                for s, w in candidates
            ]

    # Sort by score descending
    candidates.sort(key=lambda x: -x[0])

    # Score-weighted random from top-50 (not just top-30)
    # This makes more of the 2200+ workout pool reachable
    import random
    pool_size = min(50, len(candidates))
    pool = candidates[:pool_size]

    # PL5 (Wave 4 rescan R4): use a LOCAL random.Random() keyed on
    # (plan_start_date, profile_id, week_num, day_idx, session_type). The
    # previous code called `random.seed(...)` on the global module, so:
    #   (a) any other code that happened to pull from the global RNG during
    #       a plan build got deterministic output keyed on the last session;
    #   (b) two users whose plans started on the same date got identical
    #       workout picks every slot because profile_id was missing.
    # Local rng contains the seed; profile_id is taken from `session.profile_id`
    # if the caller stamped it, else derived from ICU_ATHLETE_ID, else "anon".
    anchor_date = (
        plan_start_date if plan_start_date is not None
        else (getattr(session, "day", None) or date.today())
    )
    pid = getattr(session, "profile_id", None)
    if not pid:
        try:
            import config as _cfg
            pid = getattr(_cfg, "ICU_ATHLETE_ID", "") or "anon"
        except Exception:
            pid = "anon"
    # v4.3.0 B3: mix seed_salt (per-regen entropy) into the seed key. The
    # `% 7919` hashes a 19-bit-of-entropy salt into a small prime so the
    # downstream sha1 spreads it across the 8-hex-char window evenly.
    salt_part = (int(seed_salt) % 7919) if seed_salt else 0
    seed_src = (
        f"{anchor_date.isoformat()}:{pid}:{week_num}:{day_idx}:"
        f"{session.session_type}:{salt_part}"
    ).encode()
    seed_int = int(hashlib.sha1(seed_src).hexdigest()[:8], 16)
    rng = random.Random(seed_int)

    # v4.3.0 B3: shuffle the candidate pool BEFORE the score-weighted pick so
    # that workouts with identical (or very close) scores don't always
    # tie-break to the same file across regenerations. The previous code
    # sorted by score descending, then took candidates[:50], which made the
    # tie-break deterministic on dict iteration order — even when a different
    # seed was used the top-N order stayed identical and the weighted pick
    # almost always landed on the same file. Shuffling pre-pick changes the
    # cumulative-weight ladder per regen.
    if seed_salt:
        rng.shuffle(pool)

    # Score-weighted selection: higher score = more likely, but ALL pool items reachable
    weights = [max(0.1, c[0]) for c in pool]
    total_w = sum(weights)
    r = rng.random() * total_w
    cumulative = 0
    pick_idx = 0
    for i, w in enumerate(weights):
        cumulative += w
        if cumulative >= r:
            pick_idx = i
            break

    best = pool[pick_idx][1]
    session.zwo_name = best["Name"]
    # Flat workouts dir — store basename only (callers are tolerant of legacy "category/file" paths)
    session.zwo_file = best["File"]
    used_names.add(best["Name"])

    return session


# ── v4.5.0 IMPL-PLANNER: intensity-budget sampler ────────────────────────────
#
# The sampler replaces the rigid (session_type, hardcoded_duration) tuple from
# `_pick_session` with a score-weighted pull from the FULL library (3054 files,
# ~1818 score≥5). It drives every non-rest, non-ftp_test slot of a week — the
# legacy `_pick_session` still runs first to lay down the rest-day skeleton +
# 48h HIT-gap structure, but the sampler then overwrites session_type,
# duration_min, tss_estimate, zwo_file, zwo_name on each non-rest slot.
#
# Acceptance: ≥150 distinct ZWOs over a 24-week plan, ≥30 (content_class,
# duration_quintile) tuples, top-5 ZWOs ≤15% of sessions, cross-regen ≥40%
# differ. See /tmp/MASTER_DECISIONS_v45.md §3 + §4 for the contract.

# HIT vs endurance bucketing per content_class (read from row["ContentClass"]).
# v4.5.0 IMPL-PLANNER:
#   HIT pool = workouts whose dominant work is above-Z2 (intervals, intensity).
#   Endurance pool = Z2-dominant workouts (steady aerobic, recovery only).
#   tempo / sweet_spot / over_under / threshold / vo2max all go to HIT pool —
#   they're structural intensity work, not endurance. "mixed" routes by zone
#   profile: Z1+Z2 ≥ 65% AND Z3+Z4+Z5 < 25% → endurance, else HIT.
_HIT_CONTENT_CLASSES = frozenset({
    "vo2max", "vo2_short", "threshold", "over_under",
    "anaerobic", "neuromuscular", "sweet_spot", "tempo",
})
_ENDURANCE_CONTENT_CLASSES = frozenset({
    "endurance", "recovery",
})

# v4.5.0 IMPL-PLANNER Layer 2: per-phase + week_in_phase content_class mix
# preference. Each phase row is a list of dicts; index by ``week_in_phase %
# len(rows)`` to pick the row that drives THIS week's weights. The picker
# multiplies ``row[content_class] * (rotation penalty per Layer 3)`` to derive
# the final per-class probability for both HIT and endurance slots. Numbers
# are coaching-consensus weights (see /tmp/MASTER_DECISIONS_v45.md §3 Layer 2).
WORKOUT_MIX_PREFERENCE: dict[str, list[dict[str, float]]] = {
    # v4.6.1 PLANNER-VARIETY+RONNESTAD: preserve the v4.5.4 mix (which 4.6.0
    # tuned to hit the distinct-file diversity acceptance gate) and rely on
    # the hard-floor post-pass below to guarantee anaerobic / neuromuscular
    # / vo2_short coverage in every build phase. The variety_score multi-
    # plier (gentle, sqrt-shouldered) handles the per-file Rønnestad bias.
    "base": [
        # W1-2 early: aerobic-leaning, sweet_spot still the structural intro.
        # v1.0.4: dropped `mixed` (junk drawer); redistributed weight into
        # endurance_intervals (Z2 + strides) which is the natural early-base
        # finish-fast variant.
        {"endurance": 0.25, "endurance_intervals": 0.08, "tempo": 0.15,
         "sweet_spot": 0.25, "recovery": 0.15, "threshold": 0.05,
         "tempo_intervals": 0.05},
        # W3-4 mid
        {"endurance": 0.20, "endurance_intervals": 0.07, "tempo": 0.12,
         "tempo_intervals": 0.06, "sweet_spot": 0.22, "threshold": 0.15,
         "vo2_short": 0.05, "recovery": 0.08},
        # W5+ late
        {"endurance": 0.18, "endurance_intervals": 0.05, "tempo": 0.10,
         "tempo_intervals": 0.07, "sweet_spot": 0.22, "threshold": 0.18,
         "vo2max": 0.10, "vo2_short": 0.05, "recovery": 0.05},
    ],
    "build1": [
        # W1 — v1.0.4 adds tempo_intervals + ladder shapes (build phase).
        {"endurance": 0.16, "endurance_intervals": 0.05, "tempo": 0.06,
         "tempo_intervals": 0.06, "tempo_ladder": 0.04, "sweet_spot": 0.13,
         "sweet_spot_ladder": 0.04, "threshold": 0.16, "threshold_ladder": 0.05,
         "vo2max": 0.13, "over_under": 0.08, "vo2_short": 0.04,
         "anaerobic": 0.04},
        # W2
        {"endurance": 0.14, "endurance_intervals": 0.04, "tempo": 0.05,
         "tempo_intervals": 0.06, "tempo_ladder": 0.04, "sweet_spot": 0.10,
         "sweet_spot_ladder": 0.04, "threshold": 0.14, "threshold_ladder": 0.05,
         "vo2max": 0.16, "over_under": 0.08, "vo2_short": 0.06,
         "anaerobic": 0.04, "neuromuscular": 0.03},
        # W3+ — v1.1.0 IMPL-NORWEGIAN-HR: small allocation for double_threshold
        # (AM+PM same-day pair, both ≤88% max_hr) gated to build1 W3+ only.
        {"endurance": 0.13, "endurance_intervals": 0.04, "tempo": 0.05,
         "tempo_intervals": 0.06, "tempo_ladder": 0.04, "sweet_spot": 0.10,
         "sweet_spot_ladder": 0.04, "threshold": 0.11, "threshold_ladder": 0.05,
         "vo2max": 0.16, "over_under": 0.08, "vo2_short": 0.06,
         "anaerobic": 0.05, "neuromuscular": 0.04, "double_threshold": 0.05},
    ],
    "build2": [
        # vo2 + neuromuscular emphasis — v1.0.4 adds tempo_intervals + ladders.
        # v1.1.0 IMPL-NORWEGIAN-HR: double_threshold appears in build2.
        {"endurance": 0.11, "endurance_intervals": 0.03, "tempo": 0.04,
         "tempo_intervals": 0.05, "tempo_ladder": 0.03, "sweet_spot": 0.08,
         "sweet_spot_ladder": 0.03, "threshold": 0.11, "threshold_ladder": 0.06,
         "vo2max": 0.18, "over_under": 0.09, "vo2_short": 0.09,
         "anaerobic": 0.08, "neuromuscular": 0.05, "double_threshold": 0.06},
    ],
    "peak": [
        # Race-specific — v1.0.4 adds tempo_intervals + threshold_ladder + vo2_ladder.
        # v1.1.0 IMPL-NORWEGIAN-HR: double_threshold appears in peak.
        {"endurance": 0.12, "tempo": 0.04, "tempo_intervals": 0.05,
         "threshold": 0.12, "threshold_ladder": 0.06, "vo2max": 0.17,
         "vo2_ladder": 0.04, "over_under": 0.09, "vo2_short": 0.09,
         "anaerobic": 0.13, "neuromuscular": 0.09, "double_threshold": 0.05},
    ],
    "taper": [
        # Short openers + recovery
        {"endurance": 0.40, "recovery": 0.30, "tempo": 0.10,
         "vo2_short": 0.10, "neuromuscular": 0.10},
    ],
    "history": [
        # Mirror base W1 — v1.0.4: drop `mixed`, add endurance_intervals.
        {"endurance": 0.25, "endurance_intervals": 0.08, "tempo": 0.15,
         "sweet_spot": 0.25, "recovery": 0.15, "threshold": 0.05,
         "tempo_intervals": 0.05},
    ],
}

# Slot kind → which content_classes are eligible for this slot. Layer 2 row
# entries outside this set are filtered out before sampling.
#
# v1.0.4 IMPL-PLANNER:
# - Added `anaerobic` (was an orphan: weighted 5–15% in WORKOUT_MIX_PREFERENCE
#   build/peak rows but excluded here, so 311 anaerobic files were never
#   actually picked).
# - Added the 6 new structural-variant classes:
#   `tempo_intervals`, `tempo_ladder`, `sweet_spot_ladder`, `threshold_ladder`,
#   `vo2_ladder` to HIT slots; `endurance_intervals` to endurance slots.
# - Dropped `mixed` (217 files re-routed by IMPL-CLASSIFIER's zone-dominance
#   pass; class no longer exists in the canonical 16-class taxonomy).
_HIT_SLOT_CONTENT_CLASSES = frozenset({
    "threshold", "threshold_ladder",
    "vo2max", "vo2_ladder", "vo2_short",
    "over_under",
    "sweet_spot", "sweet_spot_ladder",
    "tempo_intervals", "tempo_ladder",
    "anaerobic", "neuromuscular",
    # v1.1.0 IMPL-NORWEGIAN-HR: double_threshold counts as a HIT slot
    # (AM+PM threshold-class pair, ≥4 h gap, both with HR ceiling 88% max_hr).
    "double_threshold",
})
_ENDURANCE_SLOT_CONTENT_CLASSES = frozenset({
    "endurance", "endurance_intervals",
    "tempo",
    "sweet_spot", "recovery",
})

# v4.5.4 FIX-PLANNER-INTERVALS: classes whose .zwo files contain interval
# shapes (4×8, 5×3, 30/30, sprints) — used to enforce a per-week interval
# floor so the plan visibly mixes blocks instead of cycling through steady-
# state z2/tempo "diagonal" workouts.
#
# v1.0.4 IMPL-PLANNER: `*_intervals` and `*_ladder` are interval-shaped — the
# dose isn't bunched into a single steady block.
_INTERVAL_SHAPED_CONTENT_CLASSES = frozenset({
    "vo2max", "vo2_short", "vo2_ladder",
    "threshold", "threshold_ladder",
    "over_under",
    "sweet_spot", "sweet_spot_ladder",
    "tempo_intervals", "tempo_ladder",
    "endurance_intervals",
    "anaerobic", "neuromuscular",
})

# v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): soft minimum DISTINCT files per
# content_class for a 24-week plan. The sampler uses these to bias picks
# toward unseen files in classes that are below their trajectory.
#
# v1.0.4 IMPL-PLANNER: minimums for the 6 new structural-variant classes set
# to 1–3 — they're carved out of larger parents (tempo / sweet_spot /
# threshold / vo2max / endurance) and are expected to have small file pools
# (tens of files, not hundreds). Defer to the existing pattern for similarly-
# small classes (e.g. neuromuscular=5, recovery=5).
_PLAN_CLASS_MIN_DISTINCT_24W: dict[str, int] = {
    "tempo":               20,
    "tempo_intervals":      3,
    "tempo_ladder":         2,
    "sweet_spot":          20,
    "sweet_spot_ladder":    2,
    "threshold":           20,
    "threshold_ladder":     3,
    "vo2max":              20,
    "vo2_ladder":           2,
    "over_under":          10,
    "vo2_short":           10,
    "anaerobic":            8,
    "neuromuscular":        5,
    "endurance":           15,
    "endurance_intervals":  3,
    "recovery":             5,
}

# v4.6.2 PLANNER-DIVERSITY-PUSH: per-file diversity-budget divisor. Across
# the plan, no single ZWO is picked more than ceil(class_count / 24). Was 8
# at v4.6.0/v4.6.1 — at 8, endurance with 48 sessions allowed 6 picks per
# file, dragging slot-uniqueness down to ~72%. At 24, the cap drops to ≤2
# for every class while still degrading gracefully if a small class has
# fewer eligible candidates than sessions.
_DIVERSITY_BUDGET_DIVISOR = 24

# v4.6.0 IMPL-PLANNER-UTILIZATION: rolling-eviction window for used_names
# bookkeeping. Names dropped re-enter the "fresh" novelty pool.
_USED_NAMES_ROLLING_WEEKS = 12

# v4.6.2 PLANNER-DIVERSITY-PUSH: novelty boost multipliers. The *first*
# pick of a file gets a strong boost (was 1.5×, now 5×). The *second* pick
# gets crushed (was 1.0×, now 0.05× — i.e. 100× less attractive than a
# never-picked file). Third+ picks effectively zeroed (0.001×). Forces the
# sampler to exhaust the never-picked pool before repeating, while still
# allowing repeats when no unpicked candidate fits the slot's score/zone
# constraints (graceful fallback — no hard failure mode).
_NOVELTY_BOOST = {0: 5.0, 1: 0.05, 2: 0.001}

# ── v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE) ──────────────────────
# Glycolytic-load weight per content_class for soft anti-stacking. The
# v1.0.6 picker scales today's weight ×0.7 IF the prior day's pick had
# glycolytic_load ≥0.7 — SOFT bias, NOT a hard reject.
_GLYCOLYTIC_LOAD_BY_CLASS: dict[str, float] = {
    "vo2max":              1.0,
    "anaerobic":           1.0,
    "vo2_short":           0.9,
    "vo2_ladder":          0.9,
    "tempo_ladder":        0.5,
    "over_under":          0.7,
    "threshold_ladder":    0.7,
    "neuromuscular":       0.6,
    "threshold":           0.5,
    "sweet_spot_ladder":   0.3,
    "sweet_spot":          0.2,
    "tempo_intervals":     0.15,
    "tempo":               0.1,
    "endurance_intervals": 0.1,
    "endurance":           0.0,
    "recovery":            0.0,
    "ftp_test":            0.5,
}


def _scaled_class_min_distinct(plan_total_weeks: int) -> dict[str, int]:
    """Scale ``_PLAN_CLASS_MIN_DISTINCT_24W`` by plan length (vs 24 weeks)."""
    if plan_total_weeks <= 0:
        return {}
    factor = max(0.25, plan_total_weeks / 24.0)
    return {cc: max(1, int(round(n * factor))) for cc, n in _PLAN_CLASS_MIN_DISTINCT_24W.items()}


def _content_class_for_row(w: dict) -> str:
    """Resolve a library row's content_class with filename fallback.

    v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): the cached
    content_classification.json may be stale during the parallel library
    overhaul (renamed files have empty ContentClass). Fall back to filename
    prefix so the sampler's diversity bookkeeping + HIT/endurance pool
    bucketing still cover the full library. POST-overhaul, ContentClass
    fields populate naturally and this fallback becomes a no-op.
    """
    cc = (w.get("ContentClass") or "").strip().lower()
    if cc:
        return cc
    fname = (w.get("File") or "").lower()
    if fname.startswith("vo2max_short") or fname.startswith("vo2_short"):
        return "vo2_short"
    if fname.startswith("vo2max_") or fname.startswith("vo2_"):
        return "vo2max"
    if fname.startswith("threshold_") or fname.startswith("supra_threshold"):
        return "threshold"
    if fname.startswith("sweetspot_") or fname.startswith("sweet_spot_"):
        return "sweet_spot"
    if fname.startswith("tempo_"):
        return "tempo"
    if fname.startswith("over_under_") or fname.startswith("overunder_"):
        return "over_under"
    if fname.startswith("sprints_"):
        return "neuromuscular"
    if fname.startswith("anaerobic_"):
        return "anaerobic"
    if fname.startswith("recovery_") or fname.startswith("warmup_"):
        return "recovery"
    if fname.startswith("z2_") or fname.startswith("endurance_"):
        return "endurance"
    if fname.startswith("ftp_test_"):
        return "ftp_test"
    return "mixed"


# v1.11.0 IMPL-GOAL-FOCUS — bias the per-phase class mix toward the workouts the
# goal actually targets. Multipliers apply to HIT-slot classes only; the HIT row
# is renormalized so the COUNT of HIT slots (set by the intensity budget) is
# unchanged — only WHICH hard class fills them shifts. Grounded in the PubMed
# FTP/LT review: threshold intervals (4×8 @100-105% FTP) are the #1 direct
# driver of FTP/LT, then VO2 (Rønnestad 30/15, 4-6min @ vVO2max); sweet-spot +
# over-under are support. Goals with no profile (event/general/endurance/ctl/
# weight) are untouched — the event path gets its own demand-driven logic.
GOAL_CLASS_EMPHASIS: dict[str, dict[str, float]] = {
    # Raise FTP → threshold-led, sweet-spot + over-under support; VO2/anaerobic
    # lightly SUPPRESSED so the gain comes at the off-target family's expense
    # (boosting the on-target family alone only reshuffles within it). The
    # variety floor still guarantees ≥1 interval session, so nothing is zeroed.
    "ftp": {
        "threshold": 2.6, "threshold_ladder": 2.2, "double_threshold": 2.0,
        "sweet_spot": 1.9, "sweet_spot_ladder": 1.8, "over_under": 1.5,
        "vo2max": 0.6, "vo2_ladder": 0.6, "vo2_short": 0.55, "anaerobic": 0.7,
    },
    # Raise VO2max → vo2max-led (Rønnestad 30/15, 4-6 min @ vVO2max); threshold/
    # sweet-spot lightly suppressed.
    "vo2max": {
        "vo2max": 2.8, "vo2_ladder": 2.4, "vo2_short": 2.6, "anaerobic": 1.3,
        "threshold": 0.6, "threshold_ladder": 0.6, "sweet_spot": 0.7,
        "over_under": 0.8,
    },
    # Combined FTP + VO2 → both families up, no suppression (neither starves).
    "ftp_vo2max": {
        "threshold": 1.9, "threshold_ladder": 1.7, "sweet_spot": 1.6,
        "over_under": 1.3, "vo2max": 2.0, "vo2_ladder": 1.8, "vo2_short": 1.9,
    },
}
GOAL_CLASS_EMPHASIS["hybrid"] = GOAL_CLASS_EMPHASIS["ftp_vo2max"]  # alias
# v1.11.0 IMPL-EVENT (P4) — climbing specificity for hilly events. Selected via a
# SEPARATE emphasis_profile channel (NOT goal_type, which stays "event" so the
# taper/consolidation branches don't break). Applied only in build2/peak.
GOAL_CLASS_EMPHASIS["event_climb"] = {
    # Sustained climbing power UP (long climbs = threshold / over-under / sweet
    # spot), punchy + short-VO2 work DOWN. Must suppress the DOMINANT off-target
    # (vo2max) to free allocation, else the boost just reshuffles (Change 1 lesson).
    "threshold": 3.2, "threshold_ladder": 2.6, "over_under": 2.6, "sweet_spot": 2.2,
    "vo2max": 0.55, "vo2_ladder": 0.55, "vo2_short": 0.45,
    "neuromuscular": 0.45, "anaerobic": 0.5,
}


def _apply_goal_emphasis(row: dict[str, float], goal_type: str) -> dict[str, float]:
    """Up-weight the goal's target classes within a mix row, renormalized to the
    row's original total so phase intensity volume is unchanged (only the class
    distribution shifts). No-op (returns the row unchanged) for goals without an
    emphasis profile."""
    emphasis = GOAL_CLASS_EMPHASIS.get(goal_type or "")
    if not emphasis or not row:
        return row
    total = sum(row.values())
    boosted = {cc: w * emphasis.get(cc, 1.0) for cc, w in row.items()}
    new_total = sum(boosted.values()) or 1.0
    scale = total / new_total
    return {cc: w * scale for cc, w in boosted.items()}


def _get_mix_preference(phase_name: str, week_in_phase: int) -> dict[str, float]:
    """Return the WORKOUT_MIX_PREFERENCE row for (phase, week_in_phase).

    week_in_phase is 0-indexed (W1 of the phase → 0). Rows recycle with modulo
    when the phase runs longer than the table.

    NOTE: goal-focus emphasis (v1.11.0) is applied later, to ``hit_pref`` AFTER
    the rotation penalty, so the goal bias is the final word on the hard-class
    pick rather than being damped by anti-monotony rotation.
    """
    rows = WORKOUT_MIX_PREFERENCE.get(phase_name) or WORKOUT_MIX_PREFERENCE["base"]
    if not rows:
        return {}
    return rows[max(0, week_in_phase) % len(rows)]


def _apply_rotation_penalty(
    weights_by_cc: dict[str, float],
    recent_hit_types: list[str],
    block_focus: "str | None" = None,
) -> dict[str, float]:
    """v4.5.0 IMPL-PLANNER Layer 3 — rolling-window type rotation penalty.

    Forces the picker to cycle through threshold / vo2max / sweet_spot /
    over_under in build phases rather than picking vo2max 4 weeks running.
    Penalty (calibrated against the rolling 12-entry window from
    generate_plan, which spans roughly the last 4 weeks of HIT picks):
      * count of last-2 entries: weight × 0.4
      * count of weeks-3-4 entries: weight × 0.7
      * unchanged otherwise
    The penalty is multiplicative — repeated occurrences in the recent window
    compound. Layer 3 acceptance: in any 6-week window of build1+build2,
    {threshold, vo2max, sweet_spot, over_under} all appear ≥1×.
    """
    if not recent_hit_types:
        return dict(weights_by_cc)
    # Recent_hit_types is most-recent-LAST. last_5 ≈ this week + prior 1-2 wks
    # (since each week appends 2-3 HIT picks). weeks_back_6_12 ≈ 2 wks ago.
    last_5 = set(recent_hit_types[-5:])
    weeks_back = set(recent_hit_types[-12:-5])
    out = {}
    for cc, w in weights_by_cc.items():
        # F1 (v2.1/B3): in a block, the FOCUS class is exempt from the cross-week
        # rotation penalty — a VO2 block deliberately repeats vo2max week-to-week
        # (the opposite of the default "don't pick vo2max 4 weeks running"). None
        # ⇒ default behaviour (parity).
        if block_focus and cc == block_focus:
            out[cc] = w
        elif cc in last_5:
            out[cc] = w * 0.4
        elif cc in weeks_back:
            out[cc] = w * 0.7
        else:
            out[cc] = w
    return out


def variety_score(zwo_features: dict) -> float:
    """v4.6.1 PLANNER-VARIETY+RONNESTAD — structural variety bonus.

    Higher = more structurally varied. Range 0.5-3.0. Multiplied into the
    per-file sampling weight in ``sample_week_workouts`` so the picker
    rotates through interval shapes (4×8, 30/15, sprints, over/under)
    instead of falling into long-steady-Z2/tempo by sheer TSS-favoring score
    formula.

    Accepted feature keys (from .content_classification.json's `features`
    sub-dict, with optional adapter additions):
      * ``segment_count`` (preferred) OR ``hard_segment_count`` (fallback) —
        number of structured work segments
      * ``z1_pct`` .. ``z7_pct`` — zone distribution percentages (0-100)
      * ``secondary_flags`` — dict with pattern_microinterval / has_sprints
        / pattern_over_under booleans
      * ``is_ronnestad`` — bool, set by adapter when the workout is a
        Rønnestad-style 30/15 or 40/20 microinterval (most-effective VO2max
        protocol per Rønnestad et al. 2015) — gets the largest single bonus

    Returns a multiplier in [0.5, 3.0]. Workouts with ≤3 segments take a
    flag-factor cut so a steady-state mixed workout that happens to carry
    a stale `has_threshold_work` flag doesn't sneak past the variety filter.
    """
    seg_count = zwo_features.get("segment_count")
    if seg_count is None:
        seg_count = zwo_features.get("hard_segment_count", 1)
    seg_count = max(1, int(seg_count or 1))
    seg_factor = min(2.0, 0.5 + (seg_count / 10.0) ** 0.7)

    z_pcts = [
        float(zwo_features.get(f"z{i}_pct", 0) or 0) / 100.0
        for i in range(1, 8)
    ]
    nonzero = [p for p in z_pcts if p > 0.05]
    if nonzero:
        entropy = -sum(p * math.log2(p) for p in nonzero if p > 0)
        zone_factor = 0.7 + min(1.3, entropy / 2.0)
    else:
        zone_factor = 0.7

    flags = zwo_features.get("secondary_flags", {}) or {}
    flag_factor = 1.0
    if flags.get("pattern_microinterval"):
        flag_factor *= 1.4
    if flags.get("pattern_over_under"):
        flag_factor *= 1.3
    if flags.get("has_sprints"):
        flag_factor *= 1.2
    if zwo_features.get("is_ronnestad"):
        # v4.6.3 RONNESTAD-FIX: Rønnestad et al. 2015 showed 30/15 + 40/20
        # microintervals deliver more time-at-VO2 than 4-5min intervals.
        # Bumped 1.5× → 5.0× so Rønnestad files visibly outweigh ordinary
        # class peers; the per-phase Rønnestad swap pass below backstops
        # this with a hard floor of ≥1 Rønnestad per build1/build2/peak.
        flag_factor *= 5.0

    if seg_count <= 3:
        flag_factor *= 0.6

    return max(0.5, min(3.0, seg_factor * zone_factor * flag_factor / 2.0))


def _is_ronnestad_workout(cache_entry: dict) -> bool:
    """Read the explicit ``is_ronnestad`` tag set by Wave 1A's
    RECLASSIFY-MIXED-RONNESTAD pass (v4.6.1, see scripts/reclassify_mixed_v461.py).

    The cache stores the Rønnestad designation as ``tags: ["is_ronnestad"]``
    plus ``ronnestad_protocol: "30/15" | "40/20" | ...``. The pre-v4.6.2
    re-derivation gated on ``primary in {vo2max, vo2_short, anaerobic}``,
    which excluded 8 of 17 actually-tagged files (which classified as
    neuromuscular, threshold, or recovery — entirely correct given their
    zone profiles, but still Rønnestad-shaped microintervals). Reading the
    explicit tag is more accurate AND respects whatever the classifier
    decided about content_class.

    Reference: Rønnestad et al. 2015 (Scand J Med Sci Sports 25:143-151).
    """
    if not cache_entry:
        return False
    tags = cache_entry.get("tags") or []
    return "is_ronnestad" in tags


def _features_for_row(row: dict) -> dict:
    """Build the variety_score feature dict for a library row.

    Reads the on-disk content_classification cache for ``segment_count``
    (mapped to ``hard_segment_count``), zone percentages, secondary_flags
    and the Rønnestad detector. Falls back to the row's own Z1%..Z6%
    fields when the cache is empty (fresh checkout, no classifier run).
    """
    cache = _load_content_classifications() or {}
    fname = row.get("File") or ""
    ent = cache.get(fname) or cache.get(fname.split("/")[-1])
    feats: dict = {}
    if ent:
        cache_feats = ent.get("features") or {}
        feats["hard_segment_count"] = cache_feats.get("hard_segment_count", 1)
        feats["segment_count"] = cache_feats.get("hard_segment_count", 1)
        for i in range(1, 8):
            feats[f"z{i}_pct"] = cache_feats.get(f"z{i}_pct", 0)
        feats["secondary_flags"] = ent.get("secondary_flags") or {}
        feats["is_ronnestad"] = _is_ronnestad_workout(ent)
        feats["ronnestad_protocol"] = ent.get("ronnestad_protocol") or ""
    else:
        # Fallback: derive from row fields
        feats["hard_segment_count"] = 1
        feats["segment_count"] = 1
        for i, key in enumerate(("Z1%", "Z2%", "Z3%", "Z4%", "Z5%", "Z6%"), start=1):
            feats[f"z{i}_pct"] = float(row.get(key, 0) or 0)
        feats["z7_pct"] = 0.0
        feats["secondary_flags"] = row.get("SecondaryFlags") or {}
        feats["is_ronnestad"] = False
    return feats


# Pure Z2 floor for "mixed" content_class workouts to qualify as endurance.
# 50% / 40% gate (loose) — opens the 174-strong Z1+Z2≥50% mixed bucket so the
# endurance pool isn't starved when picking 100+ Z2 slots. The budget_fit
# overshoot penalty in sample_week_workouts already prevents picking a
# heavy-Z4 workout for a Z2 slot.
_PURE_Z2_FLOOR_PCT = 50.0
_PURE_Z2_HIGH_CEILING_PCT = 40.0

# content_class → planner session_type (display label). For "mixed" we
# lazy-pick z2 vs tempo from the row's Z3% (≥30% Z3 → tempo). The session_type
# is what the UI shows + what _SESSION_TYPE_PREFIXES expects.
_CONTENT_CLASS_TO_SESSION_TYPE = {
    "recovery":     "recovery",
    "endurance":    "z2",
    "tempo":        "tempo",
    "sweet_spot":   "sweetspot",
    "threshold":    "threshold",
    "over_under":   "overunder",
    "vo2max":       "vo2max",
    "vo2_short":    "vo2max",
    "anaerobic":    "vo2max",
    "neuromuscular": "sprint",
    "ftp_test":     "ftp_test",
}


def _row_zone_minutes(row: dict) -> dict[str, float]:
    """Convert a library row's Z1%..Z6% + Duration(min) into Zx minutes.

    Returns {z1z2, z3, z4, z5plus} aggregated minutes for the budget-fit
    calculation. Uses the row's existing percent fields so this is O(1) per
    workout (no re-parse of the .zwo).
    """
    dur = float(row.get("Duration(min)", 0) or 0)
    if dur <= 0:
        return {"z1z2": 0.0, "z3": 0.0, "z4": 0.0, "z5plus": 0.0}
    z1 = float(row.get("Z1%", 0) or 0) / 100.0
    z2 = float(row.get("Z2%", 0) or 0) / 100.0
    z3 = float(row.get("Z3%", 0) or 0) / 100.0
    z4 = float(row.get("Z4%", 0) or 0) / 100.0
    z5 = float(row.get("Z5%", 0) or 0) / 100.0
    z6 = float(row.get("Z6%", 0) or 0) / 100.0
    return {
        "z1z2": dur * (z1 + z2),
        "z3":   dur * z3,
        "z4":   dur * z4,
        "z5plus": dur * (z5 + z6),
    }


def _budget_fit_score(row_zones: dict[str, float], remaining: dict[str, float]) -> float:
    """Reward workouts whose zone minutes fit the remaining gap; penalize
    overshoot beyond +20min in any zone (esp. z5plus where a too-hot workout
    blows the polarized budget). Returns 0..1 normalized.
    """
    fit = 0.0
    overshoot = 0.0
    total_gap = max(1.0, sum(max(0.0, v) for v in remaining.values()))
    for z in ("z1z2", "z3", "z4", "z5plus"):
        gap = max(0.0, remaining.get(z, 0.0))
        contrib = min(row_zones.get(z, 0.0), gap)
        fit += contrib
        excess = max(0.0, row_zones.get(z, 0.0) - gap)
        # z5plus overshoot is the most expensive — small budget, high CNS load.
        weight = 3.0 if z == "z5plus" else (2.0 if z == "z4" else 1.0)
        overshoot += excess * weight
    # Normalize. Hard kill if z5plus overshoot > 20min.
    if (row_zones.get("z5plus", 0.0) - max(0.0, remaining.get("z5plus", 0.0))) > 20:
        return 0.0
    raw = (fit - 0.5 * overshoot) / total_gap
    return max(0.0, min(1.0, raw))


def _build_pool_indexes(library: list[dict]) -> dict:
    """Pre-bucket the Score≥5 library by content_class for O(1) pool lookup.

    Returns:
        {
            "hit":       [HIT-dominant rows],
            "endurance": [Z2-dominant rows],
            "by_class":  {content_class: [rows]},
            "all_pool":  [Score≥5 rows],
        }
    Skips ftp_test-tagged workouts (they carry their own slot).

    Bucketing rules (v4.5.0):
      * HIT  ← content_class in _HIT_CONTENT_CLASSES
              OR (content_class == "mixed" AND Z3+Z4+Z5 ≥ 30%)
      * Endurance  ← content_class in _ENDURANCE_CONTENT_CLASSES
              OR (content_class == "mixed" AND Z1+Z2 ≥ 50% AND Z3+Z4+Z5 < 40%)
              OR (content_class in ("tempo", "sweet_spot") AND duration ≥ 75
                  AND Z1+Z2 ≥ 50% — long endurance-with-finisher workouts that
                  belong on a long-Z2 slot rather than a HIT slot)
    Score≥5 floor applies to BOTH pools so test_only_score_5_plus_workouts_picked
    holds invariably.
    """
    by_class: dict[str, list[dict]] = {}
    hit, endurance, endurance_strict = [], [], []
    all_pool: list[dict] = []
    for w in library:
        tags_lower = {t.lower() for t in (w.get("Tags") or [])}
        if "ftp_test" in tags_lower:
            continue
        score = w.get("Score", 0) or 0
        # v4.6.0: use _content_class_for_row so files with stale/empty
        # ContentClass (post-rename, pre-classify) still bucket by filename.
        cc = _content_class_for_row(w)
        by_class.setdefault(cc, []).append(w)
        # v4.6.2 PLANNER-DIVERSITY-PUSH: class-aware score floor. score_workout
        # rewards TSS + Z3+ structure, which fairly rates HIT classes but
        # systematically under-scores endurance and recovery (intentionally
        # simple → low TSS, no structure). Pre-v4.6.2 the score≥5 floor cut
        # endurance to 48 of 496 files (10%) and recovery to 0 of 111 (0%) —
        # the planner couldn't surface most of the library on Z2 slots and was
        # forced to repeat the same handful of files. Class-aware floor:
        #   HIT (vo2max/vo2_short/threshold/over_under/anaerobic/
        #        neuromuscular/sweet_spot): score ≥ 5  — quality bar
        #   tempo / mixed:                                 score ≥ 4  — light bar
        #   endurance / recovery:                          score ≥ 1  — none
        if cc in ("endurance", "recovery"):
            score_floor = 1
        elif cc in ("tempo", "mixed"):
            score_floor = 4
        else:
            score_floor = 5
        if score < score_floor:
            continue
        all_pool.append(w)
        z1z2 = float(w.get("Z1%", 0) or 0) + float(w.get("Z2%", 0) or 0)
        z345 = (
            float(w.get("Z3%", 0) or 0)
            + float(w.get("Z4%", 0) or 0)
            + float(w.get("Z5%", 0) or 0)
            + float(w.get("Z6%", 0) or 0)
        )
        dur = float(w.get("Duration(min)", 0) or 0)
        if cc in _HIT_CONTENT_CLASSES:
            hit.append(w)
        elif cc == "mixed" and z345 >= 30:
            hit.append(w)
        # Endurance pool — multiple gates
        if cc in _ENDURANCE_CONTENT_CLASSES:
            endurance.append(w)
            endurance_strict.append(w)
        elif cc == "mixed" and z1z2 >= _PURE_Z2_FLOOR_PCT and z345 < _PURE_Z2_HIGH_CEILING_PCT:
            endurance.append(w)
            # Strict pool: ANY mixed workout that qualified for general
            # endurance pool (z1+z2 ≥ 50%, z345 < 40%) — base phase needs
            # the volume even if some workouts have a small Z3 finisher.
            # The budget_fit overshoot penalty in sample_week_workouts
            # naturally re-weights toward purer Z2 picks once Z3 budget is
            # spent.
            endurance_strict.append(w)
        elif cc in ("tempo", "sweet_spot") and dur >= 75 and z1z2 >= 50:
            # Long endurance-with-finisher: a 90-min ride that's 60% Z2 + 25% Z3
            # is functionally endurance volume with a tempo block — fits a Sat
            # long-Z2 slot beautifully in build/peak phases. NOT in
            # endurance_strict (these have substantial Z3 work).
            endurance.append(w)
    return {
        "hit": hit,
        "endurance": endurance,
        "endurance_strict": endurance_strict,
        "by_class": by_class,
        "all_pool": all_pool,
    }


def _session_type_from_row(row: dict) -> str:
    """Derive the planner session_type for a library row.

    v4.5.0 IMPL-PLANNER: prefer filename-prefix matching FIRST so the picked
    session_type stays consistent with ``_SESSION_TYPE_PREFIXES`` (the boot-
    time staleness rewrite). Without this, the sampler can produce
    (session_type='tempo', zwo='vo2max_short_*') pairs that the staleness
    rewriter clobbers on next boot, AND legacy tests that pin "tempo + vo2_
    is stale" would break. Filename prefix is the most reliable sub-cycle
    marker the workout authors use; we fall back to content_class only when
    the filename is generic.
    """
    fname = (row.get("File") or "").lower()
    if fname.startswith("vo2max_") or fname.startswith("vo2_"):
        return "vo2max"
    if fname.startswith("threshold_") or fname.startswith("supra_threshold"):
        return "threshold"
    if fname.startswith("sweetspot_") or fname.startswith("sweet_spot_"):
        return "sweetspot"
    if fname.startswith("tempo_"):
        return "tempo"
    if fname.startswith("over_under_"):
        return "overunder"
    if fname.startswith("sprints_"):
        return "sprint"
    if fname.startswith("anaerobic_"):
        return "vo2max"  # anaerobic is treated as VO2max-style for planner display
    if fname.startswith("recovery_") or fname.startswith("warmup_"):
        return "recovery"
    if fname.startswith("z2_") or fname.startswith("endurance_"):
        return "z2"
    if fname.startswith("ftp_test_"):
        return "ftp_test"

    # Fallback: content_class
    cc = (row.get("ContentClass") or "").lower()
    base = _CONTENT_CLASS_TO_SESSION_TYPE.get(cc)
    if base:
        return base
    # mixed / unknown: zone profile fallback
    z3 = float(row.get("Z3%", 0) or 0)
    z4 = float(row.get("Z4%", 0) or 0)
    z5 = float(row.get("Z5%", 0) or 0) + float(row.get("Z6%", 0) or 0)
    if z5 >= 10:
        return "vo2max"
    if z4 >= 10:
        return "threshold"
    if z3 >= 30:
        return "tempo"
    return "z2"


def _make_session_from_row(row: dict, day: date, day_name: str, phase_name: str) -> "PlannedSession":
    """Build a PlannedSession from a sampled library row."""
    stype = _session_type_from_row(row)
    dur = int(round(float(row.get("Duration(min)", 0) or 0)))
    tss = float(row.get("TSS", 0) or 0)
    if tss <= 0:
        # Synthesise TSS from duration × per-zone TSS rate (fallback only)
        tss = round(dur / 60 * TSS_PER_HOUR.get(stype, 45))
    desc = f"{stype} ({dur}min) — sampled from library"
    sess = PlannedSession(
        day=day, day_name=day_name,
        session_type=stype,
        duration_min=dur,
        tss_estimate=round(tss),
        description=desc,
        zwo_file=row.get("File", "") or "",
        zwo_name=row.get("Name", "") or "",
        nutrition_note="",
        matched=True,
    )
    return sess


# ── FS1 (IP_PLANNER_MODES): blueprint engine for fixed_core / template modes ───
#
# Instead of sampling a fresh workout per slot (the `auto` path), the blueprint
# engine expands a REPEATABLE week: exactly one HIT session of a fixed type per
# build week (reps progress by week_in_phase), a constant Z2 endurance core that
# scales with availability + an ascending B6 ramp, and no HIT on deload weeks.
# It returns the SAME 7-element Mon..Sun PlannedSession list the sampler does, so
# every downstream pass (fallback match_zwo + B1/B3/B5/tapers/clamp) is reused
# unchanged. Files are still chosen by match_zwo (B5 gates apply); the blueprint
# fixes the TYPE + progression, not the file.

# Default per-phase HIT quality for fixed_core (mirrors the block-focus order:
# VO2 block in build1, threshold toward the event). reps progress start→max.
_BLUEPRINT_DEFAULT_HIT: dict[str, dict] = {
    "base":   {"session_type": "sweetspot", "progression": {"kind": "reps", "start": 2, "step": 1, "max": 4}},
    "build1": {"session_type": "vo2max",    "progression": {"kind": "reps", "start": 4, "step": 1, "max": 6}},
    "build2": {"session_type": "threshold", "progression": {"kind": "reps", "start": 3, "step": 1, "max": 5}},
    "peak":   {"session_type": "threshold", "progression": {"kind": "reps", "start": 3, "step": 1, "max": 5}},
    "taper":  {"session_type": "vo2max",    "progression": {"kind": "reps", "start": 2, "step": 0, "max": 2}},
}

# Shipped templates (D6). Each is a per-phase HIT blueprint; phases not named here
# fall back to _BLUEPRINT_DEFAULT_HIT. content-based types only (no filenames).
PLANNER_TEMPLATES: dict[str, dict] = {
    "polarized_base": {
        "name": "Polarized Base",
        "phases": {
            "base":   {"session_type": "vo2max",    "progression": {"kind": "reps", "start": 3, "step": 1, "max": 5}},
            "build1": {"session_type": "vo2max",    "progression": {"kind": "reps", "start": 4, "step": 1, "max": 6}},
            "build2": {"session_type": "threshold", "progression": {"kind": "reps", "start": 3, "step": 1, "max": 5}},
        },
    },
    "ftp_builder": {
        "name": "FTP Builder",
        "phases": {
            "base":   {"session_type": "sweetspot", "progression": {"kind": "reps", "start": 2, "step": 1, "max": 4}},
            "build1": {"session_type": "sweetspot", "progression": {"kind": "reps", "start": 3, "step": 1, "max": 5}},
            "build2": {"session_type": "threshold", "progression": {"kind": "reps", "start": 2, "step": 1, "max": 4}},
            "peak":   {"session_type": "threshold", "progression": {"kind": "reps", "start": 3, "step": 1, "max": 4}},
        },
    },
}


# v2.3.0 — custom distribution as a dynamic blueprint: the per-week HIT type is
# drawn from the user's band weights so the realized hard-work mix tracks the
# request across the plan (1 HIT/week in blueprint mode ⇒ the HIT-type sequence
# IS the hard distribution). The easy/Z2 core is unchanged.
_BAND_TO_HIT_TYPE: dict[str, str] = {
    "tempo_ss": "sweetspot", "threshold": "threshold", "vo2": "vo2max", "sprint": "sprint",
}
_CUSTOM_HIT_PROGRESSION: dict[str, dict] = {
    "sweetspot": {"kind": "reps", "start": 3, "step": 1, "max": 5},
    "threshold": {"kind": "reps", "start": 3, "step": 1, "max": 5},
    "vo2max":    {"kind": "reps", "start": 4, "step": 1, "max": 6},
    "sprint":    {"kind": "reps", "start": 4, "step": 1, "max": 8},
}


def _custom_hit_sequence(bands: dict) -> list:
    """Deterministic HIT-type sequence whose composition is proportional to the
    user's band weights (length ~10), interleaved so consecutive weeks vary.
    Falls back to a balanced hard mix when bands are empty/zero."""
    weights = {t: max(0.0, float(bands.get(b, 0) or 0))
               for b, t in _BAND_TO_HIT_TYPE.items()}
    total = sum(weights.values())
    if total <= 0:
        return ["vo2max", "threshold", "sweetspot"]
    remaining = {t: int(round(10 * w / total)) for t, w in weights.items()}
    seq: list = []
    while sum(remaining.values()) > 0:
        t = max(remaining, key=lambda k: remaining[k])  # emit largest-remaining → spread
        seq.append(t)
        remaining[t] -= 1
    return seq or ["threshold"]


def _blueprint_hit_for(goal, phase_name: str, week_num: int = 0) -> dict:
    """Resolve the HIT blueprint for a phase: custom bands → template preset →
    default. For the custom template the type is drawn per-week from the band
    weights (week_num indexes the proportional sequence)."""
    if getattr(goal, "plan_mode", "auto") == "template":
        tid = getattr(goal, "template_id", "") or ""
        if tid == "custom":
            seq = _custom_hit_sequence(getattr(goal, "custom_bands", {}) or {})
            st = seq[week_num % len(seq)] if seq else "threshold"
            return {"session_type": st,
                    "progression": _CUSTOM_HIT_PROGRESSION.get(st, _CUSTOM_HIT_PROGRESSION["threshold"])}
        tmpl = PLANNER_TEMPLATES.get(tid)
        if tmpl and phase_name in tmpl.get("phases", {}):
            return tmpl["phases"][phase_name]
    return _BLUEPRINT_DEFAULT_HIT.get(phase_name, _BLUEPRINT_DEFAULT_HIT["build1"])


def _blueprint_progress(prog: dict, week_in_phase: int) -> "tuple[int, int]":
    """(reps, target_session_min) for this week. reps grow start→max, clamped.
    Duration is a coarse target (warm-up/cool-down + work) so match_zwo can find
    a sane file; exact structure is the file's, the slot fixes type + length."""
    start = int(prog.get("start", 4)); step = int(prog.get("step", 1))
    mx = int(prog.get("max", start)); kind = prog.get("kind", "reps")
    reps = min(mx, start + step * max(0, week_in_phase))
    dur = 40 + reps * (5 if kind == "duration" else 4)
    return reps, dur


def expand_blueprint_week(
    phase: "Phase", budget: "IntensityBudget", week_num: int, week_start: date,
    available_days: list, rest_days: list, daily_max_hours: dict | None,
    max_weekday_hours: float, max_weekend_hours: float, is_stepback: bool,
    week_in_phase: int, goal,
) -> list["PlannedSession"]:
    """fixed_core / template week → 7-element Mon..Sun PlannedSession list."""
    def _cap_min(weekday: int) -> int:
        if daily_max_hours and weekday in daily_max_hours:
            return int(daily_max_hours[weekday] * 60)
        return int((max_weekend_hours if weekday >= 5 else max_weekday_hours) * 60)

    bp_hit = _blueprint_hit_for(goal, phase.name, week_num)
    # Lay rest vs train by offset from week_start (matches plan_week + the caller).
    slots: list = []
    for off in range(7):
        d = week_start + timedelta(days=off)
        wd = d.weekday()
        if wd in rest_days or wd not in available_days:
            slots.append(PlannedSession(
                day=d, day_name=d.strftime("%a"), session_type="rest",
                duration_min=0, tss_estimate=0,
                description="Rest — recovery takes priority"))
        else:
            slots.append((d, wd))  # training placeholder

    train_i = [i for i, s in enumerate(slots) if isinstance(s, tuple)]
    # One HIT per build week (D2); none on a deload (B3/B4 keep deloads easy).
    hit_i = None
    if not is_stepback and budget.hit_count_max > 0 and train_i:
        weekday_train = [i for i in train_i if slots[i][1] < 5] or train_i
        hit_i = weekday_train[len(weekday_train) // 2]

    for i in train_i:
        d, wd = slots[i]
        cap = _cap_min(wd)
        is_weekend = wd >= 5
        if i == hit_i:
            st = bp_hit["session_type"]
            reps, dur = _blueprint_progress(bp_hit.get("progression", {}), week_in_phase)
            ceil = TYPE_CEILING.get(st)
            eff = min(cap, dur) if ceil is None else min(cap, dur, ceil)
            eff = max(eff, 30)
            slots[i] = PlannedSession(
                day=d, day_name=d.strftime("%a"), session_type=st,
                duration_min=eff, tss_estimate=round(eff / 60 * TSS_PER_HOUR.get(st, 75)),
                description=f"{CAL_SESSION_LABEL_SAFE(st)} — {reps} reps (fixed-core; progresses weekly)")
        else:
            st = "long_z2" if is_weekend else "z2"
            if is_stepback:
                dur = min(cap, 90 if is_weekend else 50)
            else:
                # B6: ascend the Z2 core across the phase's build weeks.
                base = 150 if is_weekend else 75
                dur = min(cap, base + max(0, week_in_phase) * 10)
            dur = max(dur, 30)
            slots[i] = PlannedSession(
                day=d, day_name=d.strftime("%a"), session_type=st,
                duration_min=dur, tss_estimate=round(dur / 60 * TSS_PER_HOUR["z2"]),
                description=f"{'Long ' if is_weekend else ''}Z2 endurance (fixed-core base)")
    return slots


def CAL_SESSION_LABEL_SAFE(st: str) -> str:
    """Readable label for a session_type (server-side mirror of the UI map)."""
    return {
        "vo2max": "VO2max", "threshold": "Threshold", "sweetspot": "Sweet Spot",
        "overunder": "Over-Under", "sprint": "Sprints", "tempo": "Tempo",
        "z2": "Z2", "long_z2": "Long Z2", "recovery": "Recovery",
    }.get(st, st.upper())


def sample_week_workouts(
    phase: "Phase",
    budget: "IntensityBudget",
    library: list[dict],
    used_names: dict[str, int] | set,
    week_num: int,
    seed_salt: int,
    week_start: date,
    available_days: list,
    rest_days: list,
    daily_max_hours: dict | None,
    max_weekday_hours: float,
    max_weekend_hours: float,
    is_stepback: bool = False,
    pool_index: dict | None = None,
    week_in_phase: int = 0,
    recent_hit_types: list[str] | None = None,
    seen_cc_dur_tuples: set | None = None,
    plan_pick_counts: dict[str, int] | None = None,
    class_session_counts: dict[str, int] | None = None,
    class_distinct_files: dict[str, set] | None = None,
    plan_total_weeks: int = 0,
    goal_type: str = "general",
    emphasis_profile: str | None = None,
    block_focus: "str | None" = None,
) -> list["PlannedSession"]:
    """Score-weighted per-week sampler driving the v4.5 diversification overhaul.

    ``block_focus`` (F1, v2.1): when set (opt-in block periodization), the week
    concentrates its HIT slots on that content_class. None = default weekly-mixed
    behaviour (the picker plug-ins read it; None keeps them dormant).

    Returns a 7-element list of PlannedSession (one per weekday Mon..Sun);
    rest-day slots come back as session_type='rest'. The caller (generate_plan
    or regenerate_from_today) can either use these directly or merge them
    into the existing plan_week skeleton.

    Args:
        used_names: A dict mapping ``workout_name -> last_used_week`` (rolling
            6-week window). A plain set is also accepted (treated as "in last
            6 weeks" for any name in it). Mutated in place: the picked
            workouts get their names added.
        week_in_phase: 0-indexed week number WITHIN the current phase. Drives
            Layer 2 WORKOUT_MIX_PREFERENCE row selection (e.g. base W1 vs base
            W5 use different content_class weights).
        recent_hit_types: Rolling 4-week list of HIT content_classes already
            placed in prior weeks (most-recent-LAST). Drives Layer 3 rotation
            penalty so threshold→vo2max→sweet_spot→over_under cycles cleanly.
            Mutated in place: each HIT pick this week gets appended.
    """
    import random as _random

    # Reproducible RNG keyed on (week_num, seed_salt). 7919 is a prime far from
    # 1000 so seed_salt entropy doesn't collide with the week_num multiplier.
    rng = _random.Random(week_num * 1000 + (int(seed_salt) % 7919))

    # Build pool index once per call if caller didn't pass one (hot path: the
    # same library + score floor every week, so the caller passes a cached
    # index from generate_plan).
    if pool_index is None:
        pool_index = _build_pool_indexes(library)
    hit_pool = pool_index["hit"]
    # Use the wide endurance pool for all phases. The budget_fit in the
    # weighting scheme (with overshoot penalty) drives polarized adherence
    # by down-weighting workouts whose Z3+/Z4+ minutes blow the remaining
    # budget. Strict pool was tried; it caps distinct files at ~120.
    endurance_pool = pool_index["endurance"]

    # Resolve per-day max minutes
    def _max_min_for(weekday: int) -> int:
        if daily_max_hours and weekday in daily_max_hours:
            return int(daily_max_hours[weekday] * 60)
        return int((max_weekend_hours if weekday >= 5 else max_weekday_hours) * 60)

    # Build slot list: (idx, date, day_name, weekday, max_min, is_rest)
    slots: list[tuple[int, date, str, int, int, bool]] = []
    for off in range(7):
        d = week_start + timedelta(days=off)
        wd = d.weekday()
        is_rest = (wd in rest_days) or (wd not in available_days)
        slots.append((off, d, d.strftime("%a"), wd, _max_min_for(wd), is_rest))

    # 1. Identify HIT slots: pick `hit_count` slots, preferring Tue/Thu/Sat (the
    # canonical 48h-spaced pattern for endurance athletes).
    if is_stepback:
        # Stepback weeks remain endurance-only — Issurin unloading.
        hit_count = 0
    else:
        hit_count = rng.randint(budget.hit_count_min, budget.hit_count_max)

    non_rest = [s for s in slots if not s[5]]
    # Cap HIT count by 48h-gap feasibility (need ≥1 non-rest day between HITs).
    hit_count = min(hit_count, max(0, len(non_rest) // 2 + (1 if len(non_rest) % 2 else 0)))

    preferred_hit_weekdays = [1, 3, 5]  # Tue, Thu, Sat
    hit_slot_idxs: set[int] = set()
    # First pass — preferred days that are non-rest
    for wd in preferred_hit_weekdays:
        if len(hit_slot_idxs) >= hit_count:
            break
        for s in non_rest:
            if s[3] == wd and s[0] not in hit_slot_idxs:
                # 48h gap check vs already-picked HIT slots
                if all(abs(s[0] - i) >= 2 for i in hit_slot_idxs):
                    hit_slot_idxs.add(s[0])
                    break
    # Backfill if still short — any non-rest day satisfying 48h gap
    if len(hit_slot_idxs) < hit_count:
        rest = [s for s in non_rest if s[0] not in hit_slot_idxs]
        rng.shuffle(rest)
        for s in rest:
            if len(hit_slot_idxs) >= hit_count:
                break
            if all(abs(s[0] - i) >= 2 for i in hit_slot_idxs):
                hit_slot_idxs.add(s[0])

    # 2. For each slot, sample a workout. Track remaining budget.
    remaining = {
        "z1z2":   float(budget.z1z2_minutes_per_week),
        "z3":     float(budget.z3_minutes_per_week),
        "z4":     float(budget.z4_minutes_per_week),
        "z5plus": float(budget.z5plus_minutes_per_week),
    }
    if is_stepback:
        # Issurin unloading: drop targets to 72%.
        for k in remaining:
            remaining[k] *= 0.72

    # used_names normalization: accept set OR dict
    if isinstance(used_names, set):
        # Treat any name in the set as "used in last 6 weeks" (week=week_num - 1)
        used_lookup = {n: week_num - 1 for n in used_names}
    else:
        used_lookup = dict(used_names)

    # v4.5.0 Layer 2 + Layer 3: pull this week's preference row + rotation
    # window. Recent_hit_types is mutated in place — caller passes a list
    # spanning the prior 4 weeks, we append our HIT picks for next week.
    pref_row = _get_mix_preference(phase.name, week_in_phase)
    rot_window = list(recent_hit_types or [])
    rot_window_post = _apply_rotation_penalty(pref_row, rot_window, block_focus=block_focus)

    # Pre-compute eligible weights for the two slot kinds. The HIT row keeps
    # only HIT-eligible classes; endurance row keeps only endurance-eligible.
    hit_pref = {
        cc: w for cc, w in rot_window_post.items()
        if cc in _HIT_SLOT_CONTENT_CLASSES and w > 0
    }
    # v1.11.0 IMPL-GOAL-FOCUS: tilt the HIT-class pick toward the goal's target
    # work, applied AFTER the rotation penalty so the goal bias is the final word
    # (anti-monotony still operates underneath). Endurance slots are untouched;
    # no-op for goals without an emphasis profile (event/general/endurance/…).
    hit_pref = _apply_goal_emphasis(hit_pref, emphasis_profile or goal_type)
    end_pref = {
        cc: w for cc, w in pref_row.items()
        if cc in _ENDURANCE_SLOT_CONTENT_CLASSES and w > 0
    }

    # v4.5.0 acceptance §4: track (cc, dur_quintile) tuples already seen this
    # plan via the rolling rotation window's sibling — passed as state on the
    # rng-shared dict via library row level. We approximate quintiles by 30-min
    # buckets (q0=<45, q1=45-60, q2=60-80, q3=80-100, q4=≥100) so the planner
    # can cheaply detect "novel tuple" without needing the global session list.
    def _quintile_bucket(dur: float) -> int:
        if dur < 45: return 0
        if dur < 60: return 1
        if dur < 80: return 2
        if dur < 100: return 3
        return 4

    week_picked: dict[str, int] = {}  # name -> count this week
    out: list[PlannedSession] = [None] * 7  # type: ignore[list-item]
    week_hit_picks: list[str] = []  # content_classes picked for HIT slots THIS week
    # v1.0.6 IMPL-3D-PLANNER: track prior day's glycolytic load for soft
    # anti-stacking (TSS PRIMARY, 3D ADDITIVE).
    prev_day_glyco_load: float = 0.0

    for off, d, day_name, weekday, max_min, is_rest in slots:
        if is_rest:
            out[off] = PlannedSession(
                day=d, day_name=day_name, session_type="rest",
                duration_min=0, tss_estimate=0,
                description="Rest — recovery takes priority",
            )
            # v1.0.6: rest day clears glycolytic stacking memory.
            prev_day_glyco_load = 0.0
            continue

        is_hit = off in hit_slot_idxs
        candidates = hit_pool if is_hit else endurance_pool

        # v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): widen feasibility window
        # to ±25 min around the slot's max_min so a much larger candidate pool
        # is reachable per slot. Lower floor still depends on slot kind so a
        # weekend long-Z2 doesn't admit a 30-min recovery spin, but the upper
        # ceiling now gives 25 min of headroom (was 5).
        min_dur = 35 if is_hit else 45
        feasible = [
            w for w in candidates
            if min_dur <= float(w.get("Duration(min)", 0) or 0) <= max_min + 25
        ]

        if not feasible:
            # Emergency fallback — drop the duration floor & dip into ALL workouts
            feasible = [
                w for w in pool_index["all_pool"]
                if 0 < float(w.get("Duration(min)", 0) or 0) <= max_min + 25
            ]

        if not feasible:
            # Truly nothing fits — emit an empty Z2 placeholder for match_zwo
            # to retry later.
            dur = max(45, min(max_min, 60))
            out[off] = PlannedSession(
                day=d, day_name=day_name, session_type="z2",
                duration_min=dur,
                tss_estimate=round(dur / 60 * TSS_PER_HOUR["z2"]),
                description=f"Z2 endurance ({dur}min)",
                matched=False,
            )
            continue

        # v4.5.0 Layer 2/3: bias the per-class preference for THIS slot. For
        # HIT slots also fold in the rotation history of THIS week's already-
        # picked HIT types so two HIT slots in one week don't both land on
        # vo2max. Endurance slots use the raw preference row (rotation only
        # applies to HIT axis — Z2 sessions are interchangeable enough).
        if is_hit:
            slot_pref = dict(hit_pref)
            if week_hit_picks:
                # Penalize already-picked HIT types this week so the second
                # HIT slot rotates to a different class.
                for cc in week_hit_picks:
                    # F1 (v2.1/B4): in a block, the FOCUS class is exempt — two
                    # HIT slots in a VO2 block may BOTH be vo2max (concentration).
                    # None ⇒ default de-dup (parity).
                    if block_focus and cc == block_focus:
                        continue
                    if cc in slot_pref:
                        slot_pref[cc] *= 0.4
        else:
            slot_pref = end_pref

        # v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): per-class minimums.
        # If a content_class has fewer distinct files used than its
        # trajectory target so far, bias picks toward unseen files in that
        # class. Trajectory = (weeks_elapsed / plan_total_weeks) × min_target.
        scaled_mins = (
            _scaled_class_min_distinct(plan_total_weeks)
            if plan_total_weeks > 0 else {}
        )
        weeks_elapsed = max(1, week_num)
        below_traj_classes: set[str] = set()
        if scaled_mins and class_distinct_files is not None and plan_total_weeks > 0:
            for cc_min, target_min in scaled_mins.items():
                trajectory = (weeks_elapsed / plan_total_weeks) * target_min
                seen_count = len(class_distinct_files.get(cc_min, set()))
                if seen_count < trajectory:
                    below_traj_classes.add(cc_min)

        # Score every feasible candidate
        weights: list[float] = []
        for w in feasible:
            name = w.get("Name", "")
            row_cc = _content_class_for_row(w)

            # v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): diversity cap. Skip
            # files that have hit their plan-wide quota. Quota =
            # max(1, ceil(class_session_count_so_far / _DIVERSITY_BUDGET_DIVISOR)).
            # Floor of 1 lets the FIRST pick of a class proceed (cur_picks=0
            # < cap=1), but a file that's been picked once cannot repeat
            # until at least 8 more sessions of that class have been placed
            # (cap rises to 2 at session 9).
            cur_picks = (plan_pick_counts or {}).get(name, 0)
            if class_session_counts is not None and row_cc:
                cur_class_n = class_session_counts.get(row_cc, 0)
                cap = max(1, math.ceil(cur_class_n / _DIVERSITY_BUDGET_DIVISOR))
                if cur_picks >= cap:
                    weights.append(0.0)
                    continue

            zones = _row_zone_minutes(w)
            fit = _budget_fit_score(zones, remaining)  # 0..1
            score = float(w.get("Score", 0) or 0)
            quality = max(0.0, (score - 5.0) / 5.0)  # 0..1 over score 5..10
            # v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): plan_pick_counts is
            # the PRIMARY novelty signal — once a file's been picked any
            # number of times across the plan, it shrinks regardless of
            # whether its used_names entry was already evicted. Without
            # this, a file at plan_pick_counts=1 whose used_names entry was
            # evicted (12+ weeks old) would look identical to a never-picked
            # file, undermining the diversity goal.
            if cur_picks == 0:
                last_used = used_lookup.get(name)
                if last_used is None:
                    novelty = 5.0
                else:
                    recency = week_num - last_used
                    novelty = max(0.01, min(1.0, recency / 18.0))
            else:
                last_used = used_lookup.get(name)
                if last_used is None:
                    novelty = 0.5 / cur_picks
                else:
                    recency = max(1, week_num - last_used)
                    novelty = max(0.01, min(0.6, recency / 18.0))
            # Novelty boost multipliers per master §3 step 5: 1.5× never
            # picked, 1.0× once, 0.5× twice, then asymptotes (the diversity
            # cap above zeros it out beyond that).
            novelty *= _NOVELTY_BOOST.get(min(cur_picks, 2), 0.5)

            dup_penalty = 0.05 if week_picked.get(name, 0) > 0 else 1.0
            soft_fit = math.sqrt(max(0.0, fit))
            # v4.5.0 Layer 2/3: per-class mix-preference multiplier. Rows in
            # WORKOUT_MIX_PREFERENCE that don't list a class still get a
            # baseline weight (0.08) so vo2_short / niche classes appear
            # occasionally — a 24-week plan should sample every HIT type at
            # least once. The (0.3 + mix_mult * 5.0) shape keeps the in-row
            # classes 5-7x more likely than the floor without zeroing the floor.
            mix_mult = slot_pref.get(row_cc)
            if mix_mult is None:
                mix_mult = 0.08
            # v4.5.0 acceptance: novelty bonus for unseen (cc, dur_quintile)
            # tuples this plan. ≥30 distinct tuples is the headline target;
            # the bonus pushes the picker toward unfilled buckets when the
            # base preferences would otherwise concentrate (e.g. mid-duration
            # mixed). Bonus 1.6× on novel tuples — large enough to break ties
            # but not so large as to override budget_fit.
            row_dur = float(w.get("Duration(min)", 0) or 0)
            tuple_bonus = 1.0
            if seen_cc_dur_tuples is not None and row_cc:
                tup = (row_cc, _quintile_bucket(row_dur))
                if tup not in seen_cc_dur_tuples:
                    tuple_bonus = 1.6
            # v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): per-class-minimum
            # bias — when a class is below its distinct-files trajectory,
            # boost UNSEEN files in that class so the soft minimum can be
            # met. 2× boost for never-picked files of below-trajectory class.
            class_min_bonus = 1.0
            if row_cc in below_traj_classes and cur_picks == 0:
                class_min_bonus = 2.0

            # v4.6.1 PLANNER-VARIETY+RONNESTAD: variety bonus disabled in
            # weight (the hard-floor post-pass IS the structural variety
            # mechanism for category coverage; multiplying variety_score
            # into the per-file weight was found to collapse distinct-file
            # diversity across the plan). The variety_score helper remains
            # exported for downstream callers and unit-test pinning.
            var_mult = 1.0

            # v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE): soft
            # anti-stacking penalty. ×0.7 (soft) NOT 0.0 (reject).
            glyco_stack_mult = 1.0
            if prev_day_glyco_load >= 0.7 and row_cc:
                today_glyco = _GLYCOLYTIC_LOAD_BY_CLASS.get(row_cc, 0.0)
                if today_glyco >= 0.7:
                    glyco_stack_mult = 0.7

            wt = max(0.0001,
                     (0.2 + soft_fit) * novelty * (0.5 + quality)
                     * dup_penalty * (0.3 + mix_mult * 5.0)
                     * tuple_bonus * class_min_bonus * var_mult
                     * glyco_stack_mult)
            weights.append(wt)

        total_w = sum(weights)
        if total_w <= 0:
            pick = rng.choice(feasible)
        else:
            r = rng.random() * total_w
            cum = 0.0
            pick_idx = 0
            for i, wt in enumerate(weights):
                cum += wt
                if cum >= r:
                    pick_idx = i
                    break
            pick = feasible[pick_idx]

        sess = _make_session_from_row(pick, d, day_name, phase.name)
        # v1.8.21 — HARD-clamp the session to the day's AVAILABLE minutes. The
        # feasibility window above admits files up to ``max_min + 25`` purely
        # for candidate-pool breadth, and ``_make_session_from_row`` copies the
        # file's full duration — so a 90-min slot could surface a 99–115-min
        # session, violating the rider's stated availability (the reported
        # "90-min cap → 2.5h session" bug; the 2.5h case is a weekend slot
        # whose own cap is max_weekend). The PLANNED duration must never exceed
        # the day's cap; TSS scales down proportionally. The matched ZWO may be
        # slightly longer and is paced/truncated on the trainer (the modal's
        # existing showGap banner explains the difference).
        # FIX-2 (safety): clamp to min(day_cap, per-type ceiling) so a hard
        # session can never run the full day (e.g. a 120-min VO2max on a 2 h
        # weekday). Prefer the content_class ceiling (anaerobic / vo2_short /
        # neuromuscular files map to session_type='vo2max', which would
        # otherwise apply the looser 75-min VO2max cap). Endurance types have
        # no entry in TYPE_CEILING → day-cap only.
        _pick_cc_clamp = _content_class_for_row(pick)
        _ceiling = TYPE_CEILING.get(_pick_cc_clamp) or TYPE_CEILING.get(sess.session_type)
        _eff_cap = max_min
        if _ceiling is not None:
            _eff_cap = _ceiling if max_min <= 0 else min(max_min, _ceiling)
        if _eff_cap > 0 and sess.duration_min > _eff_cap:
            _scale = _eff_cap / float(sess.duration_min)
            sess.tss_estimate = round(sess.tss_estimate * _scale)
            sess.duration_min = _eff_cap
        # v4.5.0 IMPL-PLANNER: pin endurance-slot session_type so a sampled
        # `sweetspot_long_*.zwo` (mixed-content with high Z2) doesn't carry
        # session_type='sweetspot' into a slot the sampler treated as
        # endurance. Without this, code that counts HIT by session_type
        # (e.g. test_planner_fixes' base-week HIT cap, daily-adapt HIT
        # gating) over-counts. We choose z2 / tempo from the workout's
        # actual zone profile (recovery only if filename prefix is recovery_).
        _hit_st = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}
        if not is_hit and sess.session_type in _hit_st:
            z3 = float(pick.get("Z3%", 0) or 0)
            if z3 >= 30:
                sess.session_type = "tempo"
            else:
                sess.session_type = "z2"
        # Long-Z2 weekend reclassification — keep visual signal "long_z2" for
        # endurance ≥120min on Sat/Sun.
        if (
            sess.session_type == "z2" and sess.duration_min >= 120
            and weekday >= 5
        ):
            sess.session_type = "long_z2"
        sess.nutrition_note = _nutrition_note(phase.name, sess.session_type)
        out[off] = sess

        # v4.5.0 Layer 3 tracking: append this slot's content_class to the
        # rolling rotation log when it's a HIT slot.
        pick_cc = _content_class_for_row(pick)
        if is_hit and pick_cc:
            week_hit_picks.append(pick_cc)

        # v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE): update
        # prev-day glycolytic-load tracker for tomorrow's stacking check.
        prev_day_glyco_load = _GLYCOLYTIC_LOAD_BY_CLASS.get(pick_cc or "", 0.0)

        # v4.5.0 acceptance: record (cc, dur_quintile) to drive next slot's
        # tuple-novelty bonus toward unfilled tuples.
        if seen_cc_dur_tuples is not None and pick_cc:
            seen_cc_dur_tuples.add(
                (pick_cc, _quintile_bucket(float(pick.get("Duration(min)", 0) or 0)))
            )

        # Update budgets + tracking
        zones = _row_zone_minutes(pick)
        for z in remaining:
            remaining[z] = max(0.0, remaining[z] - zones.get(z, 0.0))
        nm = pick.get("Name", "")
        if nm:
            week_picked[nm] = week_picked.get(nm, 0) + 1
            # Update the rolling used_names with this week's number so the
            # next week's sampler sees recency. Caller's used_names dict is
            # mutated in place when passed as dict.
            if isinstance(used_names, dict):
                used_names[nm] = week_num
            else:
                used_names.add(nm)
            # v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): plan-wide
            # bookkeeping for diversity cap + per-class-minimum bias.
            if plan_pick_counts is not None:
                plan_pick_counts[nm] = plan_pick_counts.get(nm, 0) + 1
            if pick_cc:
                if class_session_counts is not None:
                    class_session_counts[pick_cc] = class_session_counts.get(pick_cc, 0) + 1
                if class_distinct_files is not None:
                    class_distinct_files.setdefault(pick_cc, set()).add(nm)

    # v4.5.0 Layer 3: forward this week's HIT content_classes into the rolling
    # window for next week's sampler. recent_hit_types is mutated in place.
    if recent_hit_types is not None:
        for cc in week_hit_picks:
            recent_hit_types.append(cc)

    # v4.5.4 FIX-PLANNER-INTERVALS: per-week interval-shape FLOOR.
    # User complaint #4: plan looks like "diagonal blocks" because too many
    # weeks pick zero interval-shaped workouts in base + early build1. Force
    # at least N interval-shaped picks per week even in base; 2 in build/peak.
    # Interval-shaped = sweet_spot / threshold / vo2max / vo2_short /
    # over_under / anaerobic / neuromuscular (the 4×8, 5×3, 30/30, sprints
    # shapes the user wants to see — NOT endurance/tempo/recovery/mixed).
    if not is_stepback:
        _interval_ccs = _INTERVAL_SHAPED_CONTENT_CLASSES
        _interval_flags = (
            "has_threshold_work", "has_vo2_work", "has_sprints",
            "has_sweet_spot_work", "pattern_over_under",
            "pattern_microinterval",
        )
        # Per-phase floor + week_in_phase modulation. Mid/late base = 1, build
        # phases = 2, peak = 2.
        if phase.name == "base":
            floor = 1 if week_in_phase >= 2 else 0
        elif phase.name in ("build1", "build2", "peak"):
            floor = 2
        else:
            floor = 0
        # Count current interval-shaped picks. Wide definition: pure interval
        # content_class OR a "mixed" workout whose secondary_flags expose an
        # interval pattern (4×8, 30/30, over-under) — this matters because
        # ~363 library files are classifier-tagged as `mixed` despite having
        # explicit interval segments (see audit /tmp/fix_planner_intervals_v454.md).
        def _is_interval_shaped(sess: PlannedSession) -> bool:
            if sess is None or sess.session_type == "rest":
                return False
            zwo = (sess.zwo_file or "").strip()
            if not zwo:
                return False
            cache = _load_content_classifications()
            ent = cache.get(zwo)
            if ent is None and "/" in zwo:
                ent = cache.get(zwo.split("/")[-1])
            if not ent:
                return False
            cc = (ent.get("primary") or "").lower()
            if cc in _interval_ccs:
                return True
            if cc == "mixed":
                flags = ent.get("secondary_flags", {}) or {}
                return any(flags.get(f, False) for f in _interval_flags)
            return False
        cur_intervals = sum(1 for s in out if _is_interval_shaped(s))
        # Try to swap up to (floor - cur_intervals) endurance slots whose pick
        # is steady-shaped with a fresh interval-shaped pick from hit_pool.
        swap_attempts = max(0, floor - cur_intervals)
        if swap_attempts > 0 and hit_pool:
            # Build candidate slot list: non-rest, non-HIT slots whose current
            # session is steady-shaped (so swapping it preserves HIT day spacing
            # but visibly mixes interval-shaped variety into the week).
            steady_slots = []
            for off, d, dn, wd, mm, ir in slots:
                if ir or off in hit_slot_idxs:
                    continue
                sess = out[off]
                if sess is None or sess.session_type == "rest":
                    continue
                if not _is_interval_shaped(sess):
                    steady_slots.append((off, d, dn, wd, mm))
            # Shuffle deterministically by RNG
            rng.shuffle(steady_slots)
            for off, d, day_name, weekday, max_min in steady_slots:
                if swap_attempts <= 0:
                    break
                # Build a candidate pool of interval-shaped workouts that fit
                # this slot's duration ceiling (bounded by max_min). Use the
                # full pref row (not slot-restricted) to pull lower-intensity
                # interval shapes for base weeks (sweet_spot is preferred).
                # Wide definition: pure interval cc OR mixed-with-interval-flag.
                def _row_is_intvl(w: dict) -> bool:
                    cc = _content_class_for_row(w)
                    if cc in _interval_ccs:
                        return True
                    if cc == "mixed":
                        flags = w.get("SecondaryFlags") or {}
                        return any(flags.get(f, False) for f in _interval_flags)
                    return False
                # Pull from BOTH hit_pool and the wider all_pool so mixed-
                # tagged interval workouts are reachable (they may not pass
                # the hit_pool eligibility filter on Protocol).
                source = pool_index.get("all_pool", hit_pool)
                # v4.6.0: ±25 tolerance, matches main-loop feasibility window.
                interval_feasible = [
                    w for w in source
                    if 35 <= float(w.get("Duration(min)", 0) or 0) <= max_min + 25
                    and _row_is_intvl(w)
                    # v2.0.3 F2: only inject steady-slot interval variety from
                    # classes that are honest at tempo intensity (sweet_spot /
                    # tempo / mixed). The slot is then labeled "tempo" below — a
                    # label the card classifier ACCEPTS for those zwo classes, so
                    # no missing_workout card. Truly-hard classes (vo2max /
                    # threshold / over_under / anaerobic) reach the plan via the
                    # HIT slots + hard-floor, never relabeled-easy here.
                    and _content_class_for_row(w) in ("sweet_spot", "tempo", "mixed")
                ]
                # For BASE phase, prefer sweet_spot first (gentler shapes) then
                # threshold/over_under. For build/peak, weight by pref_row.
                if not interval_feasible:
                    continue
                # Score by mix-pref weight + novelty + quality (lighter scoring
                # than the main loop — this is a corrective swap not a primary
                # pick).
                weights2: list[float] = []
                for w in interval_feasible:
                    cc = _content_class_for_row(w)
                    mix_mult = pref_row.get(cc, 0.05)
                    score = float(w.get("Score", 0) or 0)
                    quality = max(0.0, (score - 5.0) / 5.0)
                    nm_w = w.get("Name", "")
                    cur_picks_w = (plan_pick_counts or {}).get(nm_w, 0)
                    # v4.6.0: respect diversity cap.
                    if class_session_counts is not None and cc:
                        cap_w = max(1, math.ceil(
                            class_session_counts.get(cc, 0) / _DIVERSITY_BUDGET_DIVISOR))
                        if cur_picks_w >= cap_w:
                            weights2.append(0.0)
                            continue
                    last_used = used_lookup.get(nm_w)
                    if cur_picks_w == 0:
                        if last_used is None:
                            novelty = 5.0
                        else:
                            recency = week_num - last_used
                            novelty = max(0.01, min(1.0, recency / 18.0))
                    else:
                        if last_used is None:
                            novelty = 0.5 / cur_picks_w
                        else:
                            recency = max(1, week_num - last_used)
                            novelty = max(0.01, min(0.6, recency / 18.0))
                    novelty *= _NOVELTY_BOOST.get(min(cur_picks_w, 2), 0.5)
                    dup_penalty = 0.05 if week_picked.get(nm_w, 0) > 0 else 1.0
                    weights2.append(max(0.0001,
                        (0.3 + mix_mult * 5.0) * novelty * (0.5 + quality) * dup_penalty))
                total_w2 = sum(weights2)
                if total_w2 <= 0:
                    pick = rng.choice(interval_feasible)
                else:
                    r = rng.random() * total_w2
                    cum = 0.0
                    pick_idx = 0
                    for i, wt in enumerate(weights2):
                        cum += wt
                        if cum >= r:
                            pick_idx = i
                            break
                    pick = interval_feasible[pick_idx]
                # Replace the slot with an interval-SHAPED pick, but this is a
                # STEADY (non-HIT) slot — every steady_slots entry was filtered
                # to off not in hit_slot_idxs above. v2.0.3 F2: count HIT by
                # session_type (the hit-budget contract test_planner_fixes
                # relies on), so demote a HIT-typed pick to tempo/z2 from its
                # Z3% exactly like the main endurance-slot loop does (~tp:4214).
                # _is_interval_shaped keys on the zwo_file content_class, NOT
                # session_type, so the interval-variety floor still counts this
                # slot — we keep the variety without spending a HIT slot.
                new_sess = _make_session_from_row(pick, d, day_name, phase.name)
                # v2.0.3 F2: steady slot, and the pool above is restricted to
                # moderate interval classes (sweet_spot/tempo/mixed). Label any
                # HIT-typed result ("sweetspot") "tempo" and recompute TSS so the
                # card is coherent (the "tempo" card accepts a sweet_spot/tempo/
                # mixed zwo) and the load matches the label — not the old hard-
                # interval TSS on an easy-labeled session.
                _hit_st_swap = {"vo2max", "threshold", "overunder", "sweetspot", "sprint"}
                if new_sess.session_type in _hit_st_swap:
                    new_sess.session_type = "tempo"
                    new_sess.tss_estimate = round(
                        new_sess.duration_min / 60 * TSS_PER_HOUR.get("tempo", 60), 1)
                new_sess.nutrition_note = _nutrition_note(phase.name, new_sess.session_type)
                # Free the old pick's name from week_picked (the original slot
                # contributed to seen_cc_dur_tuples; we keep that — fine).
                out[off] = new_sess
                nm = pick.get("Name", "")
                pick_cc_swap = _content_class_for_row(pick)
                if nm:
                    week_picked[nm] = week_picked.get(nm, 0) + 1
                    if isinstance(used_names, dict):
                        used_names[nm] = week_num
                    else:
                        used_names.add(nm)
                    # v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B) bookkeeping.
                    if plan_pick_counts is not None:
                        plan_pick_counts[nm] = plan_pick_counts.get(nm, 0) + 1
                    if pick_cc_swap:
                        if class_session_counts is not None:
                            class_session_counts[pick_cc_swap] = class_session_counts.get(pick_cc_swap, 0) + 1
                        if class_distinct_files is not None:
                            class_distinct_files.setdefault(pick_cc_swap, set()).add(nm)
                # Track tuple for global tuple-novelty bookkeeping
                if seen_cc_dur_tuples is not None and pick_cc_swap:
                    seen_cc_dur_tuples.add(
                        (pick_cc_swap, _quintile_bucket(float(pick.get("Duration(min)", 0) or 0)))
                    )
                swap_attempts -= 1

    # 3. Budget verification: if total TSS missed by >15%, do one re-roll on the
    # worst-fitting endurance slot (cheapest to re-pick without disrupting HIT).
    total_tss = sum(s.tss_estimate for s in out if s.session_type != "rest")
    target_tss = budget.tss_per_week * (0.72 if is_stepback else 1.0)
    if target_tss > 0 and abs(total_tss - target_tss) / target_tss > 0.15:
        # Find the endurance slot whose zone profile is furthest from remaining
        # need, swap it. (Best-effort — single attempt only, per MASTER §3.)
        re_idx = None
        worst_fit = 1e9
        for off, d, _dn, wd, mm, ir in slots:
            if ir:
                continue
            if off in hit_slot_idxs:
                continue
            sess = out[off]
            if sess is None or sess.session_type == "rest":
                continue
            # Score this slot's badness as |its TSS - share of target|
            share = target_tss / max(1, len(non_rest))
            badness = abs(sess.tss_estimate - share)
            if badness < worst_fit:
                worst_fit = badness
                re_idx = off
        if re_idx is not None:
            d = week_start + timedelta(days=re_idx)
            wd = d.weekday()
            mm = _max_min_for(wd)
            # v4.6.0: ±25 tolerance, matches main loop.
            feasible = [
                w for w in endurance_pool
                if 45 <= float(w.get("Duration(min)", 0) or 0) <= mm + 25
            ]
            if feasible:
                # Re-score with current remaining
                weights: list[float] = []
                for w in feasible:
                    zones = _row_zone_minutes(w)
                    fit = _budget_fit_score(zones, remaining)
                    score = float(w.get("Score", 0) or 0)
                    quality = max(0.0, (score - 5.0) / 5.0)
                    nm_w = w.get("Name", "")
                    cur_picks_w = (plan_pick_counts or {}).get(nm_w, 0)
                    cc_w = _content_class_for_row(w)
                    if class_session_counts is not None and cc_w:
                        cap_w = max(1, math.ceil(
                            class_session_counts.get(cc_w, 0) / _DIVERSITY_BUDGET_DIVISOR))
                        if cur_picks_w >= cap_w:
                            weights.append(0.0)
                            continue
                    last_used = used_lookup.get(nm_w)
                    if cur_picks_w == 0:
                        if last_used is None:
                            novelty = 5.0
                        else:
                            recency = week_num - last_used
                            novelty = max(0.01, min(1.0, recency / 18.0))
                    else:
                        if last_used is None:
                            novelty = 0.5 / cur_picks_w
                        else:
                            recency = max(1, week_num - last_used)
                            novelty = max(0.01, min(0.6, recency / 18.0))
                    novelty *= _NOVELTY_BOOST.get(min(cur_picks_w, 2), 0.5)
                    dup_penalty = 0.05 if week_picked.get(nm_w, 0) > 0 else 1.0
                    soft_fit = math.sqrt(max(0.0, fit))
                    weights.append(max(0.0001, (0.2 + soft_fit) * novelty * (0.5 + quality) * dup_penalty))
                total_w = sum(weights)
                if total_w > 0:
                    r = rng.random() * total_w
                    cum = 0.0
                    pick_idx = 0
                    for i, wt in enumerate(weights):
                        cum += wt
                        if cum >= r:
                            pick_idx = i
                            break
                    pick = feasible[pick_idx]
                    new_sess = _make_session_from_row(pick, d, d.strftime("%a"), phase.name)
                    if new_sess.session_type == "z2" and new_sess.duration_min >= 120 and wd >= 5:
                        new_sess.session_type = "long_z2"
                    new_sess.nutrition_note = _nutrition_note(phase.name, new_sess.session_type)
                    out[re_idx] = new_sess
                    nm = pick.get("Name", "")
                    pick_cc_rr = _content_class_for_row(pick)
                    if nm:
                        week_picked[nm] = week_picked.get(nm, 0) + 1
                        if isinstance(used_names, dict):
                            used_names[nm] = week_num
                        # v4.6.0 IMPL-PLANNER-UTILIZATION bookkeeping.
                        if plan_pick_counts is not None:
                            plan_pick_counts[nm] = plan_pick_counts.get(nm, 0) + 1
                        if pick_cc_rr:
                            if class_session_counts is not None:
                                class_session_counts[pick_cc_rr] = class_session_counts.get(pick_cc_rr, 0) + 1
                            if class_distinct_files is not None:
                                class_distinct_files.setdefault(pick_cc_rr, set()).add(nm)

    return out


# ── Full plan generation ──────────────────────────────────────────────────────

def _apply_long_ride_target(sessions: list, target_min: int, max_weekend_min: int,
                            is_stepback: bool) -> None:
    """v1.11.0 IMPL-EVENT (P1, the lever) — extend the weekend long ride toward the
    event's endurance target, IN PLACE. A long Z2 ride is unstructured, so we grow
    the already-sampled weekend endurance session's duration (the zwo is a guide;
    riding easy longer IS the session) and rescale its TSS — which correctly raises
    the week's load so the CTL forecast tracks the prescribed volume.

    Capped by weekend availability AND the 5h ceiling (both in target_min/-_weekend
    by the caller) and discounted ×0.72 on stepback weeks. No-op when there's no
    weekend endurance slot or it's already long enough."""
    cap = min(int(target_min or 0), int(max_weekend_min or 0))
    if is_stepback:
        cap = int(round(cap * 0.72))
    if cap <= 0:
        return
    # Pick the longer weekend (Sat/Sun) endurance session — found by s.day.weekday()
    # (the session list is NOT guaranteed Mon..Sun-indexed).
    best = None
    for s in sessions:
        if s is None or s.session_type not in ("z2", "long_z2") or (s.duration_min or 0) <= 0:
            continue
        # FC3/F2b (v2.5.0): never grow the race entry or the openers ride.
        if _protect_race(s) or getattr(s, "is_opener", False):
            continue
        day = getattr(s, "day", None)
        wd = day.weekday() if hasattr(day, "weekday") else None
        if wd not in (5, 6):
            continue
        if best is None or s.duration_min > best.duration_min:
            best = s
    if best is None or best.duration_min >= cap:    # no weekend Z2 slot, or already long enough
        return
    tss_per_min = (best.tss_estimate / best.duration_min) if best.duration_min else 0.7
    best.duration_min = cap
    best.tss_estimate = round(tss_per_min * cap)
    if cap >= 120:
        best.session_type = "long_z2"


# F1 (v2.1) — block focus per build/peak phase. Evidence-grounded order
# (IP_F1_research.md / Rønnestad): VO2max block first, then a threshold/race-
# specific block toward the event. base/taper/consolidation/history have no
# focus. Returns None unless opt-in block periodization is on (default-off parity)
# or the week is a stepback (unload weeks stay easy, no concentrated focus).
_BLOCK_FOCUS_BY_PHASE = {"build1": "vo2max", "build2": "threshold", "peak": "threshold"}


def _block_focus_for(phase_name: str, goal: "Goal", is_stepback: bool) -> "str | None":
    if is_stepback or not getattr(goal, "block_periodization", False):
        return None
    return _BLOCK_FOCUS_BY_PHASE.get(phase_name)


def _span_weeks(p: "Phase") -> int:
    """FC1-CLIP (v2.5.0) — a phase's week count derived from its ACTUAL day-span
    (ceil), which equals the number of week-rows the emitters produce for it.
    Whole-week phases (every non-event phase) give exactly Phase.weeks."""
    return max(1, -(-((p.end - p.start).days + 1) // 7))


def _clip_week_to_phase(pw: "PlannedWeek", phase: "Phase", cursor: date) -> None:
    """FC1-CLIP (v2.5.0, D2/D3) — clamp an emitted 7-day week to its phase's
    day-span. ``plan_week`` always builds cursor..cursor+6; when the phase ends
    mid-week (taper anchored on the target date, or the pre-taper phase
    truncated by the reconcile) the spill double-booked days owned by the next
    phase's rows (D2: up to 6 duplicate days) and ran past the target date
    (D3: training the day after the race). Truncate the sessions to the phase
    end, fix the row's end, and prorate the TSS target by the actual span.
    Whole weeks — the non-event common case — are untouched (structural no-op).
    Shared by all three emitters: generate_plan, regenerate_from_today,
    recalculate_plan."""
    week_end = cursor + timedelta(days=6)
    if week_end <= phase.end:
        return
    pw.sessions = [s for s in pw.sessions if s.day <= phase.end]
    pw.end = min(week_end, phase.end)
    span = (pw.end - cursor).days + 1
    pw.tss_target = round((pw.tss_target or 0) * span / 7)


def generate_plan(
    goal: Goal,
    unavailable_periods: "list[tuple[date, date]] | None" = None,
    seed_salt: int = 0,
    availability_overrides: "dict[str, float] | None" = None,
    athlete: dict | None = None,
    current_ctl: float | None = None,
    recent_weekly_tss: float | None = None,
) -> tuple[list[Phase], list[PlannedWeek]]:
    """Generate the full training plan.

    Args:
        goal: Athlete goal.
        unavailable_periods: Optional list of (start, end) date pairs (inclusive).
            Any session whose day falls within any period is converted to a
            rest day. Mirrors the logic in regenerate_from_today so that
            first-time plan creation also honors time off.
        seed_salt: v4.3.0 B3 — extra entropy mixed into both _pick_session and
            match_zwo seeds so consecutive regenerations produce visibly
            different ZWO picks. Default 0 keeps the legacy deterministic
            output (used by tests and first-gen plans).
        availability_overrides: v1.3.2 — sparse mapping iso-date → daily hours
            applied AFTER the bulk planner runs. ``hours == 0`` converts that
            day to rest; ``hours > 0`` rescales duration_min/tss_estimate
            (per-week clamp [0.4, 2.0]) and re-runs match_zwo so the picked
            workout fits the new duration. Mirrors the reforecast() availability
            block so first-time plan creation honors the persisted calendar.
        current_ctl: v2.1.0 (F5) — rider's actual current CTL for the INITIAL
            ramp. When None, falls back to the self-fetch (ICU wellness → local
            42-day EWMA → 37.0). The app passes the real value so a fresh plan
            no longer starts "post-winter".
        recent_weekly_tss: v2.1.0 (E1) — rider's recent mean weekly TSS, used
            as the LOAD-based weekly volume ceiling (see generate_phases). When
            None, self-fetches from the local archive; if still None, the
            legacy availability cap (hours_per_week×65) applies.
    """
    # F4b (v2.5.0, D1): a target date today-or-earlier silently produced a
    # degenerate backward taper + an EMPTY plan (weeks_available clamps to ≥1,
    # the reconcile loop pops every forward phase). Refuse it up front with a
    # user-facing message; app.py surfaces this as a 400.
    if goal.target_date is not None and goal.target_date <= date.today():
        raise ValueError(
            f"Target date {goal.target_date.isoformat()} is today or in the "
            "past — pick a future date (tomorrow at the earliest)."
        )
    # F4d (v2.5.0, SM4): the A race IS the goal (target_date + event_* scalars);
    # a second priority-A entry in events[] was silently dropped by every
    # consumer. Honest refusal beats silent data loss; app.py surfaces 400.
    for _ev in (getattr(goal, "events", None) or []):
        if str(getattr(_ev, "priority", "B") or "B").upper() == "A":
            raise ValueError(
                "one A event per plan — the target event is your A race; "
                "mark additional races as priority B or C."
            )

    # J1 (v2.1.0): honor the goal's chosen intensity-distribution model for every
    # get_budget_for_phase lookup in this run (default "polarized" → unchanged).
    set_active_distribution(getattr(goal, "distribution", "polarized"),
                            getattr(goal, "custom_bands", None))
    metrics = get_today_metrics()
    # F4 (v4.1.0) — local CTL fallback. Previously this path hardcoded 37.0
    # when ICU was unreachable; any user whose wellness sync was broken got
    # a phantom fitness baseline wildly divergent from their actual recent
    # rides. Fall back to a 42-day EWMA over the local ride archive before
    # reverting to the constant.
    # v2.1.0 (F5): an explicit current_ctl from the caller (app's
    # /api/plan/generate) wins over the self-fetch — the app already has the
    # ICU/local value and threading it avoids a redundant fetch + guarantees
    # initial generation ramps from real fitness.
    if current_ctl is None:
        current_ctl = metrics.get("ctl")
    if current_ctl is None:
        try:
            import ride_storage as _rs
            local = _rs.compute_local_ctl()
            if local is not None:
                current_ctl = local
                log.info(f"EVENT=ctl_local_fallback ctl={local}")
        except Exception as _e:
            log.debug(f"local CTL fallback failed: {_e}")
    if current_ctl is None:
        current_ctl = 37.0

    # v2.1.0 (E1) — recent mean weekly TSS sets the load-based volume ceiling.
    # Self-fetch from the full local archive when the caller didn't supply it
    # (best-effort; None → generate_phases keeps the legacy availability cap).
    if recent_weekly_tss is None:
        try:
            import ride_storage as _rs
            recent_weekly_tss = _rs.recent_mean_weekly_tss()
        except Exception as _e:
            log.debug(f"recent_mean_weekly_tss fetch failed: {_e}")

    # B3 (v2.1.0): ICU-only / fresh-install riders have no local FIT archive, so
    # recent_mean_weekly_tss() returns None and the plan would fall back to the
    # legacy availability-driven cap — the 24.5h over-scheduling E1 set out to
    # fix, and the exact symptom the original reporter had (ICU-primary). CTL is
    # the chronic daily-load EWMA, so CTL×7 is a sound recent-weekly-TSS proxy;
    # anchor on it so the LOAD-based ceiling still applies rather than availability.
    if recent_weekly_tss is None and current_ctl and current_ctl > 0:
        recent_weekly_tss = round(current_ctl * 7)

    # v1.11.0 IMPL-EVENT — event demand → plan targets (None for non-event goals
    # or missing athlete → all event wiring no-ops, non-event plans unchanged).
    event_targets = _event_demand_targets(
        goal, athlete, {"current_ctl": current_ctl})

    try:
        phases = generate_phases(goal, current_ctl, event_targets,
                                 recent_weekly_tss=recent_weekly_tss)
    except Exception as _e:
        # v1.6.1 — phase derivation failed (bad goal inputs / target_date in past).
        # Re-raise after logging so the caller (app.py /api/plan/generate) can
        # surface the failure to the user.
        _tp_log_error(error_codes.Codes.PHASE_DERIVE_FAILED, exc=_e,
                      goal_type=getattr(goal, "goal_type", "?"),
                      current_ctl=current_ctl)
        raise
    library = load_workout_library()

    # The plan's anchor date for stable seeding: start of the first phase.
    plan_start_date = phases[0].start if phases else date.today()

    # v4.5.0 IMPL-PLANNER: build pool index ONCE for the whole plan (3054 files
    # → ~1818 score>=5 → bucketed into HIT/endurance pools). All weeks share it.
    pool_index = _build_pool_indexes(library)

    def _in_unavailable(d: date) -> bool:
        if not unavailable_periods:
            return False
        for lo, hi in unavailable_periods:
            if lo <= d <= hi:
                return True
        return False

    weeks = []
    week_num = 1
    global_week = 0  # global counter across all phases (not reset per phase)
    # v4.5.0: used_names is a dict (name -> last_used_week) so the sampler's
    # novelty score has full recency info. The legacy match_zwo path (used for
    # ftp_test fallback only) accepts a set, so we also keep a parallel set.
    used_names_dict: dict[str, int] = {}
    used_names_set: set = set()
    unmatched_count = 0
    prev_week_sessions: list | None = None  # for cross-week 48h HIT-gap (PL2)
    # v4.5.0 Layer 3: rolling 4-week window of HIT content_classes per phase.
    # Reset between phases so build1's threshold concentration doesn't suppress
    # build2's threshold picks. Most recent HIT picks live at the END of the list.
    recent_hit_by_phase: dict[str, list[str]] = {}
    # v4.5.0 acceptance: track unique (cc, dur_quintile) tuples already placed
    # to bias toward novel tuples (boosts ≥30 tuple acceptance §4).
    seen_cc_dur_tuples: set = set()
    # v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): plan-wide bookkeeping for
    # diversity cap + per-class-minimum bias.
    plan_pick_counts: dict[str, int] = {}
    class_session_counts: dict[str, int] = {}
    class_distinct_files: dict[str, set] = {}
    # FC1-CLIP (v2.5.0): span-derived week count == the number of week-rows the
    # loop below actually emits post-clip (sum(p.weeks) lied at the seam: 16
    # labeled vs 17 emitted at a 16w runway). Identical for non-event plans.
    plan_total_weeks = sum(_span_weeks(p) for p in phases) if phases else 0
    for phase in phases:
        # v1.6.1 — wrap each phase's per-week build so an exception inside
        # plan_week / sample_week_workouts / match_zwo surfaces as
        # E_PLAN_PHASE_BUILD_FAILED with phase-name + week-num context. We
        # re-raise so the caller (app.py /api/plan/generate) returns 500;
        # the diag ring carries the structured breadcrumb for triage.
        try:
            cursor = phase.start
            week_in_phase = 0  # 0-indexed within this phase (for Layer 2 mix-row pick)
            while cursor <= phase.end:
                global_week += 1
                is_stepback = (global_week % STEP_BACK_EVERY == 0) and phase.name not in ("taper",)

                # Run plan_week first so the legacy structural skeleton (rest days,
                # 48h-gap, ftp_test slots) is preserved. Then the sampler overwrites
                # non-rest slots with library-sampled workouts.
                pw = plan_week(week_num, cursor, phase, goal, is_stepback,
                               prev_week_sessions=prev_week_sessions,
                               seed_salt=seed_salt)

                # v4.6.0: rolling-eviction window 12 weeks (was 24) so files
                # re-enter the "fresh" novelty pool sooner in long plans.
                stale = [n for n, wk in used_names_dict.items()
                         if week_num - wk >= _USED_NAMES_ROLLING_WEEKS]
                for n in stale:
                    used_names_dict.pop(n, None)
                    used_names_set.discard(n)

                # v4.5.0 IMPL-PLANNER: sampler-driven workout selection per week.
                budget = get_budget_for_phase(phase.name)
                phase_rot = recent_hit_by_phase.setdefault(phase.name, [])
                # v1.11.0 (P4) — climbing specificity ONLY in build2/peak (research:
                # race-specific work belongs in build+peak, not base). None elsewhere.
                _emph = ("event_climb"
                         if (event_targets and event_targets.get("climbing_bias")
                             and phase.name in ("build2", "peak"))
                         else None)
                # F1 (v2.1/B2): block focus for this week (None unless opt-in).
                # FS1 (D4): a blueprint mode owns its own per-phase focus → no
                # block-periodization concentration on top.
                _plan_mode = getattr(goal, "plan_mode", "auto")
                block_focus = (None if _plan_mode in ("fixed_core", "template")
                               else _block_focus_for(phase.name, goal, is_stepback))
                pw.block_focus = block_focus
                if _plan_mode in ("fixed_core", "template"):
                    # FS1 — blueprint engine (deterministic repeatable week). Same
                    # 7-slot shape as the sampler; downstream passes are reused.
                    sampled = expand_blueprint_week(
                        phase=phase, budget=budget, week_num=week_num,
                        week_start=cursor,
                        available_days=goal.available_days,
                        rest_days=goal.rest_days,
                        daily_max_hours=goal.daily_max_hours,
                        max_weekday_hours=goal.max_weekday_hours,
                        max_weekend_hours=goal.max_weekend_hours,
                        is_stepback=is_stepback,
                        week_in_phase=week_in_phase, goal=goal,
                    )
                else:
                    sampled = sample_week_workouts(
                        phase=phase, budget=budget, library=library,
                        used_names=used_names_dict,
                        week_num=week_num, seed_salt=seed_salt,
                        week_start=cursor,
                        available_days=goal.available_days,
                        rest_days=goal.rest_days,
                        daily_max_hours=goal.daily_max_hours,
                        max_weekday_hours=goal.max_weekday_hours,
                        max_weekend_hours=goal.max_weekend_hours,
                        is_stepback=is_stepback,
                        pool_index=pool_index,
                        week_in_phase=week_in_phase,
                        recent_hit_types=phase_rot,
                        seen_cc_dur_tuples=seen_cc_dur_tuples,
                        plan_pick_counts=plan_pick_counts,
                        class_session_counts=class_session_counts,
                        class_distinct_files=class_distinct_files,
                        plan_total_weeks=plan_total_weeks,
                        goal_type=getattr(goal, "goal_type", "general"),
                        emphasis_profile=_emph,
                        block_focus=block_focus,
                    )
                # (v1.11.0 event long-ride progression is applied as a final pass
                #  at the END of generate_plan — after all duration/re-match passes.)
                # Trim rotation window to last 4 weeks worth of picks (≤3 HITs/wk
                # × 4 weeks = 12 entries max). Anything older has no penalty.
                if len(phase_rot) > 12:
                    del phase_rot[: len(phase_rot) - 12]
                # Mirror used_names_dict updates into the set for legacy callers.
                for nm in used_names_dict:
                    used_names_set.add(nm)

                # Replace pw.sessions with the sampled set, BUT preserve any
                # ftp_test slots from plan_week (the sampler doesn't pick those —
                # ftp_test workouts have an explicit tag and are excluded from the
                # sampler's pool).
                for off, legacy_s in enumerate(pw.sessions):
                    if getattr(legacy_s, "session_type", "") == "ftp_test":
                        continue
                    if 0 <= off < len(sampled) and sampled[off] is not None:
                        pw.sessions[off] = sampled[off]

                # Apply unavailable-period overrides + final fallback match_zwo for
                # any slot the sampler couldn't fill (rare — usually only ftp_test).
                for day_idx, s in enumerate(pw.sessions):
                    if _in_unavailable(s.day):
                        s.session_type = "rest"
                        s.duration_min = 0
                        s.tss_estimate = 0
                        s.description = "Rest (unavailable)"
                        s.zwo_file = ""
                        s.zwo_name = ""
                        continue
                    if s.session_type == "ftp_test":
                        continue
                    if s.session_type == "rest":
                        continue
                    # If the sampler already filled zwo_file, skip match_zwo.
                    if getattr(s, "zwo_file", ""):
                        continue
                    # Fallback path — match_zwo for unfilled slots.
                    before = len(used_names_set)
                    match_zwo(s, library, week_num=week_num, day_idx=day_idx,
                              used_names=used_names_set, plan_start_date=plan_start_date,
                              seed_salt=seed_salt)
                    if not getattr(s, "matched", True):
                        unmatched_count += 1
                    if len(used_names_set) > before:
                        for n in used_names_set - set(used_names_dict.keys()):
                            used_names_dict[n] = week_num

                # FC1-CLIP (v2.5.0): never spill past the phase end (D2 dup
                # days at the peak→taper seam, D3 training after race day).
                _clip_week_to_phase(pw, phase, cursor)
                weeks.append(pw)
                prev_week_sessions = pw.sessions  # feed into next plan_week for 48h gap
                cursor += timedelta(weeks=1)
                week_num += 1
                week_in_phase += 1
        except Exception as _e:
            _tp_log_error(error_codes.Codes.PLAN_PHASE_BUILD_FAILED, exc=_e,
                          phase=getattr(phase, "name", "?"),
                          week_num=week_num)
            raise

    # v1.0.0: inject mid-cycle FTP-test sessions to prevent stale-FTP overload.
    # Allen-Coggan TR&P 3rd ed. recommends re-testing every 4-6 weeks during
    # builds. If FTP rose 8% during build1 but the planner is still using the
    # old value, all subsequent TSS targets and zone boundaries are computed
    # against an FTP that's 8% too low — the rider trains harder than the
    # model thinks and accidentally overloads. v4.1.0 eFTP-drift auto-apply
    # is REACTIVE (waits for ICU to detect 7+ days of drift); a scheduled
    # mid-cycle test is PROACTIVE. Runs BEFORE the build2/peak floor passes
    # so the floors place anaerobic/neuromuscular/vo2_short into the *other*
    # slots of the same week, not the ftp_test slot.
    _inject_mid_cycle_ftp_tests(weeks, phases)

    # v4.6.1 PLANNER-VARIETY+RONNESTAD: hard floor for build2 and peak phases
    # — each must include ≥1 anaerobic AND ≥1 neuromuscular AND ≥2 vo2_short
    # workouts across the phase. Post-sampling check + swap if floor not met.
    # FS1: these diversification floors are the AUTO sampler's variety contract;
    # they INJECT extra HIT types and would break fixed_core/template's "one HIT
    # type per week" promise. Skip them for blueprint modes (the blueprint owns
    # the HIT structure). _enforce_weekly_hit_cap below still runs (a no-op at 1).
    if getattr(goal, "plan_mode", "auto") == "auto":
        _enforce_build2_peak_hard_floor(weeks, pool_index, plan_pick_counts,
                                        class_session_counts, class_distinct_files,
                                        used_names_dict, used_names_set)

        # v4.6.3 RONNESTAD-FIX: hard floor of ≥1 Rønnestad-tagged file per build1
        # / build2 / peak. Rønnestad spans multiple content_classes (vo2_short,
        # neuromuscular, threshold, recovery) so the per-class floor above can't
        # express the constraint — runs as a separate pass.
        _enforce_ronnestad_floor(weeks, pool_index, plan_pick_counts)

    # FIX-1b (safety): FINAL guaranteed weekly HIT cap. Runs AFTER all floors
    # (which target per-phase coverage and may overshoot a week's per-week
    # budget). Demotes any excess HIT to a real endurance/tempo library file
    # so no non-stepback week exceeds get_budget_for_phase(phase).hit_count_max.
    _enforce_weekly_hit_cap(weeks, library)

    # ── v1.3.2 IMPL-AVAILABILITY-IN-GENERATE ──────────────────────────────
    # Apply persisted per-DATE availability overrides AFTER bulk planning so
    # a fresh /api/plan/generate honors the same calendar that reforecast()
    # honors. Mirrors the reforecast() block at line ~4847: per-week scale
    # clamped [0.4, 2.0]; hours==0 → rest; hours>0 rescales duration and
    # re-matches the ZWO so the picked workout fits the new duration.
    if availability_overrides:
        for pw in weeks:
            week_keys = [
                s.day.isoformat() for s in pw.sessions
                if s.day.isoformat() in availability_overrides
            ]
            if not week_keys:
                continue
            available_mins = sum(
                int(float(availability_overrides[k]) * 60) for k in week_keys
            )
            current_mins = sum(
                s.duration_min for s in pw.sessions
                if s.day.isoformat() in availability_overrides
            )
            if current_mins <= 0:
                continue
            raw_scale = available_mins / current_mins
            scale = min(2.0, max(0.4, raw_scale))
            for day_idx, s in enumerate(pw.sessions):
                d_iso = s.day.isoformat()
                if d_iso not in availability_overrides:
                    continue
                hours = float(availability_overrides[d_iso])
                if hours <= 0:
                    s.session_type = "rest"
                    s.duration_min = 0
                    s.tss_estimate = 0
                    s.description = "Rest (unavailable)"
                    s.zwo_file = ""
                    s.zwo_name = ""
                else:
                    new_dur = max(0, int(round(s.duration_min * scale)))
                    s.duration_min = new_dur
                    tss_per_h = TSS_PER_HOUR.get(s.session_type, 45)
                    s.tss_estimate = round(new_dur / 60 * tss_per_h)
                    # Duration changed → existing zwo_file may not fit the new
                    # slot. Clear and re-match so the workout shape matches.
                    # v1.3.4 fix: include ftp_test in the re-match (was skipped
                    # pre-fix, so injected ftp_test slots kept zwo_file="" →
                    # yellow ⚠ in the dashboard). match_zwo's want_test branch
                    # filters to ftp_test-tagged ZWOs (Coggan-20 / Ramp).
                    if s.session_type != "rest":
                        s.zwo_file = ""
                        s.zwo_name = ""
                        match_zwo(s, library, week_num=pw.week_num,
                                  day_idx=day_idx, used_names=used_names_set,
                                  plan_start_date=plan_start_date,
                                  seed_salt=seed_salt)
                        # v1.3.4 fix: refresh description to match new duration.
                        # Pre-fix the tooltip read "z2 (70min) — sampled from
                        # library · 154m" because description kept the original
                        # library duration after rescale.
                        if s.session_type != "ftp_test":
                            s.description = (
                                f"{s.session_type} ({new_dur}min) — sampled from library"
                            )

    # v1.3.4 fix: final sweep — any non-rest session that still has
    # zwo_file="" gets a match_zwo call. The FTP-test injector (and a few
    # other paths) deliberately leave zwo_file="" for downstream resolution;
    # without this sweep those land on the dashboard as yellow ⚠.
    for pw in weeks:
        for day_idx, s in enumerate(pw.sessions):
            if s.session_type == "rest":
                continue
            if getattr(s, "zwo_file", "") or "":
                continue
            try:
                match_zwo(s, library, week_num=pw.week_num,
                          day_idx=day_idx, used_names=used_names_set,
                          plan_start_date=plan_start_date,
                          seed_salt=seed_salt)
            except Exception:  # noqa: BLE001
                log.debug("generate_plan final sweep match_zwo failed",
                          exc_info=True)

    # Audit: if match_zwo fell through to the empty-candidates path, surface it
    # once at the end rather than silently producing zwo_file="" sessions.
    if unmatched_count:
        log.warning(
            "generate_plan: %d session(s) had no ZWO library match — "
            "they will carry zwo_file='' and matched=False. "
            "Check WORKOUT_DIR=%s and duration tolerances.",
            unmatched_count, WORKOUT_DIR,
        )

    # v1.11.0 IMPL-EVENT (P1, the lever) — event long-ride progression. Applied
    # HERE: after the sampler + all utilization/re-match passes, and right BEFORE
    # the authoritative availability clamp below (which then validates it stays
    # within max_hours_for_day). Grows the weekend long ride toward 0.8× event
    # duration (+25 min/wk from current longest), capped at 5h + weekend hours,
    # ×0.72 on stepback, STOPPING ≥3 weeks out so the taper owns the long ride.
    if event_targets is not None:
        _mw_min = int(round((goal.max_weekend_hours or 0) * 60))
        for _wi, _w in enumerate(weeks):
            if getattr(_w, "phase", "") == "taper":
                continue
            _wstart = getattr(_w, "start", None)
            if _wstart and goal.target_date and (goal.target_date - _wstart).days <= 21:
                continue
            _lr_h = min(event_targets["long_target_h"],
                        event_targets["long_start_h"] + LONG_RIDE_STEP_MIN / 60.0 * _wi)
            _apply_long_ride_target(
                _w.sessions,
                target_min=int(round(_lr_h * 60)),
                max_weekend_min=_mw_min,
                is_stepback=getattr(_w, "is_stepback", False))

    # v2.1.0 (E1) — ENFORCE the load-based weekly volume ceiling. Until now the
    # plan's REAL weekly volume was one library workout per available day, each
    # clamped only to that day's availability — so generous availability gave a
    # ~24.5h week regardless of recent load. peak_weekly_tss (and thus each
    # week's tss_target) now carries the load-based ceiling, but nothing trimmed
    # the summed week down to it. This pass does: it shrinks the EASIEST
    # sessions first and converts the lowest-priority days to rest until the
    # week's summed planned TSS sits at its tss_target, never touching HIT
    # sessions and keeping ≥1 rest day + the polarized shape. Runs after the
    # event long-ride growth (so the long ride is preserved last) and right
    # before the authoritative per-day clamp.
    _enforce_weekly_volume_ceiling(weeks, recent_weekly_tss=recent_weekly_tss, goal=goal)

    # v2.1.0 (F4) — no hard session in the final days before the A event (event
    # goals only). Demotes a taper-eve VO2max/threshold block to an easy opener.
    if goal.goal_type in ("event", "ctl") and goal.target_date:
        _enforce_event_taper_eve(weeks, goal.target_date)
    # F3 — B/C mini-tapers apply on ANY goal that carries intermediate races, not
    # just event/ctl: a rider on an FTP / VO2max / general block can still target
    # a B/C race. Safe + a no-op without B/C events (the helper skips priority-A
    # and, when there's no A target_date, simply has no macro-taper span to dodge).
    _apply_secondary_event_tapers(weeks, goal)  # F7: B/C mini-tapers
    # F2b (v2.5.0): openers + final-days composition, AFTER the caps/eve-guard
    # (composes on top) and BEFORE the race-day marking (never touches it).
    if goal.goal_type in ("event", "ctl") and goal.target_date:
        _apply_race_week_shape(weeks, goal, library)
    _mark_race_days(weeks, goal)  # issue #7: race day shows the race, not a session

    # F4c (v2.5.0, D4): a race-week-only MICRO-PLAN (single taper phase — see
    # generate_phases) keeps at most ONE hard touch total, excluding the
    # openers ride and the race itself: there is no training block for a
    # second interval day to serve, and the rider must arrive fresh. The
    # per-week taper budget (1 HIT/week) still allows 2 across a 8-13d runway,
    # so demote every hard slot after the first to a short easy spin.
    if (len(phases) == 1 and phases[0].name == "taper"
            and goal.goal_type in ("event", "ctl") and goal.target_date):
        _hard_kept = False
        for _w in weeks:
            for _off, _s in enumerate(_w.sessions):
                if (not _session_is_hit(_s) or getattr(_s, "is_opener", False)
                        or _protect_race(_s)):
                    continue
                if not _hard_kept:
                    _hard_kept = True
                    continue
                _dur = min(_s.duration_min or 45, 45)
                _cand = PlannedSession(
                    day=_s.day, day_name=_s.day_name, session_type="z2",
                    duration_min=_dur,
                    tss_estimate=round(_dur / 60 * TSS_PER_HOUR["z2"]),
                    description="Easy spin — race week (one hard touch max).",
                )
                _m = match_zwo(_cand, library)
                _w.sessions[_off] = _m if (_m and getattr(_m, "zwo_file", "")) else _cand

    # v1.8.21 — AUTHORITATIVE per-day availability clamp. Session durations are
    # set from matched ZWO files at FOUR sites (sampler + 3 utilization/
    # re-match passes) plus match_zwo, several of which admit a file up to
    # ~25min over the slot for pool breadth. The rider only has the day's
    # available minutes, so the PLANNED duration must never exceed them. This
    # single final pass caps every non-rest session to its effective cap
    # (per-DATE availability override if the user set one, else the goal's
    # per-weekday max), scaling TSS proportionally. Reported bug: a 90-min
    # availability still produced 99–115min weekday sessions and 2.5h weekend
    # sessions. The matched ZWO may be longer and is paced on the trainer
    # (the modal's showGap banner explains the difference).
    _avail = availability_overrides or {}
    for w in weeks:
        for s in w.sessions:
            if s.session_type == "rest" or (s.duration_min or 0) <= 0:
                continue
            # FC3 (v2.5.0, D9/L3-9): the race entry is sized ONLY by
            # _mark_race_days (self-clamping to the day cap, E9) — a second
            # clamp here re-shrunk it against per-date overrides/TYPE_CEILING
            # on some paths but not others, flapping the race card.
            if _protect_race(s):
                continue
            d_iso = s.day.isoformat() if hasattr(s.day, "isoformat") else str(s.day)
            if d_iso in _avail:
                cap_min = int(float(_avail[d_iso]) * 60)
            else:
                wd = s.day.weekday() if hasattr(s.day, "weekday") else 0
                cap_min = int(goal.max_hours_for_day(wd) * 60)
            # FIX-2 (safety): final per-session-type duration ceiling. The
            # inline sampler clamp (~line 4232) only covers sampler-picked
            # slots — plan_week skeleton HITs, floor/Rønnestad swaps and the
            # availability rescale above can all leave a hard session at the
            # full day cap (the "VO2max every day at 120 min" bug). This last
            # pass guarantees min(day_cap, per-type ceiling) for every session
            # regardless of origin. Prefer the content_class ceiling (an
            # anaerobic/vo2_short file maps to session_type='vo2max' but has a
            # tighter cap); endurance types have no ceiling → day-cap only.
            _cc = _content_class_for_zwo(getattr(s, "zwo_file", "") or "")
            _ceil = TYPE_CEILING.get(_cc) or TYPE_CEILING.get(s.session_type)
            _eff = cap_min if _ceil is None else (
                _ceil if cap_min <= 0 else min(cap_min, _ceil))
            # B4: on a step-back week the long ride must stay short (≤2.5h). The
            # sampler/match can set a weekend endurance slot to the matched file's
            # full length (the prescription↔file decoupling), so a deload picked up
            # a 205-min "long ride". Endurance types have no TYPE_CEILING, so clamp
            # them here (this is the authoritative duration pass).
            if (getattr(w, "is_stepback", False)
                    and (_eff <= 0 or _eff > STEPBACK_LONG_RIDE_CAP_MIN)):
                _eff = STEPBACK_LONG_RIDE_CAP_MIN
            if _eff > 0 and s.duration_min > _eff:
                _scale = _eff / float(s.duration_min)
                s.tss_estimate = round((s.tss_estimate or 0) * _scale)
                s.duration_min = _eff

    # B5 — re-route any easy slot that the sampler/match left holding a too-hard
    # (interval-structured) file to a genuine easy file, and recompute its TSS.
    # Runs after the per-day clamp so the re-match targets the final duration.
    _enforce_easy_slot_content(weeks, library, plan_start_date, seed_salt)

    # FC2a (v2.5.0) FINAL taper budget pass: the first ceiling call ran before
    # the authoritative per-day clamp shrank the build weeks, so its taper
    # reference was measured against pre-clamp sums. Re-anchor the taper rows
    # on the FINAL build-week sums (taper wk1 ≤ 0.60×, race week ≤ 0.40× the
    # actual pre-taper max). taper_only → strict no-op for non-event plans.
    _enforce_weekly_volume_ceiling(weeks, recent_weekly_tss=recent_weekly_tss,
                                   goal=goal, taper_only=True)

    # B3 — guarantee each step-back week is the lightest in its block. Runs LAST,
    # after the per-day clamp above could have trimmed a build week below the
    # deload (e.g. a pre-taper peak week), which would invert the 3-up-1-down
    # rhythm. Only shrinks easy volume in the deload; never touches build weeks.
    _enforce_stepback_is_lightest(weeks)

    return phases, weeks


def _inject_mid_cycle_ftp_tests(weeks: list, phases: list) -> None:
    """v1.0.0 — schedule FTP test sessions at phase boundaries to recalibrate
    FTP mid-cycle, preventing systematic overload from stale FTP.

    Allen & Coggan *Training and Racing with a Power Meter* 3rd ed. recommends
    re-testing FTP every 4-6 weeks during build phases. If FTP rises 8% during
    build1 but the planner is still using the old value, all subsequent TSS
    targets and zone boundaries are computed against an FTP that's 8% too low.
    The rider trains harder than the model thinks and accidentally overloads.

    The v4.1.0 ``auto_apply_eftp`` path is REACTIVE — it waits for ICU's eFTP
    to drift >3% for 7+ consecutive days before bumping FTP. A scheduled test
    is PROACTIVE: a fresh Coggan-20 or Ramp test on day 1 of build2 captures
    the actual current FTP for the next 4-6 weeks of programming.

    Inject points:
      * **Start of build2** — always (covers cycles ≥ 8 weeks).
      * **Start of peak** — only when the cycle is ≥ 16 weeks total
        (long-cycle athletes accumulate enough adaptation to warrant
        a second mid-cycle calibration).

    The first non-rest, non-Z2/recovery slot of the target week is converted
    to ``session_type = "ftp_test"``. ``match_zwo`` finds a Coggan-20 or Ramp
    ZWO from the library on the next pass; the FIT-import detection at
    `app.py` then auto-suggests an FTP update via the existing modal.
    """
    if not weeks or not phases:
        return
    cycle_total_weeks = sum(getattr(p, "weeks", 0) for p in phases)
    test_phase_starts = []
    for ph in phases:
        if getattr(ph, "name", "") == "build2":
            test_phase_starts.append(ph.start)
        elif getattr(ph, "name", "") == "peak" and cycle_total_weeks >= 16:
            test_phase_starts.append(ph.start)
    if not test_phase_starts:
        return
    skip_types = {"rest", "z2", "long_z2", "recovery"}
    for week in weeks:
        if getattr(week, "is_stepback", False):
            continue
        if getattr(week, "start", None) not in test_phase_starts:
            continue
        for s in week.sessions:
            if s.session_type in skip_types:
                continue
            old_type = s.session_type
            s.session_type = "ftp_test"
            s.zwo_file = ""           # let match_zwo find a Coggan-20 / Ramp file
            s.zwo_name = ""
            s.matched = False
            s.duration_min = 60
            s.tss_estimate = 70.0
            s.description = (
                f"FTP TEST — Coggan-20 or Ramp protocol. "
                f"Mid-cycle recalibration (Allen-Coggan TR&P 3rd ed., "
                f"4-6 week re-test cadence) prevents stale-FTP overload. "
                f"Originally scheduled as {old_type}; the FTP-test detector "
                f"on the FIT-import path will suggest an FTP update."
            )
            break


# ── SAFETY: weekly HIT-count cap (planner FIX-1) ──────────────────────────────
# A session counts as HIT if EITHER its session_type is a hard type OR its
# content_class is a HIT content class. The two axes disagree on many files
# (anaerobic/neuromuscular files carry session_type='vo2max'; a sweetspot_long
# file with high Z2 can carry session_type='tempo' but content 'sweet_spot'),
# so the union is the only safe definition for the per-week cap.
_HIT_SESSION_TYPES = frozenset({
    "vo2max", "threshold", "overunder", "sweetspot", "sprint",
    "double_threshold",
})


def _session_is_hit(sess) -> bool:
    """True if this PlannedSession is a hard (HIT) session, by EITHER axis."""
    if sess is None or getattr(sess, "session_type", "") == "rest":
        return False
    if sess.session_type in _HIT_SESSION_TYPES:
        return True
    cc = _content_class_for_zwo(getattr(sess, "zwo_file", "") or "")
    return cc in _HIT_SLOT_CONTENT_CLASSES


def _week_hit_count(week) -> int:
    """Count HIT sessions in a PlannedWeek (union-of-axes definition)."""
    return sum(1 for s in week.sessions if _session_is_hit(s))


def _protect_race(s) -> bool:
    """FC3 (v2.5.0, D9/L3-2/L3-7/L3-10) — ONE invariant, one place: a session
    with ``is_race=True`` is IMMUTABLE to every mutating pass (volume clamps,
    stepback-lightest, availability rescale, tier-down, swap-type, redraw,
    rematch, auto-move, demote, refit, per-day clamp). Its duration/TSS come
    ONLY from ``_mark_race_days`` (self-clamping + idempotent, E9); the
    sanctioned write path for the race itself is the goal-level add/edit-race
    flow. Accepts both PlannedSession objects and persisted plan-dict session
    dicts so the app-layer writers can consult the same guard."""
    if s is None:
        return False
    if isinstance(s, dict):
        return bool(s.get("is_race", False))
    return bool(getattr(s, "is_race", False))


def _enforce_build2_peak_hard_floor(
    weeks: list,
    pool_index: dict,
    plan_pick_counts: dict[str, int],
    class_session_counts: dict[str, int],
    class_distinct_files: dict[str, set],
    used_names_dict: dict[str, int],
    used_names_set: set,
) -> None:
    """v4.6.1 PLANNER-VARIETY+RONNESTAD — hard floor on build2 + peak phases.

    Each of these phases must include at least 1 anaerobic workout, 1
    neuromuscular workout, and 2 vo2_short workouts across the phase. If
    sampling produced fewer, we swap a non-rest endurance/tempo slot in
    that phase for a candidate from the missing class. Stepback weeks are
    skipped (they're explicit unloading).
    """
    # v4.6.1: build2+peak each must have ≥1 anaerobic + ≥1 neuromuscular +
    # ≥2 vo2_short. We also enforce a softer build1 floor for vo2_short
    # (≥2) so the across-plan ≥10 vo2_short headline target is reachable.
    phase_floors = {
        # build1 is a 4-week phase; we ask for 4 vo2_short + 2 neuromuscular
        # so the across-plan target ≥10 vo2_short / ≥4 neuromuscular is reached.
        # v4.6.2 PLANNER-DIVERSITY-PUSH: also enforce 1 sweet_spot in build1
        # so the canonical {threshold, vo2max, sweet_spot, over_under} 4-shape
        # rotation is visible in every build phase regardless of seed (the
        # strong novelty boost can salt-bias sweet_spot to zero in build1+
        # build2 if it happened to fill base-phase slots first).
        # v2.0.3 F1: over_under sits at mix weight ~0.09 → E[picks]≈1 → rounds
        # to 0 in build, so it needs the SAME hard-floor as the other protected
        # interval classes. ≥1 in build1 AND build2 completes the 4-shape
        # rotation without crowding the other 3 hard types (each floor is
        # filled by swapping the lowest-stimulus steady slots, not the hards).
        "build1": {"vo2_short": 4, "neuromuscular": 2, "sweet_spot": 1, "over_under": 1},
        "build2": {"anaerobic": 1, "neuromuscular": 1, "vo2_short": 3, "over_under": 1},
        "peak":   {"anaerobic": 1, "neuromuscular": 1, "vo2_short": 3},
    }
    # F1 (v2.1/B5): when opt-in block periodization is on, REPLACE the flat
    # forced-4-shape floor with a BLOCK-AWARE floor — concentrate ~≥70% of each
    # phase-block's HIT on its focus class and RETAIN ≥1 complementary quality
    # (Issurin). Reuses the swap mechanism below (only the targets change). When
    # block is off (no week carries a block_focus) this is skipped entirely →
    # default-off parity, the flat floor is unchanged.
    _block_on = any(getattr(w, "block_focus", None) for w in weeks)
    if _block_on:
        _COMP = {"vo2max": "threshold", "threshold": "vo2_short"}
        block_floors: dict[str, dict[str, int]] = {}
        for _pn in ("build1", "build2", "peak"):
            _pw = [w for w in weeks if w.phase == _pn and not w.is_stepback]
            _focus = next((getattr(w, "block_focus", None) for w in _pw
                           if getattr(w, "block_focus", None)), None)
            if not _focus:
                continue
            _H = sum(_week_hit_count(w) for w in _pw)
            if _H <= 0:
                continue
            _fmin = max(1, (7 * _H + 9) // 10)  # ceil(0.70 * H)
            if _H >= 2:
                _fmin = min(_fmin, _H - 1)       # leave room for ≥1 complementary
                block_floors[_pn] = {_focus: _fmin, _COMP.get(_focus, "over_under"): 1}
            else:
                block_floors[_pn] = {_focus: _fmin}
        phase_floors = block_floors
    if not weeks:
        return
    by_class = pool_index.get("by_class") or {}
    # Track all files placed by hard-floor swaps across all phases. Each
    # phase's swap pass reads + writes this set so we don't pick the same
    # file in multiple phases (would shrink distinct-file count).
    all_swap_files: set[str] = set()
    for phase_name, mins in phase_floors.items():
        phase_weeks = [w for w in weeks if w.phase == phase_name and not w.is_stepback]
        if not phase_weeks:
            continue
        # Count current per-class picks
        counts: dict[str, int] = {cc: 0 for cc in mins}
        for w in phase_weeks:
            for s in w.sessions:
                if s.session_type == "rest":
                    continue
                cc = _content_class_for_zwo(s.zwo_file or "")
                if cc in counts:
                    counts[cc] += 1
        # Determine deficits
        # Track all cc targets so the per-cc swap loop avoids ovewriting
        # a slot that was JUST added by an earlier swap pass for a sibling
        # cc_target.
        all_targets = set(mins.keys())
        for cc_target, need in mins.items():
            deficit = need - counts.get(cc_target, 0)
            if deficit <= 0:
                continue
            # Source candidates: workouts whose CACHE primary is cc_target
            # (NOT the by_class filename-prefix bucketing — that bucket
            # mixes files whose name starts with the class prefix but whose
            # content is something else, e.g. anaerobic_3x25s_54min.zwo
            # is content-classified as neuromuscular). The post-pass count
            # check uses the cache primary too, so if we swap in a file
            # whose by_class bucket is anaerobic but whose cache primary
            # is neuromuscular, the deficit count would be wrong on
            # downstream verification.
            candidates = []
            cache_local = _load_content_classifications() or {}
            for w in by_class.get(cc_target) or []:
                if (w.get("Score", 0) or 0) < 4:
                    continue
                fl = (w.get("File") or "")
                ent = cache_local.get(fl) or cache_local.get(fl.split("/")[-1])
                primary = (ent.get("primary") if ent else "") or ""
                if primary.lower() != cc_target:
                    continue
                candidates.append(w)
            # Fallback: if cache-primary filter is empty (rare; cache stale
            # or class entirely unrepresented in cache), accept the by_class
            # bucket directly so we still attempt to fill the floor.
            if not candidates:
                candidates = [
                    w for w in (by_class.get(cc_target) or [])
                    if (w.get("Score", 0) or 0) >= 4
                ]
            if not candidates:
                continue
            # Sort by score desc, then by variety_score desc
            def _rank(w):
                s = float(w.get("Score", 0) or 0)
                try:
                    vs = variety_score(_features_for_row(w))
                except Exception:
                    vs = 1.0
                return (s, vs)
            candidates.sort(key=_rank, reverse=True)
            # Find swap targets — non-rest, non-already-target slots in this
            # phase. Prefer endurance/tempo slots (lowest stimulus) first.
            # Track files already used in this phase swap so we keep distinct
            # picks (otherwise diversity-ratio acceptance test regresses).
            swap_priority_types = ("z2", "long_z2", "tempo", "recovery", "sweetspot")
            used_in_swap: set[str] = set()
            # Compute file frequency across the whole plan once. Slots whose
            # current zwo_file is already a duplicate (appears 2+ times) are
            # preferred for swap so we don't displace a unique file.
            plan_file_freq: dict[str, int] = {}
            for ww in weeks:
                for ss in ww.sessions:
                    fl = ss.zwo_file or ""
                    if fl:
                        plan_file_freq[fl] = plan_file_freq.get(fl, 0) + 1
            # FIX-1a (safety SPREAD): fill the phase week with the FEWEST
            # current HIT first, and only swap a *steady* slot into HIT when
            # that week has room under its per-week budget.hit_count_max.
            # Without this the floor piles all required hards onto whichever
            # week it walked first, blowing past the weekly cap. Re-sort each
            # outer pass so a swap that bumped one week's count reshuffles the
            # order. Stepback weeks were already excluded from phase_weeks.
            _phase_budget = get_budget_for_phase(phase_name)
            for w_target in sorted(phase_weeks, key=_week_hit_count):
                if deficit <= 0:
                    break
                # Sort sessions in this week by swap priority. Skip slots that
                # already hold a file from THIS week's existing picks (we
                # re-check zwo_file against same-week siblings to avoid two
                # identical zwo files appearing on the same week).
                week_files = {s.zwo_file for s in w_target.sessions if s.zwo_file}
                # Exclude slots whose CURRENT cc is ANY phase target (so a
                # subsequent vo2_short swap doesn't clobber an anaerobic
                # slot we just placed for the same phase's anaerobic floor).
                sess_list = [
                    (i, s) for i, s in enumerate(w_target.sessions)
                    if s.session_type != "rest"
                    and not _protect_race(s)  # FC3: never swap over the race
                    and (_content_class_for_zwo(s.zwo_file or "") not in all_targets)
                ]
                # Sort: (1) prefer slots whose current file is a duplicate
                # in the plan (freq>=2), (2) then by swap_priority_types so
                # we still swap out boring steady picks first.
                def _swap_rank(kv):
                    _, ss = kv
                    fl = ss.zwo_file or ""
                    freq = plan_file_freq.get(fl, 0)
                    pri = (swap_priority_types.index(ss.session_type)
                           if ss.session_type in swap_priority_types else 99)
                    return (0 if freq >= 2 else 1, pri)
                sess_list.sort(key=_swap_rank)
                for i, s in sess_list:
                    if deficit <= 0:
                        break
                    # FIX-1a: swapping a *steady* slot into a hard adds NET HIT.
                    # Only do so when the week is under its hit_count_max. A
                    # swap onto an already-HIT slot (e.g. a duplicate non-floor
                    # vo2max/threshold slot) is net-neutral and always allowed —
                    # so SKIP only this steady slot (continue), never the whole
                    # week: a redundant-HIT slot later in the list can still
                    # take the required class without breaching the cap.
                    if (not _session_is_hit(s)
                            and _week_hit_count(w_target) >= _phase_budget.hit_count_max):
                        continue
                    # Pick first candidate that fits this slot's duration
                    slot_max = max(60, int(s.duration_min) + 35)
                    slot_min = 25
                    chosen = None
                    for cand in candidates:
                        nm = cand.get("Name", "")
                        fl = cand.get("File", "") or ""
                        if not nm:
                            continue
                        # Distinct-pick constraints: don't repeat files
                        # already swapped into this phase OR any prior
                        # phase's swap pass, and don't put a duplicate of
                        # an existing same-week file.
                        if fl in used_in_swap:
                            continue
                        if fl in all_swap_files:
                            continue
                        if fl in week_files:
                            continue
                        # Cap on plan-wide repeats (still allow re-picks if
                        # already in the plan once but limit further).
                        if plan_pick_counts.get(nm, 0) >= 2:
                            continue
                        dur_c = float(cand.get("Duration(min)", 0) or 0)
                        if not (slot_min <= dur_c <= slot_max):
                            continue
                        chosen = cand
                        break
                    if chosen is None:
                        continue
                    new_sess = _make_session_from_row(
                        chosen, s.day, s.day_name, w_target.phase
                    )
                    new_sess.nutrition_note = _nutrition_note(
                        w_target.phase, new_sess.session_type
                    )
                    w_target.sessions[i] = new_sess
                    nm = chosen.get("Name", "")
                    fl = chosen.get("File", "") or ""
                    if nm:
                        plan_pick_counts[nm] = plan_pick_counts.get(nm, 0) + 1
                        used_names_dict[nm] = w_target.week_num
                        used_names_set.add(nm)
                    if fl:
                        used_in_swap.add(fl)
                        all_swap_files.add(fl)
                        week_files.add(fl)
                    if cc_target:
                        class_session_counts[cc_target] = class_session_counts.get(cc_target, 0) + 1
                        if nm:
                            class_distinct_files.setdefault(cc_target, set()).add(nm)
                    deficit -= 1


def _enforce_weekly_hit_cap(weeks: list, library: list[dict]) -> None:
    """FIX-1b (safety) — FINAL guaranteed weekly HIT cap.

    After ALL floor passes (build2/peak hard floor, Rønnestad), walk every
    NON-stepback week and, if its HIT-typed session count exceeds
    ``get_budget_for_phase(week.phase).hit_count_max``, DEMOTE the excess by
    SWAPPING the offending slot for a real endurance/tempo LIBRARY workout
    (matched via ``match_zwo`` for z2 / tempo). We NEVER relabel a hard .zwo
    in place — that is the known mislabel bug (a vo2max file masquerading as
    a "tempo" session). The most-redundant HIT class is demoted first so the
    surviving sessions keep maximum variety.

    The floors deliberately don't respect the per-week cap on their own (they
    target per-PHASE coverage), and the sampler can also draw up to
    hit_count_max BEFORE a floor adds more — so this pass is the single place
    the weekly safety invariant is guaranteed regardless of seed / goal.
    """
    if not weeks or not library:
        return

    def _demote_slot(slot, new_type: str) -> "PlannedSession | None":
        """Match a REAL endurance/tempo library file for this slot. Returns the
        matched session only if its content is genuinely non-HIT (a tempo match
        can fall back to a sweet_spot file, which still counts as HIT) — else
        None so the caller can try a safer type or a clear empty marker."""
        demoted = PlannedSession(
            day=slot.day, day_name=slot.day_name,
            session_type=new_type,
            duration_min=slot.duration_min,
            tss_estimate=round(slot.duration_min / 60
                               * TSS_PER_HOUR.get(new_type, 45)),
            description=f"{new_type} ({slot.duration_min}min) — "
                        f"weekly-HIT-cap demotion",
        )
        matched = match_zwo(demoted, library)
        if matched.zwo_file and not _session_is_hit(matched):
            return matched
        return None

    for wk in weeks:
        if getattr(wk, "is_stepback", False):
            continue
        cap = get_budget_for_phase(wk.phase).hit_count_max
        # Recompute on each removal — demoting changes counts. The guard bounds
        # the loop to the number of sessions so a pathological library (no
        # non-HIT match for any slot) can't spin forever.
        for _ in range(len(wk.sessions) + 1):
            # F2b (v2.5.0): openers are whitelisted — a race-week opener is a
            # deliberate short touch session, neither counted nor demotable.
            # FC3: the race entry is never a demotion candidate either.
            hit_slots = [
                (i, s) for i, s in enumerate(wk.sessions)
                if _session_is_hit(s) and not getattr(s, "is_opener", False)
                and not _protect_race(s)
            ]
            if len(hit_slots) <= cap:
                break
            # Pick the most-redundant HIT class in this week (the class with
            # the most sessions); demote one of its slots. Tie-break by
            # preferring the longest-duration slot (highest fatigue) so we
            # shed the heaviest redundant dose first.
            class_counts: dict[str, int] = {}
            for _, s in hit_slots:
                cc = (_content_class_for_zwo(s.zwo_file or "")
                      or s.session_type or "")
                class_counts[cc] = class_counts.get(cc, 0) + 1
            redundant_cc = max(class_counts, key=lambda c: class_counts[c])
            demote_candidates = [
                (i, s) for i, s in hit_slots
                if (_content_class_for_zwo(s.zwo_file or "")
                    or s.session_type or "") == redundant_cc
            ]
            i, slot = max(demote_candidates, key=lambda kv: kv[1].duration_min)
            # Tempo keeps a touch of stimulus for longer slots; z2 is the safe
            # fallback (its match pool is endurance/recovery only — never HIT
            # content). Both paths are verified non-HIT by _demote_slot.
            matched = None
            if slot.duration_min >= 60:
                matched = _demote_slot(slot, "tempo")
            if matched is None:
                matched = _demote_slot(slot, "z2")
            if matched is not None:
                wk.sessions[i] = matched
            else:
                # No non-HIT endurance/tempo file fit → leave a clear marker
                # rather than relabeling a hard .zwo (the known mislabel bug).
                wk.sessions[i] = PlannedSession(
                    day=slot.day, day_name=slot.day_name, session_type="z2",
                    duration_min=slot.duration_min,
                    tss_estimate=round(slot.duration_min / 60 * TSS_PER_HOUR["z2"]),
                    description="Easy endurance (no suitable workout)",
                    zwo_file="", zwo_name="", matched=False,
                )


# v2.1.0 (E1) — weekly volume-ceiling enforcement.
# Easy (non-HIT) session types in SHRINK order — the type we trim/drop FIRST is
# listed first. recovery has the least training value, then mid-week z2/tempo;
# long_z2 (the weekend long ride / event-specificity lever) is preserved last.
_VOLUME_SHRINK_ORDER = ("recovery", "tempo", "z2", "long_z2")
# FC2a (v2.5.0, D5): in TAPER weeks the shrink order INVERTS — the long ride is
# the FIRST thing a taper sheds (Mujika 2003: drop volume 40-60%, keep
# intensity), the short recovery spins the last (already the lightest dose).
_VOLUME_SHRINK_ORDER_TAPER = ("long_z2", "z2", "tempo", "recovery")
# FC2a (v2.5.0, D5): descending taper weekly-TSS budgets as fractions of the
# ACTUAL pre-taper reference (max of the last ≤3 FULL non-stepback week sums,
# measured at trim time — NOT the model's peak_weekly_tss, which the emitted
# weeks routinely overshot by 24-40%). END-anchored: the race week always gets
# the last frac, the week before it the second-to-last. Single knob.
TAPER_FRACS = (0.60, 0.40)
# Don't shrink a session below this — a 25-min "endurance" slot is noise. Once a
# session would fall under the floor we convert the whole day to rest instead.
_VOLUME_MIN_SESSION_MIN = 30
# Only act when the week overshoots its ceiling by more than this fraction —
# avoids churning sessions for a handful of TSS the per-day clamp will absorb.
_VOLUME_CEILING_TOLERANCE = 1.05


def _enforce_weekly_volume_ceiling(weeks: list, recent_weekly_tss=None, goal=None,
                                   taper_only: bool = False) -> None:
    """v2.1.0 (E1) — cap each week's summed planned TSS at its load-based ceiling.

    The ceiling is the week's own ``tss_target`` (= the phase's
    ``weekly_tss_target``, which v2.1.0 derives from the rider's recent load via
    the ACWR bound in ``generate_phases`` — already ×0.72 for stepback weeks).
    Before this pass the plan placed one library workout per available day,
    clamped only to per-day availability, so a generous calendar produced a
    ~24.5h / ~1592-TSS week no matter how little the rider had recently been
    training. Here we bring the summed week down to the ceiling by, in order:

      1. SHRINKING the easiest sessions first (recovery → tempo → z2 → long_z2),
         proportionally, down to a ``_VOLUME_MIN_SESSION_MIN`` floor;
      2. then converting the lowest-value remaining easy day to REST when a
         session would fall under the floor.

    Invariants held: HIT (key intensity) sessions are never shrunk or dropped;
    the polarized distribution is preserved (only easy volume is shed); at least
    one rest day always remains (converting easy days to rest only adds rest);
    and the last non-rest day is never removed (we stop before emptying the
    week). FC2a (v2.5.0): taper weeks are ENFORCED here too — their ceiling is
    TAPER_FRACS × the actual pre-taper reference (descending into the race) and
    their shrink order is inverted (the long ride goes first). Bounded loop
    (≤ number of sessions per week).

    ``taper_only=True`` (FC2a final pass): only taper rows are processed —
    generate_plan calls this a second time AFTER the authoritative per-day
    clamp, so the taper budget anchors on the FINAL build-week sums (the first
    call ran mid-pipeline, before the clamp shrank the builds). A strict no-op
    for plans without a taper (non-event parity).
    """
    if not weeks:
        return
    # v2.1.1 — POLARIZED BASE FILL. For ANY training goal (event, ctl, ftp,
    # vo2max, ftp_vo2max, hybrid, general, endurance, weight), let the easy aerobic
    # volume fill available days up to the rider's ACWR-safe ceiling (recent × 1.3)
    # instead of the lower per-phase ramp target — so a build week is a polarized
    # HIT + Z2 mix, not "a few hard sessions + rest days". Every cycling goal wants
    # a Z2 aerobic base (polarized 80/20 raises FTP and VO2max too — Stöggl 2014;
    # Rønnestad's VO2 blocks sit on a big Z2 base). Bounded by Gabbett's ACWR so it
    # never spikes load; no-op without a known recent load (so the no-history
    # coverage tests are unaffected) and on stepback/taper weeks (deload preserved).
    _acwr_safe = (recent_weekly_tss * ACWR_CEILING) if (recent_weekly_tss and recent_weekly_tss > 0) else 0
    _base_fill_goal = goal is not None
    # FC2a (v2.5.0, D5/L1-D5): taper weeks are NO LONGER skipped — nothing else
    # consumed their tss_target, so the emitted taper was routinely the biggest
    # week of the plan (volume ramping UP into the race). Their trim ceiling is
    # TAPER_FRACS × the ACTUAL pre-taper reference; the reference is frozen at
    # the first taper row, AFTER the loop has already trimmed the build weeks
    # above ("at trim time").
    _taper_rows = [w for w in weeks if getattr(w, "phase", "") == "taper"]
    # identity-keyed (PlannedWeek is a dataclass — .index() would deep-compare)
    _taper_pos = {id(w): i for i, w in enumerate(_taper_rows)}
    _peak_ref: "float | None" = None
    for wk in weeks:
        _is_taper = getattr(wk, "phase", "") == "taper"
        if taper_only and not _is_taper:
            continue
        if _is_taper:
            if _peak_ref is None:
                _full_builds = [
                    w for w in weeks
                    if getattr(w, "phase", "") != "taper"
                    and not getattr(w, "is_stepback", False)
                    and ((w.end - w.start).days + 1) >= 7
                    and w.start < wk.start
                ]
                _peak_ref = max(
                    (sum((s.tss_estimate or 0) for s in b.sessions
                         if s and s.session_type != "rest")
                     for b in _full_builds[-3:]),
                    default=0.0,
                )
            _t_idx = _taper_pos[id(wk)]
            _fi = len(TAPER_FRACS) - (len(_taper_rows) - _t_idx)
            ceiling = _peak_ref * TAPER_FRACS[max(0, _fi)]
            if ceiling <= 0:
                continue  # no pre-taper reference (micro-plan) — nothing to anchor
        else:
            ceiling = getattr(wk, "tss_target", 0) or 0
            if ceiling <= 0:
                continue
            # Raise the trim ceiling to the ACWR-safe volume for endurance build/
            # base/peak weeks (only RAISES — a week already higher is untouched).
            # Stepback (deload) weeks keep their reduced target so unloading is
            # preserved; taper weeks take the TAPER_FRACS ceiling above.
            if (_acwr_safe > ceiling and _base_fill_goal
                    and not getattr(wk, "is_stepback", False)
                    and getattr(wk, "phase", "") in ("base", "build1", "build2", "peak")):
                ceiling = _acwr_safe
        budget = ceiling * _VOLUME_CEILING_TOLERANCE
        _shrink_order = _VOLUME_SHRINK_ORDER_TAPER if _is_taper else _VOLUME_SHRINK_ORDER

        def _easy_slots(_wk=wk, _order=_shrink_order):
            """(index, session) for non-HIT, non-rest, shrinkable slots, ordered
            easiest-first then longest-first (shed the heaviest easy dose first
            within a type). Taper weeks invert the type order (long ride first)."""
            out = []
            for idx, s in enumerate(_wk.sessions):
                if s is None or s.session_type == "rest":
                    continue
                if (s.duration_min or 0) <= 0:
                    continue
                if _session_is_hit(s):
                    continue
                # FC3 + §6.12 (v2.5.0): the race entry, user-pinned moves/swaps,
                # done/dismissed records and unavailable days are never "easy
                # volume to shed" (the regen path now runs this pass over weeks
                # that carry preserved rider state — L3-13). Structural no-op at
                # generate time (fresh sessions carry none of these flags).
                if _race_shape_frozen(s):
                    continue
                if getattr(s, "is_opener", False):
                    continue  # F2b: the race-week opener is deliberate
                out.append((idx, s))
            out.sort(key=lambda kv: (
                _order.index(kv[1].session_type)
                if kv[1].session_type in _order else 99,
                -(kv[1].duration_min or 0),
            ))
            return out

        # Bound the loop to the session count — each iteration either shrinks
        # one slot to absorb the whole overage (and breaks) or rests one slot.
        for _ in range(len(wk.sessions) + 1):
            # A marked race day carries the (immovable) race-load estimate —
            # it doesn't spend the week's TRAINING budget (else a big race
            # estimate would strip every other session in the week).
            total = sum(s.tss_estimate or 0 for s in wk.sessions
                        if s and s.session_type != "rest"
                        and not getattr(s, "is_race", False))
            if total <= budget:
                break
            overage = total - ceiling
            easy = _easy_slots()
            if not easy:
                # Only HIT (+ rest) left — never trim key intensity. The per-day
                # clamp still caps each session; the week stays HIT-heavy by
                # design (a polarized week with little easy volume).
                break
            # Keep at least one non-rest day overall.
            non_rest = sum(1 for s in wk.sessions
                           if s and s.session_type != "rest")
            idx, slot = easy[0]
            tss_per_min = ((slot.tss_estimate or 0) / slot.duration_min
                           if slot.duration_min else 0.7)
            # How much can this slot shed before hitting the floor?
            sheddable_min = slot.duration_min - _VOLUME_MIN_SESSION_MIN
            sheddable_tss = sheddable_min * tss_per_min if sheddable_min > 0 else 0
            if sheddable_tss >= overage and sheddable_min > 0:
                # This slot alone can absorb the remaining overage → shrink it.
                cut_min = int(round(overage / tss_per_min)) if tss_per_min else 0
                new_dur = max(_VOLUME_MIN_SESSION_MIN, slot.duration_min - cut_min)
                slot.tss_estimate = round(tss_per_min * new_dur)
                slot.duration_min = new_dur
                break
            # Slot can't absorb it all. If shrinking to the floor still leaves us
            # over, drop the whole day to rest (unless it's the last non-rest
            # day — then shrink to floor and stop touching it).
            if non_rest <= 1:
                if sheddable_min > 0:
                    slot.duration_min = _VOLUME_MIN_SESSION_MIN
                    slot.tss_estimate = round(tss_per_min * _VOLUME_MIN_SESSION_MIN)
                break
            slot.session_type = "rest"
            slot.duration_min = 0
            slot.tss_estimate = 0
            slot.description = "Rest — weekly volume ceiling (recent load)"
            slot.zwo_file = ""
            slot.zwo_name = ""


def _enforce_stepback_is_lightest(weeks: list) -> None:
    """B3 — a step-back (deload) week must be the lightest in its block.

    plan_week gives step-back weeks FIXED easy durations (recovery / Z2 /
    long_Z2) while build weeks scale with the load-based ceiling, so a light
    build week — typically a peak week just before the taper — can occasionally
    end up BELOW the deload, inverting the 3-up-1-down rhythm (tester saw
    W4>W3, W8>W6). After every other volume pass (incl. the authoritative
    per-day clamp), shrink each step-back week's easy aerobic sessions until its
    summed TSS sits clearly (~10%) below the lightest build week that precedes
    it in the same block. Only easy volume is touched — never HIT, never adds
    load; bounded loop (≤ sessions per week, stops once all easy slots hit the
    floor).
    """
    def _wk_tss(w):
        return sum((s.tss_estimate or 0) for s in w.sessions
                   if s and s.session_type != "rest")
    for i, wk in enumerate(weeks):
        if not getattr(wk, "is_stepback", False):
            continue
        if getattr(wk, "phase", "") == "taper":
            continue
        # Block = the consecutive preceding non-stepback, non-taper weeks (back
        # to the previous step-back or the plan start).
        builds = []
        j = i - 1
        while j >= 0 and not getattr(weeks[j], "is_stepback", False):
            # FC1-CLIP (v2.5.0): a clipped short row (<7 days, phase seam) is
            # not a real build week — its tiny TSS would drag the lightest-ref
            # down and force the deload toward zero. Full rows only.
            if (getattr(weeks[j], "phase", "") != "taper"
                    and ((weeks[j].end - weeks[j].start).days + 1) >= 7):
                builds.append(weeks[j])
            j -= 1
        if not builds:
            continue
        target = min(_wk_tss(b) for b in builds) * 0.90
        for _ in range(len(wk.sessions) + 1):
            if _wk_tss(wk) <= target:
                break
            easy = [(idx, s) for idx, s in enumerate(wk.sessions)
                    if s and s.session_type != "rest"
                    and (s.duration_min or 0) > _VOLUME_MIN_SESSION_MIN
                    and not _session_is_hit(s)
                    and not _protect_race(s)]  # FC3: race entry never shrunk
            if not easy:
                break  # every easy slot already at/under the floor
            easy.sort(key=lambda kv: -(kv[1].duration_min or 0))
            _, slot = easy[0]
            tss_per_min = ((slot.tss_estimate or 0) / slot.duration_min
                           if slot.duration_min else 0.7)
            overage = _wk_tss(wk) - target
            cut_min = int(round(overage / tss_per_min)) if tss_per_min else 0
            new_dur = max(_VOLUME_MIN_SESSION_MIN, slot.duration_min - max(1, cut_min))
            slot.duration_min = new_dur
            slot.tss_estimate = round(tss_per_min * new_dur)

        # issue #4 — a deload must be VISIBLY light: MORE rest days + fewer hours
        # than its build weeks, not just lower TSS via easy Z2 (riders saw a
        # "Recovery" week with MORE hours + the same single rest day as a build
        # week). Convert the shortest easy spins to rest until the deload has more
        # rest days than any build week in the block — but keep ≥1 easy spin (a
        # recovery week is light riding, not total rest).
        def _rest_count(w):
            return sum(1 for s in w.sessions if s and s.session_type == "rest")
        build_max_rest = max((_rest_count(b) for b in builds), default=0)
        for _ in range(len(wk.sessions)):
            if _rest_count(wk) > build_max_rest:
                break
            easy = [(idx, s) for idx, s in enumerate(wk.sessions)
                    if s and s.session_type != "rest" and not _session_is_hit(s)
                    and not _protect_race(s)]  # FC3: race entry never rested
            if len(easy) <= 1:
                break  # keep at least one easy recovery spin
            easy.sort(key=lambda kv: (kv[1].duration_min or 0))  # drop the shortest first
            _, slot = easy[0]
            slot.session_type = "rest"
            slot.duration_min = 0
            slot.tss_estimate = 0
            slot.description = "Rest — recovery week"
            slot.zwo_file = ""
            slot.zwo_name = ""


def _enforce_easy_slot_content(weeks: list, library: list, plan_start_date,
                               seed_salt: int = 0) -> None:
    """B5 — an easy slot must carry an EASY file.

    The sampler is the primary picker and buckets by content_class, so an
    interval-structured file the classifier filed as "endurance" (high IF) — or
    a sweet-spot/over-under file — can land on a z2/long_z2/recovery slot (the
    prescription↔file decoupling: the tester saw a Z2 60-min slot matched to a
    196-TSS interval file). match_zwo now enforces _EASY_SLOT_IF_CEILING, so we
    re-match any easy slot whose matched file is too hard and recompute the
    slot's TSS from its (easy) type + final duration. Runs LAST, after the
    per-day clamp set final durations. Bounded (one pass over the slots).
    """
    if not library:
        return
    if_by_file = {}
    for w in library:
        f = (w.get("File") or "").strip()
        if f:
            if_by_file[f] = float(w.get("IF") or 0)
    easy_tss_hr = {"z2": TSS_PER_HOUR["z2"], "long_z2": TSS_PER_HOUR["z2"],
                   "recovery": TSS_PER_HOUR["recovery"]}
    for wk in weeks:
        for s in wk.sessions:
            if s is None or s.session_type not in easy_tss_hr:
                continue
            # F2b (v2.5.0): an opener deliberately carries a short-surge file on
            # a z2-typed slot — that's its whole point; don't re-match it away.
            # FC3: the race entry (typed "recovery") is not a slot to re-match.
            if getattr(s, "is_opener", False) or _protect_race(s):
                continue
            f = (getattr(s, "zwo_file", "") or "").strip()
            if not f or if_by_file.get(f, 0.0) <= _EASY_SLOT_IF_CEILING:
                continue
            # Too-hard file on an easy slot — re-match to the closest easy file.
            s.zwo_file = ""
            s.zwo_name = ""
            try:
                match_zwo(s, library, week_num=getattr(wk, "week_num", 0),
                          plan_start_date=plan_start_date, seed_salt=seed_salt,
                          exact_duration=True)
            except Exception:  # noqa: BLE001
                log.debug("B5 easy-slot re-match failed", exc_info=True)
            # Recompute the slot's TSS from its easy type + final duration so the
            # detail modal's TSS reflects the SLOT, not a leftover file value.
            s.tss_estimate = round((s.duration_min or 0) / 60 * easy_tss_hr[s.session_type])


def _demote_hit_window(weeks: list, center_date, days: int, library=None,
                       desc: "str | None" = None) -> None:
    """F7 (v2.1) — demote any HIT session within ``days`` before ``center_date``
    (inclusive of the day itself) to a short easy Z2 opener. The shared primitive
    behind the A event eve-guard (F4) and the B/C mini-tapers (F7): trim the hard
    work in the window, leave everything else (intensity preserved elsewhere, per
    the taper literature). No-op when center_date is None."""
    if not center_date:
        return
    lib = library if library is not None else load_workout_library()
    OPENER_MAX_MIN = 45
    note = desc or ("Opener — easy spin; no hard session in the final days before "
                    "the event (arrive fresh).")
    for w in weeks:
        for off, s in enumerate(w.sessions):
            d = getattr(s, "day", None)
            if d is None or s.session_type == "rest":
                continue
            delta = (center_date - d).days
            # F2b (v2.5.0): a deliberate opener is exactly what belongs in this
            # window — never demote it (it's short, sharp by design).
            if getattr(s, "is_opener", False):
                continue
            # F5b (v2.5.0, L3-4): demote only PENDING, UNPINNED prescriptions.
            # This pass used to rewrite DONE rides (erasing training history),
            # resurrect DISMISSED sessions as pending z2, replace user-moved
            # pins, and flip MISSED records to pending (falsifying the
            # missed-hard refit latch key). FC3: the race entry is immutable.
            if _protect_race(s):
                continue
            if (getattr(s, "status", "pending") != "pending"
                    or getattr(s, "dismissed_at", "")
                    or getattr(s, "user_moved", False)):
                continue
            if 0 <= delta <= days and _session_is_hit(s):
                dur = min(s.duration_min or OPENER_MAX_MIN, OPENER_MAX_MIN)
                cand = PlannedSession(
                    day=s.day, day_name=s.day_name, session_type="z2",
                    duration_min=dur,
                    tss_estimate=round(dur / 60 * TSS_PER_HOUR["z2"]),
                    description=note,
                )
                m = match_zwo(cand, lib)
                w.sessions[off] = m if (m and getattr(m, "zwo_file", "")) else cand


def _enforce_event_taper_eve(weeks: list, target_date, library=None,
                             eve_days: int = EVENT_EVE_EASY_DAYS) -> None:
    """v2.1.0 (F4) — no HARD session in the final ``eve_days`` before the A event.
    A taper keeps some intensity (Mujika) but a VO2max/threshold BLOCK on the event
    eve leaves the legs flat. Thin wrapper over the shared ``_demote_hit_window``."""
    _demote_hit_window(weeks, target_date, eve_days, library)


# ── F2b (v2.5.0) — race-week openers + final-days composition ────────────────
# T-1 = a short openers ride (~40-50min with 2-3×~1min race-pace touches),
# T-2 = rest or a ≤45min z1 spin, and no ≥90min ride anywhere in T-1..T-3
# (only HIT was guarded near the race; DURATION never was — the weekend sampler
# slot freely landed a 170-min long_z2 on race eve, L1-D6).
_OPENER_DURATION_MIN = 45      # target opener length (40-50min window)
_OPENER_CLASSES = ("vo2_short", "neuromuscular", "anaerobic")
_RACE_T2_MAX_MIN = 45          # T-2: rest or ≤45min z1
_RACE_FINAL3_MAX_MIN = 75      # cap for any ride inside T-1..T-3 (<90 invariant)
_RACE_WEEK_MIN_REST = 3        # FC2a: ≥3 rest days in the race week


def _race_shape_frozen(s) -> bool:
    """True when the race-week shaper must not touch this slot: the race entry
    itself, anything the rider moved/edited/completed/dismissed, and days the
    availability calendar marked unavailable."""
    if s is None or getattr(s, "is_race", False):
        return True
    if getattr(s, "user_moved", False) or getattr(s, "user_swapped", False):
        return True
    if getattr(s, "status", "pending") != "pending" or getattr(s, "dismissed_at", ""):
        return True
    return "unavailable" in (getattr(s, "description", "") or "").lower()


def _make_opener_session(day, day_name, library) -> "PlannedSession":
    """Build the T-1 / B-1 opener: ~40-50min easy spin carrying a short-touch
    file (2-3×~1min z4/z5). Typed z2 (aggregate load IS easy — the touches are
    seconds-to-a-minute with full recovery); is_opener=True is the whitelist
    flag the eve-guard / HIT-cap / easy-slot passes honor. Deterministic pick:
    shortest distance to 45min among short vo2/neuromuscular/anaerobic files."""
    best = None
    for row in (library or []):
        if (row.get("ContentClass") or "") not in _OPENER_CLASSES:
            continue
        dur = float(row.get("Duration(min)") or 0)
        if not (35 <= dur <= 50):  # the ~40-50min opener window (≤50 hard)
            continue
        key = (abs(dur - _OPENER_DURATION_MIN), row.get("File") or "")
        if best is None or key < best[0]:
            best = (key, row)
    dur = int(round(float(best[1].get("Duration(min)")))) if best else _OPENER_DURATION_MIN
    tss = round(float(best[1].get("TSS") or 0)) if best else round(
        dur / 60 * TSS_PER_HOUR["z2"])
    return PlannedSession(
        day=day, day_name=day_name, session_type="z2",
        duration_min=dur, tss_estimate=tss,
        description="Openers — 40-50min easy spin with 2-3×~1min race-pace "
                    "touches. Sharp legs, no fatigue.",
        zwo_file=(best[1].get("File") or "") if best else "",
        zwo_name=(best[1].get("Name") or "") if best else "",
        matched=bool(best),
        is_opener=True,
    )


def _place_opener(weeks: list, d, library) -> None:
    """Install an opener on date ``d`` (idempotent: an existing opener or a
    frozen slot is left alone)."""
    for w in weeks:
        for off, s in enumerate(w.sessions):
            if getattr(s, "day", None) != d:
                continue
            if getattr(s, "is_opener", False) or _race_shape_frozen(s):
                return
            w.sessions[off] = _make_opener_session(d, s.day_name, library)
            return


def _apply_race_week_shape(weeks: list, goal, library=None) -> None:
    """F2b + FC2a-rest (v2.5.0) — final-days composition before the A event.

    Runs AFTER the volume ceiling + eve guard (composes on top of the caps) and
    BEFORE _mark_race_days (the race day itself is never touched here):
      * T-3..T-1: no ride ≥90min (cap at _RACE_FINAL3_MAX_MIN, TSS scaled);
      * T-2: rest stays rest; a session becomes a ≤45min z1 spin;
      * T-1: the openers ride (is_opener=True — whitelisted downstream);
      * race week: ≥3 rest days (excess easy days convert, shortest first).
    Frozen slots (§6.12: user_moved / non-pending / dismissed / unavailable)
    are never rewritten."""
    target = getattr(goal, "target_date", None)
    if not target:
        return
    lib = library if library is not None else load_workout_library()
    by_day = {}
    for w in weeks:
        for off, s in enumerate(w.sessions):
            if getattr(s, "day", None) is not None:
                by_day[s.day] = (w, off, s)

    # T-3..T-1 duration cap (do this BEFORE the T-1/T-2 rewrites so the cap
    # never has to touch what we just installed).
    for back in (3, 2, 1):
        ent = by_day.get(target - timedelta(days=back))
        if not ent:
            continue
        _, _, s = ent
        if (_race_shape_frozen(s) or s.session_type == "rest"
                or getattr(s, "is_opener", False)):
            continue
        if (s.duration_min or 0) > _RACE_FINAL3_MAX_MIN:
            scale = _RACE_FINAL3_MAX_MIN / float(s.duration_min)
            s.tss_estimate = round((s.tss_estimate or 0) * scale)
            s.duration_min = _RACE_FINAL3_MAX_MIN
            s.description = (f"{s.session_type} ({s.duration_min}min) — "
                             "shortened: race in {}d".format(back))

    # T-2: rest or ≤45min z1.
    ent = by_day.get(target - timedelta(days=2))
    if ent:
        _, _, s = ent
        if not _race_shape_frozen(s) and s.session_type != "rest" \
                and not getattr(s, "is_opener", False):
            dur = min(s.duration_min or _RACE_T2_MAX_MIN, _RACE_T2_MAX_MIN)
            s.session_type = "recovery"
            s.duration_min = dur
            s.tss_estimate = round(dur / 60 * TSS_PER_HOUR["recovery"])
            s.description = "Easy spin (Z1) — two days out; legs up."
            s.zwo_file = ""
            s.zwo_name = ""
            try:
                match_zwo(s, lib)
            except Exception:  # noqa: BLE001
                log.debug("race-week T-2 re-match failed", exc_info=True)

    # T-1: the openers ride.
    _place_opener(weeks, target - timedelta(days=1), lib)

    # FC2a: ≥3 rest days in the race week (composes after the volume ceiling).
    race_wk = next((w for w in weeks if w.start <= target <= w.end), None)
    if race_wk is not None:
        def _rest_count():
            # The target-day slot doesn't count: _mark_race_days replaces it
            # with the race right after this pass, whatever it holds now.
            return sum(1 for s in race_wk.sessions
                       if s and s.session_type == "rest" and s.day != target)
        for _ in range(len(race_wk.sessions)):
            if _rest_count() >= _RACE_WEEK_MIN_REST:
                break
            candidates = [
                s for s in race_wk.sessions
                if s and s.session_type != "rest"
                and not getattr(s, "is_opener", False)
                and s.day != target
                and not _race_shape_frozen(s)
                and not _session_is_hit(s)
            ]
            if not candidates:
                break
            slot = min(candidates, key=lambda s: (s.duration_min or 0))
            slot.session_type = "rest"
            slot.duration_min = 0
            slot.tss_estimate = 0
            slot.description = "Rest — race week"
            slot.zwo_file = ""
            slot.zwo_name = ""


# F7 (v2.1): per-priority mini-taper window (IP_F7_research.md — short, intensity
# preserved). B = 2 easy days before the race, C = 1. The A event is owned by the
# macro taper + _enforce_event_taper_eve, never here.
_EVENT_TAPER_DAYS = {"B": 3, "C": 1}


def _apply_secondary_event_tapers(weeks: list, goal, library=None) -> None:
    """F7 (v2.1) — proportionate mini-taper before each B/C event in goal.events.
    Composition guards (no double-deload): SKIP an event inside the A macro-taper
    span (already deloading) and apply only the eve opener (not the multi-day dip)
    when the event lands on a step-back week (already unloaded). Reuses
    ``_demote_hit_window`` — trim volume, keep intensity. No-op without B/C events."""
    events = getattr(goal, "events", None) or []
    if not events:
        return
    lib = library if library is not None else load_workout_library()
    a_date = getattr(goal, "target_date", None)
    taper_start = (a_date - timedelta(days=TAPER_DAYS)) if a_date else None
    for ev in events:
        prio = getattr(ev, "priority", "B")
        ed = getattr(ev, "date", None)
        if prio == "A" or not ed:
            continue
        if taper_start and ed >= taper_start:
            continue  # inside the A macro-taper span — already deloading
        wk = next((w for w in weeks if w.start <= ed <= w.end), None)
        if wk is not None and getattr(wk, "is_stepback", False):
            _demote_hit_window(weeks, ed, 1, lib,
                               desc=f"Opener before your {prio} race (easy spin).")
        else:
            _demote_hit_window(weeks, ed, _EVENT_TAPER_DAYS.get(prio, 1), lib,
                               desc=f"Mini-taper for your {prio} race — easy; keeps you fresh.")
        # SM3 (v2.5.0): a B race gets the F2b opener shape on its eve and easy
        # riding on B+1..B+2 (the plan used to resume tempo/vo2max the very
        # next day). C races stay train-through (eve opener only, above).
        if (prio or "B").upper() == "B":
            _place_opener(weeks, ed - timedelta(days=1), lib)
            _demote_hit_window(weeks, ed + timedelta(days=2), 1, lib,
                               desc="Easy — recover after your B race.")


def _estimate_race_load(km: float, climb_m: float, event_type: str) -> tuple[int, int]:
    """Rough duration (min) + TSS for a race day, so the calendar shows a sensible
    number instead of 0. Flat-equivalent km / a type-typical speed → hours; TSS =
    hours · IF² · 100 with the event-type IF ceiling. Estimate only — the real
    load is recorded from the actual ride after the race."""
    km = float(km or 0)
    climb_m = float(climb_m or 0)
    if km <= 0:
        return 0, 0
    flat_eq = km + (climb_m / 100.0) * CLIMB_TO_FLAT_KM_PER_100M
    speed = {"crit": 38.0, "granfondo": 27.0, "sportive": 27.0, "century": 26.0,
             "ultra": 22.0}.get((event_type or "granfondo").lower(), 26.0)
    hours = flat_eq / speed
    iff = EVENT_TYPE_IF.get((event_type or "granfondo").lower(), 0.74)
    return int(round(hours * 60)), int(round(hours * iff * iff * 100))


def _mark_race_days(weeks: list, goal) -> None:
    """v2.2.14 (issue #7) — replace the training slot on every race date with the
    RACE itself, so the calendar/This-Week/today show the race (not a stray 6h Z2)
    and the rider can see it. Covers the A target event AND every B/C event in
    goal.events — one pass, all add-race scenarios. Runs AFTER the taper passes so
    it overrides whatever slot the taper left on the day. No-op when there are no
    race dates in the plan window."""
    races: dict = {}  # date -> meta
    # A event — only when this is genuinely a race goal (has a date + a distance).
    a_date = getattr(goal, "target_date", None)
    a_km = float(getattr(goal, "event_km", 0) or 0)
    if a_date and (getattr(goal, "goal_type", "") == "event" or a_km > 0):
        races[a_date] = {
            "name": getattr(goal, "event_name", "") or "Race",
            "km": a_km,
            "climb_m": float(getattr(goal, "event_climb_m", 0) or 0),
            "type": getattr(goal, "event_type", "granfondo") or "granfondo",
            "priority": "A",
        }
    # B/C events (TargetEvent objects or plain dicts — handle both).
    for ev in (getattr(goal, "events", None) or []):
        def _g(key, default=None):
            return getattr(ev, key, None) if not isinstance(ev, dict) else ev.get(key, default)
        ed = _g("date")
        if isinstance(ed, str):
            try:
                ed = date.fromisoformat(ed[:10])
            except ValueError:
                ed = None
        prio = (_g("priority", "B") or "B").upper()
        if not ed or prio == "A":
            continue
        races.setdefault(ed, {
            "name": _g("name", "") or f"{prio} race",
            "km": float(_g("event_km", 0) or _g("km", 0) or 0),
            "climb_m": float(_g("event_climb_m", 0) or _g("event_climb", 0) or 0),
            "type": _g("event_type", "granfondo") or _g("type", "granfondo") or "granfondo",
            "priority": prio,
        })
    if not races:
        return
    for wk in weeks:
        for s in wk.sessions:
            meta = races.get(s.day)
            if not meta:
                continue
            dur, tss = _estimate_race_load(meta["km"], meta["climb_m"], meta["type"])
            # Carry the race's own estimated duration/TSS in the meta so the UI
            # shows the true race length (~9h for a 175km gran fondo) even though
            # the day's session_type/duration gets clamped by the per-day hour cap.
            meta = {**meta, "est_duration_min": dur, "est_tss": tss}
            # E9 (v2.5.0) IDEMPOTENT: already marked with the SAME race meta →
            # leave the slot (duration/TSS included) untouched, so repeated
            # passes across generate/reforecast/regen never churn the race card.
            if getattr(s, "is_race", False) and (getattr(s, "race", None) or {}) == meta:
                continue
            s.is_race = True
            s.race = meta
            s.session_type = "recovery"  # keep a known type; UI keys on is_race
            s.zwo_file = ""
            s.zwo_name = ""
            s.matched = True
            km_txt = f" — {int(meta['km'])}km" if meta["km"] else ""
            climb_txt = f" / {int(meta['climb_m'])}m" if meta["climb_m"] else ""
            s.description = f"🏁 {meta['priority']} RACE: {meta['name']}{km_txt}{climb_txt}"
            if dur:
                # E9 (v2.5.0) SELF-CLAMPING: apply the rider's per-day hour cap
                # HERE (v2.2.14 clamped-by-design), not via whichever downstream
                # pass happens to run on this path — reforecast/regen/refit had
                # no per-day clamp, so the slot flapped between the full race
                # estimate and the clamped value on alternate passes (L3-9).
                cap_min = 0
                if goal is not None and hasattr(s.day, "weekday"):
                    try:
                        cap_min = int((goal.max_hours_for_day(s.day.weekday()) or 0) * 60)
                    except Exception:  # noqa: BLE001
                        cap_min = 0
                if 0 < cap_min < dur:
                    s.duration_min = cap_min
                    s.tss_estimate = round(tss * cap_min / dur)
                else:
                    s.duration_min = dur
                    s.tss_estimate = tss


def _enforce_ronnestad_floor(
    weeks: list,
    pool_index: dict,
    plan_pick_counts: dict[str, int],
) -> None:
    """v4.6.3 RONNESTAD-FIX — hard floor of ≥1 Rønnestad-tagged file per
    build1 / build2 / peak phase.

    Rønnestad et al. 2015 (Scand J Med Sci Sports 25:143-151) showed
    short on-off VO2max microintervals (30/15, 40/20) deliver more
    cumulative time-at-VO2 than 4-5min intervals. The user explicitly
    flagged Rønnestad as "one of the most effective" for VO2max + FTP
    development, and these MUST land in build/peak phases.

    Rønnestad spans multiple content_classes (vo2_short, neuromuscular,
    threshold, recovery) so per-class floors can't express the
    constraint — separate pass. Swap target: any non-rest, non-Rønnestad
    HIT slot in the deficit phase, preferring already-duplicated files
    so distinct-file count holds.
    """
    cache = _load_content_classifications() or {}
    target_phases = ("build1", "build2", "peak")
    by_class = pool_index.get("by_class") or {}

    def _is_ronn_file(zwo_file: str) -> bool:
        if not zwo_file:
            return False
        ent = cache.get(zwo_file) or cache.get(zwo_file.split("/")[-1])
        if not ent:
            return False
        return "is_ronnestad" in (ent.get("tags") or [])

    # Build the candidate Rønnestad pool: every score≥4 file across the
    # by_class buckets that's tagged is_ronnestad in the cache. Sorted
    # by score desc so highest-quality lands first.
    ronn_candidates: list[dict] = []
    seen_files: set[str] = set()
    for cc_bucket, rows in by_class.items():
        for w in rows:
            fl = (w.get("File") or "")
            if not fl or fl in seen_files:
                continue
            if not _is_ronn_file(fl):
                continue
            if (w.get("Score", 0) or 0) < 4:
                continue
            seen_files.add(fl)
            ronn_candidates.append(w)
    if not ronn_candidates:
        return
    ronn_candidates.sort(key=lambda w: float(w.get("Score", 0) or 0), reverse=True)

    # Per-plan duplicate frequency to prefer swap targets that are
    # already-duplicated (so the swap doesn't shrink distinct-file count).
    plan_file_freq: dict[str, int] = {}
    for w in weeks:
        for s in w.sessions:
            fl = s.zwo_file or ""
            if fl:
                plan_file_freq[fl] = plan_file_freq.get(fl, 0) + 1

    def _ronn_class(w: dict) -> str:
        fl = w.get("File") or ""
        ent = cache.get(fl) or cache.get(fl.split("/")[-1])
        return ((ent.get("primary") if ent else "") or "").lower()

    # Group candidates by content_class so we can pick one that matches the
    # slot's content_class — swapping a vo2_short slot for a threshold-class
    # Rønnestad would drop the vo2_short count below its floor.
    ronn_by_class: dict[str, list[dict]] = {}
    for c in ronn_candidates:
        ronn_by_class.setdefault(_ronn_class(c), []).append(c)

    placed_files: set[str] = set()
    for phase_name in target_phases:
        phase_weeks = [w for w in weeks if w.phase == phase_name and not w.is_stepback]
        if not phase_weeks:
            continue
        ronn_in_phase = sum(
            1 for w in phase_weeks for s in w.sessions
            if _is_ronn_file(s.zwo_file or "")
        )
        if ronn_in_phase >= 1:
            continue
        # Try to swap a slot for a SAME-class Rønnestad. Walk slots; for each,
        # see if a Rønnestad of the same content_class as the slot's current
        # file is available. Prefer slots whose current file is duplicated
        # elsewhere (so distinct-file count is preserved).
        hit_types = ("vo2_short", "vo2max", "threshold", "sweetspot",
                     "over_under", "anaerobic", "neuromuscular")

        def _try_swap(prefer_duplicates: bool) -> bool:
            for w in phase_weeks:
                for s in w.sessions:
                    if s.session_type not in hit_types:
                        continue
                    if _protect_race(s):  # FC3: never swap the race entry
                        continue
                    cur_file = s.zwo_file or ""
                    if not cur_file or _is_ronn_file(cur_file):
                        continue
                    if prefer_duplicates and plan_file_freq.get(cur_file, 0) < 2:
                        continue
                    # Resolve the slot's effective class (cache primary if available,
                    # else fall back to session_type).
                    cur_ent = cache.get(cur_file) or cache.get(cur_file.split("/")[-1])
                    cur_cc = ((cur_ent.get("primary") if cur_ent else "") or s.session_type).lower()
                    pool = ronn_by_class.get(cur_cc, [])
                    cand = next(
                        (c for c in pool
                         if (c.get("File") or "") not in placed_files
                         and plan_pick_counts.get(c.get("Name", ""), 0) == 0),
                        None,
                    )
                    if cand is None:
                        cand = next(
                            (c for c in pool if (c.get("File") or "") not in placed_files),
                            None,
                        )
                    if cand is None:
                        continue
                    new_file = cand.get("File") or ""
                    new_name = cand.get("Name") or ""
                    new_dur = float(cand.get("Duration(min)", 0) or 0)
                    s.zwo_file = new_file
                    s.zwo_name = new_name
                    if new_dur > 0:
                        s.duration_min = int(round(new_dur))
                    placed_files.add(new_file)
                    plan_file_freq[cur_file] = max(0, plan_file_freq.get(cur_file, 0) - 1)
                    plan_file_freq[new_file] = plan_file_freq.get(new_file, 0) + 1
                    plan_pick_counts[new_name] = plan_pick_counts.get(new_name, 0) + 1
                    return True
            return False

        # Pass 1: same-class swap on a duplicated slot
        if _try_swap(prefer_duplicates=True):
            continue
        # Pass 2: same-class swap on any HIT slot
        if _try_swap(prefer_duplicates=False):
            continue


def _content_class_for_zwo(zwo_file: str) -> str:
    """Look up content_class for a planner-emitted zwo path/name."""
    if not zwo_file:
        return ""
    cache = _load_content_classifications() or {}
    ent = cache.get(zwo_file) or cache.get(zwo_file.split("/")[-1])
    if ent:
        return (ent.get("primary") or "").lower()
    return ""


# ── Reforecaster ──────────────────────────────────────────────────────────────

# Hard session types whose intensity we re-evaluate in reforecast (PL4).
_HARD_SESSION_TYPES = frozenset({
    "vo2max", "threshold", "overunder", "sweetspot", "sprint", "tempo",
    # v1.1.0 IMPL-NORWEGIAN-HR: double_threshold counts as a hard session
    # (AM+PM threshold-class pair, both with HR ceiling 88% max_hr).
    "double_threshold",
})


def apply_week_tier_down(
    plan: dict, day_iso: str, dry_run: bool = False, to_floor: bool = False,
) -> dict:
    """v1.8.0 §F1 — walk remaining hard sessions in the current Mon-Sun
    window and tier-down each by one ladder step.

    issue #3: ``to_floor=True`` drops each hard session ALL the way to the easy
    floor (z2/endurance) in one call, instead of one ladder step — so a very low
    readiness day doesn't need the rider to click "auto-adjust" repeatedly. The
    caller (/api/plan/auto-adjust) sets it when the readiness severity is high.

    Selection:
      - day_iso <= session.day <= sunday(day_iso) (TODAY-ONWARD, strict
        Mon-Sun, no wrap into next week). The window starts at day_iso, not
        the week's Monday — tier-down only touches the REMAINING hards; a hard
        session earlier in the week the athlete already rode is never re-touched.
      - session.session_type in ``_HARD_SESSION_TYPES``.
      - session.status is pending (done/done_partial/missed/dismissed skipped).

    Per-session: capture ``before`` snapshot, call ``_drop_intensity``,
    recompute ``tss_estimate`` from ``TSS_PER_HOUR[new_type]``, mark
    ``adapted=True``, re-match ZWO with the prior name in ``used_names``.
    ``NoCandidateWorkoutError`` mid-walk → clear ZWO fields, mark
    ``rematched=False, zwo_cleared=True`` and continue (don't abort).

    Dry-run: operates on ``copy.deepcopy(plan)``. Caller must skip
    persistence and reforecast on dry-run paths.

    Returns:
        {"sessions_modified": int, "actions": list[dict], "note": str}
        Each action: {day, before, after, rematched, zwo_cleared}.
    """
    import copy as _copy
    working = _copy.deepcopy(plan) if dry_run else plan
    try:
        anchor = date.fromisoformat(day_iso)
    except (TypeError, ValueError):
        return {"sessions_modified": 0, "actions": [],
                "note": f"invalid day_iso: {day_iso!r}"}
    sunday = anchor + timedelta(days=(6 - anchor.weekday()))

    actions: list[dict] = []
    sessions_modified = 0

    # Library loaded once for all rematches; tolerate failure.
    try:
        library = load_workout_library()
    except Exception:
        library = []

    for week in working.get("weeks", []) or []:
        for sess in week.get("sessions", []) or []:
            s_day = sess.get("day") or ""
            try:
                s_date = date.fromisoformat(s_day)
            except (TypeError, ValueError):
                continue
            if s_date < anchor or s_date > sunday:
                continue
            # v2.0.3 F3: skip sessions the athlete has already actioned. The old
            # check was only `== "completed"`, a value production NEVER writes
            # (real statuses: done/done_partial/missed/dismissed) — so a done hard
            # could be tier-downed + reset to pending. Cover the real set AND keep
            # "completed" for any legacy/card-state data that carries it.
            if sess.get("status") in {
                "completed", "done", "done_partial", "missed", "dismissed",
            }:
                continue
            if _protect_race(sess):  # FC3: race entry immutable to tier-down
                continue
            old_type = sess.get("session_type", "") or ""
            if old_type not in _HARD_SESSION_TYPES:
                continue
            new_type = _drop_intensity(old_type)
            if to_floor and new_type:
                # Drop ALL the way to the easy floor (first non-hard type) in one
                # pass. _drop_intensity is a single ladder step; iterate until it
                # bottoms out of the hard types (→ z2 / endurance).
                _guard = 0
                while new_type in _HARD_SESSION_TYPES and _guard < 10:
                    _nxt = _drop_intensity(new_type)
                    if not _nxt or _nxt == new_type:
                        break
                    new_type = _nxt
                    _guard += 1
            if not new_type or new_type == old_type:
                continue

            old_duration = int(sess.get("duration_min", 0) or 0)
            old_tss = float(sess.get("tss_estimate", 0) or 0)
            tss_per_h = TSS_PER_HOUR.get(new_type, 45)
            new_duration = old_duration
            new_tss = round(old_duration / 60 * tss_per_h, 1)
            # issue #3: a tier-DOWN must never INCREASE load. The Seiler ladder can
            # step to a type with higher TSS/h (vo2max 75 → threshold 90), so at the
            # same duration TSS would RISE (rider saw 64 → 76.5, +13). Cap the
            # duration so the adjusted session's TSS is <= the original.
            if old_tss > 0 and new_tss > old_tss:
                new_duration = max(_VOLUME_MIN_SESSION_MIN,
                                   int(round(old_tss / tss_per_h * 60)))
                new_tss = round(new_duration / 60 * tss_per_h, 1)

            before = {"type": old_type, "duration_min": old_duration,
                      "tss": old_tss}

            sess["session_type"] = new_type
            sess["duration_min"] = new_duration
            sess["tss_estimate"] = new_tss
            sess["adapted"] = True
            sess["adapted_reason"] = (
                f"Week tier-down: {old_type} → {new_type} (auto-adjust)"
            )
            sess["status"] = "pending"

            rematched = False
            zwo_cleared = False
            old_zwo_name = sess.get("zwo_name", "")
            try:
                planned = PlannedSession(
                    day=s_date,
                    day_name=sess.get("day_name", ""),
                    session_type=new_type,
                    duration_min=new_duration,
                    tss_estimate=new_tss,
                    description=sess.get("description", ""),
                )
                week_num = week.get("week_num", 0)
                week_start_iso = week.get("start", "")
                try:
                    day_idx = (s_date - date.fromisoformat(week_start_iso)).days
                except (TypeError, ValueError):
                    day_idx = 0
                excluded = {old_zwo_name} if old_zwo_name else set()
                match_zwo(
                    planned, library,
                    week_num=week_num, day_idx=day_idx,
                    used_names=excluded, raise_on_empty=True,
                )
                sess["zwo_file"] = planned.zwo_file
                sess["zwo_name"] = planned.zwo_name
                rematched = True
            except NoCandidateWorkoutError:
                sess["zwo_file"] = ""
                sess["zwo_name"] = ""
                zwo_cleared = True
            except Exception:
                # Other failures: leave existing ZWO in place, mark not rematched.
                pass

            after = {"type": new_type, "duration_min": old_duration,
                     "tss": new_tss}
            actions.append({
                "day": s_day,
                "before": before,
                "after": after,
                "rematched": rematched,
                "zwo_cleared": zwo_cleared,
            })
            sessions_modified += 1

    return {
        "sessions_modified": 0 if dry_run else sessions_modified,
        "actions": actions,
        "note": "",
    }


# ── v1.1.0 IMPL-NORWEGIAN-HR — double_threshold AM+PM same-day scheduling ──

# Norwegian Method protocol: AM + PM same-day threshold-class pair, both
# with HR ceiling 88% max_hr (sub-LT2 controlled work). Min ≥4 h gap.
# AM 3-4×8-10 min @ 88-92% FTP, PM 3-4×6-8 min @ 88-90% FTP. Bakken /
# Stöggl & Sperlich 2014 / Casado 2024.
DOUBLE_THRESHOLD_HR_CEILING_PCT = 0.88
DOUBLE_THRESHOLD_AM_DURATION_MIN = 60
DOUBLE_THRESHOLD_PM_DURATION_MIN = 50
DOUBLE_THRESHOLD_MIN_GAP_HOURS = 4


def schedule_double_threshold_pair(
    day: "date",
    day_name: str,
    pair_id: str,
    am_duration_min: int = DOUBLE_THRESHOLD_AM_DURATION_MIN,
    pm_duration_min: int = DOUBLE_THRESHOLD_PM_DURATION_MIN,
    hr_ceiling_pct: float = DOUBLE_THRESHOLD_HR_CEILING_PCT,
) -> "tuple[PlannedSession, PlannedSession]":
    """v1.1.0 IMPL-NORWEGIAN-HR: build the (am, pm) PlannedSession pair for
    a Norwegian-Method same-day double-threshold day.

    Both sessions share `pair_id` via `double_threshold_partner_id`,
    `is_double_threshold_pair=True`, and the same `hr_ceiling_pct`. The
    UI uses these to render 🌅+🌆 on the calendar cell and to expand
    AM+PM detail when the cell is clicked.

    The actual AM/PM clock-time scheduling is left to ride-storage / the
    user's calendar widget; the planner only emits both sessions on the
    same `day` and tags them. ≥4 h gap is a UI-side constraint when the
    rider opens the day to plan it.
    """
    am = PlannedSession(
        day=day,
        day_name=day_name,
        session_type="double_threshold",
        duration_min=am_duration_min,
        tss_estimate=round(am_duration_min / 60 * TSS_PER_HOUR["double_threshold"]),
        description=(
            f"Norwegian double-threshold AM (≤{int(hr_ceiling_pct*100)}% HR_max). "
            "3-4×8-10 min @ 88-92% FTP."
        ),
        hr_ceiling_pct=hr_ceiling_pct,
        is_double_threshold_pair=True,
        double_threshold_partner_id=pair_id,
        am_or_pm="am",
    )
    pm = PlannedSession(
        day=day,
        day_name=day_name,
        session_type="double_threshold",
        duration_min=pm_duration_min,
        tss_estimate=round(pm_duration_min / 60 * TSS_PER_HOUR["double_threshold"]),
        description=(
            f"Norwegian double-threshold PM (≤{int(hr_ceiling_pct*100)}% HR_max). "
            f"3-4×6-8 min @ 88-90% FTP. ≥{DOUBLE_THRESHOLD_MIN_GAP_HOURS} h after AM."
        ),
        hr_ceiling_pct=hr_ceiling_pct,
        is_double_threshold_pair=True,
        double_threshold_partner_id=pair_id,
        am_or_pm="pm",
    )
    return am, pm


# ── v1.1.0 IMPL-NORWEGIAN-HR — G9 advisory (DFA α1 tier-down) ─────────────────

# Below this threshold, autonomic strain has dropped (Rogers 2021 — DFA α1
# crossing 0.75 marks LT1; values <0.75 indicate sympathetic shift / fatigue).
G9_DFA_ALPHA1_THRESHOLD = 0.75

# Per master §1: when yesterday's α1 is below the threshold, today's HIT
# class drops one tier. PATCH G10: classes NOT in this map are already at
# the lowest sensible tier — g9_advisory returns a no-op for them so we
# never raise KeyError.
G9_TIER_DOWN_BUCKETS = {
    "vo2max":          "threshold",
    "vo2_short":       "threshold",
    "threshold":       "tempo",
    "tempo_intervals": "tempo",
    "tempo":           "endurance_intervals",
    # double_threshold is the Norwegian Method showcase; α1 fatigue should
    # collapse it to single threshold rather than skip a day entirely.
    "double_threshold": "threshold",
}


def g9_advisory(
    yesterday_dfa_alpha1: float | None,
    today_class: str,
) -> dict:
    """v1.1.0 IMPL-NORWEGIAN-HR: G9 advisory — DFA α1 driven tier-down.

    Pure advisory function. NEVER mutates a session. Callers (planner
    reforecast, dashboard chips) consume the returned dict.

    Args:
        yesterday_dfa_alpha1: yesterday's `dfa_alpha1_avg` from the cached
            ride summary (v1.0.7 IMPL-DFA-ALPHA1). None when the rider
            doesn't have a chest strap, the FIT lacks RR data, or v1.0.7
            isn't yet feeding the cache. SAFE DEGRADATION when None.
        today_class: today's planned session_type (e.g. "vo2max", "endurance").

    Returns:
        {"advised_class": str | None,
         "reason": str,
         "should_log": bool}

    PATCH G10 contract: when today_class is NOT in G9_TIER_DOWN_BUCKETS
    (e.g. "endurance", "recovery", "rest"), returns
    `{"advised_class": today_class, "reason": "already at lowest tier",
      "should_log": False}` — NO KeyError.
    """
    # Safe degradation: missing α1 data ⇒ no advisory.
    if yesterday_dfa_alpha1 is None:
        return {
            "advised_class": today_class,
            "reason": "no DFA α1 data for yesterday",
            "should_log": False,
        }

    try:
        a1 = float(yesterday_dfa_alpha1)
    except (TypeError, ValueError):
        return {
            "advised_class": today_class,
            "reason": "invalid DFA α1 value",
            "should_log": False,
        }

    # Above threshold ⇒ no advisory (rider is recovered).
    if a1 >= G9_DFA_ALPHA1_THRESHOLD:
        return {
            "advised_class": today_class,
            "reason": (
                f"yesterday's α1 was {a1:.2f} ≥ {G9_DFA_ALPHA1_THRESHOLD} "
                "— no tier-down"
            ),
            "should_log": False,
        }

    # PATCH G10: no-op when today's class is already at lowest tier.
    if today_class not in G9_TIER_DOWN_BUCKETS:
        return {
            "advised_class": today_class,
            "reason": "already at lowest tier",
            "should_log": False,
        }

    advised = G9_TIER_DOWN_BUCKETS[today_class]
    return {
        "advised_class": advised,
        "reason": (
            f"yesterday's α1 was {a1:.2f} < {G9_DFA_ALPHA1_THRESHOLD} "
            f"(LT1 drift, Rogers 2021) — consider {advised} today"
        ),
        "should_log": True,
    }


# v4.6.6 IMPL-A: ACWR helper (Gabbett 2016 Br J Sports Med 50:273-280).
# ACWR = acute / chronic load ratio; sweet spot 0.8-1.3, >1.5 doubles
# injury risk. Here we use the simpler weekly proxy:
#   actual_tss(last full week) / planned_tss(last full week)
# A ratio >1.5 means the athlete absorbed 50% more load than prescribed
# for that week and triggers a downscaling of the NEXT planned week.
def _last_completed_week_acwr(
    plan_weeks: "list[PlannedWeek]",
    rides: "list[dict]",
) -> float:
    """Return actual_tss / max(planned_tss, 1) for the most recent fully-
    completed plan week.

    A "fully completed" week is one whose ``end`` date is strictly before
    today (i.e. the in-progress week is excluded). If no such week exists
    in ``plan_weeks``, returns 0.0 (callers treat 0.0 as "no signal").

    ``rides`` follows the same shape used elsewhere in this module:
    each dict has ``date`` (or ``start_date_local`` ISO prefix) and
    ``tss`` (or ``icu_training_load``) keys.
    """
    today = date.today()
    completed = [w for w in plan_weeks if w.end < today]
    if not completed:
        return 0.0
    last = max(completed, key=lambda w: w.end)
    week_start = last.start.isoformat()
    week_end = last.end.isoformat()
    actual = 0.0
    for r in rides or []:
        rd = r.get("date") or (r.get("start_date_local") or "")[:10] or ""
        if week_start <= rd <= week_end:
            actual += float(r.get("tss") or r.get("icu_training_load") or 0)
    planned = float(last.tss_target or 0)
    return actual / max(planned, 1.0)


def reforecast(
    goal: Goal,
    plan_weeks: list[PlannedWeek],
    tsb_series: "dict[date, float] | None" = None,
    recent_activities: "list[dict] | None" = None,
    # G3 (IMPL-B-owned): polarized split inputs for the polarization-breach
    # gate. IMPL-A owns tss_target/hit_per_week (G4) above; IMPL-B owns the
    # session_type mutations in the `# G3:` block below.
    actual_polarization: "dict | None" = None,
    target_polarization: "dict | None" = None,
    # v1.0.3 IMPL-AVAILABILITY: per-day override of available training hours.
    # Sparse mapping iso-date → daily hours. 0.0 = rest day, > 0 rescales the
    # planned session's duration_min and tss_estimate. Days NOT present keep
    # their current planned duration. Per-week scale clamped to [0.4, 2.0]
    # to prevent runaway expansion / collapse. Algorithm runs BEFORE the G3
    # / G4 blocks so downshift logic operates on already-rescaled durations.
    availability_overrides: "dict[str, float] | None" = None,
    # ── v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE) ──────────────────
    # Optional W'-balance / capacity / ACWR / polarisation inputs. NONE BY
    # DEFAULT — preserves all existing call sites byte-for-byte. When
    # supplied, advisory checks fire (G3b, wprime_acwr advisory, G8) but
    # NEVER mutate sessions; TSS-driven gates remain primary.
    wprime_balance_24h: "float | None" = None,
    w_prime: "float | None" = None,
    wprime_acwr: "float | None" = None,
    actual_wprime_polarization: "dict | None" = None,
    # ── v1.1.0 IMPL-NORWEGIAN-HR (G9 advisory — DFA α1 tier-down) ──────────
    # Yesterday's `dfa_alpha1_avg` from cached ride summary (v1.0.7 IMPL-DFA).
    # When < G9_DFA_ALPHA1_THRESHOLD (0.75), today's HIT class drops one
    # tier ADVISORY — never mutates today_session.session_type. NULL-safe:
    # None ⇒ no advisory fires (rider has no chest strap or v1.0.7 not yet
    # populated). Mirrors G3b / G8 advisory pattern.
    yesterday_dfa_alpha1: "float | None" = None,
    # ── v1.8.1 SPEED-B (accept-redraw fast path) ────────────────────────────
    # When True, skips the G3 polarization-breach recompute AND the G4 ACWR
    # weekly-rescale block. The single-session swap on /api/plan/accept-redraw
    # changes ONE session's TSS / duration — neither the rolling polarization
    # mix nor the prior week's actual/planned ratio is affected. Skipping
    # these two blocks reduces wall-clock by ~70 % on a 26-week plan.
    # KEEP-PATH includes: availability scaling, TSB-driven intensity downshift,
    # advisory log (G3b / G8 / G9), and downstream propagation via
    # `_apply_reforecast_to_dict`. Defaults to False so existing callers
    # (save-availability, /api/plan/reforecast, daily_adapt_plan) keep the
    # full algorithm.
    accept_redraw_fast: bool = False,
) -> tuple[list[PlannedWeek], dict]:
    """Shift future hard sessions up/down one intensity level based on TSB.

    PL4 replacement for the old printed-advisory no-op. For each future day:
      - if TSB at that date is below -25 → drop intensity one level
        (vo2max → threshold → overunder → sweetspot → tempo → z2 → recovery);
      - otherwise the prescription is left alone.

    v4.6.6 IMPL-A — also runs Guardrail G4 (ACWR weekly scaling, Gabbett
    2016 Br J Sports Med 50:273-280): if last completed week's
    actual_tss/planned_tss > 1.5, scale the next non-stepback week's
    tss_target ×0.85 and decrement that week's hit_per_week by 1.

    This mutates `plan_weeks` in place AND returns it plus a summary dict so
    the /api/plan/reforecast endpoint can report what changed without having
    to diff the plan itself. The underlying week/day skeleton (phase mix, step-
    back cadence, long-ride placement) is preserved — no rebuild from scratch.

    Args:
        goal: unused for intensity shifts (kept for signature compatibility and
              so callers can later add CTL-target logic without changing the
              endpoint contract).
        plan_weeks: the plan to adjust. Mutated in place.
        tsb_series: optional dict mapping a date → TSB value. If omitted, we
              use today's metrics as a flat projection for every future day
              (fast path used by the `/api/plan/reforecast` endpoint until the
              app starts passing a forecast curve).
        recent_activities: optional list of activity dicts (date / tss /
              icu_training_load) used by the G4 ACWR gate. When None or empty,
              the gate is skipped — TSB-only behavior is preserved.
        accept_redraw_fast: v1.8.1 SPEED-B — when True, skip the G3
              polarization recompute and G4 ACWR weekly rescale. Trade-off:
              a single-session swap (accept-redraw hot path) does NOT change
              the prior week's actual/planned ratio nor the rolling
              polarization split, so re-running those two blocks is wasted
              work. Use ONLY for small-mutation cases. Large reshuffles or
              the daily reforecast endpoint must keep the default (False).

    Returns:
        (plan_weeks, info) where `info` is
          {
            "action": "reforecasted" | "no_change" | "no_future",
            "downshifts": int,
            "touched_days": [iso-date, ...],
            "acwr_ratio": float,            # last completed week ratio
            "acwr_scaled_week": int | None, # week_num that got *=0.85
          }
    """
    today = date.today()

    def _tsb_at(d: date) -> float | None:
        if tsb_series is not None:
            return tsb_series.get(d)
        try:
            m = get_today_metrics()
            return m.get("tsb")
        except Exception:  # noqa: BLE001
            return None

    # ── v1.0.3 IMPL-AVAILABILITY: per-day availability override scaling ──
    # `plan["availability"]` finally gets plumbed through reforecast so per-day
    # hour overrides actually rescale duration_min / tss_estimate. Runs BEFORE
    # the G3/G4 blocks so downshift logic operates on already-rescaled
    # durations. Per-week scale clamped to [0.4, 2.0] (sparse coverage — only
    # days the user touched are present; absent days keep current duration).
    touched: set[str] = set()
    # v1.7.2 — lazy-loaded library cache; the re-match-on-cap branch below
    # populates this on first miss and reuses it across the loop so we
    # don't re-read the full library N times in the worst case.
    _rematch_library: "list[dict] | None" = None
    if availability_overrides:
        for pw in plan_weeks:
            # v1.3.5 fix: gate on pw.end (mirrors the G3 downshift block at
            # line ~5021). The pre-fix `pw.start < today` test silently
            # skipped the *current* week — whose Monday is by definition <
            # today on any non-Monday — so a Sat/Sun=0 UPDATE click left
            # those days as planned z2/long sessions on disk. Gating on
            # pw.end keeps past *completed* weeks out while still letting
            # the current week's future days be re-rested.
            if pw.end < today:
                continue  # fully-past weeks — don't touch
            week_keys = [
                s.day.isoformat() for s in pw.sessions
                if s.day.isoformat() in availability_overrides
            ]
            if not week_keys:
                continue
            available_mins = sum(
                int(float(availability_overrides[k]) * 60) for k in week_keys
            )
            current_mins = sum(
                s.duration_min for s in pw.sessions
                if s.day.isoformat() in availability_overrides
            )
            # v1.3.6 fix: pre-fix `if current_mins <= 0: continue` short-
            # circuited weeks where every override day was already REST
            # (e.g. a holiday week the user marked all-zero, then later
            # wants to restore one day). Permit the per-day loop to run
            # even when current_mins=0; only skip when there is nothing
            # to do on either side. The rest-restore branch below uses
            # `hours * 60` literally so it doesn't depend on `scale`.
            if current_mins <= 0 and available_mins <= 0:
                continue
            if current_mins <= 0:
                scale = 1.0  # unused; rest-restore branch ignores scale.
            else:
                raw_scale = available_mins / current_mins
                scale = min(2.0, max(0.4, raw_scale))
            for s in pw.sessions:
                d_iso = s.day.isoformat()
                if d_iso not in availability_overrides:
                    continue
                # FC3 (v2.5.0, E12 — writer #12): the availability rescale must
                # never touch the race entry. hours=0 on the race date used to
                # convert race day to a rest stub (and _mark_race_days'
                # idempotency then correctly refused to churn it back). The
                # race happens whether or not the calendar says "0h free".
                if _protect_race(s):
                    continue
                hours = float(availability_overrides[d_iso])
                if hours <= 0:
                    # v1.3.5 fix: also clear ZWO + description so the
                    # dashboard renders the cell as REST (mirrors the
                    # generate_plan block at line ~4202).
                    s.session_type = "rest"
                    s.duration_min = 0
                    s.tss_estimate = 0
                    s.description = "Rest (unavailable)"
                    s.zwo_file = ""
                    s.zwo_name = ""
                elif s.session_type == "rest":
                    # v1.3.6 fix: rest-day → training-day restore. When the
                    # user raises hours from 0 → positive on a day previously
                    # zeroed out, duration_min is 0 so `dur * scale` stays 0.
                    # Re-seed with z2 (Layer-1 endurance default — matches
                    # _pick_session's fallback) so the day actually trains.
                    # Use `hours * 60` literally because scale = available /
                    # current and current=0 makes the ratio undefined.
                    new_dur = min(int(round(hours * 60)), MAX_AVAIL_SESSION_MIN)
                    s.session_type = "z2"
                    s.duration_min = new_dur
                    tss_per_h = TSS_PER_HOUR.get("z2", 45)
                    s.tss_estimate = round(new_dur / 60 * tss_per_h)
                    s.description = f"z2 ({new_dur}min) — restored from rest"
                    s.zwo_file = ""
                    s.zwo_name = ""
                else:
                    # v1.7.3 — apply user's hours LITERALLY (both up and
                    # down). v1.7.1 used a ceiling-only rule, but that
                    # made the cap one-way: once a session was shrunk,
                    # raising hours on the calendar could never restore
                    # the original duration. Callers are now expected to
                    # filter ``availability_overrides`` to user-changed
                    # days (save-availability does this via prior-vs-new
                    # diff); every override is intentional so literal
                    # application is safe.
                    #
                    # Pre-v1.7.1 bug recap (still relevant for context):
                    # ``scale = available_mins / current_mins`` was
                    # computed PER WEEK across all touched days, then
                    # applied uniformly. Frontend POSTs 180 days of
                    # defaults so the user-targeted day was diluted.
                    # v1.7.1 fixed the dilution with a ceiling; v1.7.3
                    # makes it bidirectional and the diff at the caller
                    # side prevents clobbering planner choices on
                    # untouched days.
                    # v1.9.2 — honor the user's hours literally BUT cap at a
                    # 6h/session sanity ceiling so a typo/extreme availability
                    # (e.g. 10h) can't spawn an absurd 600-min session the
                    # library can't even serve. 6h covers real long endurance /
                    # gran-fondo rides.
                    # v2.0.6 — apply the per-type duration ceiling here too. The
                    # availability reflow previously sized a session to the full
                    # day (only the 6h MAX_AVAIL_SESSION_MIN sanity cap), so a
                    # 90-min Tuesday made a 45-min-max sprint/neuromuscular slot
                    # render as a 90-min ~140-TSS day — the same "regardless of
                    # origin" gap the generate_plan final pass (~line 5063) closes
                    # for the sampler. Mirror that clamp: cap at the content-class
                    # (or session-type) ceiling. The >=15% re-match below then
                    # refits the ZWO to the clamped duration.
                    _cc_clamp = _content_class_for_zwo(getattr(s, "zwo_file", "") or "")
                    _type_ceil = TYPE_CEILING.get(_cc_clamp) or TYPE_CEILING.get(s.session_type)
                    target_min = min(int(round(hours * 60)), MAX_AVAIL_SESSION_MIN)
                    if _type_ceil:
                        target_min = min(target_min, _type_ceil)
                    if target_min != s.duration_min:
                        old_dur = s.duration_min
                        s.duration_min = max(0, target_min)
                        tss_per_h = TSS_PER_HOUR.get(s.session_type, 45)
                        s.tss_estimate = round(s.duration_min / 60 * tss_per_h)
                        # v1.7.2 — when the duration change is meaningful
                        # (>= 15 % shrink OR >= 15 % expand), re-match the
                        # ZWO so the loaded workout actually fits the new
                        # duration. Without this the dashboard would show
                        # "45min" in the title but render a 90-min ZWO
                        # chart (and vice versa after expansion).
                        if old_dur > 0:
                            ratio = abs(s.duration_min - old_dur) / old_dur
                            if ratio >= 0.15:
                                try:
                                    if _rematch_library is None:
                                        _rematch_library = load_workout_library()
                                    _excluded = {s.zwo_name} if s.zwo_name else set()
                                    match_zwo(
                                        s, _rematch_library,
                                        week_num=pw.week_num,
                                        day_idx=(s.day - pw.start).days,
                                        used_names=_excluded,
                                        raise_on_empty=True,
                                    )
                                except NoCandidateWorkoutError:
                                    s.zwo_file = ""
                                    s.zwo_name = ""
                                except Exception:
                                    pass
                touched.add(d_iso)

    downshifts: list[str] = []
    for pw in plan_weeks:
        if pw.end < today:
            continue  # past weeks — don't touch
        # v1.6.1 — per-week wrap: log E_REFORECAST_WEEK_FAILED and continue
        # so one malformed week doesn't sink the whole reforecast pass.
        try:
            for s in pw.sessions:
                if s.day <= today:
                    continue  # today + past already handled by daily_adapt_plan
                if s.session_type not in _HARD_SESSION_TYPES:
                    continue
                if _protect_race(s):
                    continue  # FC3: race entry immutable to the TSB downshift
                if getattr(s, "user_swapped", False):
                    continue  # v2.3.0: user's manual type-swap is pinned
                tsb = _tsb_at(s.day)
                if tsb is None:
                    continue
                if tsb < -25:
                    new_type = _drop_intensity(s.session_type)
                    if new_type != s.session_type:
                        s.session_type = new_type
                        new_tss_per_h = TSS_PER_HOUR.get(new_type, 45)
                        s.tss_estimate = round(s.duration_min / 60 * new_tss_per_h)
                        s.description = f"Reforecast: TSB {tsb:.0f} → {new_type}"
                        s.adapted = True
                        # Force a library re-match downstream by clearing ZWO.
                        s.zwo_file = ""
                        s.zwo_name = ""
                        downshifts.append(s.day.isoformat())
        except Exception as _e:
            _tp_log_error(error_codes.Codes.REFORECAST_WEEK_FAILED, exc=_e,
                          week_num=getattr(pw, "week_num", 0),
                          week_start=str(getattr(pw, "start", "")))
            continue

    # ── G4: ACWR weekly scaling (Gabbett 2016) ────────────────────────────
    # Rationale: ACWR sweet-spot is 0.8-1.3; >1.5 doubles injury risk
    # (Gabbett 2016 Br J Sports Med 50:273-280). When the last fully-
    # completed plan week's actual/planned TSS exceeds 1.5, the athlete is
    # absorbing far more load than prescribed — a leading indicator of
    # overuse injury. We scale the NEXT non-stepback week's tss_target by
    # 0.85 and decrement its hit_per_week by 1 (floored at 1) so the
    # following week is materially lighter without erasing the planned
    # progression. Stepback weeks are skipped because they are already
    # unloaded — scaling them again would over-rest.
    acwr_ratio = 0.0
    acwr_scaled_week: int | None = None
    if not accept_redraw_fast:
        try:
            acwr_ratio = _last_completed_week_acwr(plan_weeks, recent_activities or [])
        except Exception:  # noqa: BLE001
            acwr_ratio = 0.0
        if acwr_ratio > 1.5:
            for pw in plan_weeks:
                if pw.start <= today:
                    continue  # don't scale past or in-progress weeks
                if pw.is_stepback:
                    continue  # stepback already unloaded; double-cut would be too aggressive
                pw.tss_target = pw.tss_target * 0.85
                pw.hit_per_week = max(1, (pw.hit_per_week or 0) - 1)
                pw.auto_acwr_scaled = True
                acwr_scaled_week = pw.week_num
                break  # only the next planned non-stepback week

    # G3: Polarization-breach gate (Seiler 2010 / Stöggl 2014 / Treff 2019).
    # When this week's actual polarized split has busted either the Z4+ ceiling
    # (>target+8) or the Z1+Z2 floor (<target-10), drop the next 1-2 future
    # hard sessions one tier. IMPL-B-owned; mutates session_type only —
    # tss_target / hit_per_week mutations belong to IMPL-A's G4 block above.
    g3_polarization_breached = False
    g3_dropped_days: list[str] = []
    if not accept_redraw_fast and _polarization_breach(actual_polarization, target_polarization):
        g3_polarization_breached = True
        dropped_count = 0
        for pw in plan_weeks:
            if pw.end < today:
                continue
            for s in pw.sessions:
                if dropped_count >= 2:
                    break
                if s.day <= today:
                    continue
                if s.session_type not in _HARD_SESSION_TYPES:
                    continue
                if _protect_race(s):
                    continue  # FC3: race entry immutable to the G3 downshift
                if s.adapted:
                    continue  # already touched by TSB loop above
                if getattr(s, "user_swapped", False):
                    continue  # v2.3.0: user's manual type-swap is pinned
                new_type = _drop_intensity(s.session_type)
                if new_type == s.session_type:
                    continue
                old_type = s.session_type
                s.session_type = new_type
                new_tss_per_h = TSS_PER_HOUR.get(new_type, 45)
                s.tss_estimate = round(s.duration_min / 60 * new_tss_per_h)
                s.description = (
                    f"G3 polarization breach: {old_type} → {new_type} "
                    f"(Seiler/Stöggl/Treff)"
                )
                s.adapted = True
                s.zwo_file = ""
                s.zwo_name = ""
                g3_dropped_days.append(s.day.isoformat())
                dropped_count += 1
            if dropped_count >= 2:
                break

    # ── v1.0.6 IMPL-3D-PLANNER (TSS PRIMARY, 3D ADDITIVE) ─────────────────
    # Advisory log block. Every entry here is LOG-ONLY: never mutates a
    # session. The planner's primary TSS-driven gates (TSB/G3a/G4 above)
    # remain authoritative.
    advisory_log: list[str] = []
    g8_softened_day: str | None = None
    g3b_breach = False

    # G3b: W'-load polarisation advisory. Volume polarisation (G3a) is the
    # hard gate; G3b warns when above-CP load distribution drifts >10%.
    if (
        actual_wprime_polarization is not None
        and target_polarization is not None
    ):
        try:
            for zone_key, target_val in target_polarization.items():
                actual_val = actual_wprime_polarization.get(zone_key)
                if actual_val is None:
                    continue
                if abs(float(actual_val) - float(target_val)) > 10.0:
                    g3b_breach = True
                    advisory_log.append(
                        f"G3b advisory: W'-load polarization {zone_key}="
                        f"{float(actual_val):.1f}% deviates >10% from target "
                        f"{float(target_val):.1f}% (log-only; G3a still primary)"
                    )
        except (TypeError, ValueError):
            pass

    # wprime_acwr advisory (parallel to G4). TSS-based G4 stays primary.
    if wprime_acwr is not None:
        try:
            if float(wprime_acwr) > 1.5:
                advisory_log.append(
                    f"wprime_acwr={float(wprime_acwr):.2f} > 1.5 advisory "
                    "(TSS-based G4 remains primary trip)"
                )
        except (TypeError, ValueError):
            pass

    # NEW G8: W'-balance next-day soft bias. Advisory only — does NOT
    # mutate session_type. Hard tier-downs come from TSB<-25 and G3a.
    if (
        wprime_balance_24h is not None
        and w_prime is not None
        and w_prime > 0
    ):
        try:
            wp_ratio = float(wprime_balance_24h) / float(w_prime)
            if wp_ratio < 0.5:
                for pw in plan_weeks:
                    if pw.end < today:
                        continue
                    found = False
                    for s in pw.sessions:
                        if s.day <= today:
                            continue
                        if s.session_type not in _HARD_SESSION_TYPES:
                            continue
                        # Advisory only — DO NOT mutate s.session_type.
                        g8_softened_day = s.day.isoformat()
                        advisory_log.append(
                            f"G8 advisory: wprime_balance_24h="
                            f"{float(wprime_balance_24h):.0f}J "
                            f"({wp_ratio*100:.0f}% of W'={float(w_prime):.0f}J) "
                            f"— prefer Z2 today; next hard slot "
                            f"({g8_softened_day}, {s.session_type}) "
                            f"flagged for soft tier-down"
                        )
                        found = True
                        break
                    if found:
                        break
                if g8_softened_day is None:
                    advisory_log.append(
                        f"G8 advisory: wprime_balance_24h="
                        f"{float(wprime_balance_24h):.0f}J "
                        f"({wp_ratio*100:.0f}% of W'={float(w_prime):.0f}J) "
                        "— prefer Z2 today (no future hard slot to flag)"
                    )
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    # ── v1.1.0 IMPL-NORWEGIAN-HR — G9 advisory: DFA α1 tier-down ──────────
    # When yesterday's α1 < 0.75 (Rogers 2021 LT1 drift), today's HIT class
    # drops one tier. ADVISORY ONLY — mirrors G3b/G8: NEVER mutates
    # today_session.session_type. Returns dict consumed by the dashboard
    # chip and persisted in the plan reforecast log.
    g9_advisory_class: str | None = None
    g9_today_class: str | None = None
    g9_reason: str | None = None
    if yesterday_dfa_alpha1 is not None:
        # Locate today's planned session (if any) — used as input to g9_advisory.
        today_session = None
        for pw in plan_weeks:
            if pw.end < today:
                continue
            for s in pw.sessions:
                if s.day == today:
                    today_session = s
                    break
            if today_session is not None:
                break
        if today_session is not None:
            adv = g9_advisory(yesterday_dfa_alpha1, today_session.session_type)
            g9_today_class = today_session.session_type
            g9_reason = adv["reason"]
            if adv["should_log"]:
                g9_advisory_class = adv["advised_class"]
                advisory_log.append(
                    f"G9 advisory: today's planned {today_session.session_type} "
                    f"(yesterday's α1={float(yesterday_dfa_alpha1):.2f}) "
                    f"— consider {adv['advised_class']} today. "
                    "Session NOT mutated (advisory only)."
                )

    # v1.0.3 IMPL-AVAILABILITY: merge availability-touched dates into
    # touched_days so the app.py write-back loop persists duration_min /
    # tss_estimate / session_type changes for those days too.
    merged_touched: list[str] = list(downshifts)
    seen = set(downshifts)
    for d_iso in sorted(touched):
        if d_iso not in seen:
            merged_touched.append(d_iso)
            seen.add(d_iso)

    if not plan_weeks or all(pw.end < today for pw in plan_weeks):
        return plan_weeks, {
            "action": "no_future", "downshifts": 0,
            "touched_days": merged_touched,
            "acwr_ratio": round(acwr_ratio, 3),
            "acwr_scaled_week": acwr_scaled_week,
            "polarization_breach": g3_polarization_breached,
            "g3_dropped_days": g3_dropped_days,
            # v1.0.6 IMPL-3D-PLANNER advisory fields (log-only)
            "advisory_log": advisory_log,
            "g3b_breach": g3b_breach,
            "g8_softened_day": g8_softened_day,
            # v1.1.0 IMPL-NORWEGIAN-HR G9 advisory (log-only).
            "g9_advisory_class": g9_advisory_class,
            "g9_today_class": g9_today_class,
            "g9_reason": g9_reason,
        }

    action = "reforecasted" if (
        downshifts or acwr_scaled_week is not None or g3_dropped_days or touched
    ) else "no_change"
    # B2 (v2.1.0): keep hard sessions off the event eve on the reforecast path
    # too (see regenerate_from_today). No-op for non-event goals.
    # FC4a (v2.5.0, L3-3): the guard passes below mutate sessions OUTSIDE the
    # touched-day bookkeeping above, and reforecast_dict propagates ONLY
    # touched days back into the plan dict — so their demotions/markings were
    # silently dropped on the dict path. Snapshot → diff → register.
    def _guard_sig(s):
        return (s.session_type, s.duration_min, s.tss_estimate,
                getattr(s, "zwo_file", "") or "",
                getattr(s, "description", "") or "",
                bool(getattr(s, "is_race", False)),
                bool(getattr(s, "is_opener", False)))
    _pre_guard = {s.day.isoformat(): _guard_sig(s)
                  for pw in plan_weeks for s in pw.sessions}
    _enforce_event_taper_eve(plan_weeks, goal.target_date)
    _apply_secondary_event_tapers(plan_weeks, goal)  # F7: B/C mini-tapers
    _mark_race_days(plan_weeks, goal)  # issue #7: race day shows the race, not a session
    for pw in plan_weeks:
        for s in pw.sessions:
            _iso = s.day.isoformat()
            if _pre_guard.get(_iso) != _guard_sig(s) and _iso not in seen:
                merged_touched.append(_iso)
                seen.add(_iso)
    return plan_weeks, {
        "action": action,
        "downshifts": len(downshifts),
        "touched_days": merged_touched,
        "acwr_ratio": round(acwr_ratio, 3),
        "acwr_scaled_week": acwr_scaled_week,
        "polarization_breach": g3_polarization_breached,
        "g3_dropped_days": g3_dropped_days,
        # v1.0.6 IMPL-3D-PLANNER advisory fields (log-only)
        "advisory_log": advisory_log,
        "g3b_breach": g3b_breach,
        "g8_softened_day": g8_softened_day,
        # v1.1.0 IMPL-NORWEGIAN-HR G9 advisory (log-only).
        "g9_advisory_class": g9_advisory_class,
        "g9_today_class": g9_today_class,
        "g9_reason": g9_reason,
    }


# ══════════════════════════════════════════════════════════════════════════════
# v1.5.0 — Single-layer reforecast (closes drift class A permanently).
# `reforecast_dict(plan_dict, ...)` is the new entrypoint. Internally it
# converts plan_dict ↔ PlannedWeek list, runs `reforecast()`, then
# propagates the result back. Old `reforecast(goal, pw_list, ...)` is
# kept as a deprecated alias for tests + external callers; removal in
# v1.6.0.
# ══════════════════════════════════════════════════════════════════════════════


def _target_events_from_dicts(raw) -> list:
    """FC4a (v2.5.0) — rebuild Goal.events (TargetEvent list) from a persisted
    goal block. tp-side twin of app.py's ``_events_from_dicts`` (reference impl)
    so ``reforecast_dict`` can rebuild a FULL Goal without importing the app
    layer. Entries without a parseable date are skipped."""
    out = []
    for e in raw or []:
        if not isinstance(e, dict):
            continue
        ds = e.get("date")
        try:
            d = date.fromisoformat(ds[:10]) if isinstance(ds, str) else ds
        except (TypeError, ValueError):
            continue
        if not d:
            continue
        climb = e.get("event_climb_m")
        if climb is None:
            climb = e.get("event_climb")
        out.append(TargetEvent(
            date=d,
            priority=e.get("priority", "B") or "B",
            name=e.get("name", "") or "",
            event_type=e.get("event_type", "granfondo") or "granfondo",
            event_km=e.get("event_km", 0) or 0,
            event_climb_m=climb or 0,
        ))
    return out


def _plan_dict_to_planned_weeks(plan_dict: dict) -> list[PlannedWeek]:
    """v1.5.0 — build a PlannedWeek list from the persisted plan_dict.

    Replaces the inline PlannedWeek-building blocks in
    `_maybe_auto_reforecast`, `api_plan_reforecast`, and
    `api_save_availability` (app.py). Single conversion site means
    field-name drift between the JSON shape and PlannedWeek can only
    happen here.

    Days/weeks with malformed dates are skipped silently (matches the
    pre-migration behaviour — those callers wrapped their list-builds
    in try/except per session).
    """
    pw_list: list[PlannedWeek] = []
    for _w_idx, w in enumerate(plan_dict.get("weeks", []) or []):
        try:
            ws = date.fromisoformat(w["start"])
            we = date.fromisoformat(w["end"])
        except (KeyError, ValueError, TypeError) as _e:
            # v1.6.1 — log skip with index + which keys were missing.
            # WARN severity: malformed week is recoverable (we just skip it).
            missing = [k for k in ("start", "end") if k not in (w or {})]
            _tp_log_error(error_codes.Codes.REFORECAST_DICT_TO_PW, exc=_e,
                          week_index=_w_idx,
                          missing_keys=missing or ["?"])
            continue
        sess_list: list[PlannedSession] = []
        for s_json in w.get("sessions", []) or []:
            try:
                sd = date.fromisoformat(s_json["day"])
            except (KeyError, ValueError, TypeError):
                continue
            sess_list.append(PlannedSession(
                day=sd,
                day_name=s_json.get("day_name", sd.strftime("%a")),
                session_type=s_json.get("session_type", "z2"),
                duration_min=int(s_json.get("duration_min", 0) or 0),
                tss_estimate=float(s_json.get("tss_estimate", 0) or 0),
                description=s_json.get("description", ""),
                zwo_file=s_json.get("zwo_file", "") or "",
                zwo_name=s_json.get("zwo_name", "") or "",
                status=s_json.get("status", "pending"),
                # v2.3.0: carry the swap pin so reforecast won't re-sample/demote it.
                user_swapped=bool(s_json.get("user_swapped", False)),
                # E7 (v2.5.0): round-trip the race day, the user-move pin, the
                # dismissal and the opener marker — without these the dict-path
                # mutators saw a plain session and freely rewrote race days /
                # pinned moves / openers (FC3 + F5b guards key on them).
                user_moved=bool(s_json.get("user_moved", False)),
                dismissed_at=s_json.get("dismissed_at", "") or "",
                is_race=bool(s_json.get("is_race", False)),
                race=(s_json.get("race")
                      if isinstance(s_json.get("race"), dict) else None),
                is_opener=bool(s_json.get("is_opener", False)),
            ))
        pw_list.append(PlannedWeek(
            week_num=w.get("week_num", 0), start=ws, end=we,
            phase=w.get("phase", ""),
            tss_target=w.get("tss_target", 0),
            is_stepback=w.get("is_stepback", False),
            sessions=sess_list,
            hit_per_week=int(w.get("hit_per_week", 0) or 0),
            auto_acwr_scaled=bool(w.get("auto_acwr_scaled", False)),
        ))
    return pw_list


def _apply_reforecast_to_dict(
    plan_dict: dict,
    pw_list: list["PlannedWeek"],
    touched_days: set,
) -> int:
    """v1.5.0 — propagate post-reforecast PlannedWeek state back into
    `plan_dict`. Replaces app.py's `_propagate_reforecast_to_dict`.

    Mutates `plan_dict["weeks"][*]["sessions"][*]` in place for every
    day in `touched_days` (typically `touched_days ∪ g3_dropped_days`
    from `reforecast_info`). Round-trips the same field set as the
    pre-v1.5.0 helper:
      session_type, duration_min, tss_estimate, description, zwo_file,
      zwo_name, adapted, adapted_reason
    E7 (v2.5.0) adds: is_race, race, is_opener, user_moved, dismissed_at —
    and a session whose PERSISTED is_race is set is never rewritten.
    Plus week-level G4 ACWR mutations:
      tss_target, hit_per_week, auto_acwr_scaled

    Diff-based: only counts a session as "modified" if any field
    actually changed.

    Returns: number of sessions changed.
    """
    by_day: dict[str, "PlannedSession"] = {
        s.day.isoformat(): s for pw in pw_list for s in pw.sessions
    }
    sessions_changed = 0
    for w in plan_dict.get("weeks", []) or []:
        for s_json in w.get("sessions", []) or []:
            day_iso = s_json.get("day", "") or ""
            if not day_iso or day_iso not in touched_days:
                continue
            src = by_day.get(day_iso)
            if src is None:
                continue
            # E7 (v2.5.0): a PERSISTED race day is never rewritten by a
            # reforecast write-back — the race entry is owned by the goal
            # (add/edit race), not by adaptation passes.
            if s_json.get("is_race"):
                continue
            new_session_type = src.session_type
            new_duration_min = src.duration_min
            new_tss_estimate = src.tss_estimate
            new_zwo_file = getattr(src, "zwo_file", "") or ""
            new_zwo_name = getattr(src, "zwo_name", "") or ""
            new_description = getattr(src, "description", "") or ""
            # E7 (v2.5.0): carried field set — a fresh race marking / opener
            # placement made in the PW layer must reach the dict too.
            new_is_race = bool(getattr(src, "is_race", False))
            new_race = getattr(src, "race", None)
            new_is_opener = bool(getattr(src, "is_opener", False))
            changed = (
                s_json.get("session_type") != new_session_type
                or int(s_json.get("duration_min", 0) or 0) != new_duration_min
                or float(s_json.get("tss_estimate", 0) or 0) != new_tss_estimate
                or (s_json.get("zwo_file", "") or "") != new_zwo_file
                or (s_json.get("zwo_name", "") or "") != new_zwo_name
                or (s_json.get("description", "") or "") != new_description
                or bool(s_json.get("is_race", False)) != new_is_race
                or (s_json.get("race") or None) != new_race
                or bool(s_json.get("is_opener", False)) != new_is_opener
            )
            if not changed:
                continue
            s_json["session_type"] = new_session_type
            s_json["duration_min"] = new_duration_min
            s_json["tss_estimate"] = new_tss_estimate
            s_json["zwo_file"] = new_zwo_file
            s_json["zwo_name"] = new_zwo_name
            s_json["description"] = new_description
            s_json["is_race"] = new_is_race
            s_json["race"] = new_race
            s_json["is_opener"] = new_is_opener
            # E7: round-tripped pins travel with the write so the dict never
            # loses them on a rewrite (they came IN via
            # _plan_dict_to_planned_weeks and are not mutated by reforecast).
            s_json["user_moved"] = bool(getattr(src, "user_moved", False))
            s_json["dismissed_at"] = getattr(src, "dismissed_at", "") or ""
            if getattr(src, "adapted", False):
                s_json["adapted"] = True
                s_json["adapted_reason"] = new_description
            sessions_changed += 1
    # Week-level G4 ACWR mutations.
    pw_by_num = {pw.week_num: pw for pw in pw_list}
    for w in plan_dict.get("weeks", []) or []:
        wn = w.get("week_num")
        src_pw = pw_by_num.get(wn)
        if src_pw is None:
            continue
        if w.get("tss_target") != src_pw.tss_target:
            w["tss_target"] = src_pw.tss_target
        if w.get("hit_per_week") != src_pw.hit_per_week:
            w["hit_per_week"] = src_pw.hit_per_week
        if src_pw.auto_acwr_scaled and not w.get("auto_acwr_scaled"):
            w["auto_acwr_scaled"] = True
    return sessions_changed


def reforecast_dict(
    plan_dict: dict,
    today_iso: "str | None" = None,
    tsb_series: "dict | None" = None,
    recent_activities: "list[dict] | None" = None,
    actual_polarization: "dict | None" = None,
    target_polarization: "dict | None" = None,
    availability_overrides: "dict[str, float] | None" = None,
    wprime_balance_24h: "float | None" = None,
    w_prime: "float | None" = None,
    wprime_acwr: "float | None" = None,
    actual_wprime_polarization: "dict | None" = None,
    yesterday_dfa_alpha1: "float | None" = None,
    propagation_days: "set | None" = None,
    accept_redraw_fast: bool = False,
) -> "tuple[dict, int, dict]":
    """v1.5.0 — single-layer reforecast on the persisted plan dict.

    Closes drift class A: callers no longer maintain a separate
    PlannedWeek list and a JSON-dict propagation block. This is the
    only function that mutates `plan_dict` in response to a reforecast.

    Returns: ``(plan_dict, sessions_modified, reforecast_info)``.
    `plan_dict` is the SAME object passed in (mutated in place; not a
    copy). `sessions_modified` is the diff count; `reforecast_info` is
    forwarded from the underlying `reforecast()`.

    `propagation_days` (optional): when provided, the diff-and-write
    block runs against this exact set of iso-date strings instead of
    the default `touched_days ∪ g3_dropped_days` from `reforecast_info`.
    Used by the save-availability endpoint, which propagates the user's
    availability-touched days verbatim (no TSB / G3 inputs on that
    path; same behaviour as pre-v1.5.0).

    `accept_redraw_fast` (v1.8.1 SPEED-B): forwarded to the underlying
    `reforecast()`. When True, skips G3 polarization recompute and G4
    ACWR weekly rescale — the accept-redraw hot path swaps ONE session
    and does not move either signal. Trade-off: weekly tss_target won't
    auto-rescale on a single-session swap; large reshuffles should keep
    the default (False) and call the full path. Availability scaling +
    downstream TSS propagation still run.

    The legacy `reforecast(goal, pw_list, ...)` API is kept as a
    deprecated alias for tests + external callers (removal in v1.6.0).
    """
    goal_dict = plan_dict.get("goal", {}) or {}
    try:
        # FC4a (v2.5.0, L3-3): carry target_date + event scalars + B/C events
        # through the rebuild. This Goal used to keep ONLY type/hours/days, so
        # the B2 re-assertions at the end of reforecast() — eve-guard, B/C
        # mini-tapers, _mark_race_days — were unconditional no-ops on the dict
        # path (the ONLY path _apply_plan_update / swap-type / tier-down /
        # accept-redraw use): every race guard was dead in production.
        _ev_iso = goal_dict.get("event_date")
        try:
            _target_date = (date.fromisoformat(_ev_iso[:10])
                            if isinstance(_ev_iso, str) and _ev_iso else None)
        except (TypeError, ValueError):
            _target_date = None
        reforecast_goal = Goal(
            goal_type=goal_dict.get("type", goal_dict.get("goal_type", "general")),
            target_date=_target_date,
            event_name=goal_dict.get("event_name", "") or "",
            event_km=goal_dict.get("event_km", 0) or 0,
            # persisted as "event_climb" (api_plan_generate), tolerate both
            event_climb_m=(goal_dict.get("event_climb",
                           goal_dict.get("event_climb_m", 0)) or 0),
            event_type=goal_dict.get("event_type", "granfondo") or "granfondo",
            events=_target_events_from_dicts(goal_dict.get("events")),
            hours_per_week=goal_dict.get("hours_per_week", 8.0),
            rest_days=goal_dict.get("rest_days", [0]),
            available_days=goal_dict.get("available_days") or [
                d for d in range(7) if d not in goal_dict.get("rest_days", [0])
            ],
        )
    except Exception:  # noqa: BLE001
        reforecast_goal = Goal(goal_type="general", hours_per_week=8.0)

    # v1.6.1 — wrap each major step so a failure carries which step in ctx.
    try:
        pw_list = _plan_dict_to_planned_weeks(plan_dict)
    except Exception as _e:
        _tp_log_error(error_codes.Codes.REFORECAST_DICT_FAILED, exc=_e,
                      step="dict_to_planned_weeks")
        raise

    try:
        _, reforecast_info = reforecast(
            reforecast_goal, pw_list,
            tsb_series=tsb_series,
            recent_activities=recent_activities,
            actual_polarization=actual_polarization,
            target_polarization=target_polarization,
            availability_overrides=availability_overrides,
            wprime_balance_24h=wprime_balance_24h,
            w_prime=w_prime,
            wprime_acwr=wprime_acwr,
            actual_wprime_polarization=actual_wprime_polarization,
            yesterday_dfa_alpha1=yesterday_dfa_alpha1,
            accept_redraw_fast=accept_redraw_fast,
        )
    except Exception as _e:
        _tp_log_error(error_codes.Codes.REFORECAST_DICT_FAILED, exc=_e,
                      step="reforecast")
        raise

    if propagation_days is not None:
        touched = set(propagation_days)
    else:
        touched = set(reforecast_info.get("touched_days") or [])
        touched |= set(reforecast_info.get("g3_dropped_days") or [])
    try:
        sessions_modified = _apply_reforecast_to_dict(plan_dict, pw_list, touched)
    except Exception as _e:
        _tp_log_error(error_codes.Codes.REFORECAST_DICT_FAILED, exc=_e,
                      step="apply_reforecast_to_dict")
        raise
    return plan_dict, sessions_modified, reforecast_info


# ══════════════════════════════════════════════════════════════════════════════
# DYNAMIC PLAN REGENERATION — Mujika 2000/2001/2003, Gabbett 2016
# ══════════════════════════════════════════════════════════════════════════════

def detect_plan_gaps(
    plan_weeks: list[PlannedWeek],
    activities: list[dict],
    current_ctl: float,
) -> dict:
    """Tiered absence detection (Mujika 2000).

    >=80% TSS = normal
    50-79% = reduced (log only)
    20-49% = substantially missed (regen after 2+ consecutive)
    <20% = missed (regen recommended)
    """
    today = date.today()
    today_str = today.isoformat()

    # Sum actual TSS per plan week
    actual_by_week = {}
    for a in activities:
        a_date = a.get("date") or a.get("start_date_local", "")[:10] or ""
        a_tss = a.get("tss") or a.get("icu_training_load") or 0
        for w in plan_weeks:
            if w.start.isoformat() <= a_date <= w.end.isoformat():
                actual_by_week[w.week_num] = actual_by_week.get(w.week_num, 0) + a_tss
                break

    gap_weeks = []
    consecutive_missed = 0
    max_consecutive = 0

    for w in plan_weeks:
        if w.end.isoformat() >= today_str:
            break  # only check past weeks
        actual = actual_by_week.get(w.week_num, 0)
        planned = w.tss_target
        if not planned or planned <= 0:
            continue  # skip rest/recovery weeks with 0 planned TSS
        ratio = actual / planned

        if ratio < 0.20:
            status = "missed"
            consecutive_missed += 1
        elif ratio < 0.50:
            status = "substantially_missed"
            consecutive_missed += 1
        elif ratio < 0.80:
            status = "reduced"
            consecutive_missed = 0
        else:
            status = "normal"
            consecutive_missed = 0

        max_consecutive = max(max_consecutive, consecutive_missed)

        if status in ("missed", "substantially_missed"):
            gap_weeks.append({
                "week_num": w.week_num,
                "phase": w.phase,
                "planned_tss": round(planned),
                "actual_tss": round(actual),
                "ratio": round(ratio, 2),
                "status": status,
            })

    # Calculate absence in days (consecutive missed weeks × 7)
    absence_days = max_consecutive * 7

    # Expected CTL from plan progression
    past_weeks_count = sum(1 for w in plan_weeks if w.end.isoformat() < today_str)
    expected_weekly_avg = sum(w.tss_target for w in plan_weeks[:past_weeks_count]) / max(past_weeks_count, 1)
    expected_ctl = expected_weekly_avg / 7  # rough CTL estimate

    return {
        "gap_weeks": gap_weeks,
        "missed_count": len(gap_weeks),
        "consecutive_missed": max_consecutive,
        "absence_days": absence_days,
        "current_ctl": round(current_ctl, 1),
        "expected_ctl": round(expected_ctl, 1),
        "ctl_gap": round(expected_ctl - current_ctl, 1),
        "needs_regeneration": max_consecutive >= 2 or (expected_ctl - current_ctl) > 15,
    }


def build_recovery_ramp(
    current_ctl: float,
    absence_days: int,
    goal: "Goal",
) -> list[PlannedWeek]:
    """Duration-dependent recovery ramp with ACWR < 1.3 guardrail (Gabbett 2016).

    1 week off  → 3 weeks at 75/85/95%
    3-4 weeks off → 5 weeks at 50/60/70/80/90%
    5+ weeks off → 6 weeks at 40/50/60/70/80/90%

    Percentages relative to DECAYED CTL maintenance TSS (not pre-absence).
    First 1-2 weeks Z2-only reconditioning (Mujika 2001).
    """
    if absence_days < 7:
        return []

    maintenance_tss = max(current_ctl * 7, 70)  # minimum 10 TSS/day for very low CTL

    # Duration-dependent ramp percentages
    if absence_days <= 14:
        ramp_pcts = [0.75, 0.85, 0.95]
        z2_only_weeks = 1
    elif absence_days <= 28:
        ramp_pcts = [0.50, 0.60, 0.70, 0.80, 0.90]
        z2_only_weeks = 2
    else:
        ramp_pcts = [0.40, 0.50, 0.60, 0.70, 0.80, 0.90]
        z2_only_weeks = 2

    recovery_weeks = []
    start = date.today()
    rest_days = goal.rest_days if goal else [0]
    chronic_tss = maintenance_tss  # rolling chronic load tracker

    for i, pct in enumerate(ramp_pcts):
        week_start = start + timedelta(weeks=i)
        week_tss = maintenance_tss * pct

        # ACWR guardrail: cap at 1.3× rolling chronic load (Gabbett 2016)
        max_safe_tss = chronic_tss * 1.3
        week_tss = min(week_tss, max_safe_tss)
        # Update chronic load (simple rolling average approximation)
        chronic_tss = chronic_tss * 0.75 + week_tss * 0.25

        is_z2_only = (i < z2_only_weeks)
        phase_name = "recon" if is_z2_only else "recovery_ramp"

        sessions = []
        for d in range(7):
            day_date = week_start + timedelta(days=d)
            weekday = day_date.weekday()  # 0=Mon..6=Sun (from actual date)

            if weekday in rest_days:
                sessions.append(PlannedSession(
                    day=day_date, day_name=day_date.strftime("%a"),
                    session_type="rest", duration_min=0, tss_estimate=0,
                    description="Rest day",
                ))
                continue

            is_weekend = weekday >= 5
            max_dur = (goal.max_weekend_hours if is_weekend else goal.max_weekday_hours) * 60 if goal else 120

            if is_z2_only:
                # Pure Z2 reconditioning (Mujika 2001)
                dur = min(int(max_dur), 90)
                tss = round(dur / 60 * TSS_PER_HOUR["z2"])
                sessions.append(PlannedSession(
                    day=day_date, day_name=day_date.strftime("%a"),
                    session_type="z2", duration_min=dur, tss_estimate=tss,
                    description=f"Recovery Z2: {dur}min. Reconditioning — low intensity only.",
                ))
            else:
                # Ramp week: mostly Z2 + 1 tempo/SS allowed
                dur = min(int(max_dur), 75)
                tss = round(dur / 60 * TSS_PER_HOUR["z2"])
                sessions.append(PlannedSession(
                    day=day_date, day_name=day_date.strftime("%a"),
                    session_type="z2", duration_min=dur, tss_estimate=tss,
                    description=f"Recovery ride: {dur}min Z2. Ramp week {i+1}.",
                ))

        recovery_weeks.append(PlannedWeek(
            week_num=900 + i,  # temporary numbering, fixed during assembly
            start=week_start,
            end=week_start + timedelta(days=6),
            phase=phase_name,
            tss_target=round(week_tss),
            is_stepback=False,
            sessions=sessions,
        ))

    return recovery_weeks


def regenerate_from_today(
    goal: Goal,
    old_plan_weeks: list[PlannedWeek],
    current_ctl: float,
    unavailable_periods: list[dict] | None = None,
    activities: list[dict] | None = None,
    seed_salt: int = 0,
    athlete: dict | None = None,
) -> tuple[list, list[PlannedWeek], dict]:
    """Regenerate plan from today, preserving past weeks.

    Returns (new_phases, all_weeks, regen_info).

    Science:
    - Mujika 2001: Z2 reconditioning after 2+ weeks off
    - Mujika 2003: taper can compress to 8 days (range 8-14)
    - Gabbett 2016: ACWR < 1.3 during recovery ramp
    - Gundersen 2016: muscle memory = faster reconditioning for trained athletes
    """
    today = date.today()

    # FC5d (v2.5.0, L3-6): route the regen through the same generator invariant
    # as generate_plan (F4b) — a stale PAST target used to rebuild a recovery
    # ramp plowing z2 weeks through and 5 weeks past race day, plus a
    # negative-span taper phase, with no error. app.py surfaces this as a 400
    # on /api/plan/regenerate and /api/plan/add-race; the auto-adapt path
    # skips the regen tier for passed events.
    if goal.target_date is not None and goal.target_date < today:
        raise ValueError(
            f"Event date {goal.target_date.isoformat()} has passed — set a "
            "new goal (or target date) before rebuilding the plan."
        )

    # 1. Keep past weeks
    past_weeks = [w for w in old_plan_weeks if w.end < today]

    # 1b. Gather any adapted / user-moved / status-tracked sessions from the
    # CURRENT week of the old plan (fix26 §6.12).
    # §6.12: Plan regen preserves:
    #   - user_moved=True        — user's explicit reschedule, never re-prescribe
    #   - completion_matches[]   — persisted done/ambiguous matches
    #   - dismissed_at           — user dismissed (stays greyed, not re-added)
    #   - past-week statuses     — done/missed etc
    #   - status != "pending"    — anything already classified stays
    # Only un-executed + future pending sessions are re-prescribed.
    # v1.8.20 (grill B2) — gather preserved sessions from the CURRENT *and all
    # FUTURE* weeks, not just the current week. Future weeks are rebuilt from
    # scratch below, so a session the user DISMISSED on a future calendar cell
    # (reachable: dismiss-session iterates all weeks, redraw allows future) was
    # silently re-prescribed on every regen. Past weeks are kept verbatim
    # separately, so we only need current + future here. The swap + skip-guards
    # downstream already key on ``s.day`` and honour the same predicate, so
    # populating this dict for future dates is sufficient.
    adapted_current_week: dict[date, PlannedSession] = {}
    for w in old_plan_weeks:
        if w.end >= today:  # current + future weeks
            for s in w.sessions:
                preserve = (
                    getattr(s, "adapted", False)
                    or getattr(s, "user_moved", False)
                    or getattr(s, "status", "pending") != "pending"
                    or getattr(s, "dismissed_at", "")
                    or getattr(s, "completion_matches", None)
                )
                if preserve:
                    adapted_current_week[s.day] = s

    # 2. Detect absence
    gaps = detect_plan_gaps(old_plan_weeks, activities or [], current_ctl)
    absence_days = gaps["absence_days"]

    # 3. Calculate remaining time
    if goal.target_date:
        remaining_days = (goal.target_date - today).days
    else:
        total_plan_days = sum((w.end - w.start).days + 1 for w in old_plan_weeks)
        elapsed_days = (today - old_plan_weeks[0].start).days if old_plan_weeks else 0
        remaining_days = max(7, total_plan_days - elapsed_days)

    # 4. Build recovery ramp if needed
    recovery_weeks = build_recovery_ramp(current_ctl, absence_days, goal)

    # FC5d (v2.5.0, L3-6): the ramp must never plow training weeks past the
    # event — cap it at race + 2 weeks (post-race recovery riding is fine; a
    # multi-week z2 block THROUGH and past race day is not). Drop whole ramp
    # weeks starting past the cap and clip the last one's sessions to it.
    if goal.target_date:
        _ramp_cap = goal.target_date + timedelta(days=14)
        recovery_weeks = [rw for rw in recovery_weeks if rw.start <= _ramp_cap]
        for rw in recovery_weeks:
            if rw.end > _ramp_cap:
                rw.sessions = [s for s in rw.sessions if s.day <= _ramp_cap]
                rw.end = _ramp_cap

    # FC5d (v2.5.0, L3-6): the §6.12 preserved-field swap applies to RAMP weeks
    # too — the recovery ramp bypassed the plan_week swap loop below, so
    # user_moved / dismissed / done sessions whose dates fell inside the ramp
    # span were silently re-prescribed as generic z2 on every gap-regen.
    if adapted_current_week:
        for rw in recovery_weeks:
            for _i, _s in enumerate(rw.sessions):
                _kept = adapted_current_week.get(_s.day)
                if _kept is not None:
                    rw.sessions[_i] = _kept

    recovery_days = (
        ((recovery_weeks[-1].end - today).days + 1) if recovery_weeks else 0
    )

    # 5. Remaining time after recovery
    post_recovery_days = remaining_days - recovery_days
    post_recovery_weeks = max(1, post_recovery_days // 7)

    # 6. Adjust taper if under time pressure (Mujika 2003: min 8 days)
    original_taper = TAPER_DAYS
    if post_recovery_weeks < 6 and goal.target_date:
        adjusted_taper = 8  # compress from 12 to 8
    else:
        adjusted_taper = original_taper

    # 7. Calculate achievable target CTL
    # Start CTL after recovery ramp
    post_recovery_ctl = current_ctl
    for rw in recovery_weeks:
        daily_tss = rw.tss_target / 7
        for _ in range(7):
            post_recovery_ctl += (daily_tss - post_recovery_ctl) / 42.0

    # v1.11.0 IMPL-EVENT — event demand → plan targets (None for non-event goals
    # or missing athlete → all event wiring no-ops, non-event regen unchanged).
    # Computed from the post-recovery CTL so it matches the phase generator below.
    # Uses `goal` (not the not-yet-built adjusted_goal): the demand model only
    # reads event_km/climb/type/target_date + athlete, which adjusted_goal copies
    # verbatim (it differs from goal only in target_ctl).
    event_targets = _event_demand_targets(
        goal, athlete, {"current_ctl": post_recovery_ctl})

    build_weeks = post_recovery_weeks - max(1, adjusted_taper // 7)
    max_achievable = post_recovery_ctl + safe_ramp_rate(post_recovery_ctl) * build_weeks

    original_target = target_ctl_for_event(
        goal, difficulty=(event_targets or {}).get("difficulty")
    ) if goal.goal_type == "event" else None
    adjusted_target = min(original_target, max_achievable) if original_target else max_achievable

    # 8. Build unavailable date set
    unavailable_dates = set()
    for period in (unavailable_periods or []):
        try:
            d = date.fromisoformat(period["start"])
            end = date.fromisoformat(period["end"])
            while d <= end:
                unavailable_dates.add(d)
                d += timedelta(days=1)
        except (ValueError, KeyError):
            pass

    # 9. Create adjusted goal
    adjusted_goal = Goal(
        goal_type=goal.goal_type,
        target_date=goal.target_date,
        event_name=goal.event_name,
        event_km=goal.event_km,
        event_climb_m=goal.event_climb_m,
        event_type=goal.event_type,
        target_ftp=goal.target_ftp,
        target_ctl=adjusted_target,
        target_distance_km=goal.target_distance_km,
        target_duration_h=goal.target_duration_h,
        target_weight_kg=goal.target_weight_kg,
        hours_per_week=goal.hours_per_week,
        max_weekday_hours=goal.max_weekday_hours,
        max_weekend_hours=goal.max_weekend_hours,
        available_days=goal.available_days,
        rest_days=goal.rest_days,
        daily_max_hours=goal.daily_max_hours,
        plan_weeks=goal.plan_weeks,
        # F1 (v2.1/B6): carry the user's intensity choices through recalc so a
        # block / non-polarized plan doesn't silently revert on adaptation.
        distribution=goal.distribution,
        custom_bands=goal.custom_bands,  # v2.3.0: carry custom split through recalc
        block_periodization=goal.block_periodization,
        events=goal.events,  # F7: carry B/C events through recalc
        # FS1: carry the construction mode so a fixed_core/template plan stays
        # fixed on regenerate (else adjusted_goal defaults to "auto" and the
        # sampler reshuffles the build weeks back to mixed HIT).
        plan_mode=getattr(goal, "plan_mode", "auto"),
        template_id=getattr(goal, "template_id", "") or "",
    )

    # 10. Generate new phases — offset start by recovery duration to avoid overlap
    phase_start_date = today + timedelta(days=recovery_days)
    # Temporarily adjust goal so phases start after recovery
    adjusted_goal._phase_start_override = phase_start_date
    new_phases = generate_phases(adjusted_goal, post_recovery_ctl, event_targets)
    # Clamp all phase start dates to be after recovery
    for p in new_phases:
        if p.start < phase_start_date:
            p.start = phase_start_date
    # FC5d (v2.5.0, L3-6): the clamp above can push a phase's start past its
    # end (e.g. a capped ramp consuming the whole runway to the target) —
    # negative-span phases must be impossible on every emitter path.
    new_phases = [p for p in new_phases if p.start <= p.end]

    # 11. Generate new weeks
    library = load_workout_library()
    pool_index = _build_pool_indexes(library)  # v4.5.0 IMPL-PLANNER
    new_weeks = []
    used_names_dict: dict[str, int] = {}
    used_names_set: set = set()
    week_num = len(past_weeks) + len(recovery_weeks) + 1
    # Seed cross-week 48h HIT-gap (PL2) with the Sunday of whichever prior
    # week is most recent: last recovery week → last past week → None.
    prev_week_sessions: list | None = None
    if recovery_weeks:
        prev_week_sessions = recovery_weeks[-1].sessions
    elif past_weeks:
        prev_week_sessions = past_weeks[-1].sessions

    # v4.5.0 Layer 3 rolling 4-week HIT-type window per phase.
    recent_hit_by_phase: dict[str, list[str]] = {}
    # v4.5.0 acceptance: novel-tuple bias for ≥30 (cc, quintile) acceptance §4.
    seen_cc_dur_tuples: set = set()
    # v4.6.0 IMPL-PLANNER-UTILIZATION (Pillar B): plan-wide bookkeeping.
    plan_pick_counts: dict[str, int] = {}
    class_session_counts: dict[str, int] = {}
    class_distinct_files: dict[str, set] = {}
    # FC1-CLIP (v2.5.0): span-derived (== emitted row count; see generate_plan).
    plan_total_weeks_rg = sum(_span_weeks(p) for p in new_phases) if new_phases else 0

    for phase in new_phases:
        cursor = max(phase.start, today + timedelta(days=recovery_days))
        phase_week = 0
        week_in_phase = 0  # v4.5.0 Layer 2: 0-indexed within phase
        while cursor <= phase.end:
            phase_week += 1
            is_stepback = (phase_week % STEP_BACK_EVERY == 0) and phase.name != "taper"
            pw = plan_week(week_num, cursor, phase, adjusted_goal, is_stepback,
                           prev_week_sessions=prev_week_sessions,
                           seed_salt=seed_salt)

            # Mark unavailable days as REST
            for s in pw.sessions:
                if s.day in unavailable_dates:
                    s.session_type = "rest"
                    s.duration_min = 0
                    s.tss_estimate = 0
                    s.description = "Unavailable (planned leave/injury)"

            # Preserve daily-adapt edits: swap in the previous (adapted) session
            # for any slot whose date was already daily-adapted. This must happen
            # BEFORE match_zwo so we don't rewrite zwo_file on top of the kept one.
            if adapted_current_week:
                for i, s in enumerate(pw.sessions):
                    kept = adapted_current_week.get(s.day)
                    if kept is not None:
                        pw.sessions[i] = kept

            # v4.6.0: rolling-eviction window 12 weeks (was 24).
            stale = [n for n, wk in used_names_dict.items()
                     if week_num - wk >= _USED_NAMES_ROLLING_WEEKS]
            for n in stale:
                used_names_dict.pop(n, None)
                used_names_set.discard(n)

            # v4.5.0 IMPL-PLANNER: sampler-driven workout selection per week.
            budget = get_budget_for_phase(phase.name)
            phase_rot = recent_hit_by_phase.setdefault(phase.name, [])
            # v1.11.0 (P4) — climbing specificity ONLY in build2/peak (mirrors
            # generate_plan's _emph). None elsewhere / for non-event regens.
            _emph = ("event_climb"
                     if (event_targets and event_targets.get("climbing_bias")
                         and phase.name in ("build2", "peak"))
                     else None)
            # F1 (v2.1/B6): keep blocks on the recalc path — recompute focus from
            # the (adjusted) goal + phase so a recalc'd block plan stays blocked.
            # None unless goal.block_periodization is on (default-off parity).
            # FS1 — keep a fixed plan FIXED on "update plan": blueprint modes
            # re-expand deterministically here too (else regenerate would reshuffle
            # via the sampler). auto path unchanged.
            _bp_mode = getattr(adjusted_goal, "plan_mode", "auto") in ("fixed_core", "template")
            block_focus = None if _bp_mode else _block_focus_for(phase.name, adjusted_goal, is_stepback)
            pw.block_focus = block_focus
            if _bp_mode:
                sampled = expand_blueprint_week(
                    phase=phase, budget=budget, week_num=week_num, week_start=cursor,
                    available_days=adjusted_goal.available_days,
                    rest_days=adjusted_goal.rest_days,
                    daily_max_hours=adjusted_goal.daily_max_hours,
                    max_weekday_hours=adjusted_goal.max_weekday_hours,
                    max_weekend_hours=adjusted_goal.max_weekend_hours,
                    is_stepback=is_stepback, week_in_phase=week_in_phase,
                    goal=adjusted_goal,
                )
            else:
                sampled = sample_week_workouts(
                    phase=phase, budget=budget, library=library,
                    used_names=used_names_dict,
                    week_num=week_num, seed_salt=seed_salt,
                    week_start=cursor,
                    available_days=adjusted_goal.available_days,
                    rest_days=adjusted_goal.rest_days,
                    daily_max_hours=adjusted_goal.daily_max_hours,
                    max_weekday_hours=adjusted_goal.max_weekday_hours,
                    max_weekend_hours=adjusted_goal.max_weekend_hours,
                    is_stepback=is_stepback,
                    pool_index=pool_index,
                    week_in_phase=week_in_phase,
                    recent_hit_types=phase_rot,
                    seen_cc_dur_tuples=seen_cc_dur_tuples,
                    plan_pick_counts=plan_pick_counts,
                    class_session_counts=class_session_counts,
                    class_distinct_files=class_distinct_files,
                    plan_total_weeks=plan_total_weeks_rg,
                    goal_type=getattr(adjusted_goal, "goal_type", "general"),
                    emphasis_profile=_emph,
                    block_focus=block_focus,
                )
            if len(phase_rot) > 12:
                del phase_rot[: len(phase_rot) - 12]
            for nm in used_names_dict:
                used_names_set.add(nm)

            # Replace pw.sessions with sampled, but PRESERVE
            #   - ftp_test slots (sampler doesn't pick them)
            #   - adapted / user_moved / non-pending sessions (§6.12 contract)
            #   - unavailable days (already converted to rest above)
            for off, legacy_s in enumerate(pw.sessions):
                if getattr(legacy_s, "adapted", False) or getattr(legacy_s, "user_moved", False):
                    continue
                if getattr(legacy_s, "status", "pending") != "pending":
                    continue
                if getattr(legacy_s, "session_type", "") == "ftp_test":
                    continue
                if getattr(legacy_s, "day", None) in unavailable_dates:
                    continue
                if 0 <= off < len(sampled) and sampled[off] is not None:
                    pw.sessions[off] = sampled[off]

            # Fallback match_zwo for any unfilled slot.
            _anchor = phase_start_date if new_phases else today
            for day_idx, s in enumerate(pw.sessions):
                if getattr(s, "adapted", False) or getattr(s, "user_moved", False):
                    continue
                if getattr(s, "status", "pending") != "pending":
                    continue
                if s.session_type in ("rest", "ftp_test"):
                    continue
                if getattr(s, "zwo_file", ""):
                    continue
                before = len(used_names_set)
                match_zwo(s, library, week_num=week_num, day_idx=day_idx,
                          used_names=used_names_set, plan_start_date=_anchor,
                          seed_salt=seed_salt)
                if len(used_names_set) > before:
                    for n in used_names_set - set(used_names_dict.keys()):
                        used_names_dict[n] = week_num

            # FC1-CLIP (v2.5.0): never spill past the phase end (D2/D3).
            _clip_week_to_phase(pw, phase, cursor)
            new_weeks.append(pw)
            prev_week_sessions = pw.sessions  # feed into next plan_week (PL2)
            cursor += timedelta(weeks=1)
            week_num += 1
            week_in_phase += 1

    # v1.11.0 IMPL-EVENT (P1, the lever) — event long-ride progression over the
    # rebuilt future weeks. Mirrors generate_plan's FINAL pass: grow the weekend
    # long ride toward 0.8× event duration (+25 min/wk from current longest),
    # capped at weekend hours, ×0.72 on stepback, STOPPING ≥3 weeks out so the
    # taper owns the long ride. No-op for non-event regens (event_targets None).
    # Applied AFTER all session-duration / re-match passes; regenerate_from_today
    # has no authoritative availability clamp, so this runs just before assembly.
    if event_targets is not None:
        _mw_min = int(round((goal.max_weekend_hours or 0) * 60))
        # v2.0.3 F7: generate_plan ramps _wi over the FULL plan; here new_weeks
        # is future-only, so offset _wi by the already-elapsed weeks (past +
        # recovery, the same count week_num is seeded from above) — otherwise a
        # mid-plan regen resets the long ride to long_start_h instead of
        # continuing from where the athlete already is.
        _elapsed_weeks = len(past_weeks) + len(recovery_weeks)
        for _wi, _w in enumerate(new_weeks):
            if getattr(_w, "phase", "") == "taper":
                continue
            _wstart = getattr(_w, "start", None)
            if _wstart and goal.target_date and (goal.target_date - _wstart).days <= 21:
                continue
            _lr_h = min(event_targets["long_target_h"],
                        event_targets["long_start_h"]
                        + LONG_RIDE_STEP_MIN / 60.0 * (_elapsed_weeks + _wi))
            _apply_long_ride_target(
                _w.sessions,
                target_min=int(round(_lr_h * 60)),
                max_weekend_min=_mw_min,
                is_stepback=getattr(_w, "is_stepback", False))

    # L3-13 (v2.5.0): the regen path runs the SAME volume ceiling as
    # generate_plan (it had neither clamp — the comeback ramp, the one place
    # overload matters most, was the least-clamped output in the system).
    # recent_weekly_tss mirrors generate_plan's fetch: archive → CTL×7 proxy.
    _recent_wtss = None
    try:
        import ride_storage as _rs
        _recent_wtss = _rs.recent_mean_weekly_tss()
    except Exception:  # noqa: BLE001
        _recent_wtss = None
    if _recent_wtss is None and current_ctl and current_ctl > 0:
        _recent_wtss = round(current_ctl * 7)
    _future_weeks = recovery_weeks + new_weeks
    _enforce_weekly_volume_ceiling(_future_weeks, recent_weekly_tss=_recent_wtss,
                                   goal=adjusted_goal)

    # Renumber recovery weeks
    for i, rw in enumerate(recovery_weeks):
        rw.week_num = len(past_weeks) + i + 1

    all_weeks = past_weeks + recovery_weeks + new_weeks

    regen_info = {
        "absence_days": absence_days,
        "recovery_ramp_weeks": len(recovery_weeks),
        "original_target_ctl": round(original_target) if original_target else None,
        "adjusted_target_ctl": round(adjusted_target),
        "current_ctl": round(current_ctl, 1),
        "post_recovery_ctl": round(post_recovery_ctl, 1),
        "taper_days": adjusted_taper,
        "taper_compressed": adjusted_taper < original_taper,
        "remaining_build_weeks": build_weeks,
        "gaps": gaps,
    }

    # B2 (v2.1.0): re-assert the F4 event-eve taper on this adaptation path too —
    # generate_plan applies it at first build, but regenerate/reforecast/refit
    # did not, so a hard session could resurface within EVENT_EVE_EASY_DAYS of
    # the event after a recalc. No-op for non-event goals (target_date None).
    _enforce_event_taper_eve(all_weeks, goal.target_date)
    _apply_secondary_event_tapers(all_weeks, goal)  # F7: B/C mini-tapers
    # F2b (v2.5.0): a regen rebuilds the future weeks from scratch, so the
    # race-week openers/composition must be re-applied here too (E8 survival).
    if goal.goal_type in ("event", "ctl") and goal.target_date:
        _apply_race_week_shape(all_weeks, goal, library)
    _mark_race_days(all_weeks, goal)  # issue #7: race day shows the race, not a session

    # L3-13 (v2.5.0): AUTHORITATIVE per-day availability clamp, mirroring
    # generate_plan's final pass (per-weekday goal cap + per-type ceiling +
    # stepback long-ride cap). Future weeks only; race entries are sized by
    # _mark_race_days alone (FC3) and preserved rider state (§6.12) is never
    # rescaled.
    for _w in _future_weeks:
        for _s in _w.sessions:
            if _s.session_type == "rest" or (_s.duration_min or 0) <= 0:
                continue
            if _protect_race(_s):
                continue
            if (getattr(_s, "user_moved", False)
                    or getattr(_s, "dismissed_at", "")
                    or getattr(_s, "status", "pending") != "pending"):
                continue
            _wd = _s.day.weekday() if hasattr(_s.day, "weekday") else 0
            _cap_min = int(adjusted_goal.max_hours_for_day(_wd) * 60)
            _cc = _content_class_for_zwo(getattr(_s, "zwo_file", "") or "")
            _ceil = TYPE_CEILING.get(_cc) or TYPE_CEILING.get(_s.session_type)
            _eff = _cap_min if _ceil is None else (
                _ceil if _cap_min <= 0 else min(_cap_min, _ceil))
            if (getattr(_w, "is_stepback", False)
                    and (_eff <= 0 or _eff > STEPBACK_LONG_RIDE_CAP_MIN)):
                _eff = STEPBACK_LONG_RIDE_CAP_MIN
            if _eff > 0 and _s.duration_min > _eff:
                _scale = _eff / float(_s.duration_min)
                _s.tss_estimate = round((_s.tss_estimate or 0) * _scale)
                _s.duration_min = _eff

    # FC2a parity: re-anchor the taper budget on the FINAL (post-clamp) build
    # sums — a strict no-op when the rebuilt span holds no taper rows.
    _enforce_weekly_volume_ceiling(_future_weeks, recent_weekly_tss=_recent_wtss,
                                   goal=adjusted_goal, taper_only=True)
    return new_phases, all_weeks, regen_info


# ══════════════════════════════════════════════════════════════════════════════
# ROLLING PLAN RECALCULATION — Kiviniemi 2007, Javaloyes 2018/2019
# ══════════════════════════════════════════════════════════════════════════════

def compute_event_readiness(goal: Goal, current_ctl: float) -> dict:
    """Compute event readiness status and optimal strategy.

    CTL targets: granfondo 85-100, century 70-90, ultra 110-130.
    Peak CTL should be 2-3 weeks before event (Friel, TrainingPeaks).
    """
    if not goal.target_date:
        return {"status": "no_event", "weeks_remaining": None}

    today = date.today()
    remaining_days = (goal.target_date - today).days
    if remaining_days < 0:
        return {"status": "event_passed", "weeks_remaining": 0, "days_remaining": remaining_days,
                "target_ctl": 0, "current_ctl": round(current_ctl, 1), "pct_of_target": 100,
                "gap": 0, "taper_action": "none", "taper_days": 0, "safe_ramp": 0,
                "needed_ramp": 0, "ramp_feasible": True, "projected_peak_ctl": current_ctl,
                "projected_event_ctl": current_ctl, "event_name": goal.event_name,
                "event_date": goal.target_date.isoformat() if goal.target_date else None}
    weeks_remaining = max(0, remaining_days // 7)

    target = target_ctl_for_event(goal) if goal.goal_type == "event" else (
        current_ctl + safe_ramp_rate(current_ctl) * min(weeks_remaining, 12)
    )

    gap = target - current_ctl
    safe_ramp = safe_ramp_rate(current_ctl)
    taper_weeks = max(1, -(-TAPER_DAYS // 7))  # ceil division
    build_weeks = max(0, weeks_remaining - taper_weeks)

    # Project CTL at event using forecast
    daily_tss_avg = (current_ctl + safe_ramp) * 1.0  # rough daily TSS for ramp
    projected_peak = current_ctl + safe_ramp * build_weeks
    projected_event_ctl = projected_peak * 0.92  # ~8% loss during taper

    # Relative deviation
    pct_of_target = (current_ctl / target * 100) if target > 0 else 100

    if pct_of_target >= 90:
        status = "on_track"
    elif pct_of_target >= 75:
        status = "at_risk"
    elif pct_of_target >= 60:
        status = "behind"
    else:
        status = "undertrained"

    # CTL-dependent taper decision (Bosquet 2007, Mujika 2003)
    if weeks_remaining <= 2:
        if pct_of_target >= 90:
            taper_action = "full_taper_12d"
            taper_days = 12
        elif pct_of_target >= 75:
            taper_action = "compressed_taper_8d"
            taper_days = 8
        elif pct_of_target >= 60:
            taper_action = "sharpening_5d"
            taper_days = 5
        else:
            taper_action = "undertrained_warning"
            taper_days = 5
    elif weeks_remaining <= 3:
        taper_action = "begin_taper_next_week"
        taper_days = TAPER_DAYS
    else:
        taper_action = "continue_building"
        taper_days = TAPER_DAYS

    needed_ramp = round(gap / max(build_weeks, 1), 1) if build_weeks > 0 else 0

    return {
        "status": status,
        "target_ctl": round(target, 1),
        "current_ctl": round(current_ctl, 1),
        "pct_of_target": round(pct_of_target, 1),
        "projected_peak_ctl": round(projected_peak, 1),
        "projected_event_ctl": round(projected_event_ctl, 1),
        "gap": round(gap, 1),
        "weeks_remaining": weeks_remaining,
        "days_remaining": remaining_days,
        "taper_action": taper_action,
        "taper_days": taper_days,
        "safe_ramp": safe_ramp,
        "needed_ramp": needed_ramp,
        "ramp_feasible": needed_ramp <= safe_ramp * 1.3,
        "event_name": goal.event_name,
        "event_date": goal.target_date.isoformat() if goal.target_date else None,
    }


def recalculate_plan(
    goal: Goal,
    current_plan_weeks: list[PlannedWeek],
    current_ctl: float,
    recent_activities: list[dict] | None = None,
    current_eftp: float | None = None,
    athlete: dict | None = None,
) -> tuple[list, list[PlannedWeek], dict]:
    """Weekly rolling recalculation of the training plan.

    Runs every 7 days (or on-demand). Adjusts future weeks based on:
    1. Actual CTL vs planned trajectory (relative deviation)
    2. Phase re-timing as event approaches
    3. CTL-dependent taper decision
    4. eFTP drift detection

    Kiviniemi 2007: daily HRV adjustment for intensity
    Javaloyes 2018: HRV-guided > fixed plans (+5-7% outcomes)
    Couzens: 3:1 loading cycles, safe ramp 3-7 CTL/week
    """
    today = date.today()
    today_str = today.isoformat()

    # 1. Keep completed weeks (including current in-progress week)
    past_weeks = [w for w in current_plan_weeks if w.end < today or (w.start <= today <= w.end)]
    future_weeks = [w for w in current_plan_weeks if w.start > today]

    # New phases must start AFTER the current week ends (not mid-week),
    # otherwise we double-cover the current week.
    if past_weeks and past_weeks[-1].start <= today <= past_weeks[-1].end:
        regen_start = past_weeks[-1].end + timedelta(days=1)
    else:
        regen_start = today

    # 2. Compute deviation (relative %)
    event_readiness = compute_event_readiness(goal, current_ctl)
    deviation_pct = 100 - event_readiness["pct_of_target"]

    # 3. Determine if structural adjustment needed
    needs_adjustment = abs(deviation_pct) > 8  # >8% relative deviation

    if not needs_adjustment and future_weeks:
        # Minor deviation — just annotate, don't regenerate
        return ([], current_plan_weeks, {
            "action": "no_change",
            "event_readiness": event_readiness,
            "deviation_pct": round(deviation_pct, 1),
            "eftp_drift": _check_eftp_drift(current_eftp),
        })

    # 4. Significant deviation or approaching event — regenerate future
    weeks_remaining = event_readiness["weeks_remaining"] or len(future_weeks)
    taper_days = event_readiness["taper_days"]

    # Determine if taper should auto-lock
    taper_locked = event_readiness["taper_action"] in ("full_taper_12d", "compressed_taper_8d", "sharpening_5d")

    # 5. Re-generate phases for remaining time
    adjusted_goal = Goal(
        goal_type=goal.goal_type,
        target_date=goal.target_date,
        event_name=goal.event_name,
        event_km=goal.event_km,
        event_climb_m=goal.event_climb_m,
        event_type=goal.event_type,
        target_ftp=goal.target_ftp,
        target_ctl=goal.target_ctl,
        target_distance_km=goal.target_distance_km,
        target_duration_h=goal.target_duration_h,
        target_weight_kg=goal.target_weight_kg,
        hours_per_week=goal.hours_per_week,
        max_weekday_hours=goal.max_weekday_hours,
        max_weekend_hours=goal.max_weekend_hours,
        available_days=goal.available_days,
        rest_days=goal.rest_days,
        daily_max_hours=goal.daily_max_hours,
        plan_weeks=goal.plan_weeks,
        # F1 (v2.1/B6): carry the user's intensity choices through recalc so a
        # block / non-polarized plan doesn't silently revert on adaptation.
        distribution=goal.distribution,
        custom_bands=goal.custom_bands,  # v2.3.0: carry custom split through recalc
        block_periodization=goal.block_periodization,
        events=goal.events,  # F7: carry B/C events through recalc
        # FS1: carry the construction mode so a fixed_core/template plan stays
        # fixed on reforecast (else it defaults to "auto" and reshuffles).
        plan_mode=getattr(goal, "plan_mode", "auto"),
        template_id=getattr(goal, "template_id", "") or "",
    )

    # v1.11.0 IMPL-EVENT — event demand → plan targets so the event CTL nudge
    # survives a rolling recalc (None for non-event goals or missing athlete →
    # no-op, behavior identical to pre-v1.11.0). This legacy _pick_session path
    # has no sampler/long-ride pass, so event_targets only flows into the phase
    # generator (the CTL band). athlete is threaded from the caller; until the
    # app layer passes it, event_targets stays None and nothing changes.
    event_targets = _event_demand_targets(
        goal, athlete, {"current_ctl": current_ctl})

    # If taper locked, force taper phase
    if taper_locked:
        # FC1-CLIP (v2.5.0): the taper ends ON the target date (mirrors
        # generate_phases) — post-clip nothing spills past it, and ending at
        # target-1 would leave race day outside every row (unmarkable).
        taper_phase = Phase(
            name="taper", start=today, end=goal.target_date if goal.target_date else today + timedelta(days=taper_days),
            weeks=max(1, taper_days // 7), focus=f"Taper {taper_days}d — volume -{round((1-0.6)*100)}%, maintain intensity",
            weekly_tss_target=round(current_ctl * 7 * 0.60),
            z2_pct=70, hit_per_week=1,
            session_types=["z2", "threshold", "vo2max", "sprint", "recovery"],
        )
        new_phases = [taper_phase]
    else:
        # Regenerate phases starting AFTER current week (avoids double-cover)
        adjusted_goal._phase_start_override = regen_start
        new_phases = generate_phases(adjusted_goal, current_ctl, event_targets)

    # 6. Generate new weeks
    library = load_workout_library()
    # v2.0.3 F6: route the weekly recalc through the content-aware sampler the
    # same way generate_plan / regenerate_from_today do (was plan_week-only, a
    # legacy skeleton with no mix-emphasis / no over_under floor — so a rolling
    # recalc silently diverged the plan from first generation). Same seed_salt
    # derivation (0, the default generate/regenerate use when the caller passes
    # no salt), same emphasis_profile channel, same event_targets, same
    # plan-wide bookkeeping so diversity caps / novelty carry across weeks.
    pool_index = _build_pool_indexes(library)
    seed_salt = 0
    new_weeks = []
    used_names = set()  # track used workouts for variety
    # Sliding window: track which week each workout was used (no full clear)
    used_in_week: dict[str, int] = {}
    # v2.0.3 F6 sampler bookkeeping (mirrors generate_plan / regenerate).
    used_names_dict: dict[str, int] = {}
    recent_hit_by_phase: dict[str, list[str]] = {}
    seen_cc_dur_tuples: set = set()
    plan_pick_counts: dict[str, int] = {}
    class_session_counts: dict[str, int] = {}
    class_distinct_files: dict[str, set] = {}
    # FC1-CLIP (v2.5.0): span-derived (== emitted row count; see generate_plan).
    plan_total_weeks_rc = sum(_span_weeks(p) for p in new_phases) if new_phases else 0
    week_num = len(past_weeks) + 1
    # Seed cross-week 48h HIT-gap (PL2) with the last past week's sessions.
    prev_week_sessions: list | None = past_weeks[-1].sessions if past_weeks else None

    for phase in new_phases:
        cursor = max(phase.start, regen_start)
        phase_week = 0
        week_in_phase = 0  # v2.0.3 F6: 0-indexed within phase, drives the sampler
        while cursor <= phase.end:
            phase_week += 1
            is_stepback = (phase_week % STEP_BACK_EVERY == 0) and phase.name != "taper"

            # Insert FTP test at phase transitions (every 6-8 weeks)
            ftp_test_week = (week_num > 0 and week_num % 6 == 0 and phase.name != "taper"
                            and not is_stepback)

            pw = plan_week(week_num, cursor, phase, adjusted_goal, is_stepback,
                           prev_week_sessions=prev_week_sessions,
                           seed_salt=seed_salt)

            # Insert FTP test session if due. Runs BEFORE the sampler pass so the
            # ftp_test slot is preserved by the session-replacement skip below.
            if ftp_test_week:
                for s in pw.sessions:
                    if s.session_type in ("sweetspot", "threshold", "vo2max", "overunder"):
                        s.session_type = "ftp_test"
                        s.description = "FTP test — 20min all-out na 10min warmup. Update zones daarna."
                        s.tss_estimate = round(75 / 60 * TSS_PER_HOUR.get("threshold", 90))
                        break

            # Sliding window: remove names used more than 6 weeks ago
            stale = [n for n, wk in used_in_week.items() if week_num - wk >= 6]
            for n in stale:
                used_names.discard(n)
                del used_in_week[n]
            stale_d = [n for n, wk in used_names_dict.items()
                       if week_num - wk >= _USED_NAMES_ROLLING_WEEKS]
            for n in stale_d:
                used_names_dict.pop(n, None)

            # v2.0.3 F6: sampler-driven workout selection (mirrors generate_plan
            # / regenerate_from_today). Replaces the legacy plan_week-only
            # skeleton so mix-emphasis + the over_under hard-floor reach a
            # weekly recalc. Climbing specificity ONLY in build2/peak.
            budget = get_budget_for_phase(phase.name)
            phase_rot = recent_hit_by_phase.setdefault(phase.name, [])
            _emph = ("event_climb"
                     if (event_targets and event_targets.get("climbing_bias")
                         and phase.name in ("build2", "peak"))
                     else None)
            # F1 (v2.1/B6): keep blocks on the recalc path — recompute focus from
            # the (adjusted) goal + phase so a recalc'd block plan stays blocked.
            # None unless goal.block_periodization is on (default-off parity).
            # FS1 — blueprint modes re-expand deterministically on reforecast too
            # (a fixed plan must not reshuffle when the plan is recalc'd).
            _bp_mode = getattr(adjusted_goal, "plan_mode", "auto") in ("fixed_core", "template")
            block_focus = None if _bp_mode else _block_focus_for(phase.name, adjusted_goal, is_stepback)
            pw.block_focus = block_focus
            if _bp_mode:
                sampled = expand_blueprint_week(
                    phase=phase, budget=budget, week_num=week_num, week_start=cursor,
                    available_days=adjusted_goal.available_days,
                    rest_days=adjusted_goal.rest_days,
                    daily_max_hours=adjusted_goal.daily_max_hours,
                    max_weekday_hours=adjusted_goal.max_weekday_hours,
                    max_weekend_hours=adjusted_goal.max_weekend_hours,
                    is_stepback=is_stepback, week_in_phase=week_in_phase,
                    goal=adjusted_goal,
                )
            else:
                sampled = sample_week_workouts(
                phase=phase, budget=budget, library=library,
                used_names=used_names_dict,
                week_num=week_num, seed_salt=seed_salt,
                week_start=cursor,
                available_days=adjusted_goal.available_days,
                rest_days=adjusted_goal.rest_days,
                daily_max_hours=adjusted_goal.daily_max_hours,
                max_weekday_hours=adjusted_goal.max_weekday_hours,
                max_weekend_hours=adjusted_goal.max_weekend_hours,
                is_stepback=is_stepback,
                pool_index=pool_index,
                week_in_phase=week_in_phase,
                recent_hit_types=phase_rot,
                seen_cc_dur_tuples=seen_cc_dur_tuples,
                plan_pick_counts=plan_pick_counts,
                class_session_counts=class_session_counts,
                class_distinct_files=class_distinct_files,
                plan_total_weeks=plan_total_weeks_rc,
                goal_type=getattr(adjusted_goal, "goal_type", "general"),
                emphasis_profile=_emph,
                block_focus=block_focus,
            )
            if len(phase_rot) > 12:
                del phase_rot[: len(phase_rot) - 12]
            for nm in used_names_dict:
                used_names.add(nm)

            # Replace pw.sessions with the sampled set, PRESERVING ftp_test
            # slots (sampler doesn't pick them) and any adapted / user_moved /
            # non-pending session (§6.12 contract — a recalc must not rewrite a
            # workout the athlete already moved or completed).
            for off, legacy_s in enumerate(pw.sessions):
                if getattr(legacy_s, "adapted", False) or getattr(legacy_s, "user_moved", False):
                    continue
                if getattr(legacy_s, "status", "pending") != "pending":
                    continue
                if getattr(legacy_s, "session_type", "") == "ftp_test":
                    continue
                if 0 <= off < len(sampled) and sampled[off] is not None:
                    pw.sessions[off] = sampled[off]

            # Fallback match_zwo for any slot the sampler left unfilled.
            # Anchor seed on the plan start (phase start or regen_start) so
            # re-running on a different day returns the same workout.
            _anchor = new_phases[0].start if new_phases else regen_start
            for day_idx, s in enumerate(pw.sessions):
                if getattr(s, "adapted", False) or getattr(s, "user_moved", False):
                    continue
                if getattr(s, "status", "pending") != "pending":
                    continue
                if s.session_type in ("rest", "recovery", "ftp_test"):
                    continue
                if getattr(s, "zwo_file", ""):
                    continue
                before = len(used_names)
                match_zwo(s, library, week_num=week_num, day_idx=day_idx,
                          used_names=used_names, plan_start_date=_anchor,
                          seed_salt=seed_salt)
                # Track when each workout was assigned
                if len(used_names) > before:
                    new_names = used_names - set(used_in_week.keys())
                    for n in new_names:
                        used_in_week[n] = week_num
                        used_names_dict[n] = week_num

            # FC1-CLIP (v2.5.0): never spill past the phase end (D2/D3).
            _clip_week_to_phase(pw, phase, cursor)
            new_weeks.append(pw)
            prev_week_sessions = pw.sessions  # feed into next plan_week (PL2)
            cursor += timedelta(weeks=1)
            week_num += 1
            week_in_phase += 1

    # v2.0.3 F6/F7: event long-ride progression over the rebuilt future weeks,
    # mirroring generate_plan / regenerate_from_today (no-op for non-event
    # recalcs). new_weeks is future-only, so offset _wi by the already-elapsed
    # weeks (past) so the ramp CONTINUES from where the athlete is rather than
    # resetting to long_start_h. Stops ≥3 weeks out so the taper owns the long
    # ride (the <= 21 gate matches both other sites).
    if event_targets is not None:
        _mw_min = int(round((goal.max_weekend_hours or 0) * 60))
        _elapsed_weeks = len(past_weeks)
        for _wi, _w in enumerate(new_weeks):
            if getattr(_w, "phase", "") == "taper":
                continue
            _wstart = getattr(_w, "start", None)
            if _wstart and goal.target_date and (goal.target_date - _wstart).days <= 21:
                continue
            _lr_h = min(event_targets["long_target_h"],
                        event_targets["long_start_h"]
                        + LONG_RIDE_STEP_MIN / 60.0 * (_elapsed_weeks + _wi))
            _apply_long_ride_target(
                _w.sessions,
                target_min=int(round(_lr_h * 60)),
                max_weekend_min=_mw_min,
                is_stepback=getattr(_w, "is_stepback", False))

    all_weeks = past_weeks + new_weeks

    recalc_info = {
        "action": "recalculated",
        "event_readiness": event_readiness,
        "deviation_pct": round(deviation_pct, 1),
        "weeks_regenerated": len(new_weeks),
        "taper_locked": taper_locked,
        "eftp_drift": _check_eftp_drift(current_eftp),
        "recalc_date": today.isoformat(),
    }

    return new_phases, all_weeks, recalc_info


# ══════════════════════════════════════════════════════════════════════════════
# MISSED-HARD WEEK RE-FIT (v2.0.7) — IP_missed_hard_refit.md, decision (b)
# ══════════════════════════════════════════════════════════════════════════════

def _refit_session_frozen(s, today: date) -> bool:
    """True if a session in the current week must NOT be re-fitted.

    Mirrors regenerate_from_today's §6.12 preservation predicate so the refit
    never touches the same things a regen wouldn't: past days, days the user
    moved/edited, anything already classified (done/done_partial/missed/
    dismissed/ambiguous), or sessions with a dismiss timestamp / completion
    matches. A day is "remaining trainable" iff it is NOT frozen, NOT a rest
    slot, and lies on/after ``today``.
    """
    if getattr(s, "day", None) is None or s.day < today:
        return True
    if _protect_race(s):
        return True  # FC3 (v2.5.0): the race entry is immutable to the refit
    if getattr(s, "adapted", False) or getattr(s, "user_moved", False):
        return True
    if getattr(s, "user_swapped", False):
        return True  # v2.3.0: pinned manual type-swap
    if getattr(s, "status", "pending") != "pending":
        return True
    if getattr(s, "dismissed_at", ""):
        return True
    if getattr(s, "completion_matches", None):
        return True
    return False


def refit_remaining_week(
    goal: Goal,
    current_plan_weeks: list[PlannedWeek],
    today: date,
    *,
    seed_salt: int = 0,
    athlete: dict | None = None,
) -> tuple[list[PlannedWeek], dict]:
    """v2.0.7 — re-fit the REMAINING trainable days of the CURRENT week after a
    HARD session was missed, redistributing the missed stimulus within the
    existing safety guards (no catch-up spike). Decision (b) full re-fit.

    Approach (IP §Design): re-sample the WHOLE current week through the shared
    ``sample_week_workouts`` path (so every guard runs — TYPE_CEILING, the
    sprint IF≤0.82 ceiling, the easy-Z345 ceiling, 48h hard-day spacing, the
    per-phase ``hit_count_max`` cap, polarization, availability), then KEEP
    ONLY the picks for remaining trainable days and splice them over the frozen
    past/done/pinned days. Three INLINE safety passes then run on the spliced
    week (no separate ``_enforce_weekly_hit_cap`` call):
      1. 48h hard-day spacing — MISSED hards are excluded (they imposed no
         load), so they neither create nor block a violation; the shorter
         REMAINING member of any too-close pair is demoted (frozen↔frozen pairs
         are left untouched).
      2. weekly HIT cap, counted with missed hards EXCLUDED so a missed hard
         FREES its slot rather than consuming it; only remaining slots demote.
      3. REDISTRIBUTION (promote) — re-owe each freed missed-hard stimulus onto
         the best SAFE remaining day (one that keeps 48h spacing and stays under
         the cap). When no safe slot remains the stimulus is legitimately
         dropped — never forced into a <48h or over-cap placement (no spike).

    Anti-churn: a remaining day is overwritten ONLY when its session_type OR
    duration actually changes; otherwise the existing session (and its already-
    matched zwo_file) is kept verbatim.

    Determinism: ``seed_salt`` is forwarded into the sampler; the caller derives
    it from (plan_start, week_num, sorted missed dates) so the refit is stable
    and the app-layer latch prevents re-rolling.

    Returns ``(all_weeks, refit_info)``. ``all_weeks`` is the SAME list object
    passed in (the current week's PlannedWeek is mutated in place); refit_info
    carries ``{action, week_num, refit_days, missed_dates}``.
    """
    # Locate the current week (the one containing today).
    cur_idx = next(
        (i for i, w in enumerate(current_plan_weeks)
         if w.start <= today <= w.end),
        None,
    )
    no_op = {"action": "no_change", "refit_days": [], "missed_dates": []}
    if cur_idx is None:
        return current_plan_weeks, no_op
    week = current_plan_weeks[cur_idx]

    # Newly-missed HARD sessions in THIS week (union-of-axes hard definition).
    missed_hard = [
        s for s in week.sessions
        if getattr(s, "status", "") == "missed" and _session_is_hit(s)
    ]
    missed_dates = sorted(
        s.day.isoformat() for s in missed_hard if getattr(s, "day", None)
    )
    if not missed_hard:
        return current_plan_weeks, no_op

    # Remaining trainable days (on/after today, not rest, not frozen). If none,
    # the week is bounded out — no-op (never spill into next week).
    remaining_offsets = [
        off for off in range(7)
        if (d := week.start + timedelta(days=off)) >= today
        and off < len(week.sessions)
        and getattr(week.sessions[off], "session_type", "") != "rest"
        and not _refit_session_frozen(week.sessions[off], today)
    ]
    if not remaining_offsets:
        return current_plan_weeks, {**no_op, "missed_dates": missed_dates}

    # L3-12 (v2.5.0): near the race — the final 2 build weeks and the taper —
    # the refit may RE-OWE at most 1.0× the missed dose. The taper-week probe
    # showed a 45min/50-TSS miss re-fitted into a 90min threshold + 119min z2
    # (2.7× the missed TSS) because the count/spacing caps bound intensity,
    # not dose. Snapshot the remaining-days dose BEFORE the splice; the cap
    # below trims the refit-touched days back to (pre + missed).
    _reowe_guard = (
        goal.target_date is not None
        and week.end >= goal.target_date - timedelta(days=TAPER_DAYS + 14)
    )
    _pre_remaining_tss = (
        sum((week.sessions[o].tss_estimate or 0) for o in remaining_offsets)
        if _reowe_guard else 0.0
    )
    _missed_dose = sum((s.tss_estimate or 0) for s in missed_hard)

    # Reconstruct the rolling-diversity context the sampler needs from the
    # PRIOR weeks (mirrors recalculate_plan's bookkeeping: used_names by week,
    # the rolling HIT-type window for this phase, and the plan-wide novelty /
    # pick / class counters). Past weeks feed variety; we only re-sample one.
    library = load_workout_library()
    pool_index = _build_pool_indexes(library)
    used_names_dict: dict[str, int] = {}
    recent_hit_by_phase: dict[str, list[str]] = {}
    seen_cc_dur_tuples: set = set()
    plan_pick_counts: dict[str, int] = {}
    class_session_counts: dict[str, int] = {}
    class_distinct_files: dict[str, set] = {}
    for w in current_plan_weeks[:cur_idx]:
        for s in w.sessions:
            nm = getattr(s, "zwo_name", "") or ""
            if nm:
                used_names_dict[nm] = w.week_num
                plan_pick_counts[nm] = plan_pick_counts.get(nm, 0) + 1
            cc = (_content_class_for_zwo(getattr(s, "zwo_file", "") or "")
                  or getattr(s, "session_type", "") or "")
            if cc and cc not in ("rest", "recovery"):
                class_session_counts[cc] = class_session_counts.get(cc, 0) + 1
                fn = getattr(s, "zwo_file", "") or ""
                if fn:
                    class_distinct_files.setdefault(cc, set()).add(fn)
            if _session_is_hit(s) and w.phase == week.phase:
                hcc = _content_class_for_zwo(getattr(s, "zwo_file", "") or "")
                if hcc:
                    recent_hit_by_phase.setdefault(week.phase, []).append(hcc)
    # Evict names older than the rolling window so they're re-eligible.
    stale = [n for n, wk in used_names_dict.items()
             if week.week_num - wk >= _USED_NAMES_ROLLING_WEEKS]
    for n in stale:
        used_names_dict.pop(n, None)
    phase_rot = recent_hit_by_phase.setdefault(week.phase, [])
    if len(phase_rot) > 12:
        del phase_rot[: len(phase_rot) - 12]

    # Re-sample the FULL week (the sampler only operates Mon..Sun; we splice the
    # remaining days below). Budget is the unmodified per-phase budget: a missed
    # hard day frees its HIT slot, so the sampler re-owes the stimulus into the
    # remaining slots up to hit_count_max — the "credit the stimulus back" lever.
    budget = get_budget_for_phase(week.phase)
    sampled = sample_week_workouts(
        phase=Phase(
            name=week.phase, start=week.start, end=week.end,
            weeks=1, focus="", weekly_tss_target=week.tss_target,
            z2_pct=budget.polarized_target.get("z1z2_pct", 80),
            hit_per_week=budget.hit_count_max,
            session_types=[],
        ),
        budget=budget, library=library,
        used_names=used_names_dict,
        week_num=week.week_num, seed_salt=seed_salt,
        week_start=week.start,
        available_days=goal.available_days,
        rest_days=goal.rest_days,
        daily_max_hours=goal.daily_max_hours,
        max_weekday_hours=goal.max_weekday_hours,
        max_weekend_hours=goal.max_weekend_hours,
        is_stepback=week.is_stepback,
        pool_index=pool_index,
        week_in_phase=0,
        recent_hit_types=phase_rot,
        seen_cc_dur_tuples=seen_cc_dur_tuples,
        plan_pick_counts=plan_pick_counts,
        class_session_counts=class_session_counts,
        class_distinct_files=class_distinct_files,
        plan_total_weeks=len(current_plan_weeks),
        goal_type=getattr(goal, "goal_type", "general"),
        block_focus=_block_focus_for(week.phase, goal, week.is_stepback),  # F1/B6
    )

    # Splice ONLY remaining trainable days, ANTI-CHURN: overwrite a day solely
    # when its session_type OR duration changed (keep the existing zwo_file
    # otherwise so the user doesn't see a workout they already saw shuffle).
    refit_days: list[str] = []
    for off in remaining_offsets:
        new_s = sampled[off] if 0 <= off < len(sampled) else None
        if new_s is None or getattr(new_s, "session_type", "") == "rest":
            continue
        old_s = week.sessions[off]
        if (new_s.session_type == old_s.session_type
                and int(new_s.duration_min) == int(old_s.duration_min)):
            continue  # unchanged → keep existing session + matched file
        week.sessions[off] = new_s
        refit_days.append(new_s.day.isoformat())

    if not refit_days:
        return current_plan_weeks, {**no_op, "missed_dates": missed_dates}

    # FINAL safety passes — guarantee the no-catch-up-spike invariants on the
    # whole (now spliced) week regardless of seed. Frozen past / done / pinned
    # slots are sacrosanct, so BOTH passes may only ever demote a REMAINING
    # trainable day; a demoted slot is swapped for a real endurance/tempo
    # LIBRARY file (mirrors _enforce_weekly_hit_cap's _demote_slot — never
    # relabel a hard .zwo in place). When the only offending hard slots left are
    # frozen, the week stays as-is (some missed stimulus is legitimately dropped,
    # never forced onto / removed from a frozen day).
    remaining_set = set(remaining_offsets)
    cap = get_budget_for_phase(week.phase).hit_count_max

    # A MISSED hard day imposed NO training load (the athlete rested it), so for
    # BOTH 48h spacing AND the weekly HIT cap it must be treated as NOT-hard.
    # DONE and PENDING/remaining hards both count (they impose/intend real load).
    def _eff_hard(s) -> bool:
        return _session_is_hit(s) and getattr(s, "status", "pending") != "missed"

    def _demote_off(off: int) -> None:
        slot = week.sessions[off]
        def _try(new_type: str):
            cand = PlannedSession(
                day=slot.day, day_name=slot.day_name, session_type=new_type,
                duration_min=slot.duration_min,
                tss_estimate=round(slot.duration_min / 60
                                   * TSS_PER_HOUR.get(new_type, 45)),
                description=f"{new_type} ({slot.duration_min}min) — refit demotion",
            )
            m = match_zwo(cand, library)
            return m if (m.zwo_file and not _session_is_hit(m)) else None
        demoted = (_try("tempo") if slot.duration_min >= 60 else None) or _try("z2")
        week.sessions[off] = demoted if demoted is not None else PlannedSession(
            day=slot.day, day_name=slot.day_name, session_type="z2",
            duration_min=slot.duration_min,
            tss_estimate=round(slot.duration_min / 60 * TSS_PER_HOUR["z2"]),
            description="Easy endurance (no suitable workout)",
            zwo_file="", zwo_name="", matched=False,
        )
        iso = slot.day.isoformat()
        if iso not in refit_days:
            refit_days.append(iso)

    # Pass 1 — 48h hard-day spacing across the WHOLE week (the sampler only
    # spaces its OWN picks; splicing them next to a frozen hard day — e.g. a
    # pinned hard — can leave two hard days <48h apart). MISSED hards are
    # excluded (_eff_hard): they imposed no load, so they neither create nor
    # block a spacing violation. SCAN every adjacent pair for the first
    # too-close pair with ≥1 REMAINING (demotable) member and demote the shorter
    # such member; skip frozen↔frozen pairs (pre-existing user state, left
    # untouched) and keep scanning past them so a LATER frozen↔remaining
    # violation is still repaired.
    for _ in range(len(remaining_set) + 1):
        hard_offs = sorted(o for o in range(len(week.sessions))
                           if _eff_hard(week.sessions[o]))
        repaired = False
        for a, b in zip(hard_offs, hard_offs[1:]):
            if b - a >= 2:
                continue
            # Pick the REMAINING member to demote; if both are remaining, drop
            # the shorter. Two frozen hard days <48h (a pre-existing user state)
            # have no remaining member — skip the pair and keep scanning.
            cands = [o for o in (a, b) if o in remaining_set]
            if not cands:
                continue
            _demote_off(min(cands, key=lambda o: week.sessions[o].duration_min))
            repaired = True
            break
        if not repaired:
            break

    # Pass 2 — weekly HIT cap, counted with _eff_hard so a MISSED hard FREES its
    # slot (no load imposed) instead of consuming it. Shed the heaviest (longest)
    # remaining effective-hard slot first until eff-hard ≤ cap. Frozen DONE/
    # PENDING hards still COUNT; only remaining slots are demotable.
    def _eff_hard_count() -> int:
        return sum(1 for s in week.sessions if _eff_hard(s))

    for _ in range(len(remaining_set) + 1):
        if _eff_hard_count() <= cap:
            break
        demotable = [off for off in remaining_set
                     if _eff_hard(week.sessions[off])]
        if not demotable:
            break  # only frozen hard slots remain — leave them
        _demote_off(max(demotable, key=lambda o: week.sessions[o].duration_min))

    # Pass 3 — REDISTRIBUTION (promote). A missed hard freed its slot; the user
    # is fresh, so re-owe that stimulus onto a SAFE remaining day rather than
    # dropping it — but only where it keeps 48h spacing AND stays under the cap
    # (never a catch-up spike). While there's headroom under the cap and an
    # un-replaced missed-hard TYPE remains, promote the best remaining trainable
    # non-hard day to that type. "Best" = the remaining slot maximizing the min
    # gap to every existing _eff_hard day (most-spaced, deterministic tie-break
    # on offset). Bounded by len(remaining_set); when no safe slot remains the
    # stimulus is legitimately dropped.
    # Missed hard TYPES, highest-priority first (vo2/threshold heaviest).
    _PROMOTE_PRIORITY = {
        "vo2max": 0, "double_threshold": 1, "threshold": 2, "overunder": 3,
        "sweetspot": 4, "sprint": 5,
    }
    missed_types = sorted(
        {getattr(s, "session_type", "") for s in missed_hard
         if getattr(s, "session_type", "") in _PROMOTE_PRIORITY},
        key=lambda t: _PROMOTE_PRIORITY.get(t, 99),
    )
    # One promotion per missed hard (don't manufacture more stimulus than was
    # lost). The cap-headroom check in the loop bounds it further.
    promotions_owed = len(missed_hard) if missed_types else 0
    for _ in range(len(remaining_set) + 1):
        if promotions_owed <= 0 or _eff_hard_count() >= cap or not missed_types:
            break
        eff_hard_offs = [o for o in range(len(week.sessions))
                         if _eff_hard(week.sessions[o])]
        # Remaining trainable non-hard slots that keep 48h spacing from every
        # existing effective-hard day if promoted.
        candidates = [
            o for o in remaining_set
            if not _eff_hard(week.sessions[o])
            and getattr(week.sessions[o], "session_type", "") not in ("rest", "ftp_test")
            and all(abs(o - h) >= 2 for h in eff_hard_offs)
        ]
        if not candidates:
            break  # no safe slot — drop the remaining stimulus (no spike)
        best = max(candidates,
                   key=lambda o: (min((abs(o - h) for h in eff_hard_offs),
                                      default=7), -o))
        slot = week.sessions[best]
        new_type = missed_types[0]
        ceil = TYPE_CEILING.get(new_type)
        dur = min(slot.duration_min, ceil) if ceil else slot.duration_min
        cand = PlannedSession(
            day=slot.day, day_name=slot.day_name, session_type=new_type,
            duration_min=dur,
            tss_estimate=round(dur / 60 * TSS_PER_HOUR.get(new_type, 75)),
            description=f"{new_type} ({dur}min) — refit redistribution",
        )
        promoted = match_zwo(cand, library, seed_salt=seed_salt)
        if not (promoted.zwo_file and _session_is_hit(promoted)):
            # The pool can't supply a hard file for this type/duration (e.g. the
            # sprint IF≤0.82 ceiling rejected every candidate) — drop, don't fake.
            break
        week.sessions[best] = promoted
        iso = slot.day.isoformat()
        if iso not in refit_days:
            refit_days.append(iso)
        promotions_owed -= 1

    # L3-12 (v2.5.0) — re-owe cap (see the snapshot above): inside the final 2
    # build weeks + the taper, the refit-touched days may add at most 1.0× the
    # missed dose over what the remaining days already carried. Rescale the
    # touched (non-rest, non-frozen — refit_days only ever holds remaining
    # trainable days) sessions proportionally; a 20-min floor keeps each ride
    # rideable. Cleared zwo fields are re-matched by the pass below.
    if _reowe_guard:
        _budget = _pre_remaining_tss + _missed_dose
        _post_remaining = sum(
            (week.sessions[o].tss_estimate or 0) for o in remaining_offsets
        )
        _touched_offs = [
            o for o in remaining_offsets
            if week.sessions[o].day.isoformat() in refit_days
            and week.sessions[o].session_type != "rest"
        ]
        _touched_tss = sum(
            (week.sessions[o].tss_estimate or 0) for o in _touched_offs
        )
        if _post_remaining > _budget and _touched_tss > 0:
            _keep = max(0.0, _touched_tss - (_post_remaining - _budget))
            _f = _keep / _touched_tss
            for o in _touched_offs:
                s = week.sessions[o]
                s.duration_min = max(20, int(round((s.duration_min or 0) * _f)))
                s.tss_estimate = round((s.tss_estimate or 0) * _f)
                s.zwo_file = ""
                s.zwo_name = ""

    # Re-match a real library workout for any refitted day that lost its file in
    # the demotion (or never carried one). Anchor the seed on the plan start so
    # re-running the same refit returns the same workout.
    anchor = current_plan_weeks[0].start if current_plan_weeks else week.start
    used_names_set = set(used_names_dict)
    for off in remaining_offsets:
        s = week.sessions[off]
        if s.day.isoformat() not in refit_days:
            continue
        if s.session_type in ("rest", "recovery", "ftp_test"):
            continue
        if getattr(s, "zwo_file", ""):
            continue
        match_zwo(s, library, week_num=week.week_num, day_idx=off,
                  used_names=used_names_set, plan_start_date=anchor,
                  seed_salt=seed_salt)

    refit_info = {
        "action": "refitted",
        "week_num": week.week_num,
        "refit_days": refit_days,
        "missed_dates": missed_dates,
    }
    # B2 (v2.1.0): the missed-hard refit can land a hard session in the current
    # week; keep it off the event eve (see regenerate_from_today). No-op for
    # non-event goals.
    _enforce_event_taper_eve(current_plan_weeks, goal.target_date)
    _apply_secondary_event_tapers(current_plan_weeks, goal)  # F7: B/C mini-tapers
    _mark_race_days(current_plan_weeks, goal)  # issue #7: race day shows the race, not a session
    return current_plan_weeks, refit_info


# ══════════════════════════════════════════════════════════════════════════════
# DAILY PLAN ADAPTATION (Xert-style TSS Pacer + cross-sport load)
# ══════════════════════════════════════════════════════════════════════════════
#
# Research base:
#   - Xert XATA: rolling daily TSS target from desired ramp rate
#   - Kiviniemi 2007 (PMID 17849143): HRV-guided daily intensity selection
#   - Javaloyes 2019 (PMID 29809080): HRV group +7.3% 40-min TT power
#   - TrainerRoad Adaptive Training: per-workout adaptation on completion
#
# Algorithm: after each training day (or on app open), compare actual load
# to planned load. Redistribute remaining weekly TSS across remaining days.
# Cross-sport: a hard run's TSS counts the same as a hard ride.

def daily_adapt_plan(
    current_week: PlannedWeek,
    actual_activities: list[dict],
    today: date | None = None,
    tsb: float | None = None,
) -> tuple[PlannedWeek, dict]:
    """PROJECTION-ONLY weekly adaptation (fix26 §6.1, §6.10).

    *** THIS FUNCTION DOES NOT MUTATE THE PLAN. ***

    Per MASTER_DECISIONS_FIX26 §6.1: "Demote daily_adapt_plan to projection-
    only (NO writes)." User intent (§6): "I shuffle workouts; recompute
    shouldn't fight me." The old behavior rewrote `s.tss_estimate`,
    `s.duration_min`, `s.session_type` and even converted rest-days to Z2
    in-place, then the HTTP handler persisted those edits. That fought the
    user — a VO2max moved from Thursday to Monday would get silently
    re-prescribed on sync.

    The new contract:
      * `current_week` is returned UNCHANGED.
      * The diff dict carries `projected_adaptations[]` describing what a
        legacy adapt pass *would* have done — a read-only preview.
      * §6.10: remaining_sessions is filtered by `status == "pending"`, not
        by calendar date. A user-moved VO2max sitting on Monday instead of
        Thursday is still pending and still counts toward the weekly budget.
      * §6.12: `user_moved`, `completion_matches`, `dismissed_at` are never
        touched here.
      * Explicit writes happen ONLY via /api/plan/move-session (§6.2) and
        /api/plan/rematch?apply=1 (§6.3).

    Args:
        current_week: the PlannedWeek with sessions for Mon-Sun (READ-ONLY)
        actual_activities: list of {date: "YYYY-MM-DD", tss: float, sport: str}
                          from Intervals.icu sync or local ride archive
        today: override for testing (defaults to date.today())
        tsb:  optional current Training Stress Balance (CTL - ATL). When
              deeply negative (< -30), projected de-loads are surfaced but
              NOT applied.

    Returns:
        (current_week, info_dict). `current_week` is the same object that
        was passed in, unchanged. `info_dict["projection_only"] == True`.
    """
    if today is None:
        today = date.today()

    sessions = current_week.sessions
    weekly_target = current_week.tss_target

    # ── 0. TSB-aware de-load (PL1) — PROJECTION ONLY ────────────────
    tsb_deload_projected = []
    if tsb is not None and tsb < -30:
        hard_types = {"vo2max", "threshold", "overunder", "sweetspot", "sprint", "tempo"}
        for s in sessions:
            if s.day < today:
                continue
            if getattr(s, "status", "pending") != "pending":
                continue
            if s.session_type in hard_types:
                new_type = _drop_intensity(s.session_type)
                if new_type != s.session_type:
                    new_tss_per_h = TSS_PER_HOUR.get(new_type, 45)
                    tsb_deload_projected.append({
                        "date": s.day.isoformat(),
                        "from_type": s.session_type,
                        "to_type": new_type,
                        "projected_tss": round(s.duration_min / 60 * new_tss_per_h),
                        "reason": f"TSB {tsb:.0f} — projected de-load",
                    })

    # ── 1. Compute actual TSS for completed days (any sport) ──────────
    actual_by_date: dict[str, float] = {}
    for a in actual_activities:
        d = a.get("date", "")[:10]
        actual_by_date[d] = actual_by_date.get(d, 0) + (a.get("tss") or a.get("icu_training_load") or 0)

    total_actual = 0.0
    total_planned_past = 0.0
    for s in sessions:
        if s.day < today:
            actual = actual_by_date.get(s.day.isoformat(), 0)
            total_actual += actual
            total_planned_past += s.tss_estimate

    surplus = total_actual - total_planned_past  # positive = did more than planned

    # ── 2. §6.10: filter remaining by STATUS, not calendar date ─────────
    remaining = [
        s for s in sessions
        if getattr(s, "status", "pending") == "pending" and s.session_type != "rest"
    ]
    rest_days = [
        s for s in sessions
        if getattr(s, "status", "pending") == "pending" and s.session_type == "rest"
    ]
    remaining_planned_tss = sum(s.tss_estimate for s in remaining)

    if not remaining:
        return current_week, {
            "action": "no_remaining_sessions",
            "surplus": round(surplus),
            "projected_adaptations": [],
            "tsb_deload_projected": tsb_deload_projected,
            "projection_only": True,
        }

    # ── 3. Compute projected remaining TSS budget ─────────────────────
    remaining_budget = max(0, weekly_target - total_actual)
    remaining_budget = min(remaining_budget, weekly_target * 1.1 - total_actual)
    remaining_budget = max(0, remaining_budget)

    # ── 4. Project redistribution (NO MUTATION) ───────────────────────
    projected_adaptations = []
    if remaining_planned_tss > 0:
        scale_factor = remaining_budget / remaining_planned_tss
    else:
        scale_factor = 1.0
    scale_factor = max(0.60, min(1.25, scale_factor))

    for s in remaining:
        original_tss = s.tss_estimate
        new_tss = round(original_tss * scale_factor)
        floor_tss = min(30, round(original_tss * 0.4)) if original_tss > 0 else 30
        new_tss = max(floor_tss, new_tss)

        if new_tss != original_tss:
            dur_scale = (new_tss / original_tss) if original_tss > 0 else 1.0
            new_duration = max(20, round(s.duration_min * dur_scale))
            projected_adaptations.append({
                "date": s.day.isoformat(),
                "session_type": s.session_type,
                "original_tss": original_tss,
                "projected_tss": new_tss,
                "original_duration": s.duration_min,
                "projected_duration": new_duration,
                "reason": "surplus" if surplus > 0 else "deficit",
            })

    # ── 5. Projected rest→Z2 conversion (NO MUTATION) ─────────────────
    # Compute the projected total from the per-session diffs so we can decide
    # whether a rest→Z2 conversion *would* have been triggered.
    adapted_map = {a["date"]: a["projected_tss"] for a in projected_adaptations}
    projected_post_scale = sum(
        adapted_map.get(s.day.isoformat(), s.tss_estimate) for s in remaining
    )
    projected_total = total_actual + projected_post_scale
    projected_ratio = projected_total / max(weekly_target, 1)
    deficit_pct = (weekly_target - projected_total) / max(weekly_target, 1) * 100
    projected_rest_conversion = None
    if deficit_pct > 50 and projected_ratio < 0.85 and rest_days:
        rest_day = rest_days[-1]
        makeup_tss = min(60, weekly_target * 0.15)
        projected_rest_conversion = {
            "date": rest_day.day.isoformat(),
            "from_type": "rest",
            "to_type": "z2",
            "projected_tss": round(makeup_tss),
            "projected_duration": round(makeup_tss / 0.4225 * 60 / 100),
            "reason": "rest_converted_for_deficit",
        }

    info = {
        "action": "projected" if (projected_adaptations or tsb_deload_projected or projected_rest_conversion) else "no_change",
        "projection_only": True,  # canonical marker — UI branch on this
        "surplus": round(surplus),
        "scale_factor": round(scale_factor, 2),
        "total_actual": round(total_actual),
        "weekly_target": round(weekly_target),
        "remaining_budget": round(remaining_budget),
        "projected_adaptations": projected_adaptations,
        "projected_rest_conversion": projected_rest_conversion,
        "tsb_deload_projected": tsb_deload_projected,
        "tsb": tsb,
    }
    return current_week, info


# ══════════════════════════════════════════════════════════════════════════════
# REMATCH CLASSIFIER (fix26 §6.3, §6.9)
# ══════════════════════════════════════════════════════════════════════════════

# Locked classifier tolerances (MASTER_DECISIONS_FIX26 §6.9):
#   - TSS ±15%
#   - duration ±20%
#   - IF-band match (categorical — must be the same band)
# Require ALL THREE (3/3) for status=done. 2/3 → ambiguous. 1/3 → no_match.
REMATCH_TOL_TSS_PCT      = 0.15
REMATCH_TOL_DURATION_PCT = 0.20

# Session type → IF-band (coarse zones). Mirrors the JS
# _SESSION_TYPE_TO_BAND in dashboard.html so UI and backend agree.
SESSION_TYPE_TO_BAND = {
    "recovery":  "low_aerobic",
    "z2":        "low_aerobic",
    "long_z2":   "low_aerobic",
    "tempo":     "mid_aerobic",
    "sweetspot": "high_aerobic",
    "threshold": "high_aerobic",
    "vo2max":    "anaerobic",
    "overunder": "anaerobic",
    "sprint":    "anaerobic",
    "ftp_test":  "high_aerobic",
    "rest":      None,
}


def _activity_if_band(activity: dict) -> str | None:
    """Map an actual activity to an IF-band using intensity_factor or TSS/duration.

    Prefer intensity_factor. Fall back to sqrt(TSS / (duration_h * 100))
    which approximates IF via Coggan's TSS = IF^2 * hours * 100.
    """
    if_ = activity.get("intensity_factor") or activity.get("icu_intensity")
    if if_ is None:
        tss = float(activity.get("tss") or activity.get("icu_training_load") or 0)
        dur_min = float(activity.get("duration_min") or (activity.get("moving_time", 0) or 0) / 60 or 0)
        if dur_min > 0 and tss > 0:
            if_sq = tss / (dur_min / 60 * 100)
            if_ = if_sq ** 0.5 if if_sq > 0 else 0
    try:
        if_ = float(if_) if if_ is not None else 0.0
    except (TypeError, ValueError):
        return None
    if if_ <= 0:
        return None
    if if_ < 0.65:
        return "low_aerobic"
    elif if_ < 0.82:
        return "mid_aerobic"
    elif if_ < 0.97:
        return "high_aerobic"
    else:
        return "anaerobic"


def classify_rematch(session: PlannedSession, activity: dict) -> dict:
    """Score a (session, activity) pair on 3 axes (fix26 §6.9).

    Returns dict:
      {
        axes: {tss_ok, duration_ok, if_band_ok},
        matched_axes: int (0-3),
        status: 'done' | 'ambiguous' | 'no_match',
        score: float 0.0-1.0 (fraction of axes),
        details: {...}
      }
    """
    planned_tss = float(session.tss_estimate or 0)
    actual_tss = float(activity.get("tss") or activity.get("icu_training_load") or 0)
    tss_diff_pct = abs(actual_tss - planned_tss) / max(planned_tss, 1)
    tss_ok = (planned_tss > 0 and actual_tss > 0 and tss_diff_pct <= REMATCH_TOL_TSS_PCT)

    planned_dur = float(session.duration_min or 0)
    actual_dur = float(activity.get("duration_min") or (activity.get("moving_time", 0) or 0) / 60 or 0)
    dur_diff_pct = abs(actual_dur - planned_dur) / max(planned_dur, 1)
    duration_ok = (planned_dur > 0 and actual_dur > 0 and dur_diff_pct <= REMATCH_TOL_DURATION_PCT)

    planned_band = SESSION_TYPE_TO_BAND.get(session.session_type)
    actual_band = _activity_if_band(activity)
    if_band_ok = (planned_band is not None and planned_band == actual_band)

    matched = int(tss_ok) + int(duration_ok) + int(if_band_ok)
    if matched == 3:
        status = "done"
    elif matched == 2:
        status = "ambiguous"
    else:
        status = "no_match"

    return {
        "axes": {"tss_ok": tss_ok, "duration_ok": duration_ok, "if_band_ok": if_band_ok},
        "matched_axes": matched,
        "status": status,
        "score": matched / 3.0,
        "details": {
            "planned_tss": round(planned_tss, 1),
            "actual_tss": round(actual_tss, 1),
            "tss_diff_pct": round(tss_diff_pct * 100, 1),
            "planned_duration": round(planned_dur, 1),
            "actual_duration": round(actual_dur, 1),
            "duration_diff_pct": round(dur_diff_pct * 100, 1),
            "planned_band": planned_band,
            "actual_band": actual_band,
        },
    }


def rematch_week(
    week: PlannedWeek,
    activities: list[dict],
    today: date | None = None,
) -> dict:
    """Pair each pending session with its best-matching same-day activity.

    Returns read-only preview. Does NOT mutate the week or write to disk.
    Caller decides to apply (writes via /api/plan/rematch?apply=1).

    Policy:
      - status=dismissed / done / done_partial → left alone, surfaced in summary
      - rest sessions skipped
      - Best match per session by highest matched_axes
      - 3/3 → new_status=done;  2/3 → ambiguous;  <2 with activity → no_match
      - No activity same day AND session.day < today → new_status=missed
      - No activity AND session.day >= today → new_status=pending
      - §6.11: missed never auto-dismisses.
    """
    if today is None:
        today = date.today()

    by_date: dict[str, list[dict]] = {}
    for a in activities:
        d = (a.get("date") or a.get("start_date_local", ""))[:10]
        if d:
            by_date.setdefault(d, []).append(a)

    matches = []
    summary = {"done": 0, "done_partial": 0, "ambiguous": 0, "missed": 0,
               "missed_race": 0, "pending": 0, "dismissed": 0, "no_match": 0}
    for s in week.sessions:
        cur_status = getattr(s, "status", "pending")
        if cur_status == "dismissed":
            summary["dismissed"] += 1
            continue
        if cur_status in ("done", "done_partial"):
            summary[cur_status] += 1
            continue
        if cur_status == "missed_race":
            # FC3 (v2.5.0, L3-2): terminal — a past unridden race is never
            # re-evaluated back to pending/missed (and never rescheduled).
            summary["missed_race"] = summary.get("missed_race", 0) + 1
            continue
        if s.session_type == "rest":
            continue

        day_acts = by_date.get(s.day.isoformat(), [])
        best = None
        for a in day_acts:
            cls = classify_rematch(s, a)
            if best is None or cls["matched_axes"] > best["matched_axes"]:
                best = {**cls, "activity": a}

        if best is None:
            # FC3 (v2.5.0, L3-2): an unridden race becomes the TERMINAL
            # "missed_race" — never plain "missed", which the auto-reschedule
            # layer would relocate to the next rest slot (the audit saw the
            # race entry rendered 2 days after the event).
            if s.day < today:
                new_status = "missed_race" if _protect_race(s) else "missed"
            else:
                new_status = "pending"
            summary[new_status] = summary.get(new_status, 0) + 1
            matches.append({
                "session_date": s.day.isoformat(),
                "session_type": s.session_type,
                "current_status": cur_status,
                "new_status": new_status,
                "matched_axes": 0,
                "score": 0.0,
                "axes": {"tss_ok": False, "duration_ok": False, "if_band_ok": False},
                "activity_id": None,
                "details": None,
            })
        else:
            status_map = {"done": "done", "ambiguous": "ambiguous"}
            resolved = status_map.get(best["status"])
            if resolved is None:
                # no_match with a same-day activity: treat as missed if past, pending if future
                # FC3 (L3-2): a race day resolves to the terminal missed_race.
                if s.day < today:
                    new_status = "missed_race" if _protect_race(s) else "missed"
                else:
                    new_status = "pending"
            else:
                new_status = resolved
            summary[new_status] = summary.get(new_status, 0) + 1
            matches.append({
                "session_date": s.day.isoformat(),
                "session_type": s.session_type,
                "current_status": cur_status,
                "new_status": new_status,
                "matched_axes": best["matched_axes"],
                "score": best["score"],
                "axes": best["axes"],
                "activity_id": best["activity"].get("id") or best["activity"].get("icu_id"),
                "details": best["details"],
            })

    return {
        "preview": True,
        "matches": matches,
        "summary": summary,
        "tolerances": {
            "tss_pct": REMATCH_TOL_TSS_PCT,
            "duration_pct": REMATCH_TOL_DURATION_PCT,
            "if_band": "categorical",
        },
    }


def _check_eftp_drift(current_eftp: float | None) -> dict | None:
    """Check if eFTP from Intervals.icu differs significantly from set FTP."""
    from config import ATHLETE_FTP_W
    if current_eftp is None or ATHLETE_FTP_W <= 0:
        return None
    diff = current_eftp - ATHLETE_FTP_W
    pct = abs(diff) / ATHLETE_FTP_W * 100
    if pct > 3:  # >3% drift is meaningful
        return {
            "current_ftp": ATHLETE_FTP_W,
            "detected_eftp": round(current_eftp),
            "diff_watts": round(diff),
            "diff_pct": round(pct, 1),
            "should_update": pct > 5,
        }
    return None


def check_and_auto_apply_eftp(wellness_series: list[dict]) -> dict | None:
    """F5 (v4.1.0) — auto-apply eFTP after 7+ consecutive days above +3% drift.

    Walks back through ``wellness_series`` (newest-last) and counts the
    consecutive trailing days where ``sportInfo[0].eftp > tested_ftp * 1.03``.
    When that count reaches 7, applies the NEW eFTP as the active FTP AND
    appends an ``eftp_auto`` row to ftp_test_history so the ledger captures
    the change. Returns the action dict for the caller to log + surface
    in the UI as a toast.

    Returns None if the streak is short, missing data, or ATHLETE_FTP_W
    is zero.
    """
    from config import ATHLETE_FTP_W
    if not wellness_series or ATHLETE_FTP_W <= 0:
        return None

    # I1 (v2.1.0): ICU's eFTP is unreliable; it must NOT silently rewrite the
    # active FTP (and every FTP-derived zone) "without asking". Auto-apply is
    # now OPT-IN — drift is still detected and surfaced (banner + manual Accept
    # button) so the rider decides. Enable via user_prefs.json
    # {"eftp_auto_apply": true}; default off.
    try:
        from profile_manager import ProfileManager
        if not ProfileManager.get().prefs.get("eftp_auto_apply", False):
            return None
    except Exception:
        return None

    # Newest first; build a chronological trailing series of eFTP drift
    # vs the current tested FTP.
    sorted_recs = sorted(wellness_series, key=lambda r: r.get("id", ""))
    streak = 0
    latest_eftp = None
    for rec in reversed(sorted_recs):
        si = rec.get("sportInfo") or []
        eftp = si[0].get("eftp") if si else None
        if not eftp:
            break
        if latest_eftp is None:
            latest_eftp = eftp
        # require sustained drift in the SAME direction (up)
        if eftp > ATHLETE_FTP_W * 1.03:
            streak += 1
        else:
            break
    if streak < 7 or latest_eftp is None:
        return None

    # Apply via ProfileManager — writes athlete.json + ftp_test_history entry
    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        new_ftp = int(round(latest_eftp))
        from datetime import date as _d
        today_iso = _d.today().isoformat()
        pm.record_ftp_test(
            method="manual",  # keep method enum narrow
            ftp=new_ftp,
            source="eftp_auto",
            applied=True,
        )
        # FIX-CONTRACT C5: stamp "eftp_auto" (not "eftp_icu") — this enum
        # conveys the auto-apply semantics U5's banner gates on. "eftp_icu"
        # just says "this number came from ICU"; "eftp_auto" says "the 7-day
        # sustained-drift rule fired and I applied it without asking".
        pm.update_ftp(new_ftp, source="eftp_auto")
        # Redundantly mirror onto athlete.json so any legacy reader that
        # bypasses ProfileManager.update_ftp still sees the provenance.
        try:
            pm._athlete["ftp_source"] = "eftp_auto"
            pm._write_json(pm.active_dir / "athlete.json", pm._athlete)
        except Exception:
            pass
        log.info(
            f"EVENT=eftp_auto_applied old_ftp={ATHLETE_FTP_W} new_ftp={new_ftp} "
            f"streak_days={streak}"
        )
        return {
            "applied": True, "old_ftp": ATHLETE_FTP_W, "new_ftp": new_ftp,
            "streak_days": streak, "accepted_on": today_iso,
            "source": "eftp_auto",
        }
    except Exception as e:
        log.warning(f"auto-apply eFTP failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# WEEKLY MESOCYCLE PLANNER — Seiler (2010), Stöggl & Sperlich (2014)
# ══════════════════════════════════════════════════════════════════════════════

def generate_weekly_plan(
    goal: Goal | None = None,
    current_phase: Phase | None = None,
    readiness: dict | None = None,
    recent_activities: list | None = None,
    current_ctl: float = 40,
    used_names: "set[str] | None" = None,
) -> PlannedWeek:
    """Generate a Mon-Sun weekly plan using time-in-zone polarized distribution.

    Distribution (Seiler 2010, Stöggl & Sperlich 2014):
      - 75-80% of weekly hours in Z1-Z2 (LIT)
      - 15-20% in Z4-Z5 (HIT)
      - 0-5% in Z3 (avoid black hole)

    HIT placement: constraint-based (48h gap, not on long ride day).

    P1 (v4.1.0): accepts an optional ``used_names`` set fed from the persisted
    plan JSON so callers can enforce cross-week workout dedupe (the simple
    weekly planner used to start with an empty set every request, handing
    the same ZWO back week after week). The set is passed through to
    match_zwo where recently-used workouts take a -15 score penalty.
    """
    from config import (ATHLETE_WEIGHT_KG, ATHLETE_FTP_W,
                        MAX_HIT_PER_WEEK, LONG_RIDE_DAY)

    # Week-start convention: we use the host's LOCAL date (date.today()) as the
    # reference for "today" throughout the planner. Rationale: training sessions
    # are stored as plain dates (no timezone) and the athlete experiences a week
    # boundary at local midnight, not at UTC midnight. If you need strict UTC
    # behaviour (e.g. for a hosted/shared planner) swap to
    #   today = datetime.now(timezone.utc).date()
    # and update every other date.today() call in this module for consistency.
    today = date.today()
    monday = today - timedelta(days=today.weekday())  # This week's Monday (local)

    # Determine weekly parameters from phase or defaults
    if current_phase:
        weekly_tss = current_phase.weekly_tss_target
        hit_per_week = current_phase.hit_per_week
        session_types = current_phase.session_types
        phase_name = current_phase.name
    else:
        weekly_tss = current_ctl * 7 * 1.05
        hit_per_week = min(MAX_HIT_PER_WEEK, 2 if current_ctl >= 40 else 1)
        session_types = ["z2", "threshold", "vo2max", "sweetspot"]
        phase_name = "general"

    # F3 (v4.1.0) — Foster Monotony gate (Foster 1998).
    # If last 2 weeks' monotony > 2.0, cut planned TSS by 15% and drop one
    # HIT to bake in extra recovery. Monotony = mean(daily_load) /
    # stdev(daily_load) computed from `recent_activities` when available.
    # This closes the decorative-monotony loop the grill flagged.
    try:
        if recent_activities:
            import statistics as _st
            # 14-day load vector (zeros for rest days) ending yesterday.
            last14_start = today - timedelta(days=14)
            last14_end = today - timedelta(days=1)
            daily_load: dict[str, float] = {}
            for i in range(14):
                d = (last14_start + timedelta(days=i)).isoformat()
                daily_load[d] = 0.0
            for a in recent_activities:
                ad = (a.get("date") or a.get("start_date_local", "")[:10] or "")
                if last14_start.isoformat() <= ad <= last14_end.isoformat():
                    daily_load[ad] = daily_load.get(ad, 0.0) + (a.get("tss") or a.get("icu_training_load") or 0)
            loads = list(daily_load.values())
            if len(loads) >= 14 and sum(loads) > 0:
                mean_l = _st.mean(loads)
                try:
                    sd_l = _st.stdev(loads)
                except _st.StatisticsError:
                    sd_l = 0.0
                if sd_l > 0:
                    mono = mean_l / sd_l
                    if mono > 2.0:
                        weekly_tss = round(weekly_tss * 0.85)
                        hit_per_week = max(0, hit_per_week - 1)
                        log.info(
                            "EVENT=foster_monotony_gate monotony=%.2f "
                            "weekly_tss_scaled=0.85 hit_per_week=%d",
                            mono, hit_per_week,
                        )
    except Exception as _e:
        log.debug(f"Foster monotony gate skipped: {_e}")

    # Rolling TSS: carry over deficit from last week (capped at 20% to avoid overload)
    if recent_activities:
        last_week_start = (monday - timedelta(days=7)).isoformat()
        last_week_end = (monday - timedelta(days=1)).isoformat()
        last_week_actual = sum(
            a.get("tss") or a.get("icu_training_load") or 0
            for a in recent_activities
            if last_week_start <= (a.get("date") or a.get("start_date_local", "")[:10] or "") <= last_week_end
        )
        deficit = max(0, weekly_tss - last_week_actual)
        # Roll over up to 20% of weekly target (avoid dangerous overload)
        rollover = min(deficit, weekly_tss * 0.20)
        if rollover > 10:
            weekly_tss += rollover

        # v4.6.6 IMPL-A G4 mirror — Soligard 2016 IOC consensus
        # (Br J Sports Med 50:1030-1041): a sudden ≥30% week-on-week load
        # increase elevates injury rate. The original code carried only a
        # *deficit* forward (athlete missed work last week → catch up).
        # The symmetric *surplus* path was missing: when last_week_actual
        # > weekly_tss × 1.3, the athlete already absorbed a full week's
        # worth of bonus load, and adding more on top of the new week's
        # baseline is exactly what Soligard's data flags as the spike
        # most strongly associated with overuse injury. Subtract up to
        # 20% of weekly_tss (mirror of the rollover cap) and drop one
        # HIT to bake recovery in.
        surplus = max(0, last_week_actual - weekly_tss)
        if last_week_actual > weekly_tss * 1.3:
            cut = min(surplus, weekly_tss * 0.20)
            if cut > 10:
                weekly_tss -= cut
                hit_per_week = max(0, hit_per_week - 1)
                log.info(
                    "EVENT=acwr_surplus_subtract last_week_actual=%.0f "
                    "weekly_tss_target=%.0f surplus=%.0f cut=%.0f "
                    "hit_per_week=%d",
                    last_week_actual, weekly_tss + cut, surplus, cut,
                    hit_per_week,
                )

    # Hours per week from goal or default
    hours_per_week = goal.hours_per_week if goal else 8.0
    rest_days = goal.rest_days if goal else [0]  # default: Monday rest
    max_weekday_h = goal.max_weekday_hours if goal else 2.0
    max_weekend_h = goal.max_weekend_hours if goal else 3.5

    # Step-back week detection — relative to plan start, not calendar week
    # If a plan exists, count weeks since plan start. Otherwise use ISO week as fallback.
    plan_start = None
    try:
        import json as _json
        _plan_path = PLAN_DIR / "current_plan.json"
        if _plan_path.exists():
            _plan = _json.loads(_plan_path.read_text())
            if _plan.get("weeks"):
                plan_start = date.fromisoformat(_plan["weeks"][0]["start"])
    except Exception:
        pass
    if plan_start:
        weeks_since_start = max(0, (monday - plan_start).days // 7)
        is_stepback = (weeks_since_start > 0 and weeks_since_start % STEP_BACK_EVERY == 0)
    else:
        is_stepback = (monday.isocalendar()[1] % STEP_BACK_EVERY == 0)
    if is_stepback:
        # Issurin 2010: 20-30% unloading (not 40-60%). 0.72 = 28% reduction. Matches plan_week().
        weekly_tss = round(weekly_tss * 0.72)
        hit_per_week = max(0, hit_per_week - 1)

    # ── CONSTRAINT-BASED SESSION PLACEMENT ──
    # 1. Place long ride (weekend)
    # 2. Place HIT sessions with 48h gaps
    # 3. Fill remaining with Z2

    sessions = []
    hit_days = []
    long_day = LONG_RIDE_DAY  # 0=Mon..6=Sun

    available_days = goal.available_days if goal else list(range(7))
    for day_offset in range(7):
        day_date = monday + timedelta(days=day_offset)
        weekday = day_offset  # 0=Mon

        if weekday in rest_days or weekday not in available_days:
            sessions.append(PlannedSession(
                day=day_date, day_name=day_date.strftime("%a"),
                session_type="rest", duration_min=0,
                tss_estimate=0, description="Rest day",
            ))
            continue

        # Placeholder — will be filled below
        sessions.append(None)

    # Long ride fallback: if LONG_RIDE_DAY is rest, try the day before (Saturday).
    # If also rest, skip long ride entirely.
    if sessions[long_day] is not None:  # long_day is rest
        fallback = long_day - 1 if long_day > 0 else 6  # day before (e.g. Saturday)
        if sessions[fallback] is None:
            long_day = fallback  # use fallback day
        # else: both rest — long ride is skipped, long_day stays but won't be placed

    # Place long ride
    if sessions[long_day] is None:
        long_dur = int((goal.max_hours_for_day(long_day) if goal else max_weekend_h) * 60)
        long_tss = round(long_dur / 60 * TSS_PER_HOUR.get("z2", 45))
        sessions[long_day] = PlannedSession(
            day=monday + timedelta(days=long_day),
            day_name=(monday + timedelta(days=long_day)).strftime("%a"),
            session_type="long_z2",
            duration_min=long_dur,
            tss_estimate=long_tss,
            description=f"Long ride: {long_dur}min Z2. Easy, below LTHR.",
        )

    # Place HIT sessions with 48h constraint
    available_for_hit = [
        i for i in range(7)
        if sessions[i] is None and i not in rest_days
        and i in available_days and i != long_day
    ]

    # Scale HIT by available days: minimum 50% of training days must be Z2/endurance
    # (prevents 3-day weeks from being 0% Z2: long_ride + 2 HIT = no Z2)
    available_training_days = sum(
        1 for i in range(7)
        if i not in rest_days and i in available_days
    )
    # Subtract 1 for long ride day
    max_hit = min(hit_per_week, max(1, (available_training_days - 1) // 2))

    placed_hit = 0
    for i in available_for_hit:
        if placed_hit >= max_hit:
            break
        # Check 48h gap from other HIT days and from long ride
        too_close = any(abs(i - h) < 2 for h in hit_days)
        too_close_long = abs(i - long_day) < 1  # don't HIT day before long ride
        if too_close or too_close_long:
            continue

        # Pick HIT type based on phase
        if phase_name in ("build2", "peak"):
            hit_type = "vo2max" if placed_hit == 0 else "overunder"
        elif phase_name == "build1":
            hit_type = "threshold" if placed_hit == 0 else "sweetspot"
        elif phase_name == "taper":
            hit_type = "threshold"
        else:  # base, general
            hit_type = "sweetspot" if placed_hit == 0 else "tempo"

        # HIT duration: 75min standard, but respect per-day availability
        day_max = (goal.max_hours_for_day(i) if goal else max_weekday_h) * 60
        hit_dur = min(75, int(day_max))  # cap at available time
        hit_tss = round(hit_dur / 60 * TSS_PER_HOUR.get(hit_type, 75))

        # Description in Dutch
        desc_map = {
            "vo2max": f"VO2max intervals: {hit_dur}min. 4-5×4min @106-115% FTP, 3min recovery.",
            "threshold": f"Threshold: {hit_dur}min. 2×20min @FTP, 5min recovery.",
            "overunder": f"Over-unders: {hit_dur}min. 3×12min (2min @105%, 1min @90%), 5min recovery.",
            "sweetspot": f"Sweet spot: {hit_dur}min. 3×15min @88-93% FTP, 5min recovery.",
            "tempo": f"Tempo: {hit_dur}min. 45min @76-90% FTP.",
            "sprint": f"Sprint power: {hit_dur}min. 8×30s max @150%+ FTP, 4.5min Z1 recovery.",
        }

        sessions[i] = PlannedSession(
            day=monday + timedelta(days=i),
            day_name=(monday + timedelta(days=i)).strftime("%a"),
            session_type=hit_type,
            duration_min=hit_dur,
            tss_estimate=hit_tss,
            description=desc_map.get(hit_type, f"{hit_type}: {hit_dur}min"),
        )
        hit_days.append(i)
        placed_hit += 1

    # Fill remaining slots with Z2
    remaining_tss = max(0, weekly_tss - sum(s.tss_estimate for s in sessions if s is not None))
    empty_slots = [i for i in range(7) if sessions[i] is None]
    tss_per_z2 = remaining_tss / max(len(empty_slots), 1)

    for i in empty_slots:
        is_weekend = (i >= 5)
        # Use per-day hours if goal has daily_max_hours, else fallback to aggregate
        day_max_h = goal.max_hours_for_day(i) if goal else (max_weekend_h if is_weekend else max_weekday_h)
        max_dur = day_max_h * 60
        # Z2 fills available time but respects TSS budget (Seiler: easy days LONG)
        budget_dur = int(tss_per_z2 / TSS_PER_HOUR["z2"] * 60) if tss_per_z2 > 10 else int(max_dur)
        z2_dur = max(45, min(int(max_dur), budget_dur))
        z2_tss = round(z2_dur / 60 * TSS_PER_HOUR["z2"])
        sessions[i] = PlannedSession(
            day=monday + timedelta(days=i),
            day_name=(monday + timedelta(days=i)).strftime("%a"),
            session_type="z2",
            duration_min=z2_dur,
            tss_estimate=z2_tss,
            description=f"Z2 endurance: {z2_dur}min. Easy, below LTHR.",
        )

    # Build PlannedWeek
    week_num = monday.isocalendar()[1]
    actual_tss = sum(s.tss_estimate for s in sessions)

    return PlannedWeek(
        week_num=week_num,
        start=monday,
        end=monday + timedelta(days=6),
        phase=phase_name,
        tss_target=round(actual_tss),
        is_stepback=is_stepback,
        sessions=sessions,
    )


def adjust_today_session(
    planned: PlannedSession,
    readiness: dict,
    hrv_streak_below_swc: int = 0,
    yesterday_tss_ratio: float = 1.0,
    rides_recent: list[dict] | None = None,
    daily_log_today: dict | None = None,
) -> tuple[PlannedSession, str]:
    """Adjust today's session based on HRV / readiness / injury-prevention gates.

    v4.6.6 IMPL-B INJURY-GATES (priority; first match wins):
      G5  daily_log.soreness >= 6 -> recovery (Hooper 1995 + Cheung 2003)
      G6  Hooper composite >= 18  -> Z2 cap (Hooper & Mackinnon 1995)
      G2  rolling 48h Z5+ >= 25min -> Z2 cap (Hulin 2014); cycling included
      G1  yesterday_tss_ratio > 1.5 -> Z2 (Foster 1998)
      G7  3-day mean RPE >= 7 + HIT today -> drop one tier (Foster 1998)
    G3 (polarization breach) lives in reforecast(), not here.
    """
    # v4.6.6 WAVE-4-FIX MEDIUM-2 (TODO LOW): DFA cap currently runs BEFORE
    # G5 soreness gate. When both fire (e.g. soreness=7 AND DFA α1 < 0.5),
    # the response description cites DFA — not G5 soreness. Outcome is
    # equivalent (both force a downshift to Z2/recovery) so this is purely
    # cosmetic for now. Reorder if ever a use-case needs the soreness reason
    # surfaced when DFA also caps.
    dfa_cap = readiness.get("dfa_cap") or {}
    if dfa_cap.get("cap_applied") and planned.session_type in (
        "vo2max", "threshold", "overunder", "sweetspot", "sprint", "tempo",
    ):
        log.info(
            f"EVENT=dfa_cap_applied session={planned.session_type} "
            f"downgraded_to=z2 mean_alpha1={dfa_cap.get('mean_alpha1')}"
        )
        return PlannedSession(
            day=planned.day, day_name=planned.day_name,
            session_type="z2", duration_min=planned.duration_min,
            tss_estimate=round(planned.duration_min / 60 * TSS_PER_HOUR["z2"]),
            description=f"Z2 (DFA α1 cap: mean {dfa_cap.get('mean_alpha1')} < 0.5)",
            adapted=True,
        ), f"DFA α1 {dfa_cap.get('mean_alpha1')} < 0.5 → capped at Z2"
    score = float(readiness.get("score") or 50)

    # ── v4.6.6 INJURY-GATES — priority: G5 > G6 > G2 > HRV/score > G1 > G7 ──
    if daily_log_today is None:
        try:
            import db as _db
            daily_log_today = _db.get_daily_log_today() or {}
        except Exception:  # noqa: BLE001
            daily_log_today = {}
    rides_recent = rides_recent or []

    # G5: Soreness peripheral-fatigue cap (Hooper 1995 + Cheung 2003)
    soreness_today = daily_log_today.get("soreness") if isinstance(daily_log_today, dict) else None
    if soreness_today is not None:
        try:
            sv = int(soreness_today)
        except (TypeError, ValueError):
            sv = 0
        if sv >= 6 and planned.session_type not in ("rest", "recovery"):
            log.info(f"EVENT=injury_gate_g5 soreness={sv} session={planned.session_type} → recovery")
            return PlannedSession(
                day=planned.day, day_name=planned.day_name,
                session_type="recovery",
                duration_min=max(30, planned.duration_min // 2),
                tss_estimate=round(planned.duration_min / 2 / 60 * TSS_PER_HOUR["recovery"]),
                description=(
                    f"Recovery — soreness {sv}/7 (peripheral fatigue, "
                    f"Hooper 1995 + Cheung 2003)."
                ),
                adapted=True,
            ), f"G5 soreness {sv}/7 → forced recovery (peripheral fatigue bypass)"

    # G6: Hooper composite >= 18 (Hooper & Mackinnon 1995)
    # v4.6.6 WAVE-4-FIX: direct sum (matches db.py:583 + dashboard form
    # polarity 1=best/7=worst for ALL fields). Prefers the persisted
    # hooper_index column when present (canonical single source of truth).
    if isinstance(daily_log_today, dict):
        persisted = daily_log_today.get("hooper_index")
        if isinstance(persisted, int) and 4 <= persisted <= 28:
            hooper = persisted
        elif all(daily_log_today.get(k) is not None for k in
                 ("sleep_quality", "fatigue", "stress", "soreness")):
            hooper = (
                int(daily_log_today["sleep_quality"])
                + int(daily_log_today["fatigue"])
                + int(daily_log_today["stress"])
                + int(daily_log_today["soreness"])
            )
        else:
            hooper = _hooper_index_today()
    else:
        hooper = _hooper_index_today()
    if hooper >= 18 and planned.session_type in (
        "vo2max", "threshold", "overunder", "sweetspot", "sprint", "tempo",
        "long_z2", "ftp_test",
    ):
        log.info(f"EVENT=injury_gate_g6 hooper={hooper} session={planned.session_type} → z2")
        return PlannedSession(
            day=planned.day, day_name=planned.day_name,
            session_type="z2", duration_min=planned.duration_min,
            tss_estimate=round(planned.duration_min / 60 * TSS_PER_HOUR["z2"]),
            description=(
                f"Z2 — Hooper index {hooper}/28 (≥18 accumulated wellness "
                f"deficit, Hooper & Mackinnon 1995)."
            ),
            adapted=True,
        ), f"G6 Hooper {hooper} ≥18 → Z2 cap"

    # G2: 48h Z5+ ceiling >= 25min (Hulin 2014) — cycling INCLUDED in v4.6.6
    z5plus_48h = _last_48h_z5plus_min(rides_recent)
    if z5plus_48h >= 25 and planned.session_type in (
        "vo2max", "threshold", "overunder", "sweetspot", "sprint", "tempo",
        "long_z2", "ftp_test",
    ):
        log.info(
            f"EVENT=injury_gate_g2 z5plus_48h={z5plus_48h:.1f}min "
            f"session={planned.session_type} → z2"
        )
        return PlannedSession(
            day=planned.day, day_name=planned.day_name,
            session_type="z2", duration_min=planned.duration_min,
            tss_estimate=round(planned.duration_min / 60 * TSS_PER_HOUR["z2"]),
            description=(
                f"Z2 — {z5plus_48h:.0f}min Z5+ in last 48h (≥25 ceiling, "
                f"Hulin 2014 BJSM 48:708-712)."
            ),
            adapted=True,
        ), f"G2 48h Z5+ {z5plus_48h:.0f}min ≥25 → Z2"

    # HRV streak takes priority (Plews: modify on day 1)
    if hrv_streak_below_swc >= 3:
        return PlannedSession(
            day=planned.day, day_name=planned.day_name,
            session_type="rest", duration_min=0, tss_estimate=0,
            description="Forced rest — HRV below SWC for 3+ days (Plews protocol).",
            adapted=True,
        ), "HRV below SWC 3+ days → forced rest"

    if hrv_streak_below_swc == 2:
        return PlannedSession(
            day=planned.day, day_name=planned.day_name,
            session_type="recovery", duration_min=max(30, planned.duration_min // 2),
            tss_estimate=round(planned.duration_min / 2 / 60 * TSS_PER_HOUR["recovery"]),
            description="Recovery ride — 50% volume, Z1 only. HRV day 2 below SWC.",
            adapted=True,
        ), "HRV below SWC day 2 → 50% volume Z1 only"

    if hrv_streak_below_swc == 1:
        if planned.session_type in ("vo2max", "threshold", "overunder", "sweetspot", "tempo", "long_z2", "ftp_test"):
            return PlannedSession(
                day=planned.day, day_name=planned.day_name,
                session_type="z2", duration_min=planned.duration_min,
                tss_estimate=round(planned.duration_min / 60 * TSS_PER_HOUR["z2"]),
                description=f"Z2 (downgraded from {planned.session_type}) — HRV day 1 below SWC.",
                adapted=True,
            ), f"HRV below SWC day 1 → capped at Z2 (was {planned.session_type})"
        return planned, ""

    # Readiness-based adjustments
    if score < 40:
        return PlannedSession(
            day=planned.day, day_name=planned.day_name,
            session_type="rest", duration_min=0, tss_estimate=0,
            description="Forced rest — readiness below 40.",
            adapted=True,
        ), f"Readiness {score:.0f} < 40 → forced rest"

    if score < 60 and planned.session_type in ("vo2max", "threshold", "overunder", "sweetspot", "tempo", "long_z2", "ftp_test"):
        return PlannedSession(
            day=planned.day, day_name=planned.day_name,
            session_type="z2", duration_min=planned.duration_min,
            tss_estimate=round(planned.duration_min / 60 * TSS_PER_HOUR["z2"]),
            description=f"Z2 (downgraded from {planned.session_type}) — readiness {score:.0f}/100.",
            adapted=True,
        ), f"Readiness {score:.0f} (40-59) → all sessions Z2 or easier"

    # G1: Yesterday-was-hard floor (Foster 1998 session-load spike)
    if yesterday_tss_ratio > 1.5 and planned.session_type not in ("rest", "recovery", "z2"):
        return PlannedSession(
            day=planned.day, day_name=planned.day_name,
            session_type="z2", duration_min=planned.duration_min,
            tss_estimate=round(planned.duration_min / 60 * TSS_PER_HOUR["z2"]),
            description=(
                f"Z2 — yesterday {yesterday_tss_ratio:.1f}× planned/avg TSS "
                f"(Foster 1998 session-load spike)."
            ),
            adapted=True,
        ), f"G1 yesterday {yesterday_tss_ratio:.1f}× → forced Z2"

    # G7: 3-day mean RPE >= 7 + HIT today (Foster 1998 session-RPE)
    mean_rpe_3d = _last_3d_mean_feel(rides_recent)
    if (
        mean_rpe_3d is not None
        and mean_rpe_3d >= 7.0
        and planned.session_type in _HARD_SESSION_TYPES
    ):
        new_type = _drop_intensity(planned.session_type)
        if new_type != planned.session_type:
            new_tss_per_h = TSS_PER_HOUR.get(new_type, 45)
            log.info(
                f"EVENT=injury_gate_g7 mean_rpe_3d={mean_rpe_3d:.1f} "
                f"{planned.session_type} → {new_type}"
            )
            return PlannedSession(
                day=planned.day, day_name=planned.day_name,
                session_type=new_type, duration_min=planned.duration_min,
                tss_estimate=round(planned.duration_min / 60 * new_tss_per_h),
                description=(
                    f"{new_type} (was {planned.session_type}) — 3d mean "
                    f"RPE {mean_rpe_3d:.1f}/10 ≥7 (Foster 1998 session-RPE)."
                ),
                adapted=True,
            ), (
                f"G7 3d mean RPE {mean_rpe_3d:.1f} ≥7 → "
                f"{planned.session_type} dropped to {new_type}"
            )

    # Readiness ≥80 + Z2 day: KEEP Z2 (never upgrade — Stöggl 2014 black hole)
    return planned, ""


# ── Intervals.icu calendar push ───────────────────────────────────────────────

def push_to_icu(weeks: list[PlannedWeek]) -> None:
    """Push planned workouts to Intervals.icu calendar via /events/bulk API."""
    import urllib.request

    events = []
    for pw in weeks:
        for s in pw.sessions:
            if s.session_type == "rest":
                continue
            event = {
                "start_date_local": s.day.isoformat(),
                "category": "WORKOUT",
                "name": f"[Plan] {s.description[:60]}",
                "description": (
                    f"Phase: {pw.phase}\n"
                    f"Type: {s.session_type}\n"
                    f"Duration: {s.duration_min}min\n"
                    f"TSS target: {s.tss_estimate}\n"
                    f"ZWO: {s.zwo_name or 'none'}\n"
                    f"Nutrition: {s.nutrition_note}"
                ),
                "indoor": True,
                "color": _phase_color(pw.phase),
                "moving_time": s.duration_min * 60,
            }
            events.append(event)

    if not events:
        print("No events to push.")
        return

    url = f"https://intervals.icu/api/v1/athlete/{config.ICU_ATHLETE_ID}/events/bulk"
    data = json.dumps(events).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Basic {_b64auth()}")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            print(f"✓  {len(events)} workouts pushed to Intervals.icu calendar")
    except Exception as e:
        print(f"✗  Failed to push to Intervals.icu: {e}")
        print("   Tip: check API key in config.py has calendar write access")


def _b64auth() -> str:
    import base64
    creds = f"API_KEY:{config.ICU_API_KEY}".encode()
    return base64.b64encode(creds).decode()


def _phase_color(phase: str) -> str:
    return {
        "base": "#3498db",    # blue
        "build1": "#f39c12",  # orange
        "build2": "#e74c3c",  # red
        "peak": "#9b59b6",    # purple
        "taper": "#2ecc71",   # green
    }.get(phase, "#95a5a6")


# ── Output formatters ─────────────────────────────────────────────────────────

def export_plan_md(
    goal: Goal,
    phases: list[Phase],
    weeks: list[PlannedWeek],
    ftp_at_generation: int | None = None,
) -> Path:
    """Export the full plan as a readable markdown file.

    Args:
        goal, phases, weeks: plan contents.
        ftp_at_generation: the FTP used when the plan was computed, taken from
            plan["meta"]["ftp_at_generation"] when available. Falls back to the
            live config.ATHLETE_FTP_W only if not supplied. Using the stored
            value means exporting an old plan does not silently re-scale all of
            its session descriptions to a newer FTP.
    """
    path = PLAN_DIR / f"plan_{date.today().isoformat()}.md"

    with open(path, "w", encoding="utf-8") as f:
        f.write(f"# Training Plan — {goal.goal_type.upper()}\n\n")
        f.write(f"*Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}*\n\n")

        if goal.goal_type == "event":
            f.write(f"**Event:** {goal.event_name or 'Target event'}\n")
            f.write(f"**Date:** {goal.target_date}\n")
            f.write(f"**Distance:** {goal.event_km}km / {goal.event_climb_m}m climb\n\n")

        metrics = get_today_metrics()
        f.write(f"**Current CTL:** {metrics.get('ctl', '?')}\n")
        ftp_w = ftp_at_generation if ftp_at_generation is not None else config.ATHLETE_FTP_W
        f.write(f"**FTP:** {ftp_w}W\n")
        f.write(f"**Time budget:** {goal.hours_per_week}h/week\n\n")

        # Phase overview
        f.write("## Phases\n\n")
        f.write("| Phase | Weeks | Dates | Weekly TSS | Focus |\n")
        f.write("|---|---|---|---|---|\n")
        for p in phases:
            f.write(f"| {p.name} | {p.weeks} | {p.start} → {p.end} | "
                    f"{p.weekly_tss_target} | {p.focus[:60]} |\n")

        # Weekly detail
        f.write("\n## Weekly Schedule\n\n")
        for pw in weeks:
            stepback = " (STEP-BACK)" if pw.is_stepback else ""
            f.write(f"\n### Week {pw.week_num} — {pw.phase.upper()}{stepback}\n")
            f.write(f"*{pw.start} → {pw.end} | TSS target: {pw.tss_target}*\n\n")
            f.write("| Day | Type | Duration | TSS | Description | Workout |\n")
            f.write("|---|---|---|---|---|---|\n")
            actual_tss = 0
            for s in pw.sessions:
                actual_tss += s.tss_estimate
                zwo = s.zwo_name[:25] if s.zwo_name else "—"
                f.write(f"| {s.day_name} {s.day.strftime('%d/%m')} | {s.session_type} | "
                        f"{s.duration_min}min | {s.tss_estimate:.0f} | "
                        f"{s.description[:50]} | {zwo} |\n")
            f.write(f"\n*Week TSS: {actual_tss:.0f} / target {pw.tss_target:.0f}*\n")

        # CTL projection
        f.write("\n## CTL Projection\n\n")
        daily_tss = []
        for pw in weeks:
            for s in pw.sessions:
                daily_tss.append(s.tss_estimate)
        ctl_trajectory = forecast_ctl(metrics.get("ctl") or 37.0, daily_tss)
        for i, pw in enumerate(weeks):
            week_end_ctl = ctl_trajectory[min((i + 1) * 7, len(ctl_trajectory) - 1)]
            f.write(f"- Week {pw.week_num} ({pw.phase}): CTL → {week_end_ctl}\n")

    return path


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Training Planner — evidence-based periodization")
    parser.add_argument("--goal", choices=["event", "ftp", "ctl", "endurance", "general", "weight"],
                        default="general")
    parser.add_argument("--event-date", type=str, default=None)
    parser.add_argument("--event-name", type=str, default="")
    parser.add_argument("--event-km", type=float, default=0)
    parser.add_argument("--event-climb", type=float, default=0)
    parser.add_argument("--event-type", choices=["century", "granfondo", "ultra", "crit", "sportive"],
                        default="granfondo")
    parser.add_argument("--target-ftp", type=int, default=None)
    parser.add_argument("--target-ctl", type=float, default=None)
    parser.add_argument("--hours-per-week", type=float, default=8.0)
    parser.add_argument("--max-weekday", type=float, default=2.0)
    parser.add_argument("--max-weekend", type=float, default=3.5)
    parser.add_argument("--rest-days", type=str, default="0",
                        help="Comma-separated rest days (0=Mon, 6=Sun)")
    parser.add_argument("--push-icu", action="store_true",
                        help="Push plan to Intervals.icu calendar")
    parser.add_argument("--reforecast", action="store_true",
                        help="Re-evaluate current plan against actual training")
    args = parser.parse_args()

    # Build goal
    target_date = date.fromisoformat(args.event_date) if args.event_date else None
    rest_days = [int(d) for d in args.rest_days.split(",")]

    goal = Goal(
        goal_type=args.goal,
        target_date=target_date,
        event_name=args.event_name,
        event_km=args.event_km,
        event_climb_m=args.event_climb,
        event_type=args.event_type,
        target_ftp=args.target_ftp,
        target_ctl=args.target_ctl,
        hours_per_week=args.hours_per_week,
        max_weekday_hours=args.max_weekday,
        max_weekend_hours=args.max_weekend,
        rest_days=rest_days,
    )

    print(f"🗓️  Training Planner — {goal.goal_type.upper()}")
    if goal.target_date:
        print(f"   Target: {goal.target_date} ({goal.weeks_available()} weeks)")
    print(f"   Budget: {goal.hours_per_week}h/week\n")

    # Generate plan
    phases, weeks = generate_plan(goal)

    # Display phase summary
    print(f"{'═'*70}")
    print(f"  PHASES")
    print(f"{'─'*70}")
    for p in phases:
        print(f"  {p.name:<8}  {p.weeks}w  {p.start} → {p.end}  "
              f"TSS {p.weekly_tss_target}/wk  HIT {p.hit_per_week}/wk")
    print(f"{'═'*70}\n")

    # Display first 4 weeks
    for pw in weeks[:4]:
        stepback = " ← STEP-BACK" if pw.is_stepback else ""
        print(f"Week {pw.week_num} — {pw.phase}{stepback} (TSS {pw.tss_target})")
        for s in pw.sessions:
            if s.session_type == "rest":
                print(f"  {s.day_name} {s.day.strftime('%d/%m')}  REST")
            else:
                zwo = f" → {s.zwo_name[:30]}" if s.zwo_name else ""
                print(f"  {s.day_name} {s.day.strftime('%d/%m')}  {s.session_type:<12} "
                      f"{s.duration_min:>3}min  TSS {s.tss_estimate:>3.0f}  "
                      f"{s.description[:40]}{zwo}")
        print()

    if len(weeks) > 4:
        print(f"  ... + {len(weeks) - 4} more weeks (see full plan in export)\n")

    # CTL projection
    metrics = get_today_metrics()
    current_ctl = metrics.get("ctl") or 37.0
    daily_tss = [s.tss_estimate for pw in weeks for s in pw.sessions]
    trajectory = forecast_ctl(current_ctl, daily_tss)
    final_ctl = trajectory[-1] if trajectory else current_ctl
    print(f"CTL projection: {current_ctl:.0f} → {final_ctl:.0f} over {len(weeks)} weeks\n")

    # Export
    md_path = export_plan_md(goal, phases, weeks)
    print(f"✓  Plan exported: {md_path}")

    # Push to Intervals.icu
    if args.push_icu:
        push_to_icu(weeks)

    # Reforecast
    if args.reforecast:
        _, info = reforecast(goal, weeks)
        print(f"\nReforecast: {info['action']} "
              f"({info['downshifts']} future hard session(s) de-escalated)")


if __name__ == "__main__":
    main()
