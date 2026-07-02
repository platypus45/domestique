"""v3.0.0 account/identity fix wave — CORE contract tests (stubbed HOME).

Covers the grill-locked CORE scope of IP_ACCOUNT_FIXES:
  A1  — mid-sync profile switch: zero crossover rows, zero mirror writes
        (db.sync_write_gate stop-flag + snapshot-mismatch aborts).
  A3  — a newly created profile B starts clean (no token, no rides, no A
        workout dir, power-mode defaults, lthr_is_set=False) and an A↔B
        round-trip preserves both.
  A10 — purge_profile_data takes the write gate + bumps the sync epoch so an
        in-flight pass that fetched pre-purge can never land rows after it.
  A11 — archive migration: DB-id ownership attribution, idempotency, kill -9
        resumability with count preservation.
  A12 — delete-last: no stale registry keys, no root-dir DB/rides/plans
        resurrection, create-new stays clean.
  A14 — switch storm 20×: sync thread alive and syncing the final profile.
  AC3c — refresh_token / expires_in captured at token save when present.
  AC4  — bootstrapped flag: fresh ⇔ none of [.env, health_tracker.db,
        .setup_complete, user_prefs.json] existed; clear_bootstrapped().

Every test pins HOME to a pytest tmp dir (test_profiles.py pattern) — the
real ~/.domestique is never touched.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db as db_mod  # noqa: E402
import migrate_profiles  # noqa: E402
import profile_manager as pm_mod  # noqa: E402
import ride_storage as rs  # noqa: E402
import training_planner as tp  # noqa: E402


# ─── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _module_state_guard():
    """Snapshot/restore the module-global state my tests mutate: db sync
    machinery, DB_PATH, ICU env vars, training_planner dirs, pm singleton.
    Without this, a switch()'s stop_sync / set_db_path(None) would poison
    every later test in the pytest process."""
    saved_env = {k: os.environ.get(k) for k in
                 ("ICU_ATHLETE_ID", "ICU_API_KEY", "ICU_ACCESS_TOKEN")}
    orig_db_path = db_mod.DB_PATH
    orig_plan_dir = tp.PLAN_DIR
    orig_workout_dir = tp.WORKOUT_DIR
    yield
    db_mod.shutdown_sync()
    if db_mod._sync_write_lock.locked():
        try:
            db_mod._sync_write_lock.release()
        except RuntimeError:
            pass
    db_mod.close_all_connections()
    db_mod.set_db_path(orig_db_path)
    tp.PLAN_DIR = orig_plan_dir
    tp.WORKOUT_DIR = orig_workout_dir
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    pm_mod.ProfileManager._instance = None


def _mk_pm(home: Path):
    """Fresh ProfileManager singleton pinned to a stub HOME."""
    pm_mod.ProfileManager._instance = None
    with patch("pathlib.Path.home", return_value=Path(home)):
        pm = pm_mod.ProfileManager.get()
    return pm


def _profile_db_counts(db_file: Path) -> dict:
    if not db_file.exists():
        return {"wellness": 0, "activities": 0, "sync_log": 0}
    conn = sqlite3.connect(str(db_file))
    try:
        out = {}
        for t in ("wellness", "activities", "sync_log"):
            try:
                out[t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out[t] = 0
        return out
    finally:
        conn.close()


def _athlete_json(home: Path, pid: str) -> dict:
    return json.loads(
        (home / ".domestique" / "profiles" / pid / "athlete.json").read_text())


# ─── A1: mid-sync switch — zero crossover ────────────────────────────────────

@pytest.mark.parametrize("reset_stop_event", [False, True],
                         ids=["stop-flag-abort", "snapshot-mismatch-abort"])
def test_a1_mid_sync_switch_aborts_wellness_write(tmp_path, monkeypatch,
                                                  reset_stop_event):
    """A switch landing between fetch and write aborts the pass: no rows in
    either profile, SyncAborted raised. Parametrized over BOTH abort paths:
    the stop-flag fast path and the belt-and-braces snapshot mismatch."""
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Alice")
    b = pm.create_profile("Bob")
    pm.switch(a)
    pm.save_env("i111", "key_a")
    db_mod.shutdown_sync()  # clean stop flag left by setup switches

    def fetch_stub(days=90):
        pm.switch(b)  # identity mutates while the "response" is in flight
        if reset_stop_event:
            # Clear the stop flag so the gate reaches the snapshot check —
            # proves the abort does NOT depend on the stop event alone.
            db_mod.shutdown_sync()
        return [{"id": "2026-07-01", "ctl": 10.0, "atl": 5.0, "sportInfo": []}]

    monkeypatch.setattr(db_mod, "fetch_wellness", fetch_stub)

    with pytest.raises(db_mod.SyncAborted):
        db_mod.sync_wellness(days=1)

    profiles = tmp_path / ".domestique" / "profiles"
    assert _profile_db_counts(profiles / "alice" / "health_tracker.db")["wellness"] == 0
    assert _profile_db_counts(profiles / "bob" / "health_tracker.db")["wellness"] == 0
    # zero mirror writes in B
    assert "lthr_source" not in _athlete_json(tmp_path, "bob")
    assert "wprime_source" not in _athlete_json(tmp_path, "bob")


def test_a1_run_sync_mid_switch_no_activity_crossover(tmp_path, monkeypatch):
    """run_sync-level repro: wellness lands in A (correct owner at write
    time), then the switch hits during the activities fetch → activities
    batch + hr mirror + sync_log all abort. B gets nothing, A gets no
    stray sync_log row for the aborted pass."""
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Alice")
    b = pm.create_profile("Bob")
    pm.switch(a)
    pm.save_env("i111", "key_a")
    db_mod.shutdown_sync()

    monkeypatch.setattr(
        db_mod, "fetch_wellness",
        lambda days=90: [{"id": "2026-07-01", "ctl": 10.0, "atl": 5.0,
                          "sportInfo": []}])

    def fetch_acts(days=90):
        pm.switch(b)
        db_mod.shutdown_sync()  # force the snapshot-mismatch path
        return [{"id": "i9", "start_date_local": "2026-07-01T10:00:00",
                 "name": "ride", "lthr": 177, "athlete_max_hr": 195}]

    monkeypatch.setattr(db_mod, "fetch_activities", fetch_acts)

    with pytest.raises(db_mod.SyncAborted):
        db_mod.run_sync(days=1)

    profiles = tmp_path / ".domestique" / "profiles"
    alice = _profile_db_counts(profiles / "alice" / "health_tracker.db")
    bob = _profile_db_counts(profiles / "bob" / "health_tracker.db")
    assert alice["wellness"] == 1      # written while A still owned the pass
    assert alice["activities"] == 0    # aborted post-switch
    assert alice["sync_log"] == 0      # log write aborted too
    assert bob == {"wellness": 0, "activities": 0, "sync_log": 0}
    # the hr mirror never stamped B
    bob_athlete = _athlete_json(tmp_path, "bob")
    assert "lthr" not in bob_athlete and "lthr_source" not in bob_athlete


def test_sync_write_gate_is_exported_for_flow(tmp_path):
    """FLOW applies the same primitive in app.py's persist loops — the
    exported surface is snapshot_sync_identity() + sync_write_gate()."""
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Solo")
    pm.switch(a)
    db_mod.shutdown_sync()
    snap = db_mod.snapshot_sync_identity()
    assert snap[0] == "solo"
    with db_mod.sync_write_gate(snap):
        pass  # identity unchanged → gate passes
    with pytest.raises(ValueError):
        with db_mod.sync_write_gate("nonsense"):
            pass


# ─── A3: new profile starts clean + round-trip preservation ─────────────────

def test_a3_profile_b_starts_clean_roundtrip_preserves_a(tmp_path):
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Alice")
    pm.switch(a)
    pm.save_env("i111", "key_a")
    pm.save_icu_token("tok_a", "i111", "Alice A")
    rs.persist_icu_activity({"id": "111",
                             "start_date_local": "2026-07-01T10:00:00"})
    rs.persist_wellness({"id": "2026-07-01", "ctl": 1.0, "atl": 2.0})
    custom = tmp_path / "custom_lib"
    custom.mkdir()
    (pm.active_dir / "user_paths.json").write_text(
        json.dumps({"workout_dir": str(custom)}), encoding="utf-8")
    pm.apply_training_dirs()
    assert tp.WORKOUT_DIR == custom

    b = pm.create_profile("Bob")
    pm.switch(b)
    # clean creds — profile AND process env
    assert pm.icu_athlete_id == "" and pm.icu_access_token == ""
    assert os.environ.get("ICU_ATHLETE_ID") == ""
    # no rides / wellness / power data of A visible
    assert rs.load_all_rides() == []
    assert rs.load_icu_rides() == []
    assert rs.load_recent_wellness() == []
    import power_curve as pc
    assert pc.aggregate_power_curve(window_days=90).get("n_rides", 0) == 0
    # power-mode defaults, no fabricated LTHR (create_profile stopped seeding)
    assert pm.lthr_is_set is False
    assert pm.target_mode == "power"
    # AC2b: WORKOUT_DIR reset to the bundled default, not A's custom dir
    assert tp.WORKOUT_DIR == Path(tp.__file__).parent / "workouts"

    # round-trip back: A intact
    pm.switch(a)
    assert pm.icu_athlete_id == "i111"
    assert pm.icu_access_token == "tok_a"
    assert len(rs.load_icu_rides()) == 1
    assert len(rs.load_recent_wellness()) == 1
    assert tp.WORKOUT_DIR == custom


# ─── A10: disconnect-purge vs in-flight pass ─────────────────────────────────

def test_a10_purge_wipes_and_aborts_inflight_snapshot(tmp_path):
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Alice")
    pm.switch(a)
    db_mod.shutdown_sync()

    conn = db_mod.get_db()
    conn.execute("INSERT INTO activities (id, date) VALUES ('x1', '2026-07-01')")
    conn.execute("INSERT INTO wellness (date, ctl, atl) VALUES ('2026-07-01', 1, 2)")
    conn.execute("INSERT INTO sync_log (timestamp) VALUES ('t0')")
    conn.execute("INSERT INTO athlete_metrics (date, metric, value, source) "
                 "VALUES ('2026-07-01', 'eftp', 250, 'intervals.icu')")
    conn.execute("INSERT INTO athlete_metrics (date, metric, value, source) "
                 "VALUES ('2026-07-01', 'vo2max', 55, 'manual')")
    conn.commit()
    rs.persist_icu_activity({"id": "x1",
                             "start_date_local": "2026-07-01T10:00:00"})
    rs.persist_wellness({"id": "2026-07-01", "ctl": 1.0, "atl": 2.0})
    # icu-sourced mirror must reset; manual mirror must survive
    assert pm._set_max_hr(190, "icu") is True
    assert pm._set_wprime(20000, "manual") is True

    snap = db_mod.snapshot_sync_identity()   # an in-flight pass "fetched" here
    removed = db_mod.purge_profile_data(a)

    assert removed["activities"] == 1
    assert removed["wellness"] == 1
    assert removed["sync_log"] == 1
    assert removed["athlete_metrics_icu"] == 1
    assert removed["archive_files"] == 2

    # the pre-purge snapshot is dead: epoch bumped under the gate
    with pytest.raises(db_mod.SyncAborted, match="epoch"):
        with db_mod.sync_write_gate(snap):
            pytest.fail("gate must not admit a pre-purge snapshot")

    counts = _profile_db_counts(pm.active_dir / "health_tracker.db")
    assert counts == {"wellness": 0, "activities": 0, "sync_log": 0}
    conn2 = db_mod.get_db()
    rows = conn2.execute(
        "SELECT metric, source FROM athlete_metrics").fetchall()
    assert [(r[0], r[1]) for r in rows] == [("vo2max", "manual")]
    assert not (pm.active_dir / "rides" / "icu").exists()
    assert not (pm.active_dir / "wellness").exists()
    athlete = _athlete_json(tmp_path, "alice")
    assert "max_hr" not in athlete and "max_hr_source" not in athlete
    assert athlete["wprime_j"] == 20000 and athlete["wprime_source"] == "manual"


# ─── switch: bounded gate wait, never proceed unlocked ───────────────────────

def test_switch_sync_busy_leaves_identity_untouched(tmp_path, monkeypatch):
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Alice")
    b = pm.create_profile("Bob")
    pm.switch(a)
    monkeypatch.setattr(db_mod, "fetch_wellness", lambda days=90: [])
    monkeypatch.setattr(db_mod, "fetch_activities", lambda days=90: [])
    monkeypatch.setattr(pm_mod, "_SYNC_GATE_TIMEOUT_S", 0.3)

    alice_db = pm._profiles_dir / "alice" / "health_tracker.db"
    assert db_mod._sync_write_lock.acquire(timeout=1)
    try:
        with pytest.raises(db_mod.SyncBusy):
            pm.switch(b)   # a wedged writer holds the gate → 503, no mutation
    finally:
        db_mod._sync_write_lock.release()

    assert pm.active_id == "alice"
    assert db_mod.DB_PATH == alice_db
    assert os.environ.get("ICU_ATHLETE_ID") == pm.icu_athlete_id


# ─── A11: archive migration ──────────────────────────────────────────────────

def _seed_profile(base: Path, pid: str, act_ids=(), wellness_rows=()):
    d = base / "profiles" / pid
    d.mkdir(parents=True, exist_ok=True)
    (d / "athlete.json").write_text("{}", encoding="utf-8")
    conn = sqlite3.connect(str(d / "health_tracker.db"))
    conn.execute("CREATE TABLE activities (id TEXT PRIMARY KEY, date TEXT)")
    conn.execute("CREATE TABLE wellness (date TEXT PRIMARY KEY, ctl REAL, atl REAL)")
    for i in act_ids:
        conn.execute("INSERT INTO activities VALUES (?, '2026-07-01')", (i,))
    for row in wellness_rows:
        conn.execute("INSERT INTO wellness VALUES (?, ?, ?)", row)
    conn.commit()
    conn.close()
    return d


def _seed_global_archives(base: Path):
    """Global legacy archive: 4 icu files (2 martijn / 1 rider / 1 unmatched),
    a .last_sync_at dotfile, 3 wellness files (1 each + 1 unmatched), and a
    loose FIT + sidecar (unmatched)."""
    icu = base / "rides" / "icu"
    icu.mkdir(parents=True)
    for stem in ("m1", "m2", "r1", "x1"):
        (icu / f"{stem}.json").write_text(
            json.dumps({"ride_id": f"icu_{stem}", "external_id": stem}),
            encoding="utf-8")
    (icu / ".last_sync_at").write_text("2026-07-01T00:00:00", encoding="utf-8")
    well = base / "wellness"
    well.mkdir(parents=True)
    (well / "2026-06-01.json").write_text(
        json.dumps({"id": "2026-06-01", "ctl": 20.777937, "atl": 17.746977}),
        encoding="utf-8")
    (well / "2026-06-02.json").write_text(
        json.dumps({"id": "2026-06-02", "ctl": 30.5, "atl": 25.25}),
        encoding="utf-8")
    (well / "2026-06-03.json").write_text(
        json.dumps({"id": "2026-06-03", "ctl": 99.0, "atl": 1.0}),
        encoding="utf-8")
    (base / "rides" / "ride_x.fit").write_bytes(b"\x0e FITDATA")
    (base / "rides" / "ride_x.fit.load.json").write_text("{}", encoding="utf-8")


def _mk_migration_base(tmp_path: Path, multi: bool = True) -> Path:
    base = tmp_path / ".domestique"
    base.mkdir(parents=True)
    _seed_profile(base, "martijn", act_ids=("m1", "m2"),
                  wellness_rows=[("2026-06-01", 20.777937, 17.746977)])
    profiles = [{"id": "martijn", "name": "Martijn"}]
    if multi:
        _seed_profile(base, "rider", act_ids=("r1",),
                      wellness_rows=[("2026-06-02", 30.5, 25.25)])
        profiles.append({"id": "rider", "name": "Rider"})
    (base / "profiles.json").write_text(json.dumps({
        "version": 1, "active_profile": "martijn", "skip_picker": True,
        "profiles": profiles,
    }), encoding="utf-8")
    _seed_global_archives(base)
    return base


def _all_archive_names(base: Path) -> list:
    """Every archive file name anywhere under base (count-preservation probe)."""
    names = []
    for p in base.rglob("*"):
        if p.is_file() and p.name != "MIGRATION_NOTE.txt" and (
                p.suffix in (".fit",) or p.name.endswith(".load.json")
                or p.parent.name in ("icu", "wellness")
                or p.name == ".last_sync_at"):
            names.append(p.name)
    return sorted(names)


def test_a11_migration_db_id_attribution_multi_profile(tmp_path):
    base = _mk_migration_base(tmp_path, multi=True)
    stats = migrate_profiles.migrate_archives_to_profiles(base)

    assert stats["ran"] is True
    assert stats["icu"] == 4 and stats["wellness"] == 3 and stats["loose"] == 2
    # unmatched: x1.json + 2026-06-03.json + ride_x.fit + its sidecar
    assert stats["unmatched"] == 4

    m = base / "profiles" / "martijn"
    r = base / "profiles" / "rider"
    # DB-id attribution — matched files land with their OWNER, active or not
    assert (m / "rides" / "icu" / "m1.json").exists()
    assert (m / "rides" / "icu" / "m2.json").exists()
    assert (r / "rides" / "icu" / "r1.json").exists()
    assert (m / "wellness" / "2026-06-01.json").exists()
    assert (r / "wellness" / "2026-06-02.json").exists()
    # unmatched remainder → ACTIVE profile
    assert (m / "rides" / "icu" / "x1.json").exists()
    assert (m / "wellness" / "2026-06-03.json").exists()
    assert (m / "rides" / "ride_x.fit").exists()
    assert (m / "rides" / "ride_x.fit.load.json").exists()
    # sync bookkeeping follows the active profile
    assert (m / "rides" / "icu" / ".last_sync_at").exists()
    # global trees emptied; note + banner flag for the unmatched remainder
    assert not (base / "rides" / "icu").exists()
    assert not (base / "wellness").exists()
    assert (base / "rides" / "MIGRATION_NOTE.txt").exists()
    reg = json.loads((base / "profiles.json").read_text())
    assert reg["archive_migration"]["banner"] is True
    assert reg["archive_migration"]["assigned_to"] == "martijn"
    assert reg["archive_migration"]["unmatched"] == 4

    # idempotent: second run is a no-op
    stats2 = migrate_profiles.migrate_archives_to_profiles(base)
    assert stats2["ran"] is False


def test_a11_migration_single_profile_no_banner(tmp_path):
    base = _mk_migration_base(tmp_path, multi=False)
    stats = migrate_profiles.migrate_archives_to_profiles(base)
    assert stats["ran"] is True
    m = base / "profiles" / "martijn"
    # everything (matched + unmatched + r1 now unmatched) → the sole profile
    for name in ("m1.json", "m2.json", "r1.json", "x1.json"):
        assert (m / "rides" / "icu" / name).exists()
    for name in ("2026-06-01.json", "2026-06-02.json", "2026-06-03.json"):
        assert (m / "wellness" / name).exists()
    # single profile → honest silence: no note, no banner
    assert not (base / "rides").exists()  # fully emptied + removed
    reg = json.loads((base / "profiles.json").read_text())
    assert "archive_migration" not in reg


def test_a11_migration_kill9_resumable_count_preserved(tmp_path, monkeypatch):
    base = _mk_migration_base(tmp_path, multi=True)
    before = _all_archive_names(base)
    assert len(before) == 10  # 4 icu + dotfile + 3 wellness + fit + sidecar

    calls = {"n": 0}
    real_move = migrate_profiles.shutil.move

    def dying_move(src, dst):
        calls["n"] += 1
        if calls["n"] > 3:
            raise OSError("simulated kill -9 mid-migration")
        return real_move(src, dst)

    monkeypatch.setattr(migrate_profiles.shutil, "move", dying_move)
    with pytest.raises(OSError):
        migrate_profiles.migrate_archives_to_profiles(base)
    # interrupted: files split across old+new locations, NONE lost
    assert _all_archive_names(base) == before

    monkeypatch.setattr(migrate_profiles.shutil, "move", real_move)
    stats = migrate_profiles.migrate_archives_to_profiles(base)  # resume
    assert stats["ran"] is True
    assert _all_archive_names(base) == before          # count preservation
    assert not (base / "rides" / "icu").exists()       # global tree drained
    assert not (base / "wellness").exists()
    # every archive file now lives under a profile dir
    for n in before:
        hits = [p for p in (base / "profiles").rglob(n) if p.is_file()]
        assert len(hits) == 1, f"{n} duplicated or lost: {hits}"


def test_a11_trigger_absent_is_noop(tmp_path):
    base = tmp_path / ".domestique"
    (base / "profiles" / "solo").mkdir(parents=True)
    (base / "profiles.json").write_text(json.dumps({
        "version": 1, "active_profile": "solo",
        "profiles": [{"id": "solo", "name": "Solo"}]}), encoding="utf-8")
    stats = migrate_profiles.migrate_archives_to_profiles(base)
    assert stats == {"ran": False, "icu": 0, "wellness": 0, "loose": 0,
                     "unmatched": 0}


# ─── A12: delete-last — no artifact resurrection ─────────────────────────────

def test_a12_delete_last_then_create_no_resurrection(tmp_path):
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Solo")
    pm.switch(a)
    pm.save_env("i111", "key")
    rs.persist_icu_activity({"id": "s1",
                             "start_date_local": "2026-07-01T10:00:00"})
    profiles_root = tmp_path / ".domestique" / "profiles"
    assert (profiles_root / "solo" / "health_tracker.db").exists()

    assert pm.delete_profile(a) is True

    # registry: real pointer cleared, stray "active" key gone
    reg = json.loads((tmp_path / ".domestique" / "profiles.json").read_text())
    assert reg["active_profile"] is None
    assert "active" not in reg
    assert pm.active_id is None
    # sync detached: DB path sentinel + creds scrubbed from the process env
    assert db_mod.DB_PATH is None
    assert os.environ.get("ICU_ATHLETE_ID") is None
    assert db_mod._sync_thread is None

    # writers refuse instead of resurrecting root artifacts
    with pytest.raises(RuntimeError):
        db_mod.get_db()
    db_mod.init_db()  # explicit no-op, must not raise or create anything
    with pytest.raises(RuntimeError):
        pm.save_athlete({"ftp": 210})
    with pytest.raises(RuntimeError):
        pm.save_prefs({"hours_per_week": 9})
    with pytest.raises(RuntimeError):
        pm.record_ftp_test("manual", 250)
    with pytest.raises(RuntimeError):
        pm.update_ftp(260)
    with pytest.raises(RuntimeError):
        _ = pm.plan_dir
    assert pm._set_wprime(10000, "icu") is False
    assert pm._set_pmax(900, "icu") is False
    assert pm._set_max_hr(190, "icu") is False
    with pytest.raises(RuntimeError):
        rs._icu_rides_dir()
    with pytest.raises(RuntimeError):
        rs._wellness_dir()
    # readers degrade to empty without creating dirs
    assert rs.load_all_rides() == []
    assert rs.list_rides() == []
    assert rs.load_recent_wellness() == []
    assert rs.get_ride("ride_x") is None
    import power_curve as pc
    assert pc._load_cached_rides() == []

    # the three live-install artifacts must NOT reappear at the profiles root
    assert not (profiles_root / "health_tracker.db").exists()
    assert not (profiles_root / "rides").exists()
    assert not (profiles_root / "plans").exists()
    assert not (profiles_root / "athlete.json").exists()

    # delete-last → create-new: clean state, still no root artifacts
    b = pm.create_profile("Fresh")
    pm.switch(b)
    assert pm.active_id == "fresh"
    assert db_mod.DB_PATH == profiles_root / "fresh" / "health_tracker.db"
    reg2 = json.loads((tmp_path / ".domestique" / "profiles.json").read_text())
    assert reg2["active_profile"] == "fresh"
    assert "active" not in reg2
    assert not (profiles_root / "health_tracker.db").exists()
    assert not (profiles_root / "rides").exists()


def test_stray_active_key_cleaned_on_registry_load(tmp_path):
    """The LIVE registry carries "active": null from the old delete code —
    loading must drop it and the next save persists the cleanup."""
    base = tmp_path / ".domestique"
    prof = base / "profiles" / "solo"
    prof.mkdir(parents=True)
    (prof / "athlete.json").write_text(json.dumps({"ftp": 200}), encoding="utf-8")
    (base / "profiles.json").write_text(json.dumps({
        "version": 1, "active_profile": "solo", "skip_picker": True,
        "profiles": [{"id": "solo", "name": "Solo"}],
        "active": None,   # the stray key
    }), encoding="utf-8")
    pm = _mk_pm(tmp_path)
    assert "active" not in pm._registry
    pm.update_profile("solo", name="Solo Rider")  # any registry save
    reg = json.loads((base / "profiles.json").read_text())
    assert "active" not in reg


# ─── A14: switch storm ───────────────────────────────────────────────────────

def test_a14_switch_storm_sync_thread_alive(tmp_path, monkeypatch):
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Alice")
    b = pm.create_profile("Bob")
    pm.switch(a)
    monkeypatch.setattr(db_mod, "fetch_wellness", lambda days=90: [])
    monkeypatch.setattr(db_mod, "fetch_activities", lambda days=90: [])
    # app.py:413 wiring: the on_switch callback is the ONE restart owner
    pm.on_switch(db_mod.restart_sync)
    db_mod.restart_sync()
    assert db_mod._sync_thread is not None and db_mod._sync_thread.is_alive()

    for i in range(20):
        pm.switch(b if i % 2 == 0 else a)

    t = db_mod._sync_thread
    assert t is not None and t.is_alive(), \
        "switch storm killed the sync thread (the old double-restart bug)"
    final = pm.active_id
    assert db_mod.DB_PATH == pm._profiles_dir / final / "health_tracker.db"
    # the next pass would target the final profile
    snap = db_mod.snapshot_sync_identity()
    assert snap[0] == final and snap[1] == db_mod.DB_PATH


# ─── AC3c: refresh_token / expires_in capture ────────────────────────────────

def test_ac3c_token_capture_and_disconnect_clear(tmp_path):
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Alice")
    pm.switch(a)
    env_path = pm.active_dir / ".env"

    # ICU without refresh fields (today's live behavior) → nothing extra
    pm.save_icu_token("tok_1", "i111", "Alice")
    env_text = env_path.read_text()
    assert "ICU_ACCESS_TOKEN=tok_1" in env_text
    assert "ICU_REFRESH_TOKEN" not in env_text
    assert "ICU_TOKEN_EXPIRES_AT" not in env_text

    # ICU returns them → captured to the same 0600 .env
    import time as _time
    before = int(_time.time())
    pm.save_icu_token("tok_2", "i111", "Alice",
                      refresh_token="rt_secret", expires_in=3600)
    env_text = env_path.read_text()
    assert "ICU_ACCESS_TOKEN=tok_2" in env_text
    assert "ICU_REFRESH_TOKEN=rt_secret" in env_text
    expires_at = int(pm._load_env_file(env_path)["ICU_TOKEN_EXPIRES_AT"])
    assert before + 3500 <= expires_at <= before + 3700
    assert (os.stat(env_path).st_mode & 0o777) == 0o600

    # an unrelated API-key save must not drop the captured fields
    pm.save_env("i111", "new_key")
    env_text = env_path.read_text()
    assert "ICU_REFRESH_TOKEN=rt_secret" in env_text
    assert "ICU_ACCESS_TOKEN=tok_2" in env_text

    # disconnect clears the pair with the bearer
    pm.save_icu_token("", None)
    env_text = env_path.read_text()
    assert "ICU_ACCESS_TOKEN=\n" in env_text
    assert "ICU_REFRESH_TOKEN" not in env_text
    assert "ICU_TOKEN_EXPIRES_AT" not in env_text


# ─── AC4: bootstrapped flag ──────────────────────────────────────────────────

def test_ac4_bootstrapped_fresh_true_legacy_false_and_clear(tmp_path):
    # fresh install: NONE of the four legacy files existed
    fresh_home = tmp_path / "fresh"
    fresh_home.mkdir()
    with patch("pathlib.Path.home", return_value=fresh_home):
        migrate_profiles.migrate_to_profiles()
    reg = json.loads((fresh_home / ".domestique" / "profiles.json").read_text())
    assert reg["profiles"][0]["bootstrapped"] is True

    # legacy upgrade: a root .env existed → NOT bootstrapped
    legacy_home = tmp_path / "legacy"
    (legacy_home / ".domestique").mkdir(parents=True)
    (legacy_home / ".domestique" / ".env").write_text(
        "ICU_ATHLETE_ID=i1\nICU_API_KEY=k\n", encoding="utf-8")
    with patch("pathlib.Path.home", return_value=legacy_home):
        migrate_profiles.migrate_to_profiles()
    reg2 = json.loads((legacy_home / ".domestique" / "profiles.json").read_text())
    assert reg2["profiles"][0]["bootstrapped"] is False

    # wizard save clears it (pm.clear_bootstrapped) — idempotently
    pm = _mk_pm(fresh_home)
    assert pm.active_id == "default"
    pm.clear_bootstrapped()
    reg3 = json.loads((fresh_home / ".domestique" / "profiles.json").read_text())
    assert "bootstrapped" not in reg3["profiles"][0]
    pm.clear_bootstrapped()  # second call: no-op, no crash


# ─── create_profile: no fabricated HR identity (A3 support) ──────────────────

def test_create_profile_does_not_seed_lthr_max_hr(tmp_path):
    pm = _mk_pm(tmp_path)
    a = pm.create_profile("Nohr")
    athlete = _athlete_json(tmp_path, a)
    assert "lthr" not in athlete and "max_hr" not in athlete
    assert athlete["ftp"] == 200  # the honest defaults stay
    pm.switch(a)
    assert pm.lthr_is_set is False
    assert pm.lthr == 170 and pm.max_hr == 190  # property fallbacks intact
