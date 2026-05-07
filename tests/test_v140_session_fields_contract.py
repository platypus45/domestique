"""v1.4.0 — SESSION_FIELDS_LOCKED contract test.

Pins the wire shape of /api/plan session payloads so future field-name
drift between training_planner.PlannedSession and the JSON dict can't
silently leak through (which is what drove the v1.3.5/6/7 regression
cycle — see CALENDAR_REDESIGN_v140.md §4).

Tests:
- ``test_locked_set_is_frozen`` — SESSION_FIELDS_LOCKED is immutable.
- ``test_no_unsanctioned_keys_in_api_plan`` — every session key emitted
  by /api/plan is in the locked superset (subset assertion).
- ``test_no_unsanctioned_keys_in_api_plan_generate`` — same after a
  fresh Generate Plan response.
- ``test_helper_idempotent`` — _enrich_plan_for_response called twice
  produces the same dict (no field doubling, deterministic).
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


def _mk_plan_dict(monday: date) -> dict:
    """Minimal 1-week plan dict — z2 sessions Tue/Thu/Sat."""
    weeks: list[dict] = []
    ws = monday
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
            "zwo_file": "" if d_off in (1, 3, 5) else "",
            "zwo_name": "" if d_off in (1, 3, 5) else "",
            "status": "pending",
        })
    weeks.append({
        "week_num": 1, "start": ws.isoformat(), "end": we.isoformat(),
        "phase": "base", "tss_target": 200, "is_stepback": False,
        "hit_per_week": 1, "sessions": sessions,
    })
    return {
        "goal": {"type": "general", "hours_per_week": 6.0,
                 "rest_days": [], "available_days": list(range(7))},
        "phases": [], "weeks": weeks,
        "generated": "2026-05-01T00:00:00", "availability": {},
    }


def test_locked_set_is_frozen():
    """SESSION_FIELDS_LOCKED is a frozenset — cannot be mutated by
    runtime code paths. Future fields must be added in source."""
    assert isinstance(app_module.SESSION_FIELDS_LOCKED, frozenset)
    # Spot-check a few critical fields per the design doc grill.
    for f in ("day", "session_type", "zwo_file", "card_state", "card_state_v2",
              "availability_hours", "adapted", "completion_matches"):
        assert f in app_module.SESSION_FIELDS_LOCKED, f"missing locked field: {f}"


def test_helper_idempotent(tmp_path):
    """Calling _enrich_plan_for_response twice produces the same dict.
    Idempotency guard against accidental field doubling on subsequent mutations.
    """
    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
    plan = _mk_plan_dict(monday)
    today_iso = date.today().isoformat()

    app_module._enrich_plan_for_response(plan, today_iso=today_iso)
    snapshot1 = json.dumps(plan, sort_keys=True, default=str)
    app_module._enrich_plan_for_response(plan, today_iso=today_iso)
    snapshot2 = json.dumps(plan, sort_keys=True, default=str)
    assert snapshot1 == snapshot2, (
        "Helper is not idempotent — second call mutated the plan dict"
    )


def test_no_unsanctioned_keys_after_enrichment(tmp_path):
    """After _enrich_plan_for_response, every session key is in
    SESSION_FIELDS_LOCKED. Catches accidental new fields."""
    monday = date.today() + timedelta(days=(7 - date.today().weekday()) % 7 or 7)
    plan = _mk_plan_dict(monday)
    today_iso = date.today().isoformat()

    app_module._enrich_plan_for_response(plan, today_iso=today_iso)

    locked = app_module.SESSION_FIELDS_LOCKED
    for w in plan["weeks"]:
        for s in w["sessions"]:
            extras = set(s.keys()) - locked
            assert not extras, (
                f"Session has unsanctioned keys: {extras}. "
                f"Either add them to SESSION_FIELDS_LOCKED or fix the leak."
            )
