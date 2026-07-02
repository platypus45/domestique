"""v1.8.3 BUG-D — apply-tier-down error-action disambiguation.

Pre-v1.8.3 ``POST /api/readiness/apply-tier-down`` returned a generic
``{"ok": False, "action": "no_change"}`` whenever ``_drop_intensity``
yielded the same session_type. The user screenshot read
"could not apply: no_change" with no actionable info — they couldn't
tell whether the session was already at the bottom of the Seiler
ladder or whether the session_type was simply unknown to the planner.

v1.8.3 splits the error envelope into three explicit actions:

* ``already_easy`` — rest / recovery short-circuit (unchanged from v1.7.5).
* ``already_at_bottom`` — session is at the practical intensity floor
  (z2 / long_z2) so a further drop would just be a duration tweak, not
  a real tier-down.
* ``unknown_type`` — ``session_type`` is not in
  ``training_planner._INTENSITY_LADDER`` (e.g. ``ftp_test``, garbage).

These tests pin the three error actions plus the happy path
(vo2max → threshold) so the frontend toast can branch on
``data.action`` without sniffing message strings.
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


def _seed_plan(plan_dir: Path, session_type: str,
               duration_min: int = 60, tss: float = 60.0) -> tuple[Path, str]:
    json_path = plan_dir / "current_plan.json"
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    today_iso = today.isoformat()
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "sessions": [{
                "day": today_iso,
                "day_name": "Today",
                "session_type": session_type,
                "duration_min": duration_min,
                "tss_estimate": tss,
                "description": f"{session_type} session",
                "zwo_file": "original.zwo",
                "zwo_name": "Original Workout",
                "status": "pending",
            }],
        }],
        "availability": {},
    }
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return json_path, today_iso


def test_recovery_session_returns_already_easy(tmp_path, monkeypatch):
    """v1.7.5 contract preserved: rest/recovery short-circuit to already_easy."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan(plan_dir, session_type="recovery", duration_min=30, tss=15.0)

    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert body.get("action") == "already_easy"
    assert body.get("session_type") == "recovery"


def test_z2_session_returns_already_at_bottom(tmp_path, monkeypatch):
    """z2 is in the ladder but is the practical intensity floor — dropping
    it yields long_z2, a duration tweak rather than a meaningful tier-down.
    Surface that as ``already_at_bottom`` so the toast can tell the user
    "nothing to drop"."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan(plan_dir, session_type="z2", duration_min=90, tss=68.0)

    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert body.get("action") == "already_at_bottom"
    assert body.get("session_type") == "z2"


def test_unknown_session_type_returns_unknown_type(tmp_path, monkeypatch):
    """A session_type not in tp._INTENSITY_LADDER must return
    ``unknown_type`` rather than the silent ``no_change`` passthrough."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan(plan_dir, session_type="foobar", duration_min=60, tss=50.0)

    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is False
    assert body.get("action") == "unknown_type"
    assert body.get("session_type") == "foobar"


def test_vo2max_session_drops_to_threshold(tmp_path, monkeypatch):
    """Sanity: a hard session (vo2max) still takes the happy path and
    drops one step down the Seiler ladder — ensures the new guard rails
    don't break the v1.7.5 success flow."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_plan(plan_dir, session_type="vo2max", duration_min=60, tss=95.0)
    # Hermetic gate (tranche-10 family): the tier-down path calls
    # get_today_metrics() → live ICU wellness; a machine-wide 429 with a huge
    # Retry-After hung this test past the 120s timeout. Stub it.
    import training as _training
    monkeypatch.setattr(_training, "get_today_metrics", lambda: {}, raising=False)
    monkeypatch.setattr(app_module, "get_today_metrics", lambda: {}, raising=False)

    client = TestClient(app_module.app)
    r = client.post("/api/readiness/apply-tier-down", json={})
    assert r.status_code == 200
    body = r.json()
    assert body.get("ok") is True
    assert body.get("old_type") == "vo2max"
    assert body.get("new_type") == "threshold"


def test_dashboard_handler_branches_on_action():
    """Pin the frontend toast wiring: applyReadinessTierDown must branch
    on data.action so each error reason gets its own user-facing message."""
    dash = (Path(app_module.__file__).parent / "templates"
            / "dashboard.html").read_text(encoding="utf-8")
    # The handler exists.
    assert ("function applyReadinessTierDown" in dash
            or "async function applyReadinessTierDown" in dash)
    # Branches on each of the three v1.8.3 actions.
    assert "'already_at_bottom'" in dash
    assert "'unknown_type'" in dash
    assert "'already_easy'" in dash
