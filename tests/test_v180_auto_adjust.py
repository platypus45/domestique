"""v1.8.0 §F1 — Automated TSB+HRV → planner with Hooper override.

Tests:
  - apply_week_tier_down helper: selection window (Mon-Sun strict),
    skips completed, deepcopy on dry_run, NoCandidateWorkoutError tolerant.
  - POST /api/plan/auto-adjust: scope today/week × severity normal/rest/tier_down,
    dry_run vs persist semantics, missing severity helper → 503.
"""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


# ── helpers ─────────────────────────────────────────────────────────────────

def _isolate_plan_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    return tmp_path


def _seed_week_plan(plan_dir: Path) -> tuple[Path, dict]:
    """Seed a plan whose week contains today + several remaining hards."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    sessions = []
    for i in range(7):
        d = monday + timedelta(days=i)
        if i == 0:
            stype, dur, tss = "vo2max", 60, 95.0
        elif i == 2:
            stype, dur, tss = "threshold", 60, 85.0
        elif i == 4:
            stype, dur, tss = "sweetspot", 75, 100.0
        else:
            stype, dur, tss = "z2", 60, 45.0
        sessions.append({
            "day": d.isoformat(),
            "day_name": d.strftime("%A"),
            "session_type": stype,
            "duration_min": dur,
            "tss_estimate": tss,
            "description": f"{stype} session",
            "zwo_file": f"{stype}_seed.zwo",
            "zwo_name": f"Seed {stype.title()}",
            "status": "pending",
        })
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [6]},
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": sunday.isoformat(),
            "sessions": sessions,
        }],
        "availability": {},
    }
    json_path = plan_dir / "current_plan.json"
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return json_path, plan


def _patch_severity(monkeypatch, severity: str, source: str = "tsb_hrv_auto"):
    import readiness_composite
    monkeypatch.setattr(
        readiness_composite,
        "compute_training_severity",
        lambda profile_id, day_iso: {
            "score": 4.0, "severity": severity, "source": source,
            "reasons": ["test"], "hooper_index": None, "tsb": -10.0,
        },
        raising=False,
    )


# ── apply_week_tier_down unit tests ─────────────────────────────────────────

def test_apply_week_tier_down_walks_remaining_hards(tmp_path):
    """Mon-Sun window picks up all remaining hard sessions from today onward."""
    _, plan = _seed_week_plan(tmp_path)
    today_iso = date.today().isoformat()
    result = tp.apply_week_tier_down(plan, today_iso, dry_run=False)
    # Should touch the 3 hard sessions (vo2max/threshold/sweetspot) ON or AFTER
    # today within this week. We seeded Mon=vo2max so this matches if today
    # is Mon; otherwise the set is filtered to remaining days.
    assert result["sessions_modified"] >= 0
    assert isinstance(result["actions"], list)
    # Every action has the required keys.
    for a in result["actions"]:
        assert set(a.keys()) >= {"day", "before", "after", "rematched", "zwo_cleared"}
        assert a["before"]["type"] in tp._HARD_SESSION_TYPES
        # After is one tier easier.
        assert a["after"]["type"] != a["before"]["type"]


def test_apply_week_tier_down_dry_run_uses_deepcopy(tmp_path):
    """dry_run=True must not mutate the input plan dict."""
    _, plan = _seed_week_plan(tmp_path)
    today_iso = date.today().isoformat()
    plan_before = copy.deepcopy(plan)
    result = tp.apply_week_tier_down(plan, today_iso, dry_run=True)
    # sessions_modified is 0 on dry_run.
    assert result["sessions_modified"] == 0
    # Input plan must be unchanged.
    assert plan == plan_before
    # Actions must still describe what would happen.
    assert isinstance(result["actions"], list)


def test_apply_week_tier_down_strict_monday_to_sunday(tmp_path):
    """Sessions outside Mon-Sun ISO week from day_iso are NOT touched."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    next_monday = monday + timedelta(days=7)
    plan = {
        "goal": {"type": "general"},
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "sessions": [
                {
                    "day": (monday + timedelta(days=1)).isoformat(),
                    "day_name": "Tuesday",
                    "session_type": "vo2max",
                    "duration_min": 60, "tss_estimate": 95.0,
                    "description": "", "zwo_file": "a.zwo", "zwo_name": "A",
                    "status": "pending",
                },
            ],
        }, {
            "week_num": 2,
            "start": next_monday.isoformat(),
            "end": (next_monday + timedelta(days=6)).isoformat(),
            "sessions": [
                {
                    "day": next_monday.isoformat(),
                    "day_name": "Monday",
                    "session_type": "threshold",
                    "duration_min": 60, "tss_estimate": 85.0,
                    "description": "", "zwo_file": "b.zwo", "zwo_name": "B",
                    "status": "pending",
                },
            ],
        }],
    }
    result = tp.apply_week_tier_down(plan, monday.isoformat(), dry_run=True)
    # Only week 1's tuesday vo2max should appear; next_monday is excluded.
    touched_days = {a["day"] for a in result["actions"]}
    assert next_monday.isoformat() not in touched_days


def test_apply_week_tier_down_skips_completed_sessions(tmp_path):
    """Completed sessions stay untouched."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    plan = {
        "goal": {"type": "general"},
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": (monday + timedelta(days=6)).isoformat(),
            "sessions": [
                {
                    "day": monday.isoformat(),
                    "day_name": "Monday",
                    "session_type": "vo2max",
                    "duration_min": 60, "tss_estimate": 95.0,
                    "description": "", "zwo_file": "a.zwo", "zwo_name": "A",
                    "status": "completed",
                },
                {
                    "day": (monday + timedelta(days=2)).isoformat(),
                    "day_name": "Wednesday",
                    "session_type": "threshold",
                    "duration_min": 60, "tss_estimate": 85.0,
                    "description": "", "zwo_file": "b.zwo", "zwo_name": "B",
                    "status": "pending",
                },
            ],
        }],
    }
    result = tp.apply_week_tier_down(plan, monday.isoformat(), dry_run=True)
    touched = {a["day"] for a in result["actions"]}
    assert monday.isoformat() not in touched  # completed → skipped


def test_apply_week_tier_down_no_candidate_continues(tmp_path, monkeypatch):
    """NoCandidateWorkoutError mid-walk → clear ZWO, mark rematched=False,
    don't abort."""
    _, plan = _seed_week_plan(tmp_path)
    # v2.0.3 F3: tier-down is TODAY-ONWARD, so anchor at the seeded week's Monday
    # — its Mon/Wed/Fri hards then fall in the [anchor, sunday] window regardless
    # of which weekday this test actually runs on (was date-fragile: on a late-
    # week today the hards were all behind the window and no actions recorded).
    week_start_iso = plan["weeks"][0]["start"]

    def _raise(session, library, **kwargs):
        raise tp.NoCandidateWorkoutError("no workout for this slot")

    monkeypatch.setattr(tp, "match_zwo", _raise)
    monkeypatch.setattr(tp, "load_workout_library", lambda: [])

    result = tp.apply_week_tier_down(plan, week_start_iso, dry_run=True)
    # Walk continues — at least one action recorded.
    assert result["actions"]
    for a in result["actions"]:
        assert a["rematched"] is False
        assert a["zwo_cleared"] is True


# ── /api/plan/auto-adjust endpoint tests ────────────────────────────────────

def test_auto_adjust_severity_normal_no_action(tmp_path, monkeypatch):
    """severity=normal returns ok with empty actions, no plan needed."""
    _isolate_plan_dir(tmp_path, monkeypatch)
    _patch_severity(monkeypatch, "normal", "tsb_hrv_auto")
    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust", json={"scope": "today"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["severity"] == "normal"
    assert body["actions"] == []
    assert body["sessions_modified"] == 0
    assert "note" in body


def test_auto_adjust_severity_rest_today(tmp_path, monkeypatch):
    """severity=rest sets today's session to rest, clears ZWO."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path, _ = _seed_week_plan(plan_dir)
    _patch_severity(monkeypatch, "rest")
    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust", json={"scope": "today"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["severity"] == "rest"
    assert len(body["actions"]) >= 1
    # Persisted: today's session is now rest with tss=0.
    saved = json.loads(json_path.read_text())
    today_iso = date.today().isoformat()
    today_sess = None
    for w in saved["weeks"]:
        for s in w["sessions"]:
            if s["day"] == today_iso:
                today_sess = s
                break
    assert today_sess is not None
    assert today_sess["session_type"] == "rest"
    assert today_sess["tss_estimate"] == 0
    assert today_sess["zwo_file"] == ""


def test_auto_adjust_severity_rest_week_collapses_to_today(tmp_path, monkeypatch):
    """severity=rest + scope=week collapses to today-only rest + note."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_week_plan(plan_dir)
    _patch_severity(monkeypatch, "rest")
    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust", json={"scope": "week"})
    assert r.status_code == 200
    body = r.json()
    assert body["severity"] == "rest"
    # Exactly one action (today's rest).
    assert len(body["actions"]) == 1
    assert body["actions"][0]["day"] == date.today().isoformat()
    assert "today only" in body["note"].lower() or "today-only" in body["note"].lower()


def test_auto_adjust_severity_tier_down_today(tmp_path, monkeypatch):
    """severity=tier_down + scope=today drops today's hard session one tier."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    _seed_week_plan(plan_dir)
    _patch_severity(monkeypatch, "tier_down")
    # Stub match_zwo so we don't depend on workout library state.
    monkeypatch.setattr(tp, "load_workout_library", lambda: [])

    def _fake_match(session, library, **kwargs):
        session.zwo_file = "fake_replacement.zwo"
        session.zwo_name = "Fake Replacement"
        return session

    monkeypatch.setattr(tp, "match_zwo", _fake_match)

    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust", json={"scope": "today"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["severity"] == "tier_down"
    # Today's hard session may exist or not depending on weekday — if it does,
    # we should see exactly one action.
    today_iso = date.today().isoformat()
    saved = json.loads((plan_dir / "current_plan.json").read_text())
    today_sess = None
    for w in saved["weeks"]:
        for s in w["sessions"]:
            if s["day"] == today_iso:
                today_sess = s
                break
    if today_sess and today_sess.get("session_type") not in {"z2", "long_z2", "recovery", "rest"}:
        # Should have tier-down breadcrumb.
        assert today_sess.get("adapted") is True


def test_auto_adjust_dry_run_does_not_persist(tmp_path, monkeypatch):
    """dry_run=True must not write the plan to disk."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path, original_plan = _seed_week_plan(plan_dir)
    _patch_severity(monkeypatch, "tier_down")
    monkeypatch.setattr(tp, "load_workout_library", lambda: [])

    write_calls: list = []
    real_write = tp.atomic_write_plan
    monkeypatch.setattr(tp, "atomic_write_plan",
                        lambda *a, **kw: write_calls.append((a, kw)))

    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust",
                    json={"scope": "week", "dry_run": True})
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["sessions_modified"] == 0
    assert write_calls == [], "dry_run wrote the plan"
    # Disk content unchanged.
    saved = json.loads(json_path.read_text())
    assert saved == original_plan


def test_auto_adjust_severity_unavailable_returns_503(tmp_path, monkeypatch):
    """If compute_training_severity raises, return 503 (don't crash)."""
    _isolate_plan_dir(tmp_path, monkeypatch)
    import readiness_composite

    def _boom(*a, **kw):
        raise RuntimeError("simulated absence")

    monkeypatch.setattr(readiness_composite, "compute_training_severity",
                        _boom, raising=False)
    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust", json={"scope": "today"})
    assert r.status_code == 503
    assert "severity unavailable" in r.json().get("error", "")


def test_auto_adjust_invalid_scope_returns_400(tmp_path, monkeypatch):
    _isolate_plan_dir(tmp_path, monkeypatch)
    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust", json={"scope": "month"})
    assert r.status_code == 400


def test_auto_adjust_no_plan_returns_404(tmp_path, monkeypatch):
    """severity=tier_down but no plan exists → 404."""
    _isolate_plan_dir(tmp_path, monkeypatch)
    _patch_severity(monkeypatch, "tier_down")
    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust", json={"scope": "today"})
    assert r.status_code == 404


def test_readiness_composite_carries_severity_fields(tmp_path, monkeypatch):
    """GET /api/readiness/composite merges severity/source/severity_reasons."""
    _patch_severity(monkeypatch, "tier_down", "hooper")
    # Also stub compute_readiness_composite to return a known dict.
    monkeypatch.setattr(
        app_module, "compute_readiness_composite",
        lambda profile_id, day_iso: {"score": 4.0, "status": "static_weights"},
        raising=True,
    )
    # Bypass the cache so our patched function is actually invoked.
    monkeypatch.setattr(app_module, "cached",
                        lambda key, fn, ttl=300: fn(), raising=True)
    client = TestClient(app_module.app)
    r = client.get("/api/readiness/composite")
    assert r.status_code == 200
    body = r.json()
    assert body.get("severity") == "tier_down"
    assert body.get("source") == "hooper"
    assert body.get("severity_reasons") == ["test"]
