"""3.14.0 (QA-INT §4): a calendar run's report clears the put-backs of the rider
the run was for, in that rider's own plan file, whoever is active by then.

A profile switch does not take the plan lock, so a check of "is this rider
still active" before writing left a gap in which one rider's report could
clear another rider's put-backs. The clean-up now goes by the run's profile.
"""
from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import app as app_module
import clock
import db
import training_planner as tp
from test_icu_push import stub  # noqa: F401  (fixture: sandboxed HOME + TestClient)


def _plan_with_put_back(day: str) -> dict:
    asked = (clock.now() - timedelta(minutes=5)).isoformat()
    row = {"day": day, "day_name": "", "session_type": "z2", "duration_min": 120,
           "tss_estimate": 90, "status": "pending", "zwo_file": "", "zwo_name": "",
           "own": {"name": "Club ride", "intensity": "easy", "load_from": "intensity",
                   "source": "manual", "icu_event_id": None, "icu_day": None,
                   "replaced": None, "added_at": asked,
                   "put_back_from": "2099-01-02", "put_back_at": asked}}
    return {"weeks": [{"week_num": 1, "start": day, "end": day, "phase": "base",
                       "tss_target": 300, "is_stepback": False, "sessions": [row]}]}


def _plan_file() -> Path:
    return Path(tp.PLAN_DIR) / "current_plan.json"


def _own_of(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))["weeks"][0]["sessions"][0]["own"]


def test_a_run_report_clears_its_own_riders_put_back_not_the_active_riders(stub):
    day = clock.today().isoformat()
    a = stub.pid
    _plan_file().parent.mkdir(parents=True, exist_ok=True)
    _plan_file().write_text(json.dumps(_plan_with_put_back(day)), encoding="utf-8")
    a_plan = _plan_file()
    b = stub.pm.create_profile("Other rider")
    stub.pm.switch(b)
    _plan_file().parent.mkdir(parents=True, exist_ok=True)
    _plan_file().write_text(json.dumps(_plan_with_put_back(day)), encoding="utf-8")
    b_plan = _plan_file()
    try:
        assert stub.pm.active_id == b and a_plan != b_plan
        # A's run finished after the switch: its report lists no held ride.
        app_module._icu_push_note_result({"profile_id": a, "own_moved": []})
        assert "put_back_from" not in _own_of(a_plan)      # A's done put-back cleared
        assert _own_of(b_plan)["put_back_from"] == "2099-01-02"   # B untouched
    finally:
        stub.pm.switch(a)
        db.shutdown_sync()
