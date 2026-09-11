"""Issue #11 — "some of my rides from intervals.icu don't seem to sync".

The rider's diagnostics showed every missing ride as an intervals.icu STUB:
the five-key record ICU returns for an activity it will not re-share (it
reached ICU via Strava). v3.8.1 taught the ride store to refuse those, but
two other readers of the same activity list never got the rule:

  * training.get_today_metrics() builds the homepage "Recent activities"
    card straight from the ICU list — the stub drew as "Activity · 0min" and
    opened to "Failed to load activity (404)" (his log: ride_detail 404 on
    stub ids).
  * db.run_sync() mirrors the list into SQLite, blank row included.

One predicate now, ride_storage.is_icu_stub, used by all three.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import db as db_mod  # noqa: E402
import profile_manager as pm_mod  # noqa: E402
import ride_storage as rs  # noqa: E402
import training  # noqa: E402
import training_planner as tp  # noqa: E402


@pytest.fixture(autouse=True)
def _module_state_guard():
    """Same guard as test_account_core: the SQLite-mirror test below switches
    profiles, which rewires db.DB_PATH, the sync stop flag and the planner
    dirs for the whole pytest process. Snapshot before, restore after — and
    start from a clean sync state so an earlier suite's leftovers (a stop
    flag, an open connection on a deleted tmp DB) cannot poison this one."""
    saved_env = {k: os.environ.get(k) for k in
                 ("ICU_ATHLETE_ID", "ICU_API_KEY", "ICU_ACCESS_TOKEN")}
    orig_db_path = db_mod.DB_PATH
    orig_plan_dir, orig_workout_dir = tp.PLAN_DIR, tp.WORKOUT_DIR
    db_mod.shutdown_sync()
    db_mod.close_all_connections()
    yield
    db_mod.shutdown_sync()
    if db_mod._sync_write_lock.locked():
        try:
            db_mod._sync_write_lock.release()
        except RuntimeError:
            pass
    db_mod.close_all_connections()
    db_mod.set_db_path(orig_db_path)
    tp.PLAN_DIR, tp.WORKOUT_DIR = orig_plan_dir, orig_workout_dir
    for k, v in saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    pm_mod.ProfileManager._instance = None


def _stub(icu_id="17204133079"):
    """Exactly what intervals.icu returns for a Strava-origin activity."""
    return {"id": icu_id, "source": "STRAVA",
            "start_date_local": "2026-01-28T10:08:02",
            "name": None, "type": None,
            "elapsed_time": None, "moving_time": None}


def _real(icu_id="i2"):
    return {"id": icu_id, "start_date_local": "2026-01-28T12:00:00",
            "name": "Endurance", "type": "Ride", "moving_time": 3600,
            "icu_training_load": 55, "average_watts": 180}


# ── the predicate ─────────────────────────────────────────────────────────

def test_is_icu_stub_is_the_one_rule():
    assert rs.is_icu_stub(_stub()) is True
    assert rs.is_icu_stub(_real()) is False
    # An untagged local FIT relayed to ICU has a name but no type: a ride.
    assert rs.is_icu_stub({"id": "i3", "name": "Morning ride"}) is False
    # A typed ride with no duration field is still a ride.
    assert rs.is_icu_stub({"id": "i4", "type": "VirtualRide"}) is False
    assert rs.is_icu_stub("not a dict") is False


def test_ride_store_still_refuses_the_stub_through_the_shared_rule():
    assert rs._normalize_icu_activity(_stub()) == {}
    assert rs._normalize_icu_activity(_real())["ride_id"] == "icu_i2"


# ── homepage recent list ──────────────────────────────────────────────────

def test_recent_activities_leave_out_strava_stubs(monkeypatch):
    today = date.today().isoformat()
    monkeypatch.setattr(training, "_require_credentials", lambda: None)
    monkeypatch.setattr(training, "fetch_wellness",
                        lambda days=42: [{"id": today, "ctl": 50.0, "atl": 40.0}])
    monkeypatch.setattr(training, "fetch_activities",
                        lambda days=7: [_stub("18814420332"), _real("i2"),
                                        _stub("18815009190")])
    monkeypatch.setattr(training, "_effective_taus_from_db", lambda: {})

    out = training.get_today_metrics()

    ids = [a["id"] for a in out["recent_activities"]]
    assert ids == ["i2"], ids
    assert out["recent_activities"][0]["name"] == "Endurance"


# ── SQLite mirror ─────────────────────────────────────────────────────────

def _activity_rows(db_file: Path) -> list[tuple]:
    conn = sqlite3.connect(str(db_file))
    try:
        return conn.execute("SELECT id, name FROM activities ORDER BY id").fetchall()
    finally:
        conn.close()


def test_sqlite_mirror_leaves_out_strava_stubs(tmp_path, monkeypatch):
    pm_mod.ProfileManager._instance = None
    with patch("pathlib.Path.home", return_value=Path(tmp_path)):
        pm = pm_mod.ProfileManager.get()
    a = pm.create_profile("Alice")
    pm.switch(a)
    pm.save_env("i111", "key_a")
    db_mod.shutdown_sync()

    monkeypatch.setattr(
        db_mod, "fetch_wellness",
        lambda days=90: [{"id": "2026-01-28", "ctl": 10.0, "atl": 5.0,
                          "sportInfo": []}])
    monkeypatch.setattr(
        db_mod, "fetch_activities",
        lambda days=90: [_stub("18814420332"), _real("i2"), _stub("18815009190")])

    db_mod.run_sync(days=1)

    db_file = tmp_path / ".domestique" / "profiles" / "alice" / "health_tracker.db"
    assert _activity_rows(db_file) == [("i2", "Endurance")]


# ── purge ─────────────────────────────────────────────────────────────────

def test_purge_removes_a_zero_byte_ride_file(tmp_path, monkeypatch):
    """The same rider's log: power_curve failed to load i179410835.json,
    "Expecting value: line 1 column 1 (char 0)" — a 0-byte file from an
    interrupted write, re-warned on every load and never cleaned up."""
    icu = tmp_path / "icu"
    icu.mkdir()
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu)
    import os
    import time
    dead = icu / "i179410835.json"
    dead.write_bytes(b"")
    stale = time.time() - 3600
    os.utime(dead, (stale, stale))
    # A file another thread is rewriting this instant is briefly empty too:
    # fresh and empty means "in flight", and the purge must leave it alone.
    (icu / "i3.json").write_bytes(b"")
    (icu / "i2.json").write_text(json.dumps(
        {"ride_id": "icu_i2", "name": "Endurance", "duration_s": 3600, "tss": 55}))

    assert rs.purge_stub_icu_records() == 1
    assert not dead.exists()
    assert (icu / "i3.json").exists()
    assert (icu / "i2.json").exists()
