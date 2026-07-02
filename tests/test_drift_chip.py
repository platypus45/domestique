"""P4.2 (v3.0.0, owner decision: formalized hybrid) — plan-drift chip.

Locks:
  * threshold constant 0.15 + strict-inequality boundary behaviour;
  * snapshot {current_ctl, recent_weekly_tss, generated_on} shape + the
    stamping at the regenerate serialization site (round-trip);
  * /api/plan exposes ctl_drift (snapshot vs live CTL) when a snapshot exists;
  * chip is client render-ONLY, keyed on drift.exceeded, wired to the
    EXISTING regenerate action — no auto-regen anywhere.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

import app as app_module  # noqa: E402
import training_planner as tp  # noqa: E402


# ── threshold + pure comparison ──────────────────────────────────────────────

def test_threshold_constant_locked():
    assert app_module.PLAN_CTL_DRIFT_THRESHOLD == 0.15


@pytest.mark.parametrize("live,exceeded", [
    (115.0, False),   # exactly +15% — NOT exceeded (strict >)
    (115.1, True),
    (85.0, False),    # exactly -15% — NOT exceeded
    (84.9, True),
    (100.0, False),
    (50.0, True),
])
def test_threshold_boundary(live, exceeded):
    d = app_module._plan_ctl_drift({"current_ctl": 100.0}, live)
    assert d is not None
    assert d["exceeded"] is exceeded
    assert d["threshold"] == 0.15


def test_drift_payload_shape():
    d = app_module._plan_ctl_drift(
        {"current_ctl": 50.0, "recent_weekly_tss": 400.0,
         "generated_on": "2026-06-01"}, 41.0)
    assert d == {"snapshot_ctl": 50.0, "live_ctl": 41.0,
                 "drift_pct": 0.18, "threshold": 0.15, "exceeded": True,
                 "generated_on": "2026-06-01"}


def test_drift_none_semantics():
    assert app_module._plan_ctl_drift(None, 50.0) is None          # pre-P4.2 plan
    assert app_module._plan_ctl_drift({}, 50.0) is None            # no ctl in snap
    assert app_module._plan_ctl_drift({"current_ctl": 0}, 50) is None
    assert app_module._plan_ctl_drift({"current_ctl": 50}, None) is None
    assert app_module._plan_ctl_drift({"current_ctl": "x"}, 50) is None


# ── snapshot constructor + round-trip ────────────────────────────────────────

def test_snapshot_shape():
    s = tp.plan_ctl_snapshot(50.456, 402.31)
    assert s == {"current_ctl": 50.5, "recent_weekly_tss": 402.3,
                 "generated_on": date.today().isoformat()}
    s2 = tp.plan_ctl_snapshot(None, "junk", generated_on="2026-01-05")
    assert s2 == {"current_ctl": None, "recent_weekly_tss": None,
                  "generated_on": "2026-01-05"}


def test_snapshot_survives_json_round_trip(tmp_path):
    plan = {"weeks": [], "ctl_snapshot": tp.plan_ctl_snapshot(50.0, 400.0)}
    p = tmp_path / "current_plan.json"
    tp.atomic_write_plan(p, plan)
    back = json.loads(p.read_text(encoding="utf-8"))
    assert back["ctl_snapshot"]["current_ctl"] == 50.0
    assert back["ctl_snapshot"]["recent_weekly_tss"] == 400.0
    assert back["ctl_snapshot"]["generated_on"] == date.today().isoformat()


def _mini_plan(monday: date) -> dict:
    sessions = []
    for off in range(7):
        d = monday + timedelta(days=off)
        stype = "z2" if off in (1, 3, 5) else "rest"
        sessions.append({"day": d.isoformat(), "day_name": d.strftime("%a"),
                         "session_type": stype,
                         "duration_min": 60 if stype == "z2" else 0,
                         "tss_estimate": 45 if stype == "z2" else 0,
                         "description": ""})
    return {
        "goal": {"type": "general", "hours_per_week": 6,
                 "event_date": (monday + timedelta(weeks=4)).isoformat()},
        "weeks": [{"week_num": 1, "start": monday.isoformat(),
                   "end": (monday + timedelta(days=6)).isoformat(),
                   "phase": "base", "tss_target": 200,
                   "sessions": sessions}],
        "generated": "2026-01-01T00:00:00",
    }


def test_regenerate_stamps_fresh_snapshot(monkeypatch):
    """The regenerate serialization site refreshes ctl_snapshot from the
    regen's current_ctl (a regen re-anchors the plan → baseline moves)."""
    import ride_storage
    monkeypatch.setattr(ride_storage, "recent_mean_weekly_tss",
                        lambda *a, **k: 390.0)
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    plan = _mini_plan(monday)
    plan["ctl_snapshot"] = tp.plan_ctl_snapshot(99.0, 999.0,
                                                generated_on="2026-01-01")
    new_plan, _info = app_module._regenerate_plan_dict(
        plan, current_ctl=48.0, activities=[], seed_salt=3)
    snap = new_plan["ctl_snapshot"]
    assert snap["current_ctl"] == 48.0          # refreshed, not carried over
    assert snap["recent_weekly_tss"] == 390.0
    assert snap["generated_on"] == today.isoformat()


def test_generate_site_stamps_snapshot_source_level():
    """Both plan-dict serialization sites stamp via tp.plan_ctl_snapshot
    (the generate endpoint is exercised end-to-end by the planner suites)."""
    src = (ROOT / "app.py").read_text(encoding="utf-8")
    assert src.count("tp.plan_ctl_snapshot(") == 2
    gen = src.index('"generated": datetime.now().isoformat(),\n            '
                    '# P4.2')
    assert gen != -1


# ── /api/plan payload: snapshot + live CTL exposed ───────────────────────────

@pytest.fixture
def plan_env(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    monkeypatch.setattr(app_module, "cached", lambda key, fn, ttl=300: {})
    return tmp_path


def _write_plan(tmp_path, snapshot):
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mini_plan(monday)
    if snapshot is not None:
        plan["ctl_snapshot"] = snapshot
    (tmp_path / "current_plan.json").write_text(json.dumps(plan),
                                                encoding="utf-8")


def test_api_plan_exposes_drift(plan_env, monkeypatch):
    _write_plan(plan_env, tp.plan_ctl_snapshot(50.0, 400.0))
    monkeypatch.setattr(app_module, "_merge_training_load",
                        lambda t: {"ctl": 41.0, "atl": 30.0, "tsb": 11.0,
                                   "source": "icu"})
    client = TestClient(app_module.app)
    r = client.get("/api/plan")
    assert r.status_code == 200
    drift = r.json()["plan_json"].get("ctl_drift")
    assert drift is not None
    assert drift["snapshot_ctl"] == 50.0
    assert drift["live_ctl"] == 41.0
    assert drift["exceeded"] is True      # 18% > 15%


def test_api_plan_drift_not_exceeded_within_band(plan_env, monkeypatch):
    _write_plan(plan_env, tp.plan_ctl_snapshot(50.0, 400.0))
    monkeypatch.setattr(app_module, "_merge_training_load",
                        lambda t: {"ctl": 46.0, "atl": 30.0, "tsb": 16.0,
                                   "source": "icu"})
    client = TestClient(app_module.app)
    drift = client.get("/api/plan").json()["plan_json"]["ctl_drift"]
    assert drift["exceeded"] is False     # 8% < 15% → chip hidden client-side


def test_api_plan_no_snapshot_no_drift_no_error(plan_env, monkeypatch):
    _write_plan(plan_env, None)           # pre-P4.2 plan
    monkeypatch.setattr(app_module, "_merge_training_load",
                        lambda t: {"ctl": 41.0, "atl": 30.0, "tsb": 11.0,
                                   "source": "icu"})
    client = TestClient(app_module.app)
    r = client.get("/api/plan")
    assert r.status_code == 200
    assert "ctl_drift" not in r.json()["plan_json"]


# ── chip: structural (client is render-only) ────────────────────────────────

def test_chip_structure_and_wiring():
    html = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    assert 'id="plan-drift-chip"' in html
    assert "function renderPlanDriftChip(drift)" in html
    # Keyed strictly on the SERVER's exceeded flag (client renders only).
    assert "!drift.exceeded" in html
    assert "renderPlanDriftChip(d.plan_json.ctl_drift)" in html
    # Locked copy + wired to the EXISTING regenerate action.
    assert "Plan assumes CTL" in html
    assert 'onclick="regeneratePlan()"' in html
    # No auto-regen: the chip renderer must not CALL regeneratePlan itself.
    start = html.index("function renderPlanDriftChip")
    body = html[start:html.index("\n}", start)]
    assert "await regeneratePlan" not in body
    assert body.count("regeneratePlan()") == 1  # only inside the onclick attr
