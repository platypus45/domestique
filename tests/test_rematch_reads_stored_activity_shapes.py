"""A ridden session is matched from the shapes the activities actually arrive in.

Production, 2026-09-15: Monday's threshold session (79 min, 105 TSS) was
ridden -- 91 min moving, 120 TSS, IF 0.89, uploaded from the Bryton. The
morning ride-sync marked it missed and the auto-reschedule moved it onto
Tuesday's endurance day. The matcher's collector read `duration_min` /
`moving_time` and `intensity_factor`; the SQLite activities row carries
`duration_sec` and keeps `icu_intensity` (a percentage) inside `raw_json`, and
the ride store's record carries `started_at` and `duration_s`. So the ride
scored 0 min and no intensity: 1 axis of 3, "missed".

The rows below are those shapes, with that ride's numbers."""
from __future__ import annotations

import json
from datetime import date, timedelta
from unittest.mock import patch

import app as app_module
import clock
import training_planner as tp

TODAY = date(2026, 9, 15)          # a Tuesday
MONDAY = TODAY - timedelta(days=1)


def _plan():
    start = MONDAY
    sessions = [
        {"day": MONDAY.isoformat(), "day_name": "Mon", "session_type": "sweetspot",
         "duration_min": 79, "tss_estimate": 105, "status": "pending",
         "zwo_file": "threshold_3x15min-5min_100pct_76min.zwo"},
        {"day": TODAY.isoformat(), "day_name": "Tue", "session_type": "z2",
         "duration_min": 73, "tss_estimate": 55, "status": "pending",
         "zwo_file": "endurance_steady_74pct_70min.zwo"},
    ] + [{"day": (start + timedelta(days=i)).isoformat(), "day_name": "", "session_type": "rest",
          "duration_min": 0, "tss_estimate": 0, "status": "pending", "zwo_file": ""}
         for i in range(2, 7)]
    return {"goal": {"type": "continuous", "available_days": [0, 1, 2, 3, 4], "rest_days": [5, 6]},
            "availability": {},
            "weeks": [{"week_num": 1, "start": start.isoformat(),
                       "end": (start + timedelta(days=6)).isoformat(), "phase": "base",
                       "tss_target": 300, "is_stepback": False, "sessions": sessions}]}


SQLITE_ROW = {   # db.query_activities: the activities table as stored
    "id": "i186623454", "date": MONDAY.isoformat(), "name": "Afternoon Ride", "sport": "Ride",
    "duration_sec": 5440, "tss": 120.0, "avg_power": None, "avg_hr": 156.0,
    "raw_json": json.dumps({"id": "i186623454", "start_date_local": f"{MONDAY}T14:06:14",
                            "type": "Ride", "moving_time": 5440, "elapsed_time": 6917,
                            "icu_training_load": 120, "icu_intensity": 89.068825}),
}
STRAVA_STUB = {  # the husk the same ride left behind: no sport, no load
    "id": "20171057567", "date": MONDAY.isoformat(), "sport": "", "tss": None,
    "raw_json": json.dumps({"id": "20171057567", "source": "STRAVA"}),
}
RIDE_STORE = {   # ride_storage.list_rides: the ICU cache record
    "ride_id": "icu_i186623454", "started_at": f"{MONDAY}T14:06:14", "sport": "Ride",
    "tss": 120.0, "duration_s": 6917, "name": "Afternoon Ride", "source": "icu",
}


def _reconcile(rows, rides):
    plan = _plan()
    clock.freeze(TODAY)
    try:
        with patch("db.query_activities", return_value=rows), \
             patch("ride_storage.list_rides", return_value=rides):
            app_module._reconcile_current_week(plan, TODAY)
            moves = app_module._auto_apply_missed_moves(plan, TODAY)
    finally:
        clock.unfreeze()
    by_day = {s["day"]: s for s in plan["weeks"][0]["sessions"]}
    return by_day, moves


def test_a_ridden_session_is_done_and_stays_on_its_day():
    by_day, moves = _reconcile([STRAVA_STUB, SQLITE_ROW], [RIDE_STORE])
    assert by_day[MONDAY.isoformat()]["status"] == "done"
    assert by_day[MONDAY.isoformat()]["session_type"] == "sweetspot"
    assert moves == []
    assert by_day[TODAY.isoformat()]["session_type"] == "z2"


def test_the_ride_store_record_alone_is_seen_on_its_day():
    """Its duration is elapsed time (115 min, over tolerance), so it may not be
    a full match -- but it is a ride on Monday, not nothing."""
    plan = _plan()
    week, _ = app_module._load_current_week_dto(plan, TODAY)
    clock.freeze(TODAY)
    try:
        with patch("db.query_activities", return_value=[]), \
             patch("ride_storage.list_rides", return_value=[RIDE_STORE]):
            acts = app_module._collect_week_activities(week, TODAY, include_today=True)
    finally:
        clock.unfreeze()
    assert [(a["date"], round(a["duration_min"])) for a in acts] == [(MONDAY.isoformat(), 115)]


def test_icu_intensity_is_a_percentage():
    assert tp._activity_if_band({"icu_intensity": 89.07}) == "high_aerobic"
    assert tp._activity_if_band({"intensity_factor": 0.89}) == "high_aerobic"


def _row(tss, minutes, intensity_pct):
    return {**SQLITE_ROW, "tss": float(tss), "duration_sec": minutes * 60,
            "raw_json": json.dumps({"moving_time": minutes * 60, "icu_intensity": intensity_pct})}


def test_a_harder_longer_ride_is_done_and_the_session_is_not_rescheduled():
    """The owner's point: 130 min at IF 0.95 for a 79-min sweetspot day fits no
    tolerance, and it is more than the session asked for. Moving the session
    onto the next day would make two hard days of one."""
    by_day, moves = _reconcile([_row(170, 130, 95)], [])
    assert by_day[MONDAY.isoformat()]["status"] == "done"
    assert moves == []


def test_a_ride_that_reached_half_the_load_is_partly_done_and_not_moved():
    by_day, moves = _reconcile([_row(60, 40, 90)], [])
    assert by_day[MONDAY.isoformat()]["status"] == "done_partial"
    assert moves == []


def test_a_short_easy_spin_still_leaves_the_session_missed_and_rescheduled():
    """What rescheduling is for: the session's stimulus was never delivered."""
    by_day, moves = _reconcile([_row(20, 30, 55)], [])
    assert [m["from"] for m in moves] == [MONDAY.isoformat()]
