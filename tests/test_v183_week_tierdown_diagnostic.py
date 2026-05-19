"""v1.8.3 BUG-C — auto-adjust diagnostic when actions=[].

User reported "Auto-adjust preview · Severity: TIER_DOWN · No sessions
need adjustment" with no explanation. apply_week_tier_down now returns a
diagnostic block enumerating each candidate's rejection reason
(rest_day / already_easy / completed / at_bottom) so the modal can tell
the user WHY nothing was changed.

Tests:
  - All-easy week → actions=[] and diagnostic populated with reasons.
  - At least one hard pending session → actions non-empty.
  - Diagnostic enumerates a reason per in-week candidate considered.
  - /api/plan/auto-adjust passes diagnostic through when actions=[].
  - Frontend dashboard.html contains the reason-list render block.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

import app as app_module
import training_planner as tp


def _isolate_plan_dir(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setattr(app_module, "_plan_dir", lambda: tmp_path)
    return tmp_path


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


def _all_easy_week_plan(today: date) -> dict:
    """Week where every session is z2/recovery/rest — nothing to tier-down."""
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    types = ["z2", "recovery", "long_z2", "z2", "recovery", "rest", "z2"]
    sessions = []
    for i, stype in enumerate(types):
        d = monday + timedelta(days=i)
        sessions.append({
            "day": d.isoformat(),
            "day_name": d.strftime("%A"),
            "session_type": stype,
            "duration_min": 60 if stype != "rest" else 0,
            "tss_estimate": 45.0 if stype != "rest" else 0.0,
            "description": f"{stype} ride",
            "zwo_file": f"{stype}.zwo" if stype != "rest" else "",
            "zwo_name": stype.title() if stype != "rest" else "",
            "status": "pending",
        })
    return {
        "goal": {"type": "general"},
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": sunday.isoformat(),
            "sessions": sessions,
        }],
        "availability": {},
    }


def _one_hard_week_plan(today: date) -> dict:
    """Week with one pending hard session today + rest are easy/completed."""
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    sessions = [{
        "day": today.isoformat(),
        "day_name": today.strftime("%A"),
        "session_type": "vo2max",
        "duration_min": 60,
        "tss_estimate": 95.0,
        "description": "vo2 ride",
        "zwo_file": "vo2.zwo",
        "zwo_name": "VO2",
        "status": "pending",
    }]
    # Fill rest of week with easy + one completed.
    for i in range(7):
        d = monday + timedelta(days=i)
        if d == today:
            continue
        sessions.append({
            "day": d.isoformat(),
            "day_name": d.strftime("%A"),
            "session_type": "z2",
            "duration_min": 60,
            "tss_estimate": 45.0,
            "description": "z2",
            "zwo_file": "z2.zwo",
            "zwo_name": "Z2",
            "status": "pending",
        })
    return {
        "goal": {"type": "general"},
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": sunday.isoformat(),
            "sessions": sessions,
        }],
        "availability": {},
    }


# ── unit tests on the helper ───────────────────────────────────────────────


def test_diagnostic_populated_when_all_easy(tmp_path):
    """All-easy week → actions=[] and diagnostic enumerates rejection reasons."""
    today = date.today()
    plan = _all_easy_week_plan(today)
    result = tp.apply_week_tier_down(plan, today.isoformat(), dry_run=True)
    assert result["actions"] == []
    diag = result.get("diagnostic")
    assert isinstance(diag, dict)
    assert isinstance(diag.get("reasons"), list)
    assert diag["candidates_considered"] >= 1
    # All reasons are one of the expected buckets.
    valid = {"rest_day", "already_easy", "completed", "at_bottom"}
    for r in diag["reasons"]:
        assert r["reason"] in valid, f"unexpected reason: {r!r}"
        assert "day" in r
    # Expect at least one already_easy and one rest_day in the all-easy fixture
    # (today onward — i.e. depending on weekday, may be a subset).
    reason_kinds = {r["reason"] for r in diag["reasons"]}
    assert "already_easy" in reason_kinds or "rest_day" in reason_kinds


def test_diagnostic_present_with_hard_session(tmp_path):
    """One pending hard today → actions non-empty + diagnostic still emitted."""
    today = date.today()
    plan = _one_hard_week_plan(today)
    # Stub workout library / match_zwo so the helper doesn't need real ZWOs.
    import training_planner as _tp

    def _fake_match(session, library, **kwargs):
        session.zwo_file = "fake.zwo"
        session.zwo_name = "Fake"
        return session

    orig_match = _tp.match_zwo
    orig_load = _tp.load_workout_library
    _tp.match_zwo = _fake_match  # type: ignore[assignment]
    _tp.load_workout_library = lambda: []  # type: ignore[assignment]
    try:
        result = _tp.apply_week_tier_down(plan, today.isoformat(), dry_run=True)
    finally:
        _tp.match_zwo = orig_match
        _tp.load_workout_library = orig_load

    assert len(result["actions"]) >= 1
    # Diagnostic still emitted (audit value), but it shouldn't include the
    # acted-on day as a rejection.
    diag = result.get("diagnostic")
    assert isinstance(diag, dict)
    acted_days = {a["day"] for a in result["actions"]}
    rejected_days = {r["day"] for r in diag.get("reasons", [])}
    assert not (acted_days & rejected_days), \
        "diagnostic should not list days that were also acted on"


def test_diagnostic_enumerates_each_candidate(tmp_path):
    """Mixed rejection reasons → diagnostic lists each candidate's reason."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    sessions = [
        # rest_day: explicit rest
        {"day": monday.isoformat(), "day_name": "Mon",
         "session_type": "rest", "duration_min": 0, "tss_estimate": 0.0,
         "description": "", "zwo_file": "", "zwo_name": "",
         "status": "pending"},
        # already_easy: z2
        {"day": (monday + timedelta(days=1)).isoformat(), "day_name": "Tue",
         "session_type": "z2", "duration_min": 60, "tss_estimate": 45.0,
         "description": "", "zwo_file": "z2.zwo", "zwo_name": "Z2",
         "status": "pending"},
        # completed: hard but already done
        {"day": (monday + timedelta(days=2)).isoformat(), "day_name": "Wed",
         "session_type": "vo2max", "duration_min": 60, "tss_estimate": 95.0,
         "description": "", "zwo_file": "v.zwo", "zwo_name": "V",
         "status": "completed"},
    ]
    plan = {
        "goal": {"type": "general"},
        "weeks": [{
            "week_num": 1,
            "start": monday.isoformat(),
            "end": sunday.isoformat(),
            "sessions": sessions,
        }],
    }
    # Use monday as the anchor so all three candidates are inside the window.
    result = tp.apply_week_tier_down(plan, monday.isoformat(), dry_run=True)
    diag = result.get("diagnostic")
    assert isinstance(diag, dict)
    by_day = {r["day"]: r["reason"] for r in diag["reasons"]}
    assert by_day[monday.isoformat()] == "rest_day"
    assert by_day[(monday + timedelta(days=1)).isoformat()] == "already_easy"
    assert by_day[(monday + timedelta(days=2)).isoformat()] == "completed"
    assert diag["candidates_considered"] == 3


def test_diagnostic_invalid_day_iso():
    """Invalid day_iso path returns an empty diagnostic too."""
    result = tp.apply_week_tier_down({"weeks": []}, "not-a-date", dry_run=True)
    diag = result.get("diagnostic")
    assert isinstance(diag, dict)
    assert diag["candidates_considered"] == 0
    assert diag["reasons"] == []


# ── /api/plan/auto-adjust endpoint passthrough ──────────────────────────────


def test_endpoint_emits_diagnostic_when_actions_empty(tmp_path, monkeypatch):
    """Endpoint includes diagnostic in the response when actions=[]."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    plan = _all_easy_week_plan(date.today())
    (plan_dir / "current_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    _patch_severity(monkeypatch, "tier_down")
    monkeypatch.setattr(tp, "load_workout_library", lambda: [])

    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust",
                    json={"scope": "week", "dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["actions"] == []
    assert "diagnostic" in body, "diagnostic should be emitted when actions=[]"
    diag = body["diagnostic"]
    assert isinstance(diag.get("reasons"), list)
    assert diag["candidates_considered"] >= 1


def test_endpoint_omits_diagnostic_when_actions_nonempty(tmp_path, monkeypatch):
    """When the helper acts on at least one session, diagnostic is omitted
    from the response payload (actions already explain what changed)."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    plan = _one_hard_week_plan(date.today())
    (plan_dir / "current_plan.json").write_text(json.dumps(plan), encoding="utf-8")
    _patch_severity(monkeypatch, "tier_down")
    monkeypatch.setattr(tp, "load_workout_library", lambda: [])

    def _fake_match(session, library, **kwargs):
        session.zwo_file = "fake.zwo"
        session.zwo_name = "Fake"
        return session

    monkeypatch.setattr(tp, "match_zwo", _fake_match)

    client = TestClient(app_module.app)
    r = client.post("/api/plan/auto-adjust",
                    json={"scope": "week", "dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    if body["actions"]:
        assert "diagnostic" not in body, \
            "diagnostic should be suppressed when actions non-empty"


# ── frontend wiring smoke test ──────────────────────────────────────────────


def test_dashboard_html_renders_diagnostic_reasons():
    """dashboard.html contains the diagnostic-render block keyed off
    data.diagnostic.reasons so the modal explains why nothing was adjusted."""
    html_path = (
        Path(__file__).resolve().parent.parent / "templates" / "dashboard.html"
    )
    src = html_path.read_text(encoding="utf-8")
    # Reads data.diagnostic.
    assert "data.diagnostic" in src
    # Iterates reasons.
    assert "diag.reasons" in src
    # Renders the per-day reason list with a stable id we can grep for.
    assert "auto-adjust-diagnostic-list" in src
    # Maps internal reason codes to user-friendly labels.
    assert "already_easy" in src
    assert "rest_day" in src
