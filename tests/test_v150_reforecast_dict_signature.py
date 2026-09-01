"""v1.5.0 — `tp.reforecast_dict(plan_dict, ...)` is the new single mutation
entrypoint. Closes drift class A (CALENDAR_REDESIGN §4): the propagation
that lived in app.py:_propagate_reforecast_to_dict now lives inside
training_planner.py's `_apply_reforecast_to_dict`, called by
`reforecast_dict`. There is no longer a way for app.py and
training_planner.py to drift on field names — the round-trip is one
function.

Tests:

1. Returns the SAME dict object (mutation in place; no copy).
2. Identity reforecast (no overrides, empty tsb_series) → 0 sessions
   modified, plan content unchanged.
3. Old `tp.reforecast(goal, pw_list, ...)` still callable as deprecated
   alias (preserves existing test imports + external callers).
4. `_propagate_reforecast_to_dict` must be GONE from app.py (drift class
   A closure check).
5. Drift class A: the propagated `zwo_file` must round-trip on a session
   reforecast() didn't actively change (regression for v1.3.5/6/7 cycle).
"""
from __future__ import annotations

import re
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import app as app_module  # noqa: E402
import training_planner as tp  # noqa: E402


def _mk_plan_dict(monday: date, weeks_count: int = 2) -> dict:
    """Minimal plan dict with one z2 session per Mon/Wed/Fri."""
    weeks = []
    for w_idx in range(weeks_count):
        ws = monday + timedelta(weeks=w_idx)
        we = ws + timedelta(days=6)
        sessions = []
        for off in range(7):
            d = ws + timedelta(days=off)
            sessions.append({
                "day": d.isoformat(),
                "day_name": d.strftime("%a"),
                "session_type": "z2" if off in (1, 3, 5) else "rest",
                "duration_min": 60 if off in (1, 3, 5) else 0,
                "tss_estimate": 45 if off in (1, 3, 5) else 0,
                "description": "Z2 endurance" if off in (1, 3, 5) else "Rest",
                "zwo_file": "real_z2.zwo" if off in (1, 3, 5) else "",
                "zwo_name": "Z2 60min" if off in (1, 3, 5) else "",
                "status": "pending",
            })
        weeks.append({
            "week_num": w_idx + 1,
            "phase": "base",
            "start": ws.isoformat(),
            "end": we.isoformat(),
            "tss_target": 200,
            "is_stepback": False,
            "sessions": sessions,
            "hit_per_week": 0,
            "auto_acwr_scaled": False,
        })
    return {
        "goal": {
            "type": "weeks",
            "hours_per_week": 6.0,
            "rest_days": [0],
            "available_days": [1, 2, 3, 4, 5, 6],
        },
        "weeks": weeks,
        "availability": {},
    }


def test_returns_same_dict_object():
    """`reforecast_dict` mutates plan_dict in place; the returned dict
    is the SAME object (test by `is`, not just equality)."""
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mk_plan_dict(monday)
    out, _smod, _info = tp.reforecast_dict(plan)
    assert out is plan, "reforecast_dict must return the SAME dict object"


def test_identity_reforecast_no_changes():
    """No availability overrides, empty tsb_series, no recent_activities →
    no fields should change. sessions_modified == 0.
    """
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mk_plan_dict(monday)
    # Snapshot the z2 session fields.
    before = {
        s["day"]: dict(s) for w in plan["weeks"] for s in w["sessions"]
        if s["session_type"] == "z2"
    }
    _out, sessions_modified, info = tp.reforecast_dict(
        plan, tsb_series={}, availability_overrides={},
    )
    # No mutations expected.
    assert sessions_modified == 0, (
        f"identity reforecast must not modify sessions; got {sessions_modified}"
    )
    for day, snap in before.items():
        live = next(
            s for w in plan["weeks"] for s in w["sessions"]
            if s["day"] == day
        )
        for f in ("session_type", "duration_min", "tss_estimate", "zwo_file",
                  "zwo_name", "description"):
            assert live[f] == snap[f], (
                f"identity call mutated {f} on {day}: {snap[f]} → {live[f]}"
            )
    assert isinstance(info, dict), "reforecast_info must be a dict"


def test_old_reforecast_alias_still_callable():
    """The legacy `tp.reforecast(goal, pw_list, ...)` API is preserved as
    a deprecated alias for tests + external callers (removal in v1.6.0).
    """
    assert callable(getattr(tp, "reforecast", None)), (
        "tp.reforecast must remain a callable for back-compat"
    )
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mk_plan_dict(monday)
    pw_list = tp._plan_dict_to_planned_weeks(plan)
    # Should not raise.
    out_pw, info = tp.reforecast(
        tp.Goal(goal_type="weeks", hours_per_week=6.0),
        pw_list,
        tsb_series={},
        availability_overrides={},
    )
    assert isinstance(info, dict)
    assert isinstance(out_pw, list)


def test_propagate_reforecast_to_dict_removed_from_app_py():
    """Drift class A closure check: the old propagation helper must be
    GONE from app.py. Comments mentioning the name in changelog-style
    notes are allowed; a `def` is not.
    """
    app_path = ROOT / "src" / "app.py"
    src = app_path.read_text(encoding="utf-8")
    # No `def _propagate_reforecast_to_dict(...)` anywhere.
    assert not re.search(r"^def _propagate_reforecast_to_dict\b", src, re.MULTILINE), (
        "app.py must NOT define _propagate_reforecast_to_dict; "
        "v1.5.0 collapsed it into tp.reforecast_dict's "
        "_apply_reforecast_to_dict helper"
    )
    # No call sites either.
    call_sites = re.findall(r"_propagate_reforecast_to_dict\s*\(", src)
    assert not call_sites, (
        f"app.py must NOT call _propagate_reforecast_to_dict; found "
        f"{len(call_sites)} callsite(s)"
    )


def test_drift_class_a_zwo_file_round_trip():
    """Regression for the v1.3.5/6/7 cycle: a session whose ZWO survived
    reforecast must keep its zwo_file in the persisted plan dict.

    This was the failure mode that drove v1.3.7 — the propagation block
    in `api_plan_reforecast` hard-coded zwo_file="" for every touched
    day, clobbering whatever reforecast left in place. With v1.5.0
    there's only one mutation site, and identity reforecasts don't
    touch zwo_file at all.
    """
    monday = date.today() - timedelta(days=date.today().weekday())
    plan = _mk_plan_dict(monday)
    # Apply availability overrides on a Wed (typically a z2 day).
    wed = (monday + timedelta(days=2)).isoformat()
    plan["availability"] = {wed: {"hours": 1.0}}
    _out, _smod, _info = tp.reforecast_dict(
        plan,
        tsb_series={},
        availability_overrides={wed: 1.0},
        # Explicit propagation_days mirrors api_save_availability behaviour.
        propagation_days={wed},
    )
    # All other Wed/Mon/Fri sessions across weeks must keep their ZWO.
    for w in plan["weeks"]:
        for s in w["sessions"]:
            if s["session_type"] == "z2" and s["day"] != wed:
                assert s.get("zwo_file") == "real_z2.zwo", (
                    f"zwo_file clobbered on {s['day']} (expected real_z2.zwo, "
                    f"got {s.get('zwo_file')!r}) — drift class A regression"
                )
