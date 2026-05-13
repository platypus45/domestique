"""v1.6.4 — availability calendar persists across save → reload.

User reported: "I change the availability calendar and update, then new
workouts. Then I close the app and reopen — it's all back to old state.
It doesn't save."

Two-layer bug:

1. **Frontend race** (dashboard.html ~line 2803): the Plan-tab handler
   fired ``initAvailCalendar()`` synchronously BEFORE ``loadPlan()``'s
   async fetch resolved. ``loadAvailData()`` then read an empty
   ``window._planData`` and filled ``_availData`` from the weekly-grid
   defaults, hiding every saved override the moment the user opened the
   tab. The display state regressed even though the disk file was
   intact.

2. **Backend contract** (this test): the round-trip must survive. The
   tests below pin the contract regardless of the JS race.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module


def _isolate_plan_dir(tmp_path: Path, monkeypatch) -> Path:
    """Point ``_plan_dir()`` at a temp dir so we don't clobber the real plan."""
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    return tmp_path


def _seed_minimal_plan(plan_dir: Path) -> Path:
    """Write a minimal current_plan.json so save-availability has something to mutate."""
    json_path = plan_dir / "current_plan.json"
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
        "phases": [],
        "weeks": [],
        "availability": {},
    }
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return json_path


def test_save_availability_persists_to_disk(tmp_path, monkeypatch):
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_minimal_plan(plan_dir)

    client = TestClient(app_module.app)

    payload = {
        "availability": {
            "2026-05-14": {"hours": 0, "type": "holiday"},
            "2026-05-15": {"hours": 0, "type": "holiday"},
            "2026-05-16": {"hours": 3.5, "type": "available"},
        }
    }
    r = client.post("/api/plan/save-availability", json=payload)
    assert r.status_code == 200
    assert r.json().get("ok") is True

    # Now read from disk directly — bypass /api/plan so we know it's the
    # write path, not the read path, that's working.
    persisted = json.loads((plan_dir / "current_plan.json").read_text(encoding="utf-8"))
    assert persisted["availability"] == payload["availability"]


def test_get_plan_returns_saved_availability(tmp_path, monkeypatch):
    """The frontend reads ``window._planData.availability`` — so /api/plan
    must surface it verbatim, otherwise the calendar can't render the user's
    overrides even with the JS race fixed."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_minimal_plan(plan_dir)

    client = TestClient(app_module.app)

    payload = {
        "availability": {
            "2026-06-01": {"hours": 0, "type": "injury"},
            "2026-06-02": {"hours": 2.0, "type": "available"},
        }
    }
    client.post("/api/plan/save-availability", json=payload)

    r = client.get("/api/plan")
    assert r.status_code == 200
    body = r.json()
    # /api/plan returns either the plan dict directly or wrapped under plan_json.
    plan_payload = body.get("plan_json") if "plan_json" in body else body
    assert plan_payload is not None
    returned = plan_payload.get("availability") or {}
    for k, v in payload["availability"].items():
        assert returned.get(k) == v, f"availability key {k} not preserved: {returned.get(k)} vs {v}"


def test_save_availability_survives_multiple_saves(tmp_path, monkeypatch):
    """Each save should fully replace the availability dict (the frontend
    sends the full computed dict every time, not a delta)."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_minimal_plan(plan_dir)

    client = TestClient(app_module.app)

    first = {"availability": {"2026-07-01": {"hours": 0, "type": "holiday"}}}
    client.post("/api/plan/save-availability", json=first)

    second = {
        "availability": {
            "2026-07-01": {"hours": 1.5, "type": "available"},  # changed type+hours
            "2026-07-02": {"hours": 0, "type": "holiday"},      # new key
        }
    }
    client.post("/api/plan/save-availability", json=second)

    persisted = json.loads((plan_dir / "current_plan.json").read_text(encoding="utf-8"))
    assert persisted["availability"] == second["availability"]
