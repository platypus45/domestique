"""A ridden session is matched from the shapes the activities actually arrive in.

Production, 2026-09-15: Monday's threshold session (79 min, 105 TSS) was
ridden -- 91 min moving, 120 TSS, IF 0.89, uploaded from the Bryton. The
morning ride-sync marked it missed and the auto-reschedule moved it onto
Tuesday's endurance day. The matcher's collector read `duration_min` /
`moving_time` and `intensity_factor`; the SQLite activities row carries
`duration_sec` and keeps `icu_intensity` (a percentage) inside `raw_json`. So
the ride scored 0 min and no intensity: 1 axis of 3, "missed".

And any same-day ride outside the tolerances made a session missed, so a
longer or harder ride got its hard session moved onto the next day. A finished
day's rides now count together: the planned load at the planned band or
harder is done, half the load is done_partial (not moved), less is missed.

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


def _base_plan():
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


def _plan(race=False):
    plan = _base_plan()
    if race:
        mon = plan["weeks"][0]["sessions"][0]
        mon.update({"session_type": "recovery", "is_race": True, "duration_min": 300, "tss_estimate": 250})
    return plan


def _reconcile(rows, today=TODAY, plan=None, day_type=None):
    plan = plan or _plan()
    if day_type:
        plan["weeks"][0]["sessions"][0].update(day_type)
    clock.freeze(today)
    try:
        with patch("db.query_activities", return_value=rows), \
             patch("ride_storage.list_rides", return_value=[]):
            app_module._reconcile_current_week(plan, today)
            moves = app_module._auto_apply_missed_moves(plan, today)
    finally:
        clock.unfreeze()
    by_day = {s["day"]: s for s in plan["weeks"][0]["sessions"]}
    return by_day, moves


def _row(tss, minutes, intensity_pct, day=MONDAY, rid="r"):
    return {**SQLITE_ROW, "id": rid, "date": day.isoformat(), "tss": float(tss), "duration_sec": minutes * 60,
            "raw_json": json.dumps({"moving_time": minutes * 60, "icu_intensity": intensity_pct})}


MON = MONDAY.isoformat()


def test_a_ridden_session_is_done_and_stays_on_its_day():
    by_day, moves = _reconcile([STRAVA_STUB, SQLITE_ROW])
    assert by_day[MON]["status"] == "done"
    assert by_day[MON]["session_type"] == "sweetspot"
    assert moves == []
    assert by_day[TODAY.isoformat()]["session_type"] == "z2"


def test_icu_intensity_is_a_percentage():
    assert tp._activity_if_band({"icu_intensity": 89.07}) == "high_aerobic"
    assert tp._activity_if_band({"intensity_factor": 0.89}) == "high_aerobic"


def test_a_harder_longer_ride_is_done_and_the_session_is_not_rescheduled():
    """The owner's point: 130 min at IF 0.95 for a 79-min sweetspot day fits no
    tolerance, and it is more than the session asked for."""
    by_day, moves = _reconcile([_row(170, 130, 95)])
    assert by_day[MON]["status"] == "done"
    assert moves == []


def test_the_planned_load_below_the_planned_band_is_only_partly_done():
    """105 TSS of endurance is not the sweetspot session."""
    by_day, moves = _reconcile([_row(110, 150, 60)])
    assert by_day[MON]["status"] == "done_partial"
    assert moves == []


def test_half_the_load_is_partly_done_and_not_moved():
    by_day, moves = _reconcile([_row(60, 50, 75)])
    assert by_day[MON]["status"] == "done_partial"
    assert moves == []


def test_a_short_hard_ride_under_half_the_load_is_missed_and_rescheduled():
    """The band alone is not the session: 25 TSS at IF 0.85 on a 105 TSS day."""
    _, moves = _reconcile([_row(25, 20, 85)])
    assert [m["from"] for m in moves] == [MON]


def test_a_spin_on_an_easy_day_is_not_the_easy_day():
    long_day = {"session_type": "long_z2", "duration_min": 180, "tss_estimate": 150}
    _, moves = _reconcile([_row(20, 25, 60)], day_type=long_day)
    assert [m["from"] for m in moves] == [MON]


def test_a_days_rides_count_together_whatever_their_order():
    """Neither ride matches on its own: a commute, and a ride split in two by a
    café stop. Judged one at a time the day was a commute or half a session."""
    commute, first, second = _row(15, 20, 60, rid="c"), _row(55, 40, 90, rid="a"), _row(65, 50, 90, rid="b")
    for rows in ([commute, first, second], [second, first, commute]):
        by_day, moves = _reconcile(rows)
        assert by_day[MON]["status"] == "done", rows[0]["id"]
        assert moves == []


def test_today_is_not_judged_before_it_is_over():
    """A morning commute must not settle the evening's session."""
    by_day, _ = _reconcile([_row(55, 45, 80, day=TODAY)], day_type=None)
    assert by_day[TODAY.isoformat()]["status"] == "pending"


def test_an_unridden_race_day_is_still_a_missed_race():
    """Even a substantial ride that is not the race keeps the race terminal."""
    by_day, _ = _reconcile([_row(150, 120, 90)], plan=_plan(race=True))
    assert by_day[MON]["status"] == "missed_race"


def test_a_malformed_raw_json_row_does_not_hide_the_others():
    bad = {**SQLITE_ROW, "id": "bad", "raw_json": "[1]"}
    by_day, _ = _reconcile([bad, SQLITE_ROW])
    assert by_day[MON]["status"] == "done"


def test_a_partly_done_day_is_completed_by_a_ride_that_arrives_later():
    plan = _plan()
    by_day, _ = _reconcile([_row(60, 50, 75, rid="first")], plan=plan)
    assert by_day[MON]["status"] == "done_partial"
    by_day, moves = _reconcile([_row(60, 50, 75, rid="first"), _row(70, 45, 90, rid="late")], plan=plan)
    assert by_day[MON]["status"] == "done"
    assert moves == []


def test_a_partly_done_day_never_moves_down():
    plan = _plan()
    _reconcile([_row(60, 50, 75)], plan=plan)
    by_day, moves = _reconcile([], plan=plan)          # its ride no longer listed
    assert by_day[MON]["status"] == "done_partial"
    assert moves == []
    by_day, moves = _reconcile([_row(20, 20, 60, rid="other")], plan=plan)   # only a spin listed
    assert by_day[MON]["status"] == "done_partial"
    assert moves == []


def test_the_availability_pass_leaves_days_that_are_over_alone():
    """The stub an auto-move left on Monday came back the next morning as
    "z2 (73min) -- restored from rest" (production, 2026-09-15)."""
    plan = _plan()
    mon, tue = plan["weeks"][0]["sessions"][:2]
    mon.update({"session_type": "rest", "duration_min": 0, "tss_estimate": 0,
                "status": "moved_from:2026-09-15", "description": "Moved to 2026-09-15"})
    tue.update({"status": "done"})
    plan["availability"] = {MON: {"hours": 3.0, "type": "available"},
                            TODAY.isoformat(): {"hours": 3.0, "type": "available"}}
    weeks = [tp.week_from_dict(w) for w in plan["weeks"]]
    clock.freeze(TODAY)
    try:
        tp.reforecast(tp.Goal(goal_type="continuous"), weeks,
                      tsb_series={MONDAY + timedelta(days=i): 0.0 for i in range(7)},
                      availability_overrides={MON: 3.0, TODAY.isoformat(): 3.0})
    finally:
        clock.unfreeze()
    days = {x.day.isoformat(): x for x in weeks[0].sessions}
    assert days[MON].session_type == "rest", days[MON].description
    assert days[TODAY.isoformat()].duration_min == 73, "a done day keeps its session"
