"""SQLite persistence layer for wellness & activity data from Intervals.icu."""

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import date, datetime, timedelta
from pathlib import Path

# User data directory: writable, survives app updates.
# NOTE: directory creation is deferred to first DB write (init_db / set_db_path)
# so that profile_manager._maybe_migrate_data_dir() can detect a stale-but-
# empty ~/.domestique vs. a fresh install at boot. Otherwise this import
# would race ahead and create the new dir before the v3 migration runs.
_USER_DATA = Path.home() / ".domestique"

# Load .env — check user data dir first, then project dir
for _env_candidate in [_USER_DATA / ".env", Path(__file__).parent / ".env"]:
    if _env_candidate.exists():
        for _line in _env_candidate.read_text().splitlines():
            if "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())
        break

from training import fetch_wellness, fetch_activities, ICUCredentialsMissing

log = logging.getLogger(__name__)

# DB in user data dir (writable), NOT in PyInstaller bundle (read-only)
DB_PATH = _USER_DATA / "health_tracker.db"

_local = threading.local()
_db_version = 0  # incremented on profile switch; each thread tracks its own version

# Background-sync state (exposed via get_sync_status())
_auth_disabled = False  # set True after repeated HTTP 401s; stops retry loop
_consecutive_failures = 0  # updated by _sync_loop; surfaced for diagnostics
_last_sync_error: str | None = None


def get_db() -> sqlite3.Connection:
    """Return a thread-local database connection with WAL mode.

    Uses a version counter instead of a single bool flag to ensure ALL threads
    reopen after a profile switch (not just the first one to check).
    """
    local_ver = getattr(_local, "db_version", -1)
    if local_ver != _db_version or not hasattr(_local, "conn") or _local.conn is None:
        if hasattr(_local, "conn") and _local.conn is not None:
            try:
                _local.conn.close()
            except Exception:
                pass
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        _local.conn = sqlite3.connect(str(DB_PATH), timeout=10)
        # Enable WAL for better concurrency and FK for referential integrity.
        # Both PRAGMAs must be set on every connection (SQLite does not persist
        # journal_mode=WAL per-DB for all interpreters; foreign_keys is per-connection).
        try:
            _local.conn.execute("PRAGMA journal_mode = WAL")
        except sqlite3.OperationalError:
            # WAL not available (e.g. network FS); fall back silently.
            pass
        _local.conn.execute("PRAGMA foreign_keys = ON")
        _local.conn.row_factory = sqlite3.Row
        _local.db_version = _db_version
    return _local.conn


def set_db_path(path: Path) -> None:
    """Update the global DB_PATH. Call close_all_connections() first."""
    global DB_PATH
    DB_PATH = path


def close_all_connections() -> None:
    """Close current thread's connection and signal ALL threads to reopen.

    Uses a version counter (not a single bool) so every thread sees the change —
    the first thread to reopen does NOT clear the signal for others.
    """
    global _db_version
    if hasattr(_local, "conn") and _local.conn is not None:
        try:
            _local.conn.close()
        except Exception:
            pass
        _local.conn = None
    _db_version += 1  # all threads with stale version will reopen


# Identifier regex: unquoted SQLite identifiers (table/column names) must start
# with a letter or underscore and contain only word chars. This is intentionally
# conservative — the f-string below interpolates these names raw into DDL so
# sqlite3 parameter binding cannot protect them.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# Column types can include length/precision (e.g. `VARCHAR(32)`, `NUMERIC(10,2)`)
# and qualifiers (NOT NULL, DEFAULT <literal>). Allow a conservative subset:
# word chars, spaces, parens, commas, single-quoted string literals, and a few
# punctuation chars used in DEFAULT expressions. Anything else rejected.
_COLTYPE_RE = re.compile(r"^[A-Za-z0-9_ ,\(\)\.'\-\+]+$")


def _maybe_add_column(db: sqlite3.Connection, table: str, column: str, coltype: str) -> None:
    """ALTER TABLE add column if missing. SQLite raises OperationalError on duplicate.

    Validates ``table``, ``column``, and ``coltype`` against strict allow-lists
    before interpolating them into the DDL. `sqlite3` cannot bind identifiers as
    parameters, so without this check a caller passing attacker-controlled
    strings could trigger SQL injection. All current callers pass hardcoded
    literals — this is a latent hardening step.
    """
    if not isinstance(table, str) or not _IDENT_RE.match(table):
        raise ValueError(f"invalid table name: {table!r}")
    if not isinstance(column, str) or not _IDENT_RE.match(column):
        raise ValueError(f"invalid column name: {column!r}")
    if not isinstance(coltype, str) or not _COLTYPE_RE.match(coltype):
        raise ValueError(f"invalid column type: {coltype!r}")
    try:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")
    except sqlite3.OperationalError as e:
        # "duplicate column name" — column already exists, that's fine.
        if "duplicate column" not in str(e).lower():
            raise


def init_db():
    """Create tables and indexes if they don't exist. Idempotent."""
    db = get_db()
    # All table creation uses IF NOT EXISTS — safe to re-run on existing DBs.
    db.executescript("""
        CREATE TABLE IF NOT EXISTS wellness (
            date       TEXT PRIMARY KEY,
            ctl        REAL,
            atl        REAL,
            hrv        REAL,
            rhr        INTEGER,
            sleep_secs INTEGER,
            sleep_score INTEGER,
            eftp       REAL,
            raw_json   TEXT
        );

        CREATE TABLE IF NOT EXISTS activities (
            id             TEXT PRIMARY KEY,
            date           TEXT NOT NULL,
            name           TEXT,
            sport          TEXT,
            duration_sec   INTEGER,
            tss            REAL,
            avg_power      REAL,
            avg_hr         REAL,
            distance_km    REAL,
            kilojoules     REAL,
            calories       REAL,
            elevation_gain REAL,
            raw_json       TEXT
        );

        CREATE INDEX IF NOT EXISTS idx_activities_date ON activities(date);

        CREATE TABLE IF NOT EXISTS sync_log (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp       TEXT NOT NULL,
            wellness_synced INTEGER DEFAULT 0,
            activity_synced INTEGER DEFAULT 0,
            status          TEXT DEFAULT 'ok',
            error           TEXT
        );

        CREATE TABLE IF NOT EXISTS athlete_metrics (
            date       TEXT NOT NULL,
            metric     TEXT NOT NULL,
            value      REAL NOT NULL,
            source     TEXT DEFAULT 'manual',
            notes      TEXT,
            PRIMARY KEY (date, metric)
        );

        -- PK is (date, metric) so lookups by metric alone are NOT indexed.
        -- This index supports query_metric_history / query_metrics_latest.
        CREATE INDEX IF NOT EXISTS idx_athlete_metrics_metric ON athlete_metrics(metric);

        CREATE TABLE IF NOT EXISTS daily_log (
            date           TEXT PRIMARY KEY,
            sleep_quality  INTEGER CHECK(sleep_quality BETWEEN 1 AND 7),
            fatigue        INTEGER CHECK(fatigue BETWEEN 1 AND 7),
            soreness       INTEGER CHECK(soreness BETWEEN 1 AND 7),
            stress         INTEGER CHECK(stress BETWEEN 1 AND 7),
            mood           INTEGER CHECK(mood BETWEEN 1 AND 7),
            hooper_index   REAL,
            notes          TEXT,
            created_at     TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS blood_markers (
            date        TEXT NOT NULL,
            marker      TEXT NOT NULL,
            value       REAL NOT NULL,
            unit        TEXT,
            notes       TEXT,
            PRIMARY KEY (date, marker)
        );
        -- PK prefix covers (date, marker) so date-prefixed queries already use the PK.
        -- No separate idx_blood_markers_date needed.

        -- wellness.date is PRIMARY KEY, so no separate idx_wellness_date needed.
    """)
    # Migrate existing activities tables: add new columns if they don't exist.
    # SQLite's ALTER TABLE ADD COLUMN raises OperationalError on duplicates;
    # _maybe_add_column swallows that specific case.
    _maybe_add_column(db, "activities", "distance_km", "REAL")
    _maybe_add_column(db, "activities", "kilojoules", "REAL")
    _maybe_add_column(db, "activities", "calories", "REAL")
    _maybe_add_column(db, "activities", "elevation_gain", "REAL")
    db.commit()


def sync_wellness(days: int = 90) -> int:
    """Fetch wellness from Intervals.icu and upsert into SQLite. Returns count.

    Transactional: if any row fails mid-loop, the entire batch is rolled back.
    """
    try:
        data = fetch_wellness(days=days)
    except Exception as e:
        log.error("Failed to fetch wellness: %s", e)
        raise
    if not data:
        return 0

    db = get_db()
    count = 0
    try:
        for w in data:
            dt = w.get("id")
            if not dt:
                continue
            si = w.get("sportInfo") or []
            eftp = si[0].get("eftp") if len(si) > 0 else None
            db.execute(
                """INSERT OR REPLACE INTO wellness
                   (date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    dt,
                    w.get("ctl"),
                    w.get("atl"),
                    w.get("hrv"),
                    w.get("restingHR"),
                    w.get("sleepSecs"),
                    w.get("sleepScore"),
                    eftp,
                    json.dumps(w),
                ),
            )
            # Auto-log VO2max, eFTP, wPrime from Intervals.icu/Garmin
            vo2max = w.get("vo2max")
            if vo2max and isinstance(vo2max, (int, float)) and vo2max > 0:
                db.execute(
                    "INSERT OR REPLACE INTO athlete_metrics (date, metric, value, source) VALUES (?, 'vo2max', ?, 'intervals.icu')",
                    (dt, round(vo2max, 1)),
                )
            if eftp and isinstance(eftp, (int, float)) and eftp > 0:
                # Don't clobber manual eFTP entries: check source before replace.
                existing = db.execute(
                    "SELECT source FROM athlete_metrics WHERE date = ? AND metric = 'eftp'",
                    (dt,),
                ).fetchone()
                if not (existing and existing[0] == "manual"):
                    db.execute(
                        "INSERT OR REPLACE INTO athlete_metrics (date, metric, value, source) VALUES (?, 'eftp', ?, 'intervals.icu')",
                        (dt, round(eftp)),
                    )
            w_prime = si[0].get("wPrime") if len(si) > 0 else None
            if w_prime and isinstance(w_prime, (int, float)) and w_prime > 0:
                # Manual-source guard: if the user logged W' manually (e.g.
                # from a 3-min all-out test), don't let ICU overwrite it.
                # IMPL-WBAL may later add a `_set_wprime(value, source)`
                # helper in profile_manager.py; until then we mirror the
                # eftp guard above (source='manual' wins).
                existing_wp = db.execute(
                    "SELECT source FROM athlete_metrics WHERE date = ? AND metric = 'w_prime'",
                    (dt,),
                ).fetchone()
                if not (existing_wp and existing_wp[0] == "manual"):
                    db.execute(
                        "INSERT OR REPLACE INTO athlete_metrics (date, metric, value, source) VALUES (?, 'w_prime', ?, 'intervals.icu')",
                        (dt, round(w_prime)),
                    )

            # v1.0.6 IMPL-3D-INGEST: pull Pmax from ICU sportInfo[0].pMax
            # (best 1s power; live: 1,114.7 W on 2026-05-05). Mirror of the
            # wPrime block above with the same manual-source guard.
            p_max = si[0].get("pMax") if len(si) > 0 else None
            if p_max and isinstance(p_max, (int, float)) and p_max > 0:
                existing_pm = db.execute(
                    "SELECT source FROM athlete_metrics WHERE date = ? AND metric = 'pmax'",
                    (dt,),
                ).fetchone()
                if not (existing_pm and existing_pm[0] == "manual"):
                    db.execute(
                        "INSERT OR REPLACE INTO athlete_metrics (date, metric, value, source) VALUES (?, 'pmax', ?, 'intervals.icu')",
                        (dt, round(p_max)),
                    )

            count += 1
    except Exception:
        db.rollback()
        raise
    db.commit()

    # v3.6.0-fix26 (IMPL-WBAL §4.1): after the ICU sync, mirror the most
    # recent `w_prime` into the active profile so MetricsEngine picks it
    # up on the next session construct instead of using the `ftp*80`
    # fallback. Guarded by profile_manager._set_wprime() which ignores
    # writes that would downgrade a manually-typed value.
    _refresh_wprime_from_metrics()
    # v1.0.6 IMPL-3D-INGEST: same mirror pattern for Pmax.
    _refresh_pmax_from_metrics()

    return count


def _refresh_wprime_from_metrics() -> None:
    """Copy the newest athlete_metrics.w_prime (source='intervals.icu') into
    the active ProfileManager athlete.wprime_j.

    Called after `sync_wellness()` finishes its ICU batch. Failures are
    logged but never re-raised — the ICU sync should still succeed even
    if profile mirroring misfires (e.g. no profile loaded yet, disk
    full, race with a switch_profile()). Reading the latest row is cheap
    (PK date prefix index) so this is O(1) regardless of metrics-table
    size.
    """
    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        db = get_db()
        row = db.execute(
            "SELECT value, source FROM athlete_metrics "
            "WHERE metric = 'w_prime' ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return
        value, source = row[0], row[1]
        if value is None or float(value) <= 0:
            return
        # Only mirror intervals.icu-sourced w_prime into the profile via the
        # "icu" priority tier. A 'manual' row in athlete_metrics should not
        # be re-promoted here — manual profile writes go through
        # save_athlete directly and already tag source="manual".
        if source != "intervals.icu":
            return
        pm._set_wprime(int(float(value)), "icu")
    except Exception as e:
        log.warning("refresh_wprime_from_metrics failed: %s", e)


def _refresh_pmax_from_metrics() -> None:
    """v1.0.6 IMPL-3D-INGEST: copy the newest athlete_metrics.pmax row
    (source='intervals.icu') into the active ProfileManager athlete.pmax_w.

    Mirror of `_refresh_wprime_from_metrics()`. Called after sync_wellness()
    finishes its ICU batch. Failures are logged but never re-raised.
    """
    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        db = get_db()
        row = db.execute(
            "SELECT value, source FROM athlete_metrics "
            "WHERE metric = 'pmax' ORDER BY date DESC LIMIT 1"
        ).fetchone()
        if not row:
            return
        value, source = row[0], row[1]
        if value is None or float(value) <= 0:
            return
        # Only mirror intervals.icu-sourced pmax. Manual rows go through
        # save_athlete directly with source="manual" already.
        if source != "intervals.icu":
            return
        pm._set_pmax(int(float(value)), "icu")
    except Exception as e:
        log.warning("refresh_pmax_from_metrics failed: %s", e)


def sync_activities(days: int = 90) -> int:
    """Fetch activities from Intervals.icu and upsert into SQLite. Returns count.

    Transactional: if any row fails mid-loop, the entire batch is rolled back.
    """
    try:
        data = fetch_activities(days=days)
    except Exception as e:
        log.error("Failed to fetch activities: %s", e)
        raise
    if not data:
        return 0

    db = get_db()
    count = 0
    try:
        for a in data:
            aid = a.get("id", a.get("start_date_local", ""))
            dt = a.get("start_date_local", "")[:10]
            if not aid:
                continue
            # Widened projection — real columns (not just raw_json) for fast filtering.
            distance_m = a.get("distance")
            distance_km = None
            if distance_m is not None:
                try:
                    distance_km = float(distance_m) / 1000.0
                except (TypeError, ValueError):
                    distance_km = None
            kilojoules = a.get("kilojoules")
            calories = a.get("calories")
            elevation_gain = a.get("total_elevation_gain")
            db.execute(
                """INSERT OR REPLACE INTO activities
                   (id, date, name, sport, duration_sec, tss, avg_power, avg_hr,
                    distance_km, kilojoules, calories, elevation_gain, raw_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(aid),
                    dt,
                    a.get("name"),
                    a.get("sport_type", a.get("type", "")),
                    a.get("moving_time") or a.get("elapsed_time"),
                    a.get("icu_training_load") or a.get("training_load"),
                    a.get("average_watts"),
                    a.get("average_heartrate"),
                    distance_km,
                    kilojoules,
                    calories,
                    elevation_gain,
                    json.dumps(a),
                ),
            )
            count += 1
    except Exception:
        db.rollback()
        raise
    db.commit()
    return count


def run_sync(days: int = 90) -> dict:
    """Run full sync and log result. Skips if no ICU credentials configured.

    Wraps sync_wellness() + sync_activities() in a single logical transaction:
    either both succeed (single sync_log row, status=ok) or both roll back
    (sync_log row recorded with status=error and exception message).
    """
    import config
    if not getattr(config, "ICU_ATHLETE_ID", None):
        return {"timestamp": datetime.now().isoformat(), "wellness": 0,
                "activities": 0, "status": "skipped", "error": "No ICU credentials"}
    db = get_db()
    ts = datetime.now().isoformat()
    w_count = a_count = 0
    error = None
    status = "ok"
    sync_exc: Exception | None = None
    try:
        w_count = sync_wellness(days)
        a_count = sync_activities(days)
    except Exception as e:
        # Each sync_* already rolled back its own partial batch; roll back
        # anything else still open on this connection before writing sync_log.
        try:
            db.rollback()
        except Exception:
            pass
        error = str(e)
        status = "error"
        sync_exc = e
        log.error("Sync error: %s", e)

    # sync_log write happens regardless of outcome, in its own transaction.
    try:
        db.execute(
            "INSERT INTO sync_log (timestamp, wellness_synced, activity_synced, status, error) VALUES (?, ?, ?, ?, ?)",
            (ts, w_count, a_count, status, error),
        )
        db.commit()
    except Exception as log_exc:
        log.error("Failed to write sync_log: %s", log_exc)
        try:
            db.rollback()
        except Exception:
            pass

    if sync_exc is not None:
        # Re-raise so callers / background loop can react (e.g. backoff on 401).
        raise sync_exc
    return {"timestamp": ts, "wellness": w_count, "activities": a_count, "status": status, "error": error}


def query_wellness(days: int = 28) -> list[dict]:
    """Query wellness from local SQLite."""
    db = get_db()
    oldest = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT * FROM wellness WHERE date >= ? ORDER BY date", (oldest,)
    ).fetchall()
    return [dict(r) for r in rows]


def query_activities(days: int = 14) -> list[dict]:
    """Query activities from local SQLite."""
    db = get_db()
    oldest = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT * FROM activities WHERE date >= ? ORDER BY date", (oldest,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_sync_status() -> dict:
    """Return last sync info, record counts, and background-sync health."""
    db = get_db()
    last = db.execute("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1").fetchone()
    w_count = db.execute("SELECT COUNT(*) FROM wellness").fetchone()[0]
    a_count = db.execute("SELECT COUNT(*) FROM activities").fetchone()[0]
    return {
        "last_sync": dict(last) if last else None,
        "wellness_records": w_count,
        "activity_records": a_count,
        "auth_disabled": _auth_disabled,
        "consecutive_failures": _consecutive_failures,
        "last_error": _last_sync_error,
    }


# ── Athlete Metrics ──────────────────────────────────────────────────────────

def log_metric(dt: str, metric: str, value: float, source: str = "manual", notes: str = None):
    """Insert or update a single metric value."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO athlete_metrics (date, metric, value, source, notes) VALUES (?, ?, ?, ?, ?)",
        (dt, metric, value, source, notes),
    )
    db.commit()


def log_metrics_from_settings(updates: dict):
    """Auto-log metrics when settings are saved."""
    today_str = date.today().isoformat()
    metric_map = {
        "ATHLETE_WEIGHT_KG": "weight",
        "ATHLETE_FTP_W": "ftp",
        "ATHLETE_LBM_KG": "lbm",
        "ATHLETE_LTHR": "lthr",
        "ATHLETE_MAX_HR": "max_hr",
    }
    db = get_db()
    for config_key, val in updates.items():
        metric_name = metric_map.get(config_key)
        if metric_name:
            db.execute(
                "INSERT OR REPLACE INTO athlete_metrics (date, metric, value, source) VALUES (?, ?, ?, 'settings')",
                (today_str, metric_name, float(val)),
            )
    db.commit()


def query_metric_history(metric: str, days: int = 365) -> list[dict]:
    """Query history for a single metric."""
    db = get_db()
    oldest = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT date, value, source, notes FROM athlete_metrics WHERE metric = ? AND date >= ? ORDER BY date",
        (metric, oldest),
    ).fetchall()
    return [dict(r) for r in rows]


def query_wkg_history(days: int = 365) -> list[dict]:
    """Query W/kg history (derived from weight + ftp)."""
    db = get_db()
    oldest = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        """SELECT w.date, ROUND(f.value / w.value, 2) as value
           FROM athlete_metrics w
           JOIN athlete_metrics f ON w.date = f.date
           WHERE w.metric = 'weight' AND f.metric = 'ftp'
           AND w.date >= ? AND w.value > 0
           ORDER BY w.date""",
        (oldest,),
    ).fetchall()
    return [dict(r) for r in rows]


def query_metrics_latest() -> dict:
    """Return latest value for each metric."""
    db = get_db()
    rows = db.execute(
        "SELECT metric, value, date FROM athlete_metrics GROUP BY metric HAVING date = MAX(date)"
    ).fetchall()
    return {r["metric"]: {"value": r["value"], "date": r["date"]} for r in rows}


# ── Daily Log (Morning Questionnaire) ────────────────────────────────────────

def upsert_daily_log(dt: str, sleep_quality: int, fatigue: int, soreness: int,
                     stress: int, mood: int, notes: str = None) -> dict:
    """Insert or update daily wellness log. Returns the entry.

    v4.6.6 IMPL-C: hooper_index = sleep_quality + fatigue + stress + soreness
    (sum of 4 fields each 1-7, range 4-28). Hooper & Mackinnon 1995 — the
    "wellness composite". IMPL-B's G6 gate fires when hooper_index ≥ 18.

    Each input field MUST be int 1..7; raises ValueError otherwise. (Schema
    CHECK enforces it too, but we validate up-front so the error surfaces as
    a 400 in the API rather than a 500 from sqlite.)
    """
    for nm, v in (("sleep_quality", sleep_quality), ("fatigue", fatigue),
                  ("soreness", soreness), ("stress", stress), ("mood", mood)):
        if not isinstance(v, int) or not (1 <= v <= 7):
            raise ValueError(f"{nm} must be int 1..7, got {v!r}")
    hooper = sleep_quality + fatigue + stress + soreness
    db = get_db()
    db.execute(
        """INSERT OR REPLACE INTO daily_log
           (date, sleep_quality, fatigue, soreness, stress, mood, hooper_index, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (dt, sleep_quality, fatigue, soreness, stress, mood, hooper, notes),
    )
    db.commit()
    return {
        "date": dt, "sleep_quality": sleep_quality, "fatigue": fatigue,
        "soreness": soreness, "stress": stress, "mood": mood,
        "hooper_index": hooper, "notes": notes,
    }


def query_daily_log(days: int = 14) -> list[dict]:
    """Return daily log entries for recent days."""
    db = get_db()
    oldest = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT * FROM daily_log WHERE date >= ? ORDER BY date DESC", (oldest,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_daily_log_today() -> dict | None:
    """Return today's daily log entry, or None."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM daily_log WHERE date = ?", (date.today().isoformat(),)
    ).fetchone()
    return dict(row) if row else None


# ── Blood Markers ─────────────────────────────────────────────────────────────

def upsert_blood_marker(dt: str, marker: str, value: float, unit: str = None, notes: str = None):
    """Insert or update a blood marker result."""
    db = get_db()
    db.execute(
        "INSERT OR REPLACE INTO blood_markers (date, marker, value, unit, notes) VALUES (?, ?, ?, ?, ?)",
        (dt, marker, value, unit, notes),
    )
    db.commit()


def query_blood_markers(days: int = 730) -> list[dict]:
    """Return all blood marker entries."""
    db = get_db()
    oldest = (date.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT * FROM blood_markers WHERE date >= ? ORDER BY date DESC", (oldest,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── Background sync thread ──────────────────────────────────────────────────

_sync_thread = None
_sync_stop = threading.Event()  # cancellation signal for sync thread
_sync_lock = threading.Lock()   # serializes stop/start/restart


def _is_auth_error(exc: Exception) -> bool:
    """Best-effort detection of HTTP 401/403 from urllib / typed / generic.

    Recognises training.ICUAuthError first (typed, preferred), then falls back
    to urllib HTTPError and string-match for legacy paths.
    """
    try:
        from training import ICUAuthError
        if isinstance(exc, ICUAuthError):
            return True
    except Exception:
        pass
    try:
        from urllib.error import HTTPError
        if isinstance(exc, HTTPError) and exc.code in (401, 403):
            return True
    except Exception:
        pass
    msg = str(exc).lower()
    return "401" in msg or "403" in msg or "unauthorized" in msg


def _sync_loop(interval_sec: int = 1800):
    """Background thread that syncs every `interval_sec` seconds.

    Exponential backoff on failure:
      - success → reset counter, sleep interval_sec
      - failure → counter++, sleep min(3600, 60 * 2**min(counter, 6))
      - 5 consecutive HTTP 401s → set _auth_disabled=True, stop retrying
    """
    global _auth_disabled, _consecutive_failures, _last_sync_error
    consecutive_auth_failures = 0

    while not _sync_stop.is_set():
        if _auth_disabled:
            log.warning("Background sync disabled (auth failures); exiting loop")
            return
        try:
            run_sync(days=90)
            log.info("Background sync completed")
            _consecutive_failures = 0
            _last_sync_error = None
            consecutive_auth_failures = 0
            sleep_for = interval_sec
        except ICUCredentialsMissing as e:
            # No credentials — no point retrying until user configures them.
            _last_sync_error = str(e)
            log.info("Background sync skipped: %s", e)
            sleep_for = interval_sec
        except Exception as e:
            _consecutive_failures += 1
            _last_sync_error = str(e)
            log.error("Background sync failed (#%d): %s", _consecutive_failures, e)
            if _is_auth_error(e):
                consecutive_auth_failures += 1
                if consecutive_auth_failures >= 5:
                    _auth_disabled = True
                    log.error(
                        "5 consecutive auth failures — disabling background sync. "
                        "Check ICU credentials and restart."
                    )
                    return
            else:
                consecutive_auth_failures = 0
            # Exponential backoff, capped at 1 hour.
            backoff = 60 * (2 ** min(_consecutive_failures, 6))
            sleep_for = min(3600, max(interval_sec, backoff))
        _sync_stop.wait(sleep_for)  # interruptible sleep


def stop_sync() -> None:
    """Signal the sync thread to stop."""
    _sync_stop.set()


def restart_sync() -> None:
    """Stop old sync thread, close connections, start fresh.

    Guarded by _sync_lock to prevent racing stop/start from concurrent callers
    (e.g. profile switches). If the old thread doesn't exit within the join
    timeout, we refuse to start a new one to avoid two concurrent syncs.
    """
    global _sync_thread, _sync_stop, _auth_disabled, _consecutive_failures, _last_sync_error
    with _sync_lock:
        stop_sync()
        if _sync_thread is not None:
            _sync_thread.join(timeout=5)
            if _sync_thread.is_alive():
                log.error(
                    "Old sync thread still alive after 5s join — refusing to "
                    "start a new one to avoid double-sync. Investigate hung sync."
                )
                return
        close_all_connections()
        # Reset health state before starting fresh.
        _auth_disabled = False
        _consecutive_failures = 0
        _last_sync_error = None
        # Use a fresh Event under the lock so start/stop observers agree.
        _sync_stop = threading.Event()
        _sync_thread = threading.Thread(target=_sync_loop, daemon=True, name="sync")
        _sync_thread.start()


def start_background_sync():
    """Start the background sync thread (once).

    Guarded by _sync_lock so concurrent callers don't each create a thread.
    """
    global _sync_thread, _sync_stop
    with _sync_lock:
        if _sync_thread is not None and _sync_thread.is_alive():
            return
        _sync_stop = threading.Event()  # fresh event to avoid stale state
        _sync_thread = threading.Thread(target=_sync_loop, daemon=True, name="sync")
        _sync_thread.start()
