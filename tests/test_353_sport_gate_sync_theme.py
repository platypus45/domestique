"""v3.5.3 — three owner-reported fixes, root-caused by the wave.

1. HIKE ≠ RIDE: the ICU persist path dropped the sport field, so a hike
   passed _is_cycling_sport("")'s deliberate empty→cycling default and
   completed the day's planned CYCLING session (green ✓ + its TSS in the
   day/week totals). Fix: persist ICU's type as `sport`; the existing
   calendar gate then drops non-cycling activities. One-time db backfill
   heals envelopes older than the incremental sync window.

2. INCREMENTAL SYNC: .last_sync_at was only a 1-hour throttle; the fetch
   always pulled 90 days and the re-persist wiped locally-hydrated streams
   (power-curve backfill), making every open look and cost like a full
   resync. Fix: window bounded by time-since-last-sync; streams and
   terminal markers carry forward like the DFA keys.

3. THEME RESET: pywebview's default private_mode=True wipes the WKWebView
   data store at every launch — localStorage (theme, volume unit, FF range)
   died between runs. Fix: private_mode=False + pinned storage_path.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import app as app_module  # noqa: E402
import ride_storage as rs  # noqa: E402


# ── 1. sport persisted + calendar gate ───────────────────────────────────────

def test_normalize_icu_activity_persists_sport():
    base = {"id": "i9", "name": "x",
            "start_date_local": "2026-07-24T16:02:00", "duration": 100}
    assert rs._normalize_icu_activity({**base, "type": "Hike"})["sport"] == "Hike"
    assert rs._normalize_icu_activity({**base, "sport_type": "Run"})["sport"] == "Run"
    # No type at all → empty string, which downstream deliberately treats as
    # cycling (untagged local FIT rides depend on that default).
    assert rs._normalize_icu_activity(base)["sport"] == ""


def _plan_week():
    # Mirrors tests/test_this_week_calendar.py::_plan_with_today — the shape
    # merge_plan_with_rides needs to mark the current ISO week.
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sessions = [{
        "day": (monday + timedelta(days=off)).isoformat(), "day_name": "X",
        "session_type": "z2", "duration_min": 60, "tss_estimate": 50,
        "status": "pending", "zwo_file": "endurance_clean_60min.zwo",
        "zwo_name": "Endurance 60", "description": "",
    } for off in range(7)]
    return {
        "goal": {"type": "general"},
        "weeks": [{
            "week_num": 1, "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "phase": "build", "tss_target": 350, "is_stepback": False,
            "sessions": sessions,
        }],
        "availability": {},
    }


def _activity(rid, day_iso, dur_s, tss, sport):
    return {"source": "icu", "ride_id": rid, "name": rid, "sport": sport,
            "started_at": f"{day_iso}T10:00:00", "duration_s": dur_s,
            "tss": tss, "avg_power_w": None,
            "time_in_zone": {"z1": dur_s, "z2": 0, "z3": 0, "z4": 0,
                             "z5": 0, "z6": 0, "z7": 0}}


def test_hike_does_not_complete_a_planned_cycling_day():
    today = date.today().isoformat()
    payload = app_module.merge_plan_with_rides(
        _plan_week(), [_activity("icu_hike", today, 8196, 11, "Hike")])
    wk = next(w for w in payload["weeks"] if w.get("is_current"))
    day = next(d for d in wk["days"] if d["date"] == today)
    assert day.get("actual") is None
    assert day.get("card_state") != "completed"
    assert (wk.get("actual_tss") or 0) == 0


def test_ride_and_empty_sport_still_complete_the_day():
    today = date.today().isoformat()
    for sport in ("Ride", "VirtualRide", "GravelRide", "MountainBikeRide", ""):
        payload = app_module.merge_plan_with_rides(
            _plan_week(), [_activity("icu_r", today, 3600, 50, sport)])
        wk = next(w for w in payload["weeks"] if w.get("is_current"))
        day = next(d for d in wk["days"] if d["date"] == today)
        assert day.get("actual") is not None, f"sport={sport!r}"


# ── 1b. one-time db backfill ─────────────────────────────────────────────────

def test_sport_backfill_heals_from_db_and_marks_once(tmp_path, monkeypatch):
    import db
    icu = tmp_path / "rides" / "icu"
    icu.mkdir(parents=True)
    (icu / "i1.json").write_text(json.dumps(
        {"external_id": "i1", "name": "old hike"}))
    (icu / "i2.json").write_text(json.dumps(
        {"external_id": "i2", "name": "old ride", "sport": "Ride"}))
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu)
    dbfile = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", dbfile)
    db.init_db()
    db.get_db().execute(
        "INSERT INTO activities (id, date, name, sport, duration_sec, tss) "
        "VALUES ('i1','2026-01-01','old hike','Hike',100,5)")
    db.get_db().commit()
    assert rs.backfill_icu_sports_from_db() == 1
    assert json.loads((icu / "i1.json").read_text())["sport"] == "Hike"
    assert json.loads((icu / "i2.json").read_text())["sport"] == "Ride"  # untouched
    assert (icu / ".sport_backfill_done").exists()
    assert rs.backfill_icu_sports_from_db() == 0  # marker no-op


def test_full_cycling_taxonomy_matches_planned():
    """The offline /api/activities fallback now feeds typed envelopes into
    _matches_planned — every real cycling type must count as the planned
    session ridden; cross-sport must not."""
    for sport in ("Ride", "VirtualRide", "EBikeRide", "GravelRide",
                  "MountainBikeRide", "EMountainBikeRide"):
        assert app_module._matches_planned([{"sport": sport}], "z2"), sport
    for sport in ("Hike", "Run", "RockClimbing", "NordicSki"):
        assert not app_module._matches_planned([{"sport": sport}], "z2"), sport


def test_sport_backfill_partial_heal_does_not_stamp_marker(tmp_path, monkeypatch):
    """One envelope healable, one not in db — marker must NOT stamp, so a
    later sync (when db knows more) can finish the job."""
    import db
    icu = tmp_path / "icu"
    icu.mkdir()
    (icu / "i1.json").write_text(json.dumps({"external_id": "i1"}))
    (icu / "i9.json").write_text(json.dumps({"external_id": "i9"}))  # not in db
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu)
    dbfile = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", dbfile)
    db.init_db()
    db.get_db().execute(
        "INSERT INTO activities (id, date, name, sport, duration_sec, tss) "
        "VALUES ('i1','2026-01-01','x','Hike',100,5)")
    db.get_db().commit()
    assert rs.backfill_icu_sports_from_db() == 1
    assert not (icu / ".sport_backfill_done").exists()
    # retry heals nothing new but still doesn't stamp
    assert rs.backfill_icu_sports_from_db() == 0
    assert not (icu / ".sport_backfill_done").exists()


def test_sport_backfill_empty_db_does_not_stamp_marker(tmp_path, monkeypatch):
    """An empty read can mean wrong-db-context — must stay healable later."""
    import db
    icu = tmp_path / "icu"
    icu.mkdir()
    (icu / "i1.json").write_text(json.dumps({"external_id": "i1"}))
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu)
    dbfile = tmp_path / "t.db"
    monkeypatch.setattr(db, "DB_PATH", dbfile)
    db.init_db()
    assert rs.backfill_icu_sports_from_db() == 0
    assert not (icu / ".sport_backfill_done").exists()


# ── 2. persist carries hydrated streams; sync window is incremental ──────────

def test_persist_carries_streams_and_markers_forward(tmp_path, monkeypatch):
    icu = tmp_path / "icu"
    icu.mkdir()
    monkeypatch.setattr(rs, "_icu_rides_dir", lambda: icu)
    monkeypatch.setattr(rs, "_maybe_attach_prs", lambda *a, **k: None, raising=False)
    payload = {"id": "i7", "type": "Ride", "name": "r",
               "start_date_local": "2026-07-20T10:00:00", "duration": 3600}
    p = rs.persist_icu_activity(payload)
    # Simulate the power-curve backfill hydrating the envelope.
    data = json.loads(p.read_text())
    data["streams"] = {"watts": [200, 210]}
    data["no_streams_available"] = False
    data["efforts"] = [{"duration": 60, "watts": 250}]
    p.write_text(json.dumps(data))
    # Hourly re-sync re-persists the same ICU payload (which has none of it).
    p2 = rs.persist_icu_activity(payload)
    fresh = json.loads(p2.read_text())
    assert fresh.get("streams") == {"watts": [200, 210]}
    assert fresh.get("efforts") == [{"duration": 60, "watts": 250}]
    # Terminal markers are DELIBERATELY not carried: a transient 429 must
    # stay retryable, not become a permanent streams blackout.
    assert "no_streams_available" not in fresh
    # The per-ride refresh endpoint bypasses the carry entirely so a fresh
    # detail fetch can replace stale streams (ICU re-processed the ride).
    p3 = rs.persist_icu_activity(payload, carry_hydrated=False)
    assert "streams" not in json.loads(p3.read_text())


def test_sync_window_bounds_and_source_pin():
    src = Path(app_module.__file__).read_text()
    assert "fetch_recent_activities(days=_sync_days)" in src
    assert "fetch_recent_activities(days=90)" not in src
    # force=True ("Sync Now") is the escape hatch for late uploads and
    # ICU-side edits beyond the incremental window — full window restored.
    assert "90 if (force or not last)" in src
    # The formula: first sync (no marker) = 90d; steady state = small window.
    import time
    now = time.time()
    for last, expect in [(None, 90), (0, 90),
                         (now - 3 * 86400, 7),
                         (now - 20 * 86400, 22),
                         (now - 400 * 86400, 90)]:
        d = 90 if not last else min(90, max(7, int((now - last) / 86400) + 2))
        assert d == expect, (last, d, expect)


# ── 3. theme persistence: no more private-mode wipe ──────────────────────────

def test_launcher_starts_webview_with_persistent_storage():
    src = (Path(app_module.__file__).parent / "launcher.py").read_text()
    assert "private_mode=False" in src
    assert "storage_path=" in src
    import re
    # the bare wiping-default CALL is gone (comments still mention it)
    assert not re.search(r"^\s*webview\.start\(\)", src, re.M)
