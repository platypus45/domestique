"""v1.7.1 — per-day availability hours act as a ceiling (not weekly-average).

User reported: "I set today's availability from 1.5h to 60 min → Update →
workout from 110 minutes to 90 minutes. But I only have 60 min!"

Pre-v1.7.1 bug: ``reforecast()``'s availability path computed
``scale = sum(available_mins) / sum(current_mins)`` PER WEEK across all
touched days, then applied the scale uniformly. The frontend POSTs every
day in the visible calendar (180 days) with weekly-grid-default hours, so
a single user-edited day's effect was diluted by the surrounding
defaults — the 110 → 60 shrink came out as 110 → 90.

v1.7.1 treats per-day hours as a CEILING: if the user's hours imply a
shorter session than the planner chose, shrink to fit. If the user's
hours allow more time than the planner needs, keep the planner's choice
(don't extend training without the rider explicitly asking).
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


def _seed_two_day_plan(plan_dir: Path) -> Path:
    """Build a plan with a Wednesday 110min z2 and a Thursday 120min z2 so
    we can verify the cap fires on the targeted day without contaminating
    the untouched day."""
    json_path = plan_dir / "current_plan.json"
    # Use a week that hasn't ended yet so reforecast's pw.end < today
    # guard doesn't skip it.
    today = date.today()
    monday = today + timedelta(days=(7 - today.weekday()))  # next Monday
    wed = (monday + timedelta(days=2)).isoformat()
    thu = (monday + timedelta(days=3)).isoformat()
    plan = {
        "goal": {"type": "general", "hours_per_week": 8.0, "rest_days": [0]},
        "phases": [],
        "weeks": [
            {
                "week_num": 1,
                "start": monday.isoformat(),
                "end": (monday + timedelta(days=6)).isoformat(),
                "sessions": [
                    {
                        "day": wed,
                        "day_name": "Wed",
                        "session_type": "z2",
                        "duration_min": 110,
                        "tss_estimate": 82.0,
                        "description": "Z2 endurance",
                        "zwo_file": "z2_110.zwo",
                        "zwo_name": "Z2 (110min)",
                        "status": "pending",
                    },
                    {
                        "day": thu,
                        "day_name": "Thu",
                        "session_type": "z2",
                        "duration_min": 120,
                        "tss_estimate": 90.0,
                        "description": "Z2 endurance",
                        "zwo_file": "z2_120.zwo",
                        "zwo_name": "Z2 (120min)",
                        "status": "pending",
                    },
                ],
            }
        ],
        "availability": {},
    }
    json_path.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    return json_path


def _post_availability(client, wed: str, thu: str, wed_hours: float, thu_hours: float):
    return client.post(
        "/api/plan/save-availability",
        json={"availability": {
            wed: {"hours": wed_hours, "type": "available"},
            thu: {"hours": thu_hours, "type": "available"},
        }},
    )


def _load_session(json_path: Path, day_iso: str) -> dict:
    plan = json.loads(json_path.read_text(encoding="utf-8"))
    for w in plan.get("weeks", []):
        for s in w.get("sessions", []):
            if s.get("day") == day_iso:
                return s
    return {}


def test_per_day_cap_shrinks_to_hours(tmp_path, monkeypatch):
    """User sets Wednesday to 1h. Wednesday's 110min session shrinks to 60.
    Pre-v1.7.1 it shrank only to ~90 because the weekly average diluted the cap."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path = _seed_two_day_plan(plan_dir)
    plan_before = json.loads(json_path.read_text(encoding="utf-8"))
    wed = plan_before["weeks"][0]["sessions"][0]["day"]
    thu = plan_before["weeks"][0]["sessions"][1]["day"]

    client = TestClient(app_module.app)
    r = _post_availability(client, wed, thu, wed_hours=1.0, thu_hours=2.0)
    assert r.status_code == 200, r.text

    wed_session = _load_session(json_path, wed)
    assert wed_session["duration_min"] == 60, \
        f"Wed expected 60min, got {wed_session['duration_min']}"
    # TSS recalculated from session_type's TSS_PER_HOUR (z2 = 45) × 1h = 45.
    assert wed_session["tss_estimate"] == 45


def test_per_day_cap_does_not_extend(tmp_path, monkeypatch):
    """User has 3h on Wednesday (more than planner needs). The 110min
    session is kept — availability is a ceiling, not a target."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path = _seed_two_day_plan(plan_dir)
    plan_before = json.loads(json_path.read_text(encoding="utf-8"))
    wed = plan_before["weeks"][0]["sessions"][0]["day"]
    thu = plan_before["weeks"][0]["sessions"][1]["day"]

    client = TestClient(app_module.app)
    r = _post_availability(client, wed, thu, wed_hours=3.0, thu_hours=3.0)
    assert r.status_code == 200, r.text

    wed_session = _load_session(json_path, wed)
    assert wed_session["duration_min"] == 110, \
        f"Wed expected unchanged 110min, got {wed_session['duration_min']}"


def test_per_day_cap_isolates_untouched_day(tmp_path, monkeypatch):
    """Shrinking Wednesday MUST NOT change Thursday's duration. Pre-v1.7.1
    the weekly-average scale propagated to every touched day."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path = _seed_two_day_plan(plan_dir)
    plan_before = json.loads(json_path.read_text(encoding="utf-8"))
    wed = plan_before["weeks"][0]["sessions"][0]["day"]
    thu = plan_before["weeks"][0]["sessions"][1]["day"]

    client = TestClient(app_module.app)
    # Wednesday capped to 1h; Thursday's 2h matches its 120min plan exactly.
    r = _post_availability(client, wed, thu, wed_hours=1.0, thu_hours=2.0)
    assert r.status_code == 200, r.text

    thu_session = _load_session(json_path, thu)
    assert thu_session["duration_min"] == 120, \
        f"Thu expected unchanged 120min, got {thu_session['duration_min']}"


def test_per_day_cap_zero_hours_marks_rest(tmp_path, monkeypatch):
    """Regression guard: the 0-hours-becomes-rest branch (v1.3.5) still works.
    User clicks "Mark Holiday" → hours=0, type=holiday → session goes to rest."""
    plan_dir = _isolate_plan_dir(tmp_path, monkeypatch)
    json_path = _seed_two_day_plan(plan_dir)
    plan_before = json.loads(json_path.read_text(encoding="utf-8"))
    wed = plan_before["weeks"][0]["sessions"][0]["day"]
    thu = plan_before["weeks"][0]["sessions"][1]["day"]

    client = TestClient(app_module.app)
    r = client.post(
        "/api/plan/save-availability",
        json={"availability": {
            wed: {"hours": 0, "type": "holiday"},
            thu: {"hours": 2.0, "type": "available"},
        }},
    )
    assert r.status_code == 200, r.text

    wed_session = _load_session(json_path, wed)
    assert wed_session["session_type"] == "rest"
    assert wed_session["duration_min"] == 0
    assert wed_session["tss_estimate"] == 0


def test_reforecast_dict_per_day_cap_unit(tmp_path):
    """Direct unit test on tp.reforecast_dict — bypass HTTP so we can pin
    the exact scaling logic. Two z2 sessions in the same week; user caps
    the first at 1h; only that session shrinks."""
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
                 "description": "", "zwo_file": "", "zwo_name": "",
                 "status": "pending"},
                {"day": thu, "day_name": "Thu", "session_type": "z2",
                 "duration_min": 120, "tss_estimate": 90.0,
                 "description": "", "zwo_file": "", "zwo_name": "",
                 "status": "pending"},
            ],
        }],
    }
    plan, _modified, _ri = tp.reforecast_dict(
        plan,
        tsb_series={},
        availability_overrides={wed: 1.0, thu: 2.0},
        propagation_days={wed, thu},
    )
    wed_s = next(s for s in plan["weeks"][0]["sessions"] if s["day"] == wed)
    thu_s = next(s for s in plan["weeks"][0]["sessions"] if s["day"] == thu)
    assert wed_s["duration_min"] == 60
    assert thu_s["duration_min"] == 120  # untouched (planner kept its choice)
