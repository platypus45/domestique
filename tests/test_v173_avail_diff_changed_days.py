"""v1.7.3 — save-availability only reflows days the user actually changed.

Pre-v1.7.3 flow:
- Frontend POSTs every day in the visible calendar (180 days of weekly-
  grid defaults).
- Backend converts the full dict into ``availability_overrides`` and
  feeds it to ``reforecast()``'s scaling loop.
- v1.7.1's ceiling-only cap meant once a session was shrunk, raising
  hours on the calendar could never restore the planner's duration.

v1.7.3 captures the prior ``plan["availability"]`` before overwriting,
then builds ``availability_overrides`` from only the days where the
incoming hours differ from the stored value. The cap branch in
``training_planner.reforecast`` now applies the user's hours LITERALLY
(both up and down) — safe because the caller has already filtered to
intentional edits.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module


def _isolate_plan_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    return tmp_path


def _seed_plan(plan_dir: Path) -> tuple[Path, str, str]:
    json_path = plan_dir / "current_plan.json"
    today = date.today()
    monday = today + timedelta(days=(7 - today.weekday()))
    wed = (monday + timedelta(days=2)).isoformat()
    thu = (monday + timedelta(days=3)).isoformat()
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "sessions": [
                {"day": wed, "day_name": "Wed", "session_type": "z2",
                 "duration_min": 110, "tss_estimate": 82.0,
                 "description": "Z2", "zwo_file": "wed.zwo",
                 "zwo_name": "Wed Original", "status": "pending"},
                {"day": thu, "day_name": "Thu", "session_type": "z2",
                 "duration_min": 120, "tss_estimate": 90.0,
                 "description": "Z2", "zwo_file": "thu.zwo",
                 "zwo_name": "Thu Original", "status": "pending"},
            ],
        }],
        "availability": {},
    }
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return json_path, wed, thu


def _load_session(json_path: Path, day_iso: str) -> dict:
    plan = json.loads(json_path.read_text(encoding="utf-8"))
    for w in plan.get("weeks", []):
        for s in w.get("sessions", []):
            if s.get("day") == day_iso:
                return s
    return {}


def test_resubmitting_unchanged_avail_returns_zero(tmp_path, monkeypatch):
    """User saves an availability dict, then saves the IDENTICAL dict again.
    Second save must not modify any session (0 changes)."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path, wed, thu = _seed_plan(plan_dir)
    client = TestClient(app_module.app)

    payload = {"availability": {
        wed: {"hours": 1.0, "type": "available"},
        thu: {"hours": 2.0, "type": "available"},
    }}
    r1 = client.post("/api/plan/save-availability", json=payload)
    assert r1.status_code == 200
    n1 = r1.json().get("sessions_modified", -1)
    assert n1 >= 1  # first save shrank Wed

    # Resubmit identical payload — Wed already 60min, Thu already 120min.
    r2 = client.post("/api/plan/save-availability", json=payload)
    assert r2.status_code == 200
    n2 = r2.json().get("sessions_modified", -1)
    assert n2 == 0, f"Expected 0 changes on idempotent resave, got {n2}"


def test_cap_then_restore_via_raised_hours(tmp_path, monkeypatch):
    """User caps Wed to 1h → 60min. Then RAISES it to 2.5h → expansion
    restores duration to 150min. Pre-v1.7.3 the ceiling-only rule
    blocked this: the second save left Wed at 60min."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path, wed, thu = _seed_plan(plan_dir)
    client = TestClient(app_module.app)

    # Step 1: shrink.
    r1 = client.post(
        "/api/plan/save-availability",
        json={"availability": {wed: {"hours": 1.0, "type": "available"}}},
    )
    assert r1.status_code == 200
    assert _load_session(json_path, wed)["duration_min"] == 60

    # Step 2: raise back up. New POST keeps Wed at 1h (would no-op) AND
    # Thu at 2h (also no-op) BUT changes Wed to 2.5h.
    r2 = client.post(
        "/api/plan/save-availability",
        json={"availability": {wed: {"hours": 2.5, "type": "available"}}},
    )
    assert r2.status_code == 200
    wed_session = _load_session(json_path, wed)
    assert wed_session["duration_min"] == 150, \
        f"Wed expected expanded 150min, got {wed_session['duration_min']}"


def test_autofill_default_days_do_not_count_as_user_edits(tmp_path, monkeypatch):
    """Frontend POSTs 180 days; only ONE is user-edited. The other 179
    must be treated as auto-fill and NOT touched.

    Setup: pre-seed prior availability with Thu at 2.0h. User edits Wed
    to 1h. POST includes both. v1.7.3 diff: Wed=changed, Thu=same →
    overrides = {Wed: 1.0}. Thu's session stays at 120min."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path, wed, thu = _seed_plan(plan_dir)

    # Seed prior availability so Thu is in the stored set.
    plan = json.loads(json_path.read_text(encoding="utf-8"))
    plan["availability"] = {thu: {"hours": 2.0, "type": "available"}}
    json_path.write_text(json.dumps(plan), encoding="utf-8")

    client = TestClient(app_module.app)
    # Frontend re-sends Thu at the SAME 2.0h plus Wed at 1.0h (new).
    r = client.post(
        "/api/plan/save-availability",
        json={"availability": {
            wed: {"hours": 1.0, "type": "available"},
            thu: {"hours": 2.0, "type": "available"},
        }},
    )
    assert r.status_code == 200, r.text

    wed_session = _load_session(json_path, wed)
    thu_session = _load_session(json_path, thu)
    assert wed_session["duration_min"] == 60, \
        f"Wed expected 60min (user-edited), got {wed_session['duration_min']}"
    assert thu_session["duration_min"] == 120, \
        f"Thu expected unchanged 120min (auto-fill), got {thu_session['duration_min']}"


def test_no_user_edits_returns_zero_and_no_writeback_changes(tmp_path, monkeypatch):
    """If incoming availability matches stored prior in every entry,
    the endpoint short-circuits to a 0-change response without invoking
    reforecast at all."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path, wed, thu = _seed_plan(plan_dir)

    plan = json.loads(json_path.read_text(encoding="utf-8"))
    plan["availability"] = {
        wed: {"hours": 1.5, "type": "available"},
        thu: {"hours": 2.0, "type": "available"},
    }
    json_path.write_text(json.dumps(plan), encoding="utf-8")

    client = TestClient(app_module.app)
    r = client.post(
        "/api/plan/save-availability",
        json={"availability": {
            wed: {"hours": 1.5, "type": "available"},
            thu: {"hours": 2.0, "type": "available"},
        }},
    )
    assert r.status_code == 200
    assert r.json().get("sessions_modified") == 0

    # Sessions on disk are unchanged.
    assert _load_session(json_path, wed)["duration_min"] == 110
    assert _load_session(json_path, thu)["duration_min"] == 120
