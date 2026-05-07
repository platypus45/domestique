"""v1.3.7 regression — calendar at the bottom of the dashboard renders again.

v1.3.6 (`c96a1847`) shipped two fixes — availability rest→z2 restore and a
3D-fitness backfill UX. Combined with the pre-existing
`/api/plan/reforecast` propagation block (lines ~7146-7162 in app.py), users
with even modest availability overrides ended up with every future planned
session carrying ``zwo_file=""`` after Plan-tab open. That made
`_classify_card_state` emit ``"missing_workout"`` for every cell, which the
calendar UI rendered as a yellow ⚠ icon with no workout title — the user
report was "calendar at the bottom shows nothing".

The regression isn't that the JSON shape broke (it didn't — `/api/calendar`
still returned 200 with valid `weeks`/`days`/`card_state`). It's that the
*content* of those cells lost the picked workout because the propagation
block hard-coded ``s_json["zwo_file"] = ""`` for every touched day,
clobbering whatever ``tp.reforecast`` had left in place.

This test reproduces the regression and asserts the v1.3.7 fix:
``api_plan_reforecast`` propagates ``src.zwo_file`` rather than always
clearing it, so sessions whose ZWO survived reforecast keep their workout.
"""
from __future__ import annotations

import json
import sys
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
import training_planner as tp  # noqa: E402


def _mk_plan_dict(monday: date, weeks_count: int = 2) -> dict:
    """Build a minimal plan dict with one ZWO-backed z2 session per
    available day. Past + current + future weeks all carry zwo_file so we
    can prove the propagation preserves them.
    """
    weeks: list[dict] = []
    for w_idx in range(weeks_count):
        ws = monday + timedelta(weeks=w_idx)
        we = ws + timedelta(days=6)
        sessions = []
        for d_off in range(7):
            day = ws + timedelta(days=d_off)
            sessions.append({
                "day": day.isoformat(),
                "day_name": day.strftime("%a"),
                "session_type": "z2" if d_off in (1, 3, 5) else "rest",
                "duration_min": 60 if d_off in (1, 3, 5) else 0,
                "tss_estimate": 45 if d_off in (1, 3, 5) else 0,
                "description": "Z2 endurance" if d_off in (1, 3, 5) else "Rest",
                "zwo_file": "real_z2.zwo" if d_off in (1, 3, 5) else "",
                "zwo_name": "Z2 60min" if d_off in (1, 3, 5) else "",
            })
        weeks.append({
            "week_num": w_idx + 1,
            "start": ws.isoformat(),
            "end": we.isoformat(),
            "phase": "base",
            "tss_target": 200,
            "is_stepback": False,
            "hit_per_week": 1,
            "sessions": sessions,
        })
    return {
        "goal": {
            "type": "general",
            "hours_per_week": 6.0,
            "rest_days": [],
            "available_days": list(range(7)),
        },
        "phases": [],
        "weeks": weeks,
        "generated": "2026-05-01T00:00:00",
        "availability": {},
    }


def _next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


@pytest.fixture
def isolated_plan(tmp_path):
    """Stage a temp plan dir with availability overrides on the future
    z2 days, plus the patches needed to keep the test hermetic against
    the rest of the suite (DB activities query, ride storage scans)."""
    from fastapi.testclient import TestClient

    monday = _next_monday(date.today())
    plan = _mk_plan_dict(monday, weeks_count=2)
    # Plant availability that touches future days but doesn't change
    # anything (hours match what the session already plans).
    av = {}
    for d_off in (1, 3, 5):  # only the z2 days
        for w_idx in range(2):
            day = monday + timedelta(weeks=w_idx, days=d_off)
            av[day.isoformat()] = {"hours": 1.0, "type": "available"}
    plan["availability"] = av
    (tmp_path / "current_plan.json").write_text(json.dumps(plan, default=str))

    orig_plan_dir = tp.PLAN_DIR
    tp.PLAN_DIR = tmp_path

    # Mock db.query_activities so /api/plan/reforecast doesn't 500 when
    # the test DB hasn't been provisioned by an earlier suite.
    patches = [
        patch("app.db.query_activities", return_value=[]),
        patch("ride_storage.list_rides", return_value=[]),
        patch("app._load_all_rides_safe", return_value=[]),
    ]
    for p in patches:
        p.start()

    client = TestClient(app_module.app)
    try:
        yield tmp_path, client
    finally:
        for p in reversed(patches):
            p.stop()
        tp.PLAN_DIR = orig_plan_dir


def test_reforecast_preserves_zwo_file_when_no_swap(isolated_plan):
    """v1.3.7 regression — reforecast must NOT clobber zwo_file when the
    per-day branches in tp.reforecast didn't actually need to swap the
    workout. Pre-fix, the propagation block hard-coded `zwo_file=""` for
    every touched day, so any plan with availability overrides ended up
    with empty zwo_file on every future session → calendar painted yellow
    ⚠ everywhere.
    """
    tmp_path, client = isolated_plan

    r = client.post("/api/plan/reforecast")
    assert r.status_code == 200, r.text

    after = json.loads((tmp_path / "current_plan.json").read_text())
    z2_days_with_zwo = 0
    z2_days_total = 0
    for w in after["weeks"]:
        for s in w["sessions"]:
            if s.get("session_type") == "z2":
                z2_days_total += 1
                if s.get("zwo_file"):
                    z2_days_with_zwo += 1
    assert z2_days_total >= 4, f"expected ≥4 z2 days, got {z2_days_total}"
    # v1.3.7 fix: propagation now preserves the picked zwo so the
    # calendar can render the actual workout title rather than yellow ⚠.
    assert z2_days_with_zwo == z2_days_total, (
        f"v1.3.7 regression: {z2_days_total - z2_days_with_zwo} of "
        f"{z2_days_total} z2 sessions lost their zwo_file after reforecast"
    )


def test_calendar_endpoint_renders_picked_workouts_post_reforecast(isolated_plan):
    """End-to-end — after reforecast runs, /api/calendar must surface
    `card_state="planned"` (with a real zwo_file) for sessions whose
    workout survived, not `card_state="missing_workout"` everywhere.
    """
    tmp_path, client = isolated_plan

    # First fire reforecast (mirrors what `checkPlanGaps()` does on Plan
    # tab open).
    r1 = client.post("/api/plan/reforecast")
    assert r1.status_code == 200, r1.text

    # Now fetch calendar.
    r2 = client.get("/api/calendar")
    assert r2.status_code == 200
    cal = r2.json()
    assert isinstance(cal.get("weeks"), list)
    assert len(cal["weeks"]) > 0

    # Count card_state distribution across future days.
    today_iso = date.today().isoformat()
    planned_count = 0
    missing_count = 0
    for w in cal["weeks"]:
        for d in (w.get("days") or []):
            if not d or d.get("date", "") < today_iso:
                continue
            cs = d.get("card_state")
            if cs == "planned":
                planned_count += 1
            elif cs == "missing_workout":
                missing_count += 1
    # v1.3.7 contract: planned cells should outnumber missing_workout
    # cells. Pre-fix, every touched day became missing_workout → user
    # saw yellow ⚠ everywhere.
    assert planned_count >= 2, (
        f"v1.3.7 regression: only {planned_count} planned cells found "
        f"(vs {missing_count} missing_workout). Calendar is painting "
        f"yellow ⚠ everywhere instead of the picked workouts."
    )
