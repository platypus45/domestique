"""SQLite persistence layer for wellness & activity data from Intervals.icu."""

import clock  # the one clock every module reads (see src/clock.py)
import json
import logging
import os
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

# User data directory: writable, survives app updates.
# NOTE: directory creation is deferred to first DB write (init_db / set_db_path)
# so that profile_manager._maybe_migrate_data_dir() can detect a stale-but-
# empty ~/.domestique vs. a fresh install at boot. Otherwise this import
# would race ahead and create the new dir before the v3 migration runs.
from user_home import domestique_home
_USER_DATA = domestique_home()  # 3.4.3: DOMESTIQUE_HOME-aware (dev preview sandbox)

# Load .env — check user data dir first, then project dir
for _env_candidate in [_USER_DATA / ".env", Path(__file__).parent / ".env",
                       # dev mode, repo root (Part-B src/ layout)
                       Path(__file__).parent.parent / ".env"]:
    if _env_candidate.exists():
        for _line in _env_candidate.read_text().splitlines():
            if "=" in _line and not _line.startswith("#"):
                _k, _v = _line.split("=", 1)
                os.environ.setdefault(_k.strip(), _v.strip())
        break

from training import fetch_wellness, fetch_activities, ICUCredentialsMissing
from ride_storage import is_icu_stub  # v3.11.6 — one stub rule for every reader
from ride_storage import wellness_value as _wv  # one empty-value rule, both wellness stores

log = logging.getLogger(__name__)

# DB in user data dir (writable), NOT in PyInstaller bundle (read-only)
DB_PATH = _USER_DATA / "health_tracker.db"

_local = threading.local()
_db_version = 0  # incremented on profile switch; each thread tracks its own version


class SyncAborted(RuntimeError):
    """A gated sync write was abandoned: the owning identity (active profile,
    DB path, or purge epoch) changed between fetch and write, or a stop was
    requested while waiting for the write gate. Benign — the next pass
    re-fetches under the new identity."""


class SyncBusy(RuntimeError):
    """The sync write gate could not be acquired within the bounded wait.
    Callers (profile switch, purge) surface this as HTTP 503 — they must
    NEVER proceed unlocked."""


# ── AC1 write-window gate ────────────────────────────────────────────────────
# Data-plane lock: every sync WRITE SECTION (wellness batch, activities batch,
# sync_log row, each _refresh_* mirror, app.py's persist loops via
# sync_write_gate) holds it briefly; switch()/purge hold it while mutating
# identity. Lock order: pm._switch_lock → _sync_write_lock, never reverse.
# _sync_lock (below) stays control-plane only (thread stop/start).
_sync_write_lock = threading.Lock()
# Bumped by purge_profile_data() under the gate: in-flight passes that fetched
# BEFORE the purge fail their next snapshot check and abort (contract A10 —
# zero post-purge stale rows). Profile switches do NOT bump it: an A→B→A
# round-trip mid-fetch still targets A, which is the correct owner.
_sync_epoch = 0


def snapshot_sync_identity() -> tuple:
    """(active_profile_id, DB_PATH, sync_epoch) at this instant.

    Taken at the START of a sync pass / persist task; every subsequent write
    section re-checks it inside sync_write_gate(). Never raises — before the
    profile system is up it degrades to (None, DB_PATH, epoch), which still
    pins the DB path + epoch.
    """
    try:
        from profile_manager import ProfileManager
        pm = ProfileManager.get()
        pid = getattr(pm, "active_id", None)
    except Exception:
        pid = None
    return (pid, DB_PATH, _sync_epoch)


@contextmanager
def sync_write_gate(snapshot):
    """AC1 write-window gate — wrap every sync write section in this.

    Usage (FLOW applies the same pattern in app.py's lazy-sync / backfill /
    wellness-persist loops)::

        snap = db.snapshot_sync_identity()   # at task start, BEFORE fetching
        ...network fetches OUTSIDE any lock...
        with db.sync_write_gate(snap):
            ...writes targeting the snapshotted profile...

    Acquisition is a timeout-retry loop that re-checks the sync stop event so
    a stopping switch/restart can never deadlock against a blocked writer
    (restart_sync's bounded join stays safe). After acquiring, the CURRENT
    (profile, DB_PATH, epoch) must equal ``snapshot`` or SyncAborted is
    raised — a switch or purge landed since the fetch, so these writes would
    target the wrong profile.
    """
    try:
        snap_id, snap_path, snap_epoch = snapshot
    except (TypeError, ValueError):
        raise ValueError(
            "snapshot must be the 3-tuple from db.snapshot_sync_identity()"
        )
    while True:
        # Module-global lookup each iteration: restart_sync replaces the
        # Event object; a stale local reference would miss the new signal.
        if _sync_stop.is_set():
            raise SyncAborted("sync stop requested while waiting for write gate")
        if _sync_write_lock.acquire(timeout=0.25):
            break
    try:
        cur_id, cur_path, cur_epoch = snapshot_sync_identity()
        if (cur_id, cur_path, cur_epoch) != (snap_id, snap_path, snap_epoch):
            raise SyncAborted(
                f"sync identity changed since fetch: profile {snap_id!r} -> "
                f"{cur_id!r}, db {snap_path} -> {cur_path}, "
                f"epoch {snap_epoch} -> {cur_epoch}"
            )
        yield
    finally:
        _sync_write_lock.release()

# Background-sync state (exposed via get_sync_status())
_auth_disabled = False  # set True after repeated HTTP 401s; stops retry loop
_consecutive_failures = 0  # updated by _sync_loop; surfaced for diagnostics
_last_sync_error: str | None = None

# v3.0.1 (IP_ICU_PUSH D3b): optional hook called after each SUCCESSFUL sync
# pass. app.py registers its once-a-day ICU calendar reconcile here at boot
# (the CLI path never registers). Failures are logged and swallowed — a
# broken callback must never break the sync loop.
post_sync_callback = None


def get_db() -> sqlite3.Connection:
    """Return a thread-local database connection with WAL mode.

    Uses a version counter instead of a single bool flag to ensure ALL threads
    reopen after a profile switch (not just the first one to check).

    Raises RuntimeError when DB_PATH is the None sentinel (no active profile,
    AC6a) — connecting would mkdir + create an empty DB at a dead path, which
    is exactly the deleted-profile resurrection bug (probe L3).
    """
    if DB_PATH is None:
        raise RuntimeError("no active profile: database path is unset")
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


def set_db_path(path: "Path | None") -> None:
    """Point the process at another database, and make every thread follow.

    ``None`` is the AC6a no-active-profile sentinel (set by delete-last):
    get_db()/init_db() then raise / no-op instead of resurrecting files.

    The version bump is part of moving the path, not a second call the caller
    has to remember. Connections are per-thread (``_local.conn``), so a caller
    that repoints DB_PATH alone leaves every OTHER thread reading the OLD
    file: the request threads behind TestClient, the sync daemon, any pool
    thread that has already opened one. They keep that connection until
    something bumps ``_db_version``.

    That is not hypothetical. Four test fixtures restore the path this way in
    teardown, and the next test then seeds rows on the main thread while its
    endpoint reads the previous test's deleted temp database and answers "no
    data" -- test_one_fitness_state's readiness cases, failing in CI and
    passing alone, for as long as the two calls could be separated.
    """
    global DB_PATH
    DB_PATH = path
    close_all_connections()   # bumps _db_version; every thread reopens at `path`


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
    if DB_PATH is None:
        # AC6a: no active profile — nothing to initialize; creating a DB here
        # would resurrect root-dir artifacts after a delete-last.
        log.warning("init_db skipped: no active profile (DB path unset)")
        return
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
            -- v3.6.0: readiness-to-train, 1-10 where HIGH = READY (the
            -- opposite direction to the Hooper items above, where high = bad).
            -- Ten Haaf 2017 (PMID 27834554): pre-session fatigue +
            -- readiness-to-train discriminated functional overreaching in 30
            -- cyclists at 3 days (78% correct); it is NOT one of the four
            -- Hooper items, so it needs its own column and its own direction.
            readiness_to_train INTEGER CHECK(readiness_to_train BETWEEN 1 AND 10),
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
    # NOTE: SQLite cannot add a CHECK constraint to an existing table, so an
    # UPGRADED install gets a bare INTEGER where a fresh install has
    # CHECK(readiness_to_train BETWEEN 1 AND 10). Benign — upsert_daily_log
    # validates the range in Python on every write, which is the only path that
    # writes this column — and not worth rebuilding a table of rider history for.
    _maybe_add_column(db, "daily_log", "readiness_to_train", "INTEGER")
    # 3.14.0 F1: which Hooper answers a day's check-in took from intervals.icu
    # (sorted CSV of HOOPER_COLS). NULL = the rider's own check-in, which every
    # row from before 3.14.0 is: the import never writes such a row.
    _maybe_add_column(db, "daily_log", "icu_fields", "TEXT")
    _maybe_add_column(db, "activities", "distance_km", "REAL")
    _maybe_add_column(db, "activities", "kilojoules", "REAL")
    _maybe_add_column(db, "activities", "calories", "REAL")
    _maybe_add_column(db, "activities", "elevation_gain", "REAL")
    # v1.0.7 IMPL-TAU-FIT-WIRING (PATCH G11): is_race INTEGER (not BOOL) to
    # match SQLite affinity rules + sibling activities columns. tau_fitting
    # weights race-tagged rides higher in the marker count.
    _maybe_add_column(db, "activities", "is_race", "INTEGER DEFAULT 0")
    db.commit()


def sync_wellness(days: int = 90, _snapshot=None) -> int:
    """Fetch wellness from Intervals.icu and upsert into SQLite. Returns count.

    Transactional: if any row fails mid-loop, the entire batch is rolled back.

    AC1: the network fetch runs OUTSIDE any lock; the post-fetch write
    section holds ``_sync_write_lock`` and re-asserts the ``_snapshot``
    identity (raising SyncAborted when a profile switch / purge landed since
    the fetch). ``_snapshot`` defaults to a fresh snapshot for direct callers.
    """
    snapshot = _snapshot if _snapshot is not None else snapshot_sync_identity()
    try:
        data = fetch_wellness(days=days)
    except Exception as e:
        log.error("Failed to fetch wellness: %s", e)
        raise
    if not data:
        return 0

    count = 0
    with sync_write_gate(snapshot):
        db = get_db()
        # 3.14.0 F1 (grill L3): one look per pass. A database whose migration
        # did not run has no icu_fields column; the import then sits this pass
        # out with ONE warning instead of one per day (90 every 30 minutes).
        check_ins = _daily_log_has_icu_fields(db)
        if not check_ins:
            log.warning("check-in import skipped this pass: the daily log has "
                        "no icu_fields column (database not migrated)")
        try:
            for w in data:
                dt = w.get("id")
                if not dt:
                    continue
                si = w.get("sportInfo") or []
                eftp = si[0].get("eftp") if len(si) > 0 else None
                db.execute(
                    # An UPSERT that keeps what only Domestique knows, the same
                    # fix `activities` got for `is_race`. INSERT OR REPLACE is
                    # a DELETE plus an INSERT on a primary-key conflict, so a
                    # day intervals.icu has no HRV for wiped the reading the
                    # rider typed in themselves (/api/wellness/manual-hrv).
                    # COALESCE is the rule: ICU's value wins when ICU HAS one,
                    # and silence from ICU is not an answer that overwrites.
                    """INSERT INTO wellness
                       (date, ctl, atl, hrv, rhr, sleep_secs, sleep_score, eftp, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(date) DO UPDATE SET
                           ctl         = COALESCE(excluded.ctl, wellness.ctl),
                           atl         = COALESCE(excluded.atl, wellness.atl),
                           hrv         = COALESCE(excluded.hrv, wellness.hrv),
                           rhr         = COALESCE(excluded.rhr, wellness.rhr),
                           sleep_secs  = COALESCE(excluded.sleep_secs, wellness.sleep_secs),
                           sleep_score = COALESCE(excluded.sleep_score, wellness.sleep_score),
                           eftp        = COALESCE(excluded.eftp, wellness.eftp),
                           raw_json    = excluded.raw_json""",
                    (
                        dt,
                        # Empty is NULL, so COALESCE keeps the stored value:
                        # an hrv/restingHR/sleep of 0 is no reading either.
                        _wv("ctl", w.get("ctl")),
                        _wv("atl", w.get("atl")),
                        _wv("hrv", w.get("hrv")),
                        _wv("restingHR", w.get("restingHR")),
                        _wv("sleepSecs", w.get("sleepSecs")),
                        _wv("sleepScore", w.get("sleepScore")),
                        _wv("eftp", eftp),
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

                # v1.1.0 IMPL-NORWEGIAN-HR: pull max_hr from ICU wellness payload
                # when exposed (athlete profile may carry it as `maxHr` or
                # `max_hr`). Same manual-source guard as wPrime / pMax.
                # PATCH G6: metric name is `max_hr` (not `hr_max`) — matches
                # ProfileManager.max_hr property at profile_manager.py:147.
                ahr = w.get("maxHr") or w.get("max_hr")
                if ahr and isinstance(ahr, (int, float)) and 140 <= ahr <= 220:
                    existing_hr = db.execute(
                        "SELECT source FROM athlete_metrics WHERE date = ? AND metric = 'max_hr'",
                        (dt,),
                    ).fetchone()
                    if not (existing_hr and existing_hr[0] == "manual"):
                        db.execute(
                            "INSERT OR REPLACE INTO athlete_metrics (date, metric, value, source) VALUES (?, 'max_hr', ?, 'intervals.icu')",
                            (dt, round(ahr)),
                        )

                # 3.14.0 W1: the scale weight logged in intervals.icu fills
                # the weight history (Body card). Same guard as eFTP: a weight
                # the rider logged or saved that day is never replaced. Display
                # only; the profile weight moves only for a rider who ticked
                # "Use my intervals.icu weight" (_refresh_weight_from_metrics).
                wt = _wv("weight", w.get("weight"))
                if (isinstance(wt, (int, float)) and not isinstance(wt, bool)
                        and WEIGHT_KG_RANGE[0] <= wt <= WEIGHT_KG_RANGE[1]):
                    existing_wt = db.execute(
                        "SELECT source FROM athlete_metrics WHERE date = ? AND metric = 'weight'",
                        (dt,),
                    ).fetchone()
                    if not (existing_wt and existing_wt[0] in ("manual", "settings")):
                        db.execute(
                            "INSERT OR REPLACE INTO athlete_metrics (date, metric, value, source) VALUES (?, 'weight', ?, 'intervals.icu')",
                            (dt, round(float(wt), 1)),
                        )

                # 3.14.0 F1: the day's morning check-in from intervals.icu.
                # After the wellness INSERT on purpose: that INSERT opened
                # this batch's write transaction, so the import's read and
                # write cannot interleave with a form save on another
                # connection (the save waits for SQLite's write lock and then
                # replaces the row, making the day the rider's). The import's
                # own guard keeps a rider's row even without that order.
                if check_ins:
                    _import_icu_check_in(db, dt, w)

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
    # AC1: each mirror is its own gated write section against `snapshot` —
    # a switch landing between the batch above and a mirror skips the mirror.
    _refresh_wprime_from_metrics(snapshot)
    # v1.0.6 IMPL-3D-INGEST: same mirror pattern for Pmax.
    _refresh_pmax_from_metrics(snapshot)
    # v1.1.0 IMPL-NORWEGIAN-HR: same mirror pattern for max_hr.
    _refresh_max_hr_from_metrics(snapshot)
    # 3.14.0 W2: the profile weight, only for a rider who chose to follow it.
    _refresh_weight_from_metrics(snapshot)

    return count


def _refresh_wprime_from_metrics(snapshot=None) -> None:
    """Copy the newest athlete_metrics.w_prime (source='intervals.icu') into
    the active ProfileManager athlete.wprime_j.

    Called after `sync_wellness()` finishes its ICU batch. Failures are
    logged but never re-raised — the ICU sync should still succeed even
    if profile mirroring misfires (e.g. no profile loaded yet, disk
    full). Reading the latest row is cheap (PK date prefix index) so this
    is O(1) regardless of metrics-table size.

    AC1: the read+write runs inside the sync write gate targeting
    ``snapshot``; when the identity moved (profile switch / purge) the
    mirror is SKIPPED (SyncAborted logged at INFO) instead of stamping
    the wrong profile. Re-resolving ProfileManager.get() inside the gated
    section is safe — switch() needs the same gate to mutate identity.
    """
    if snapshot is None:
        snapshot = snapshot_sync_identity()
    try:
        with sync_write_gate(snapshot):
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
    except SyncAborted as e:
        log.info("wprime mirror skipped: %s", e)
    except Exception as e:
        log.warning("refresh_wprime_from_metrics failed: %s", e)


def _refresh_pmax_from_metrics(snapshot=None) -> None:
    """v1.0.6 IMPL-3D-INGEST: copy the newest athlete_metrics.pmax row
    (source='intervals.icu') into the active ProfileManager athlete.pmax_w.

    Mirror of `_refresh_wprime_from_metrics()` (incl. the AC1 write gate).
    Called after sync_wellness() finishes its ICU batch. Failures are
    logged but never re-raised.
    """
    if snapshot is None:
        snapshot = snapshot_sync_identity()
    try:
        with sync_write_gate(snapshot):
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
    except SyncAborted as e:
        log.info("pmax mirror skipped: %s", e)
    except Exception as e:
        log.warning("refresh_pmax_from_metrics failed: %s", e)


def _refresh_max_hr_from_metrics(snapshot=None) -> None:
    """v1.1.0 IMPL-NORWEGIAN-HR: copy the newest athlete_metrics.max_hr row
    (source='intervals.icu') into the active ProfileManager athlete.max_hr.

    Mirror of `_refresh_wprime_from_metrics()` (incl. the AC1 write gate).
    Called after sync_wellness() finishes its ICU batch. Failures are
    logged but never re-raised.

    PATCH G6: metric name is `max_hr` (not `hr_max`) — matches
    ProfileManager.max_hr property at profile_manager.py:147.
    """
    if snapshot is None:
        snapshot = snapshot_sync_identity()
    try:
        with sync_write_gate(snapshot):
            from profile_manager import ProfileManager
            pm = ProfileManager.get()
            db = get_db()
            row = db.execute(
                "SELECT value, source FROM athlete_metrics "
                "WHERE metric = 'max_hr' ORDER BY date DESC LIMIT 1"
            ).fetchone()
            if not row:
                return
            value, source = row[0], row[1]
            if value is None or float(value) <= 0:
                return
            # Only mirror intervals.icu-sourced max_hr. Manual rows go through
            # save_athlete directly with source="manual" already.
            if source != "intervals.icu":
                return
            pm._set_max_hr(int(float(value)), "icu")
    except SyncAborted as e:
        log.info("max_hr mirror skipped: %s", e)
    except Exception as e:
        log.warning("refresh_max_hr_from_metrics failed: %s", e)


# 3.14.0 W1/W2 — the intervals.icu weight.
WEIGHT_KG_RANGE = (30, 200)        # the range save_athlete and the wizard accept
# One definition of "the rider's intervals.icu weight" for the follow mirror,
# the hand-back when the rider ticks the box, and the wizard's prefill (grill
# M5): the newest scale reading of the last 30 days. An older reading is not
# today's weight, and following it silently replaced a weight the rider had
# just confirmed.
ICU_WEIGHT_RECENT_DAYS = 30
# ponytail: a fixed 0.5 kg dead band against daily scale noise (±0.5–1 kg),
# so W/kg, the FTP-band advisory and refits do not follow every weigh-in.
# Upgrade path: follow a 7-day median if riders report drift.
WEIGHT_FOLLOW_MIN_CHANGE_KG = 0.5


def icu_weight_recent(days: int = ICU_WEIGHT_RECENT_DAYS) -> tuple[str, float] | None:
    """(date, kg) of the newest intervals.icu weight in the last ``days``
    days, or None. Reads the rows the wellness sync stores (W1)."""
    oldest = (clock.today() - timedelta(days=days)).isoformat()
    row = get_db().execute(
        "SELECT date, value FROM athlete_metrics WHERE metric = 'weight' "
        "AND source = 'intervals.icu' AND date >= ? ORDER BY date DESC LIMIT 1",
        (oldest,),
    ).fetchone()
    if not row or row[1] is None:
        return None
    return row[0], round(float(row[1]), 1)


def icu_weight_from_records(records, days: int = ICU_WEIGHT_RECENT_DAYS) -> tuple[str, float] | None:
    """icu_weight_recent()'s rule on wellness records fetched from
    intervals.icu before anything is stored (the setup wizard's prefill)."""
    oldest = (clock.today() - timedelta(days=days)).isoformat()
    best = None
    for w in records or []:
        if not isinstance(w, dict):
            continue
        d = str(w.get("id") or "")
        wt = _wv("weight", w.get("weight"))
        if (d >= oldest and isinstance(wt, (int, float)) and not isinstance(wt, bool)
                and WEIGHT_KG_RANGE[0] <= wt <= WEIGHT_KG_RANGE[1]
                and (best is None or d > best[0])):
            best = (d, round(float(wt), 1))
    return best


def _refresh_weight_from_metrics(snapshot=None) -> None:
    """3.14.0 W2: follow the newest intervals.icu weight into the profile, for
    a rider who ticked "Use my intervals.icu weight" (weight_source "icu")
    and only then. A typed weight ("manual") and a profile with no source
    recorded (every profile from before 3.14.0) are the rider's and never
    move here; the explicit check is the real guard, because the fact table
    lets any source write over a weight with no source.

    Same AC1 write gate as the other mirrors. Failures are logged, never
    raised: the sync has already landed its batch.
    """
    if snapshot is None:
        snapshot = snapshot_sync_identity()
    try:
        with sync_write_gate(snapshot):
            from profile_manager import ProfileManager
            pm = ProfileManager.get()
            if pm._athlete.get("weight_source") != "icu":
                return
            newest = icu_weight_recent()
            if newest is None:
                return
            value = newest[1]
            current = pm._athlete.get("weight_kg")
            try:
                if current is not None and abs(float(current) - value) < WEIGHT_FOLLOW_MIN_CHANGE_KG:
                    return
            except (TypeError, ValueError):
                pass
            pm._set_fact("weight", value, "icu")
    except SyncAborted as e:
        log.info("weight mirror skipped: %s", e)
    except Exception as e:
        log.warning("refresh_weight_from_metrics failed: %s", e)


def _refresh_hr_from_activities(snapshot=None) -> None:
    """v2.5.0 W3: mirror LTHR + max_hr from the newest synced ICU activity.

    AC1: read+write run inside the sync write gate targeting ``snapshot``
    (default: fresh snapshot for direct callers). A mid-sync profile switch
    or purge skips the mirror (SyncAborted, INFO) — the alice/bob repro's
    R2 defect was exactly this mirror stamping A's LTHR into B's profile.

    ICU activity payloads carry the athlete-level fields `lthr` and
    `athlete_max_hr` (live-verified 177/195, ACTIVITY:READ scope). The
    wellness payload does NOT carry maxHr/lthr — the sync_wellness max_hr
    block above never fires in practice — and athlete/sportSettings is 403
    without SETTINGS:READ. So the newest activity row is the one live
    source for both numbers.

    Mirror of `_refresh_wprime_from_metrics()`: called after
    sync_activities() commits its ICU batch. Failures are logged but never
    re-raised.

    Guards:
      * lthr_source == "manual" blocks the LTHR mirror (a settings-form
        LTHR is stamped manual in app.py W2d and must never be clobbered);
        max_hr precedence lives in pm._set_max_hr (manual > icu) and is
        pre-checked here so a blocked write stays silent.
      * ranges: lthr [100, 220], athlete_max_hr [140, 220]; each field is
        taken from the newest activity where IT is plausible.
      * max_hr must stay > lthr after the write — a violating post-write
        pair skips both writes with one WARNING.
      * unchanged values are skipped (idempotent — no churn writes, no
        daily lthr_source_date advance). One INFO line on change.
    """
    if snapshot is None:
        snapshot = snapshot_sync_identity()
    try:
        with sync_write_gate(snapshot):
            from profile_manager import ProfileManager
            pm = ProfileManager.get()
            db = get_db()
            # Newest first (idx_activities_date); ICU echoes the athlete-level
            # HR fields on every activity, the LIMIT just bounds the scan when
            # recent payloads carry no plausible values at all.
            rows = db.execute(
                "SELECT json_extract(raw_json, '$.lthr'),"
                "       json_extract(raw_json, '$.athlete_max_hr')"
                " FROM activities ORDER BY date DESC, id DESC LIMIT 20"
            ).fetchall()
            lthr = next((int(r[0]) for r in rows
                         if isinstance(r[0], (int, float)) and 100 <= r[0] <= 220),
                        None)
            max_hr = next((int(r[1]) for r in rows
                           if isinstance(r[1], (int, float)) and 140 <= r[1] <= 220),
                          None)
            if lthr is None and max_hr is None:
                return

            # Raw dict reads (not the properties): pm.lthr defaults to 170, which
            # would mask an unset key and skip the first-ever mirror.
            cur_lthr = pm._athlete.get("lthr")
            cur_max = pm._athlete.get("max_hr")
            write_lthr = (
                lthr is not None
                and str(pm._athlete.get("lthr_source", "") or "") != "manual"
                and lthr != cur_lthr
            )
            write_max = (
                max_hr is not None
                and str(pm._athlete.get("max_hr_source", "") or "") != "manual"
                and max_hr != cur_max
            )
            if not (write_lthr or write_max):
                return

            # Post-write invariant: max_hr > lthr (target_mode's hr gate). Only
            # checkable when both sides exist; a missing side means there is no
            # pair to violate and hr mode is separately gated by lthr_is_set.
            final_lthr = lthr if write_lthr else cur_lthr
            final_max = max_hr if write_max else cur_max
            if final_lthr is not None and final_max is not None \
                    and float(final_max) <= float(final_lthr):
                log.warning(
                    "hr mirror skipped: max_hr=%s <= lthr=%s would break the "
                    "hr-mode invariant (activity payload lthr=%s athlete_max_hr=%s)",
                    final_max, final_lthr, lthr, max_hr,
                )
                return

            changed = []
            if write_lthr:
                # save_athlete validates [100, 220] and writes atomically; the
                # icu + date stamp rides along (W2d provenance hint reads it).
                pm.save_athlete({
                    "lthr": lthr,
                    "lthr_source": "icu",
                    "lthr_source_date": clock.today().isoformat(),
                })
                changed.append(f"lthr={lthr}")
            if write_max:
                pm._set_max_hr(max_hr, "icu")
                changed.append(f"max_hr={max_hr}")
            log.info("EVENT=hr_mirror_from_activity %s source=icu", " ".join(changed))
    except SyncAborted as e:
        log.info("hr mirror skipped: %s", e)
    except Exception as e:
        log.warning("refresh_hr_from_activities failed: %s", e)


def sync_activities(days: int = 90, _snapshot=None) -> int:
    """Fetch activities from Intervals.icu and upsert into SQLite. Returns count.

    Transactional: if any row fails mid-loop, the entire batch is rolled back.

    AC1: fetch OUTSIDE any lock; the post-fetch write section holds the sync
    write gate against ``_snapshot`` (raises SyncAborted when the identity
    moved) — see sync_wellness().
    """
    snapshot = _snapshot if _snapshot is not None else snapshot_sync_identity()
    try:
        data = fetch_activities(days=days)
    except Exception as e:
        log.error("Failed to fetch activities: %s", e)
        raise
    if not data:
        return 0

    count = 0
    with sync_write_gate(snapshot):
        db = get_db()
        try:
            for a in data:
                aid = a.get("id", a.get("start_date_local", ""))
                dt = a.get("start_date_local", "")[:10]
                if not aid:
                    continue
                # v3.11.6 (issue #11) — same rule as the ride store: a stub
                # ICU will not re-share (Strava-origin) has no data to mirror.
                if is_icu_stub(a):
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
                    # An UPSERT, not INSERT OR REPLACE. On a primary-key
                    # conflict SQLite implements OR REPLACE as DELETE then
                    # INSERT, so every column this statement does not name
                    # went back to its schema default -- and `is_race`, which
                    # only Domestique knows, was reset to 0 on every sync for
                    # every ride. Naming the columns intervals.icu owns means
                    # the local ones are preserved by default, so the next
                    # locally-decided column cannot be erased by omission.
                    """INSERT INTO activities
                       (id, date, name, sport, duration_sec, tss, avg_power, avg_hr,
                        distance_km, kilojoules, calories, elevation_gain, raw_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(id) DO UPDATE SET
                           date = excluded.date,
                           name = excluded.name,
                           sport = excluded.sport,
                           duration_sec = excluded.duration_sec,
                           tss = excluded.tss,
                           avg_power = excluded.avg_power,
                           avg_hr = excluded.avg_hr,
                           distance_km = excluded.distance_km,
                           kilojoules = excluded.kilojoules,
                           calories = excluded.calories,
                           elevation_gain = excluded.elevation_gain,
                           raw_json = excluded.raw_json""",
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

    # v2.5.0 W3: mirror the athlete-level lthr / athlete_max_hr carried on
    # every ICU activity payload into the active profile — same post-batch
    # pattern as the _refresh_* calls at the end of sync_wellness().
    # AC1: gated against the same snapshot (its own short write section).
    _refresh_hr_from_activities(snapshot)

    return count


def run_sync(days: int = 90) -> dict:
    """Run full sync and log result. Skips if no ICU credentials configured.

    Wraps sync_wellness() + sync_activities() in a single logical transaction:
    either both succeed (single sync_log row, status=ok) or both roll back
    (sync_log row recorded with status=error and exception message).

    AC1: snapshots the owning (profile_id, DB_PATH, epoch) at ENTRY; every
    write section (wellness batch, activities batch, mirrors, the sync_log
    row) re-asserts it under the write gate. A mid-pass profile switch /
    purge raises SyncAborted (benign — the pass is abandoned; nothing was
    written to the wrong profile).
    """
    import config
    log.info("EVENT=sync_start days=%s", days)
    if not getattr(config, "ICU_ATHLETE_ID", None):
        # Auth header is Bearer (OAuth) or Basic (key), but the wellness/activity
        # URLs need the athlete id in the path — surface WHY we skipped.
        has_token = bool(getattr(config, "ICU_ACCESS_TOKEN", None))
        log.info("EVENT=sync_skipped reason=no_athlete_id oauth_token=%s", has_token)
        return {"timestamp": clock.now().isoformat(), "wellness": 0,
                "activities": 0, "status": "skipped", "error": "No ICU credentials"}
    snapshot = snapshot_sync_identity()
    ts = clock.now().isoformat()
    w_count = a_count = 0
    error = None
    status = "ok"
    sync_exc: Exception | None = None
    try:
        w_count = sync_wellness(days, _snapshot=snapshot)
        a_count = sync_activities(days, _snapshot=snapshot)
    except SyncAborted as e:
        # Identity changed mid-pass (switch/purge). Do NOT write sync_log —
        # it would need the gate and target the departed profile anyway.
        log.info("EVENT=sync_aborted days=%s reason=%s", days, e)
        raise
    except Exception as e:
        # Each sync_* already rolled back its own partial batch inside its
        # gated section; a fetch-level failure leaves no open transaction.
        error = str(e)
        status = "error"
        sync_exc = e
        log.error("Sync error: %s", e)

    # sync_log write happens regardless of outcome, in its own gated
    # transaction (it's a write section too — db:650-654 in the grill).
    try:
        with sync_write_gate(snapshot):
            db = get_db()
            db.execute(
                "INSERT INTO sync_log (timestamp, wellness_synced, activity_synced, status, error) VALUES (?, ?, ?, ?, ?)",
                (ts, w_count, a_count, status, error),
            )
            db.commit()
    except SyncAborted as e:
        # Data batches landed on the right profile before the switch; only
        # the log row is skipped.
        log.info("EVENT=sync_log_skipped reason=%s", e)
    except Exception as log_exc:
        log.error("Failed to write sync_log: %s", log_exc)

    if sync_exc is not None:
        # Re-raise so callers / background loop can react (e.g. backoff on 401).
        log.info("EVENT=sync_done status=error wellness=%d activities=%d err=%s",
                 w_count, a_count, str(sync_exc)[:120])
        raise sync_exc
    log.info("EVENT=sync_done status=%s wellness=%d activities=%d", status, w_count, a_count)
    return {"timestamp": ts, "wellness": w_count, "activities": a_count, "status": status, "error": error}


def query_wellness(days: int = 28) -> list[dict]:
    """Query wellness from local SQLite."""
    db = get_db()
    oldest = (clock.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT * FROM wellness WHERE date >= ? ORDER BY date", (oldest,)
    ).fetchall()
    return [dict(r) for r in rows]


def query_activities(days: int = 14) -> list[dict]:
    """Query activities from local SQLite."""
    db = get_db()
    oldest = (clock.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT * FROM activities WHERE date >= ? ORDER BY date", (oldest,)
    ).fetchall()
    return [dict(r) for r in rows]


def _set_is_race(activity_id: str, is_race: bool) -> bool:
    """v1.0.7 IMPL-TAU-FIT-WIRING — set the is_race flag on an activity row.

    Returns True if the row existed (and was updated), False otherwise.
    Casts the bool to 0/1 at the boundary so the SQLite column stays an
    INTEGER (PATCH G11). Idempotent — toggling to the same value is a no-op
    update (no row-count change visible to the caller).
    """
    if not isinstance(activity_id, str) or not activity_id:
        raise ValueError("activity_id must be a non-empty string")
    db = get_db()
    cur = db.execute(
        "UPDATE activities SET is_race = ? WHERE id = ?",
        (1 if is_race else 0, activity_id),
    )
    db.commit()
    return cur.rowcount > 0


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
    today_str = clock.today().isoformat()
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
    oldest = (clock.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT date, value, source, notes FROM athlete_metrics WHERE metric = ? AND date >= ? ORDER BY date",
        (metric, oldest),
    ).fetchall()
    return [dict(r) for r in rows]


def query_wkg_history(days: int = 365) -> list[dict]:
    """Query W/kg history (derived from weight + ftp)."""
    db = get_db()
    oldest = (clock.today() - timedelta(days=days)).isoformat()
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

# The four Hooper questions of the morning check-in (1 = best, 7 = worst).
HOOPER_COLS = ("sleep_quality", "fatigue", "soreness", "stress")

# 3.14.0 F1 — the morning check-in from intervals.icu wellness.
#
# intervals.icu's scales are not in its API schema; its developer documented
# them on the public forum (sources pinned in tests/test_wellness_import.py):
# soreness/fatigue/stress 0 None, 1 Low, 2 Avg, 3 High, 4 Extreme; sleepQuality
# 1 Great, 2 Good, 3 Average, 4 Poor; injury 1 None, 2 Niggle, 3 Poor,
# 4 Injured. 1 (or 0) is always the good end. Each answer maps to the morning
# form's button with the same label and is stored the way the form stores
# that button (round(level * 7 / 5)), so an answer given in intervals.icu and
# the same tap in Domestique are the same number for every reader.
_FORM_STORE = {1: 1, 2: 3, 3: 4, 4: 6, 5: 7}   # form button (1-5) -> stored 1-7
_FORM_NORMAL = 4     # a question not answered: the form's untouched "Normal"
_FORM_MOOD = 4       # the form never asks mood and always stores 4
_ICU_CHECKIN = {
    # intervals.icu field: (daily_log column, {code: form button})
    "soreness":     ("soreness",      {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}),
    "fatigue":      ("fatigue",       {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}),
    "stress":       ("stress",        {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}),
    "sleepQuality": ("sleep_quality", {1: 1, 2: 2, 3: 3, 4: 4}),
}
# Owner decision (3.14.0, grill M3): an injury logged as Poor (3) or Injured
# (4) counts as sore legs ("Sore") for the check-in, so the recovery gate sees
# it instead of the verdict reading "Ready to train".
_ICU_INJURY_CODES = (1, 2, 3, 4)
_ICU_INJURY_SORE_FROM = 3
_FORM_SORE = _FORM_STORE[4]
# intervals.icu's injury labels, the one table every reader uses (the card's
# extras line, the readiness verdict's line, the recovery gate's wording).
ICU_INJURY_LABELS = {1: "None", 2: "Niggle", 3: "Poor", 4: "Injured"}


def _icu_code(v):
    """An intervals.icu answer code as an int, or None (missing, null,
    a string, a bool, a non-integral number)."""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def icu_check_in(record: dict) -> dict:
    """The Hooper answers in one intervals.icu wellness day, stored as the
    form would store them: {column: 1..7}. {} when the day is not a check-in.

    A day counts only when soreness, fatigue or stress was answered (or an
    injury that counts as soreness): watches fill sleep quality in from their
    sleep score, so sleep alone is not an answer. And a sleep quality on a
    record that carries a sleep score is the watch's, not the rider's (grill
    H2): it is left unanswered, and the watch's sleep still reaches readiness
    through the sleep-hours channel.
    """
    if not isinstance(record, dict):
        return {}
    out = {}
    for key, (col, levels) in _ICU_CHECKIN.items():
        v = _icu_code(record.get(key))
        if v is None or v not in levels:
            continue                       # missing, null or off the scale
        if key == "sleepQuality" and _wv("sleepScore", record.get("sleepScore")) is not None:
            continue
        out[col] = _FORM_STORE[levels[v]]
    injury = _icu_code(record.get("injury"))
    if injury in _ICU_INJURY_CODES and injury >= _ICU_INJURY_SORE_FROM:
        out["soreness"] = max(out.get("soreness", 0), _FORM_SORE)
    return out if out.keys() & {"soreness", "fatigue", "stress"} else {}


def icu_soreness_from_injury(record: dict) -> bool:
    """True when the day's soreness reaches "Sore" only because of the injury
    logged in intervals.icu (the card names it "soreness (from your injury)")."""
    if not isinstance(record, dict):
        return False
    injury = _icu_code(record.get("injury"))
    if not (injury in _ICU_INJURY_CODES and injury >= _ICU_INJURY_SORE_FROM):
        return False
    rest = {k: v for k, v in record.items() if k != "injury"}
    return (icu_check_in(rest) or {}).get("soreness", 0) < _FORM_SORE


def icu_injury_label(dt: str) -> str | None:
    """The injury the rider logged in intervals.icu on ``dt`` when it counts as
    sore legs ("Poor" / "Injured"), else None. Reads the stored wellness day."""
    row = get_db().execute("SELECT raw_json FROM wellness WHERE date = ?", (dt,)).fetchone()
    try:
        record = json.loads(row[0]) if row and row[0] else {}
    except (TypeError, ValueError):
        return None
    injury = _icu_code((record or {}).get("injury")) if isinstance(record, dict) else None
    if injury in _ICU_INJURY_CODES and injury >= _ICU_INJURY_SORE_FROM:
        return ICU_INJURY_LABELS[injury]
    return None


def _daily_log_has_icu_fields(conn) -> bool:
    try:
        return any(r[1] == "icu_fields"
                   for r in conn.execute("PRAGMA table_info(daily_log)").fetchall())
    except sqlite3.Error:
        return False


def _import_icu_check_in(conn, dt: str, record: dict) -> None:
    """Fill the day's morning check-in from intervals.icu, unless the day is
    the rider's.

    Writes only rows the import owns (icu_fields set). The rider owns every
    row with icu_fields NULL, including every row from before 3.14.0, and a
    save from the form makes an imported day the rider's. A present record
    rebuilds the imported day from scratch (grill M2): an answer cleared in
    intervals.icu goes back to unanswered, and a record with no answer left
    removes the imported row. A day the sync did not fetch is not touched.

    Runs inside sync_wellness's batch; it catches its own errors, because a
    malformed answer must never roll back the HRV/CTL batch around it.
    """
    try:
        row = conn.execute(
            "SELECT icu_fields FROM daily_log WHERE date = ?", (dt,)).fetchone()
        if row is not None and not row[0]:
            return                         # the rider's check-in
        answers = icu_check_in(record)
        if not answers:
            if row is not None:
                conn.execute("DELETE FROM daily_log WHERE date = ? "
                             "AND icu_fields IS NOT NULL", (dt,))
            return
        vals = {c: _FORM_NORMAL for c in HOOPER_COLS}
        vals.update(answers)
        conn.execute(
            """INSERT INTO daily_log (date, sleep_quality, fatigue, soreness, stress,
                                      mood, hooper_index, icu_fields)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date) DO UPDATE SET
                   sleep_quality = excluded.sleep_quality,
                   fatigue = excluded.fatigue, soreness = excluded.soreness,
                   stress = excluded.stress, mood = excluded.mood,
                   hooper_index = excluded.hooper_index,
                   icu_fields = excluded.icu_fields
               WHERE daily_log.icu_fields IS NOT NULL""",   # never the rider's row
            (dt, vals["sleep_quality"], vals["fatigue"], vals["soreness"],
             vals["stress"], _FORM_MOOD, sum(vals.values()),
             ",".join(sorted(answers))))
    except Exception as e:  # noqa: BLE001 — never cost the wellness batch
        log.warning("check-in import skipped for %s: %s", dt, e)


def upsert_daily_log(dt: str, sleep_quality: int, fatigue: int, soreness: int,
                     stress: int, mood: int, notes: str = None,
                     readiness_to_train: int = None) -> dict:
    """Insert or update daily wellness log. Returns the entry.

    v4.6.6 IMPL-C: hooper_index = sleep_quality + fatigue + stress + soreness
    (sum of 4 fields each 1-7, range 4-28), the well-being sum of Hooper et
    al. 1995 (Med Sci Sports Exerc 27:106-112). What a sum means for training
    is decided in one place, readiness_composite.hooper_band (3.14.0).

    Each input field MUST be int 1..7; raises ValueError otherwise. (Schema
    CHECK enforces it too, but we validate up-front so the error surfaces as
    a 400 in the API rather than a 500 from sqlite.)
    """
    for nm, v in (("sleep_quality", sleep_quality), ("fatigue", fatigue),
                  ("soreness", soreness), ("stress", stress), ("mood", mood)):
        if not isinstance(v, int) or not (1 <= v <= 7):
            raise ValueError(f"{nm} must be int 1..7, got {v!r}")
    if readiness_to_train is not None:
        if not isinstance(readiness_to_train, int) or not (1 <= readiness_to_train <= 10):
            raise ValueError(
                f"readiness_to_train must be int 1..10, got {readiness_to_train!r}")
    hooper = sleep_quality + fatigue + stress + soreness
    db = get_db()
    # v3.6.0 — INSERT OR REPLACE rewrites the WHOLE row, so a save that omits
    # readiness_to_train (the Hooper form posting without it) would NULL a
    # rating the rider already gave. Carry the stored value forward when the
    # caller passes None.
    #
    # Consequence, deliberate: `None` means "omitted", so there is no value a
    # caller can pass to CLEAR a rating. Nothing exposes clearing today — the
    # form only ever adds the key, and re-rating overwrites. If a clear
    # affordance is ever added it needs its own sentinel, not None.
    if readiness_to_train is None:
        try:
            prior = db.execute(
                "SELECT readiness_to_train FROM daily_log WHERE date = ?", (dt,)
            ).fetchone()
            if prior is not None and prior[0] is not None:
                readiness_to_train = int(prior[0])
        except sqlite3.Error:
            pass
    # INSERT OR REPLACE also clears icu_fields: a save from the form makes an
    # imported day the rider's, and the import never writes it again.
    db.execute(
        """INSERT OR REPLACE INTO daily_log
           (date, sleep_quality, fatigue, soreness, stress, mood, hooper_index,
            notes, readiness_to_train)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (dt, sleep_quality, fatigue, soreness, stress, mood, hooper, notes,
         readiness_to_train),
    )
    db.commit()
    return {
        "date": dt, "sleep_quality": sleep_quality, "fatigue": fatigue,
        "soreness": soreness, "stress": stress, "mood": mood,
        "hooper_index": hooper, "notes": notes,
        "readiness_to_train": readiness_to_train,
    }


def query_daily_log(days: int = 14) -> list[dict]:
    """Return daily log entries for recent days."""
    db = get_db()
    oldest = (clock.today() - timedelta(days=days)).isoformat()
    rows = db.execute(
        "SELECT * FROM daily_log WHERE date >= ? ORDER BY date DESC", (oldest,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_daily_log_today() -> dict | None:
    """Return today's daily log entry, or None."""
    db = get_db()
    row = db.execute(
        "SELECT * FROM daily_log WHERE date = ?", (clock.today().isoformat(),)
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
    oldest = (clock.today() - timedelta(days=days)).isoformat()
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
    # A typed NON-auth ICU error is a deliberate classification and must win
    # over the substring fallback below. training._get maps a missing-scope 403
    # to ICUServerError precisely so a working read-only connection is not
    # nagged to reconnect — the "403" substring undid that. It also read
    # ICUServerError("HTTP 500 on activity/i403992") as an auth failure,
    # because the ACTIVITY ID contains "403".
    try:
        from training import ICUNetworkError, ICURateLimitError, ICUServerError
        if isinstance(exc, (ICUNetworkError, ICURateLimitError, ICUServerError)):
            return False
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


def _is_dead_credential(exc: Exception) -> bool:
    """HTTP 401 from ICU — the credential is gone, not a blip.

    Probed live against intervals.icu: an invalid bearer token, an invalid API
    key and a bad athlete path all answer 401. A blocked User-Agent answers 403
    (Cloudflare "error code: 1010") and a missing scope answers 403, so only 401
    is proof. Network blips never reach here at all — they are URLError /
    timeout / 5xx, retried 3× inside training._get and raised as a different
    exception type.
    """
    try:
        from training import ICUAuthError
        return isinstance(exc, ICUAuthError) and getattr(exc, "status", None) == 401
    except Exception:
        return False


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
            cb = post_sync_callback
            if cb is not None:
                try:
                    cb()
                except Exception:
                    log.debug("post-sync callback failed", exc_info=True)
        except ICUCredentialsMissing as e:
            # No credentials — no point retrying until user configures them.
            _last_sync_error = str(e)
            log.info("Background sync skipped: %s", e)
            sleep_for = interval_sec
        except SyncAborted as e:
            # AC1: a profile switch / purge landed mid-pass. Not a failure —
            # the stop event is normally set too, so the while-condition
            # exits and restart_sync spawns a fresh thread for the new
            # profile; a pure epoch bump (purge) just waits for next tick.
            log.info("Background sync pass aborted: %s", e)
            sleep_for = interval_sec
        except Exception as e:
            _consecutive_failures += 1
            _last_sync_error = str(e)
            log.error("Background sync failed (#%d): %s", _consecutive_failures, e)
            if _is_auth_error(e):
                consecutive_auth_failures += 1
                # A 401 is a dead credential — pause on the FIRST one. The
                # 5-strike ladder cost 2h of CONTINUOUS uptime before the
                # reconnect banner appeared (4 × 1800s of interruptible sleep),
                # and this counter is loop-local, so every app restart put it
                # back to zero. On a desktop app opened for ten minutes, a
                # revoked token read as "sync just stops working, no message".
                # 403 keeps the budget: it is not proof of a dead credential.
                if _is_dead_credential(e) or consecutive_auth_failures >= 5:
                    _auth_disabled = True
                    log.error(
                        "EVENT=icu_auth_disabled after=%d auth failure(s) — "
                        "pausing background sync. Reconnect intervals.icu.",
                        consecutive_auth_failures
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


def shutdown_sync(timeout: float = 5.0) -> bool:
    """Stop the sync thread AND reset the stop event (AC6a delete-last).

    Plain stop_sync() leaves the stop flag set until the next restart_sync;
    after a delete-last there IS no restart, and a permanently-set flag
    would make every future write gate abort (including in an unrelated
    later profile session in the same process). This joins the thread
    bounded, then swaps in a fresh unset Event — safe because the old
    thread is confirmed dead (or we keep the set flag and report False).
    """
    global _sync_thread, _sync_stop
    with _sync_lock:
        _sync_stop.set()
        t = _sync_thread
        if t is not None:
            t.join(timeout=timeout)
            if t.is_alive():
                log.error(
                    "shutdown_sync: thread still alive after %.1fs join — "
                    "keeping stop flag set (write gates will abort)", timeout
                )
                return False
        _sync_thread = None
        _sync_stop = threading.Event()
        return True


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


# ── AC3b/A10: disconnect / athlete-change purge ─────────────────────────────

# Athlete.json mirror fields that ICU sync writes: (value_key, source_key,
# extra keys removed alongside). Only entries whose source is exactly "icu"
# are reset — manual values always survive a purge.
_ICU_MIRROR_FIELDS = (
    ("wprime_j", "wprime_source", ()),
    ("pmax_w", "pmax_source", ()),
    ("max_hr", "max_hr_source", ()),
    ("lthr", "lthr_source", ("lthr_source_date",)),
)


def purge_profile_data(profile_id: str, athlete_changed: bool = False) -> dict:
    """Wipe ICU-synced data for ``profile_id`` (disconnect / different-athlete
    reconnect — contract A4).

    Deletes every row in activities / wellness / sync_log, plus the
    intervals.icu-sourced athlete_metrics rows (vo2max/eftp/w_prime/pmax/
    max_hr/weight auto-logs — residual rows from the first athlete otherwise
    resurface through the mirrors), and the morning check-ins imported from
    intervals.icu (3.14.0 grill H1: they would keep feeding today's band and
    its baseline; the rider's own check-ins stay). Wipes the profile's
    ICU-derived ARCHIVES too (<profile>/rides/icu/ incl. .last_sync_at,
    <profile>/wellness/) — contract A4 "no residual rows in DB or archives".
    Loose FIT imports and ride summaries are USER data and survive. Resets
    the icu-sourced athlete.json mirrors (lthr / max_hr / wprime / pmax
    where *_source == "icu") so the profile's numbers fall back to defaults
    until the next athlete syncs. Manual values are never touched.

    A weight that followed intervals.icu keeps its value and becomes the
    rider's ("manual"; grill M6): removing it would leave the planner without
    a weight for event targets, and leaving it "icu" would follow the next
    account's scale. ``athlete_changed`` (a reconnect as a different
    intervals.icu athlete) also asks the rider to check that weight.

    A10: holds the sync write gate for the whole wipe and bumps the sync
    epoch, so an in-flight pass that FETCHED before the purge fails its
    snapshot check at the next write section — zero post-purge stale rows.

    Raises SyncBusy when the gate can't be acquired within ~10s (caller
    surfaces 503 and retries; never proceeds unlocked). Returns per-table
    deleted-row counts for the caller's response payload.
    """
    global _sync_epoch
    from profile_manager import ProfileManager, _safe_profile_dir
    pm = ProfileManager.get()
    profile_dir = _safe_profile_dir(pm, profile_id)

    if not _sync_write_lock.acquire(timeout=10.0):
        raise SyncBusy(
            f"sync write in progress; purge of '{profile_id}' aborted — retry"
        )
    try:
        # Invalidate every in-flight snapshot BEFORE wiping so a pass that
        # already fetched can never land rows after us.
        _sync_epoch += 1

        removed = {"activities": 0, "wellness": 0, "sync_log": 0,
                   "athlete_metrics_icu": 0, "daily_log_icu": 0,
                   "archive_files": 0}

        # ICU-derived archives (post-AC2a these live inside the profile dir).
        # rides/icu/ is wholly ICU-synced (records + .last_sync_at); wellness/
        # is wholly ICU-synced day records. Loose *.fit + ride summaries in
        # rides/ are user data — untouched.
        import shutil as _shutil
        for _arch in (profile_dir / "rides" / "icu", profile_dir / "wellness"):
            if _arch.is_dir():
                try:
                    removed["archive_files"] += sum(
                        1 for p in _arch.iterdir() if p.is_file()
                    )
                    _shutil.rmtree(str(_arch))
                except OSError as e:
                    log.warning("purge: failed to remove %s: %s", _arch, e)
        db_file = profile_dir / "health_tracker.db"
        if db_file.exists():
            is_active_db = (
                profile_id == getattr(pm, "active_id", None)
                and DB_PATH is not None
                and Path(DB_PATH) == db_file
            )
            conn = get_db() if is_active_db else sqlite3.connect(str(db_file), timeout=10)
            try:
                for table in ("activities", "wellness", "sync_log"):
                    try:
                        cur = conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed names
                        removed[table] = cur.rowcount if cur.rowcount >= 0 else 0
                    except sqlite3.OperationalError:
                        pass  # table absent in a legacy/partial DB
                try:
                    cur = conn.execute(
                        "DELETE FROM athlete_metrics WHERE source = 'intervals.icu'"
                    )
                    removed["athlete_metrics_icu"] = (
                        cur.rowcount if cur.rowcount >= 0 else 0
                    )
                except sqlite3.OperationalError:
                    pass
                try:
                    cur = conn.execute(
                        "DELETE FROM daily_log WHERE icu_fields IS NOT NULL")
                    removed["daily_log_icu"] = cur.rowcount if cur.rowcount >= 0 else 0
                except sqlite3.OperationalError:
                    pass  # a non-active DB from before 3.14.0: no column, no imports
                conn.commit()
            finally:
                if not is_active_db:
                    conn.close()

        # Reset icu-sourced athlete.json mirrors. Active profile: through the
        # in-memory dict (kept coherent); other profiles: edit the file.
        athlete_path = profile_dir / "athlete.json"
        if profile_id == getattr(pm, "active_id", None):
            athlete = pm._athlete
        else:
            try:
                athlete = json.loads(athlete_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError, ValueError):
                athlete = None
        if isinstance(athlete, dict):
            changed = False
            for value_key, source_key, extras in _ICU_MIRROR_FIELDS:
                if str(athlete.get(source_key, "") or "") == "icu":
                    athlete.pop(value_key, None)
                    athlete.pop(source_key, None)
                    for k in extras:
                        athlete.pop(k, None)
                    changed = True
            if str(athlete.get("weight_source", "") or "") == "icu":
                athlete["weight_source"] = "manual"
                if athlete_changed:
                    athlete["weight_needs_check"] = True
                changed = True
            if changed:
                pm._write_json(athlete_path, athlete)

        log.info("EVENT=profile_purged profile=%s removed=%s", profile_id, removed)
        return removed
    finally:
        _sync_write_lock.release()
