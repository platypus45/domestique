"""v2.3.0 — Swap training type (+ the pin that survives reforecast).

The day a user swaps must keep its type/duration through a reforecast/refit that
would otherwise auto-downshift it on low TSB. That pin (user_swapped) has to
round-trip the Path-A serialization (_plan_dict_to_planned_weeks) AND be honored
by the reforecast downshift loops + _refit_session_frozen.
"""
import tempfile
import pathlib
from datetime import date, timedelta

import pytest

import training_planner as tp
import app


@pytest.fixture
def sandbox_plan_dir(monkeypatch):
    tmp = pathlib.Path(tempfile.mkdtemp())
    monkeypatch.setattr(tp, "PLAN_DIR", tmp / "plans", raising=False)
    yield tmp


def _gen_plan(client):
    body = {"goal_type": "ftp", "hours_per_week": 8, "plan_weeks": 8,
            "available_days": [1, 2, 3, 4, 5, 6], "rest_days": [0]}
    r = client.post("/api/plan/generate", json=body)
    assert r.status_code == 200, r.text[:300]
    return r.json()["plan_json"]


# ── serialization round-trips the pin ─────────────────────────────────────────

def test_user_swapped_roundtrips_path_b():
    s = {"day": "2026-07-01", "day_name": "Wed", "session_type": "vo2max",
         "duration_min": 60, "tss_estimate": 75, "description": "", "status": "pending",
         "user_swapped": True}
    obj = app._planned_session_from_json(s)
    assert getattr(obj, "user_swapped", False) is True
    back = app._planned_session_to_json(obj)
    assert back.get("user_swapped") is True


def test_user_swapped_roundtrips_path_a():
    plan = {"weeks": [{"start": "2026-06-29", "end": "2026-07-05", "week_num": 1,
                       "phase": "build1", "tss_target": 400, "sessions": [
        {"day": "2026-07-01", "day_name": "Wed", "session_type": "vo2max",
         "duration_min": 60, "tss_estimate": 75, "description": "", "status": "pending",
         "user_swapped": True}]}]}
    pw = tp._plan_dict_to_planned_weeks(plan)
    assert pw[0].sessions[0].user_swapped is True


# ── the pin survives a low-TSB reforecast (the critical guard) ─────────────────

def test_swapped_day_pinned_through_reforecast():
    # Two future vo2max days, both under deep fatigue (TSB -30): the SWAPPED one
    # must stay vo2max; the control one is free to be auto-downshifted.
    today = date(2026, 6, 28)
    d_swapped = today + timedelta(days=10)
    d_control = today + timedelta(days=12)
    wk_start = today + timedelta(days=7)
    wk_end = wk_start + timedelta(days=13)

    def sess(d, swapped):
        return {"day": d.isoformat(), "day_name": d.strftime("%a"),
                "session_type": "vo2max", "duration_min": 60, "tss_estimate": 75,
                "description": "", "status": "pending", "user_swapped": swapped}

    plan = {
        "goal": {"type": "ftp", "distribution": "polarized", "plan_weeks": 8},
        "availability": {},
        "weeks": [{"start": wk_start.isoformat(), "end": wk_end.isoformat(),
                   "week_num": 2, "phase": "build2", "tss_target": 600, "sessions": [
            sess(d_swapped, True), sess(d_control, False)]}],
    }
    # Force deep fatigue across the whole window so the downshift loop fires.
    tsb_series = {}
    d = today
    while d <= wk_end:
        tsb_series[d] = -30.0
        d += timedelta(days=1)

    plan2, _mod, _info = tp.reforecast_dict(
        plan, today_iso=today.isoformat(), tsb_series=tsb_series,
        recent_activities=[], availability_overrides={})

    by_day = {s["day"]: s for w in plan2["weeks"] for s in w["sessions"]}
    # Pinned day kept its type; control day was demoted off vo2max.
    assert by_day[d_swapped.isoformat()]["session_type"] == "vo2max"
    assert by_day[d_control.isoformat()]["session_type"] != "vo2max"


# ── _refit_session_frozen honors the pin ──────────────────────────────────────

def test_refit_freezes_swapped_session():
    today = date(2026, 6, 28)
    s = tp.PlannedSession(day=today + timedelta(days=3), day_name="x",
                          session_type="vo2max", duration_min=60, tss_estimate=75,
                          description="", status="pending", user_swapped=True)
    assert tp._refit_session_frozen(s, today) is True


# ── endpoint: swap mutates + pins + clamps ────────────────────────────────────

def test_swap_type_endpoint(sandbox_plan_dir):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    pj = _gen_plan(client)
    today = date.today()
    # pick a future non-rest day
    target = None
    for w in pj["weeks"]:
        for s in w["sessions"]:
            if s["session_type"] != "rest" and date.fromisoformat(s["day"]) > today + timedelta(days=2):
                target = s["day"]
                break
        if target:
            break
    assert target, "no future trainable day in generated plan"

    r = client.post("/api/plan/swap-type",
                    json={"date": target, "session_type": "vo2max", "duration_min": 120})
    assert r.status_code == 200, r.text[:300]
    j = r.json()
    assert j["ok"] and j["session_type"] == "vo2max"
    # Duration clamped to the vo2max ceiling (<=75) unless the matched file set it.
    assert j["duration_min"] <= 75

    # Persisted with the pin + new type.
    import json as _json
    plan = _json.loads((tp.PLAN_DIR / "current_plan.json").read_text())
    hit = {s["day"]: s for w in plan["weeks"] for s in w["sessions"]}[target]
    assert hit["session_type"] == "vo2max"
    assert hit["user_swapped"] is True


def test_swap_type_rejects_unknown(sandbox_plan_dir):
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    _gen_plan(client)
    r = client.post("/api/plan/swap-type",
                    json={"date": "2026-07-01", "session_type": "banana", "duration_min": 60})
    assert r.status_code == 400
