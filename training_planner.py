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
import sys
import threading
import xml.etree.ElementTree as ET

log = logging.getLogger(__name__)
from contextlib import contextmanager
from dataclasses import dataclass, field, asdict
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
_WORKOUT_LIB_CACHE: dict[str, tuple] = {}

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

# ── Plan-write serialization (PL3) ───────────────────────────────────────────
# Six FastAPI endpoints in app.py all write current_plan.json via the
# `tmp_path = json_path.with_suffix('.tmp')` → `tmp_path.rename(json_path)`
# pattern. Without serialization, concurrent daily-adapt + auto-recalc can
# silently drop adaptations (the last rename wins). Endpoints use
# `with training_planner.plan_write_lock(): ...` around the tmp-write + rename.
# Helper `atomic_write_plan(json_path, plan)` wraps the full write for callers
# who don't want to manage the tmp path themselves.
_plan_write_lock = threading.Lock()


@contextmanager
def plan_write_lock():
    """Context manager over the module-level plan-write lock.

    Use around paired tmp-write + rename (or any atomic plan mutation) to
    serialize writes across the plan endpoints in app.py.
    """
    with _plan_write_lock:
        yield


def atomic_write_plan(json_path: "Path | str", plan: dict) -> None:
    """Atomically write ``plan`` to ``json_path`` under the plan-write lock.

    Writes to `<json_path>.tmp` under the module-level lock and renames on
    success. Replaces the ad-hoc `tmp_path = json_path.with_suffix('.tmp')` +
    `tmp_path.rename(json_path)` pattern in app.py.
    """
    p = Path(json_path)
    tmp = p.with_suffix('.tmp')
    with _plan_write_lock:
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


def _session_is_stale(session_type: str, zwo_file: str) -> bool:
    """Return True if this (session_type, zwo_file) pair is a classifier-era
    mismatch that should be re-matched. rest / empty sessions are never stale.
    """
    if not zwo_file or session_type == "rest":
        return False
    allowed = _SESSION_TYPE_PREFIXES.get(session_type)
    if not allowed:
        return False  # unknown session_type — don't touch
    return not any(zwo_file.startswith(p) for p in allowed)


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
        plan_start = None
        try:
            plan_start = date.fromisoformat(plan.get("generated_at", "")[:10])
        except Exception:
            plan_start = date.today()

        rewritten = 0
        # Rolling used_names window (last 6 weeks) — mirrors generate_plan's
        # sliding-window dedupe so re-matches don't collide on a workout we
        # already placed elsewhere in the plan.
        used_names: set[str] = set()
        for w_json in weeks:
            week_num = w_json.get("week_num", 1)
            for idx, s_json in enumerate(w_json.get("sessions", [])):
                st = s_json.get("session_type") or ""
                zwo = s_json.get("zwo_file") or ""
                if not _session_is_stale(st, zwo):
                    if s_json.get("zwo_name"):
                        used_names.add(s_json["zwo_name"])
                    continue
                # Build a PlannedSession and re-match.
                try:
                    ps = PlannedSession(
                        day=date.fromisoformat(s_json.get("day", plan_start.isoformat())),
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
                    if new_zwo and new_zwo != zwo:
                        s_json["zwo_file"] = new_zwo
                        s_json["zwo_name"] = getattr(ps, "zwo_name", "") or s_json.get("zwo_name", "")
                        if getattr(ps, "description", None):
                            s_json["description"] = ps.description
                        rewritten += 1
                except Exception:
                    # Per-session failures must not abort the whole pass.
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
_INTENSITY_LADDER = (
    "vo2max", "threshold", "overunder", "sweetspot",
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
    "sprint":    95,  # neuromuscular: short max efforts, high WP → high TSS/hr
    # v1.1.0 IMPL-NORWEGIAN-HR: AM+PM sub-LT2 threshold pair. Per-half ~85
    # (slightly under threshold because the HR ceiling caps glycolytic load
    # — Stöggl & Sperlich 2014). Total day = 2× this when both halves run.
    "double_threshold": 85,
}

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


def get_budget_for_phase(phase_name: str) -> "IntensityBudget":
    """Return the IntensityBudget for a phase, defaulting to ``base``."""
    return BUDGETS.get(phase_name, BUDGETS["base"])


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


def target_ctl_for_event(goal: Goal) -> float:
    """Determine target CTL based on event profile."""
    targets = EVENT_CTL_TARGETS.get(goal.event_type, EVENT_CTL_TARGETS["granfondo"])
    # Adjust by event difficulty
    base_target = targets["strong"]
    if goal.event_climb_m > 3000:
        base_target += 5
    if goal.event_km > 180:
        base_target += 5
    return base_target


# ── Phase generator (backwards periodization) ────────────────────────────────

def generate_phases(goal: Goal, current_ctl: float) -> list[Phase]:
    """Generate training phases working backwards from the target date."""
    total_weeks = goal.weeks_available()
    target_date = goal.target_date or (date.today() + timedelta(weeks=16))

    # Determine target CTL based on goal type
    if goal.target_ctl:
        target = goal.target_ctl
    elif goal.goal_type == "event":
        target = target_ctl_for_event(goal)
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

    # Cap by time budget (TSS/hour at mixed intensity ≈ 60)
    max_tss_from_hours = goal.hours_per_week * 65
    peak_weekly_tss = min(peak_weekly_tss, max_tss_from_hours)

    # ── Allocate phases backwards from target date ────────────────────────

    phases = []
    cursor = target_date

    # TAPER: 10-14 days (Mujika & Padilla 2003: 8-14 days optimal)
    # Only create taper for event/ctl goals — not for general, ftp, vo2max, etc.
    taper_weeks = 0
    if goal.goal_type in ("event", "ctl"):
        taper_weeks = max(1, -(-TAPER_DAYS // 7))  # ceil(12/7) = 2
        taper_start = max(date.today(), cursor - timedelta(days=TAPER_DAYS))
        phases.append(Phase(
            name="taper",
            start=taper_start,
            end=cursor - timedelta(days=1),
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
    # (mtime_hash, list_of_workouts). Re-parsing 1,753 ZWO files on every call
    # is ~200ms+ of disk I/O, and the library changes rarely.
    global _WORKOUT_LIB_CACHE
    cache_key = str(WORKOUT_DIR)

    if not WORKOUT_DIR.exists():
        return []

    zwo_paths = sorted(WORKOUT_DIR.glob("*.zwo"))
    # Hash the (count, max_mtime) — fast and sufficient to detect edits/adds/removes.
    try:
        max_mtime = max((p.stat().st_mtime for p in zwo_paths), default=0.0)
    except OSError:
        max_mtime = 0.0
    mtime_hash = (_CLASSIFIER_VERSION, len(zwo_paths), max_mtime)

    cached = _WORKOUT_LIB_CACHE.get(cache_key)
    if cached and cached[0] == mtime_hash:
        return cached[1]

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
                avg_p = (plo + phi) / 2
                total_sec += dur
                # TSS for a linear power ramp from a→b over duration T (hours):
                #   TSS = (1/T) ∫₀ᵀ (a + (b-a)·t/T)² · 100 dt · T
                #       = (a² + a·b + b²)/3 · T · 100
                # For a constant (a==b) this reduces to a²·T·100 as expected.
                # Using mean² underestimates when a≠b (e.g. 0.5→0.9 gives
                # mean²=0.49 but true integral 0.5033 → ~2.7% high on TSS).
                tss_accum += (plo * plo + plo * phi + phi * phi) / 3 * (dur / 3600) * 100
                max_power = max(max_power, plo, phi)
                _acc_zone(avg_p * 100, dur)
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
) -> PlannedSession:
    """Find a ZWO workout matching this session, rotating for variety.

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
    """
    if session.session_type == "rest":
        return session

    if used_names is None:
        used_names = set()

    # Map planner session_types → workout category keywords (from Protocol field)
    # Protocol format: "Sweet Spot — 3×20min @ 88%" → starts with category name
    type_to_category = {
        "z2":         "Endurance",
        "long_z2":    "Endurance",
        "recovery":   "Recovery",
        "sweetspot":  "Sweet Spot",
        "threshold":  "Threshold",
        "vo2max":     "VO2max",
        "overunder":  "Over-Unders",
        "tempo":      "Tempo",
        "sprint":     "Sprint",
    }
    # Fallback categories: if primary has too few matches, also accept these.
    # v4.5.0 IMPL-PLANNER: add "Mixed" to z2 / long_z2 / tempo / vo2max / overunder
    # / recovery — the Endurance protocol bucket has only 44 files (vs 1069
    # Mixed), so without this the Z2 slot collapses onto ~5 ZWO files. Mixed
    # workouts whose dominant zone IS Z2 (with a tempo/SS finisher) belong here;
    # match_zwo's Score and duration filters keep the wrong ones out.
    type_to_fallback = {
        "z2":         ["Endurance", "Recovery", "Mixed"],
        "long_z2":    ["Endurance", "Mixed"],
        "recovery":   ["Recovery", "Endurance", "Mixed"],
        "sweetspot":  ["Sweet Spot", "Threshold", "Mixed"],
        "threshold":  ["Threshold", "Sweet Spot", "Over-Unders", "Mixed"],
        "vo2max":     ["VO2max", "Anaerobic", "Mixed"],
        "overunder":  ["Over-Unders", "Threshold", "Mixed"],
        "tempo":      ["Tempo", "Sweet Spot", "Mixed"],
        "sprint":     ["Sprint", "Anaerobic", "VO2max"],
    }

    primary_cat = type_to_category.get(session.session_type, "Endurance")
    fallback_cats = type_to_fallback.get(session.session_type, [primary_cat])
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
        if w["Score"] < 3:
            continue
        tags_lower = {t.lower() for t in (w.get("Tags") or [])}
        if "ftp_test" in tags_lower and not want_test:
            continue
        if want_test and "ftp_test" not in tags_lower:
            continue
        dur_diff = abs(w["Duration(min)"] - target_dur)
        # PL7: duration bucket uses <= at the 120-min boundary so a 120-min
        # workout picks up the wider (60-min) tolerance. The previous
        # `target_dur >= 120` vs strict `<` cousin step caused a 120-min
        # target to admit a 180-min workout but a 119-min target to reject
        # it — a jumpy discontinuity right at the base/long-ride transition.
        # Keeping `>=` for the 120+ bucket ensures inclusion at exactly 120.
        max_diff = 60 if target_dur >= 120 else 40
        if dur_diff > max_diff:
            continue

        protocol = w.get("Protocol", "")
        cat = protocol.split(" — ")[0] if " — " in protocol else protocol

        # Score: category match + evidence score + duration proximity
        score = float(w["Score"])

        if cat == primary_cat:
            score += 5  # primary category match
        elif cat in fallback_cats:
            score += 2  # fallback match
        else:
            continue  # skip non-matching categories

        score -= dur_diff / 10  # prefer closer duration
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

    if not candidates:
        # No library match. For bulk plan generation we log + flag the session
        # so ``generate_plan`` can surface the count once at the end. For
        # ad-hoc rematches (raise_on_empty=True) we raise so the caller — and
        # ultimately the UI — sees a concrete error instead of a blank zwo.
        log.warning(
            "match_zwo: no candidates for session_type=%s duration=%smin "
            "target_if≈%s primary_cat=%s fallbacks=%s library_size=%d",
            session.session_type, target_dur,
            getattr(session, "target_if", None),
            primary_cat, fallback_cats, len(library),
        )
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


def _get_mix_preference(phase_name: str, week_in_phase: int) -> dict[str, float]:
    """Return the WORKOUT_MIX_PREFERENCE row for (phase, week_in_phase).

    week_in_phase is 0-indexed (W1 of the phase → 0). Rows recycle with modulo
    when the phase runs longer than the table.
    """
    rows = WORKOUT_MIX_PREFERENCE.get(phase_name) or WORKOUT_MIX_PREFERENCE["base"]
    if not rows:
        return {}
    return rows[max(0, week_in_phase) % len(rows)]


def _apply_rotation_penalty(
    weights_by_cc: dict[str, float],
    recent_hit_types: list[str],
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
        if cc in last_5:
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
) -> list["PlannedSession"]:
    """Score-weighted per-week sampler driving the v4.5 diversification overhaul.

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
    rot_window_post = _apply_rotation_penalty(pref_row, rot_window)

    # Pre-compute eligible weights for the two slot kinds. The HIT row keeps
    # only HIT-eligible classes; endurance row keeps only endurance-eligible.
    hit_pref = {
        cc: w for cc, w in rot_window_post.items()
        if cc in _HIT_SLOT_CONTENT_CLASSES and w > 0
    }
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
            cache = _CONTENT_CLASSIFICATION_CACHE or {}
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
                # Replace the slot. Use the picked workout's natural session_type
                # (sweetspot/threshold/etc) since this is an interval slot now.
                new_sess = _make_session_from_row(pick, d, day_name, phase.name)
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

def generate_plan(
    goal: Goal,
    unavailable_periods: "list[tuple[date, date]] | None" = None,
    seed_salt: int = 0,
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
    """
    metrics = get_today_metrics()
    # F4 (v4.1.0) — local CTL fallback. Previously this path hardcoded 37.0
    # when ICU was unreachable; any user whose wellness sync was broken got
    # a phantom fitness baseline wildly divergent from their actual recent
    # rides. Fall back to a 42-day EWMA over the local ride archive before
    # reverting to the constant.
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

    phases = generate_phases(goal, current_ctl)
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
    plan_total_weeks = sum(p.weeks for p in phases) if phases else 0
    for phase in phases:
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
            )
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

            weeks.append(pw)
            prev_week_sessions = pw.sessions  # feed into next plan_week for 48h gap
            cursor += timedelta(weeks=1)
            week_num += 1
            week_in_phase += 1

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
    _enforce_build2_peak_hard_floor(weeks, pool_index, plan_pick_counts,
                                    class_session_counts, class_distinct_files,
                                    used_names_dict, used_names_set)

    # v4.6.3 RONNESTAD-FIX: hard floor of ≥1 Rønnestad-tagged file per build1
    # / build2 / peak. Rønnestad spans multiple content_classes (vo2_short,
    # neuromuscular, threshold, recovery) so the per-class floor above can't
    # express the constraint — runs as a separate pass.
    _enforce_ronnestad_floor(weeks, pool_index, plan_pick_counts)

    # Audit: if match_zwo fell through to the empty-candidates path, surface it
    # once at the end rather than silently producing zwo_file="" sessions.
    if unmatched_count:
        log.warning(
            "generate_plan: %d session(s) had no ZWO library match — "
            "they will carry zwo_file='' and matched=False. "
            "Check WORKOUT_DIR=%s and duration tolerances.",
            unmatched_count, WORKOUT_DIR,
        )

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
        "build1": {"vo2_short": 4, "neuromuscular": 2, "sweet_spot": 1},
        "build2": {"anaerobic": 1, "neuromuscular": 1, "vo2_short": 3},
        "peak":   {"anaerobic": 1, "neuromuscular": 1, "vo2_short": 3},
    }
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
            for w_target in phase_weeks:
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
    if availability_overrides:
        for pw in plan_weeks:
            if pw.start < today:
                continue  # past weeks — don't touch
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
            for s in pw.sessions:
                d_iso = s.day.isoformat()
                if d_iso not in availability_overrides:
                    continue
                hours = float(availability_overrides[d_iso])
                if hours <= 0:
                    s.session_type = "rest"
                    s.duration_min = 0
                    s.tss_estimate = 0
                else:
                    new_dur = max(0, int(round(s.duration_min * scale)))
                    s.duration_min = new_dur
                    tss_per_h = TSS_PER_HOUR.get(s.session_type, 45)
                    s.tss_estimate = round(new_dur / 60 * tss_per_h)
                touched.add(d_iso)

    downshifts: list[str] = []
    for pw in plan_weeks:
        if pw.end < today:
            continue  # past weeks — don't touch
        for s in pw.sessions:
            if s.day <= today:
                continue  # today + past already handled by daily_adapt_plan
            if s.session_type not in _HARD_SESSION_TYPES:
                continue
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
    if _polarization_breach(actual_polarization, target_polarization):
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
                if s.adapted:
                    continue  # already touched by TSB loop above
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
    adapted_current_week: dict[date, PlannedSession] = {}
    for w in old_plan_weeks:
        if w.start <= today <= w.end:
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
            break

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
    recovery_days = len(recovery_weeks) * 7

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

    build_weeks = post_recovery_weeks - max(1, adjusted_taper // 7)
    max_achievable = post_recovery_ctl + safe_ramp_rate(post_recovery_ctl) * build_weeks

    original_target = target_ctl_for_event(goal) if goal.goal_type == "event" else None
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
    )

    # 10. Generate new phases — offset start by recovery duration to avoid overlap
    phase_start_date = today + timedelta(days=recovery_days)
    # Temporarily adjust goal so phases start after recovery
    adjusted_goal._phase_start_override = phase_start_date
    new_phases = generate_phases(adjusted_goal, post_recovery_ctl)
    # Clamp all phase start dates to be after recovery
    for p in new_phases:
        if p.start < phase_start_date:
            p.start = phase_start_date

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
    plan_total_weeks_rg = sum(p.weeks for p in new_phases) if new_phases else 0

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

            new_weeks.append(pw)
            prev_week_sessions = pw.sessions  # feed into next plan_week (PL2)
            cursor += timedelta(weeks=1)
            week_num += 1
            week_in_phase += 1

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
    )

    # If taper locked, force taper phase
    if taper_locked:
        taper_phase = Phase(
            name="taper", start=today, end=goal.target_date - timedelta(days=1) if goal.target_date else today + timedelta(days=taper_days),
            weeks=max(1, taper_days // 7), focus=f"Taper {taper_days}d — volume -{round((1-0.6)*100)}%, maintain intensity",
            weekly_tss_target=round(current_ctl * 7 * 0.60),
            z2_pct=70, hit_per_week=1,
            session_types=["z2", "threshold", "vo2max", "sprint", "recovery"],
        )
        new_phases = [taper_phase]
    else:
        # Regenerate phases starting AFTER current week (avoids double-cover)
        adjusted_goal._phase_start_override = regen_start
        new_phases = generate_phases(adjusted_goal, current_ctl)

    # 6. Generate new weeks
    library = load_workout_library()
    new_weeks = []
    used_names = set()  # track used workouts for variety
    # Sliding window: track which week each workout was used (no full clear)
    used_in_week: dict[str, int] = {}
    week_num = len(past_weeks) + 1
    # Seed cross-week 48h HIT-gap (PL2) with the last past week's sessions.
    prev_week_sessions: list | None = past_weeks[-1].sessions if past_weeks else None

    for phase in new_phases:
        cursor = max(phase.start, regen_start)
        phase_week = 0
        while cursor <= phase.end:
            phase_week += 1
            is_stepback = (phase_week % STEP_BACK_EVERY == 0) and phase.name != "taper"

            # Insert FTP test at phase transitions (every 6-8 weeks)
            ftp_test_week = (week_num > 0 and week_num % 6 == 0 and phase.name != "taper"
                            and not is_stepback)

            pw = plan_week(week_num, cursor, phase, adjusted_goal, is_stepback,
                           prev_week_sessions=prev_week_sessions)

            # Insert FTP test session if due
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

            # Match ZWO workouts — rotate for variety.
            # Anchor seed on the plan start (phase start or regen_start) so
            # re-running on a different day returns the same workout.
            _anchor = new_phases[0].start if new_phases else regen_start
            for day_idx, s in enumerate(pw.sessions):
                if s.session_type not in ("rest", "recovery", "ftp_test"):
                    before = len(used_names)
                    match_zwo(s, library, week_num=week_num, day_idx=day_idx,
                              used_names=used_names, plan_start_date=_anchor)
                    # Track when each workout was assigned
                    if len(used_names) > before:
                        new_names = used_names - set(used_in_week.keys())
                        for n in new_names:
                            used_in_week[n] = week_num

            new_weeks.append(pw)
            prev_week_sessions = pw.sessions  # feed into next plan_week (PL2)
            cursor += timedelta(weeks=1)
            week_num += 1

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
    summary = {"done": 0, "done_partial": 0, "ambiguous": 0, "missed": 0, "pending": 0, "dismissed": 0, "no_match": 0}
    for s in week.sessions:
        cur_status = getattr(s, "status", "pending")
        if cur_status == "dismissed":
            summary["dismissed"] += 1
            continue
        if cur_status in ("done", "done_partial"):
            summary[cur_status] += 1
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
            new_status = "missed" if s.day < today else "pending"
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
                new_status = "missed" if s.day < today else "pending"
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
