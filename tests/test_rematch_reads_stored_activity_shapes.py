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
            # The order _apply_plan_update runs them in.
            app_module._undo_auto_moves_for_ridden_days(plan, today)
            app_module._reconcile_current_week(plan, today)
            moves = app_module._auto_apply_missed_moves(plan, today)
    finally:
        clock.unfreeze()
    by_day = {s["day"]: s for s in plan["weeks"][0]["sessions"]}
    return by_day, moves


_STARTS = iter(range(10_000))


def _row(tss, minutes, intensity_pct, day=MONDAY, rid="r"):
    """A ride as the activities table stores it, starting an hour after the
    previous one built here (distinct rides do not start together)."""
    hour = next(_STARTS) % 14 + 6
    return {**SQLITE_ROW, "id": rid, "date": day.isoformat(), "tss": float(tss), "duration_sec": minutes * 60,
            "raw_json": json.dumps({"moving_time": minutes * 60, "icu_intensity": intensity_pct,
                                    "start_date_local": f"{day}T{hour:02d}:00:00"})}


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


def test_an_endurance_ride_on_a_sweetspot_day_is_partly_done_and_not_moved():
    """110 TSS of endurance is not the sweetspot session, but a day ridden that
    much is not owed a second one tomorrow."""
    by_day, moves = _reconcile([_row(110, 150, 70)])
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
    by_day, moves = _reconcile([_row(15, 20, 55)], plan=_plan(race=True))
    assert by_day[MON]["status"] == "missed_race"
    assert moves == []


def test_a_race_ridden_shorter_than_its_placeholder_is_not_missed():
    by_day, moves = _reconcile([_row(160, 120, 95)], plan=_plan(race=True))
    assert by_day[MON]["status"] in ("done", "done_partial")
    assert moves == []


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


def test_a_partly_done_day_survives_a_sync_that_lists_no_ride():
    plan = _plan()
    _reconcile([_row(60, 50, 75)], plan=plan)
    by_day, moves = _reconcile([], plan=plan)          # its ride missing from one sync
    assert by_day[MON]["status"] == "done_partial"
    assert moves == []


def test_a_partly_done_day_the_listed_rides_do_not_support_is_missed():
    """4035dd89 judged today too, so a morning commute could leave a session
    done_partial; the next day's reconcile must undo that."""
    plan = _plan()
    plan["weeks"][0]["sessions"][0]["status"] = "done_partial"
    _, moves = _reconcile([_row(12, 20, 60, rid="commute")], plan=plan)
    assert [m["from"] for m in moves] == [MON]


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


VO2_DAY = {"session_type": "vo2max", "duration_min": 60, "tss_estimate": 80}


def test_sprints_beside_a_long_easy_ride_do_not_make_a_vo2_day():
    """The intensity must come from a ride that carried the load."""
    by_day, moves = _reconcile([_row(90, 150, 62, rid="long"), _row(12, 10, 105, rid="sprints")], day_type=VO2_DAY)
    assert by_day[MON]["status"] == "done_partial"
    assert moves == []


def test_commutes_to_half_the_load_are_partly_done_not_the_interval_session():
    by_day, moves = _reconcile([_row(20, 30, 60, rid="am"), _row(24, 35, 62, rid="pm")], day_type=VO2_DAY)
    assert by_day[MON]["status"] == "done_partial"
    assert moves == []


def test_a_commute_under_half_the_load_leaves_the_interval_session_owed():
    _, moves = _reconcile([_row(20, 25, 62)], day_type=VO2_DAY)
    assert [m["from"] for m in moves] == [MON]


def test_the_same_ride_from_two_sources_is_one_ride():
    """Same buckets, same start (an intervals.icu copy and a FIT import)."""
    a = {**_row(60, 50, 75, rid="icu"), "raw_json": json.dumps({"moving_time": 3000, "icu_intensity": 75,
                                                             "start_date_local": f"{MONDAY}T10:00:05"})}
    b = {**a, "id": "fit", "raw_json": json.dumps({"moving_time": 3000, "icu_intensity": 75,
                                                   "start_date_local": f"{MONDAY}T10:00:00"})}
    easy = {"session_type": "z2", "duration_min": 120, "tss_estimate": 100}
    by_day, _ = _reconcile([a, b], day_type=easy)
    assert by_day[MON]["status"] == "done_partial", "counted twice, 60 TSS would be 120: done"


def test_two_identical_commutes_are_two_rides():
    z2 = {"session_type": "z2", "duration_min": 70, "tss_estimate": 45, "zwo_file": ""}
    by_day, moves = _reconcile([_row(23, 36, 60, rid="am"), _row(23, 36, 60, rid="pm")], day_type=z2)
    assert by_day[MON]["status"] == "done"
    assert moves == []


def test_a_weeks_last_day_is_judged_the_morning_after():
    plan = _plan()
    sun = plan["weeks"][0]["sessions"][6]
    sun.update({"session_type": "long_z2", "duration_min": 180, "tss_estimate": 130})
    nxt = MONDAY + timedelta(days=7)
    plan["weeks"].append({"week_num": 2, "start": nxt.isoformat(), "end": (nxt + timedelta(days=6)).isoformat(),
                          "phase": "base", "tss_target": 300, "is_stepback": False,
                          "sessions": [{"day": (nxt + timedelta(days=i)).isoformat(), "day_name": "",
                                        "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
                                        "status": "pending", "zwo_file": ""} for i in range(7)]})
    sunday = MONDAY + timedelta(days=6)
    _reconcile([_row(200, 260, 70, day=sunday)], today=nxt, plan=plan)
    assert plan["weeks"][0]["sessions"][6]["status"] == "done"


def test_zero_hours_rests_a_future_session_still_marked_missed():
    plan = _plan()
    thu = plan["weeks"][0]["sessions"][3]
    thu.update({"session_type": "sweetspot", "duration_min": 79, "tss_estimate": 105, "status": "missed"})
    thu_iso = thu["day"]
    weeks = [tp.week_from_dict(w) for w in plan["weeks"]]
    clock.freeze(TODAY)
    try:
        tp.reforecast(tp.Goal(goal_type="continuous"), weeks,
                      tsb_series={MONDAY + timedelta(days=i): 0.0 for i in range(7)},
                      availability_overrides={thu_iso: 0.0})
    finally:
        clock.unfreeze()
    assert {x.day.isoformat(): x for x in weeks[0].sessions}[thu_iso].session_type == "rest"


def test_an_interval_workout_ridden_long_is_done_by_its_own_intensity():
    """A VO2 workout averages about IF 0.73 over the ride (the library median);
    judged against the vo2max band it read as not ridden and was moved (the
    third review). The served workout's own intensity decides."""
    day = {**VO2_DAY, "zwo_file": ""}      # estimate: 80 TSS in 60 min -> IF 0.89
    by_day, moves = _reconcile([_row(110, 80, 90)], day_type=day)
    assert by_day[MON]["status"] == "done"
    assert moves == []


def test_interval_days_ridden_off_tolerance_are_never_moved():
    for kind, tss, dur, pct in (("vo2max", 110, 81, 73), ("vo2max", 48, 36, 73), ("sprint", 70, 63, 71),
                                ("ftp_test", 70, 84, 56), ("overunder", 145, 126, 80)):
        planned = {"session_type": kind, "duration_min": 60 if kind != "overunder" else 90,
                   "tss_estimate": 80 if kind != "overunder" else 103}
        by_day, moves = _reconcile([_row(tss, dur, pct)], day_type=planned)
        assert moves == [], (kind, tss, by_day[MON]["status"])


def test_a_session_zeroed_while_missed_is_not_moved_as_a_rest():
    plan = _plan()
    thu = plan["weeks"][0]["sessions"][3]
    thu.update({"session_type": "sweetspot", "duration_min": 79, "tss_estimate": 105, "status": "missed"})
    weeks = [tp.week_from_dict(w) for w in plan["weeks"]]
    clock.freeze(TODAY)
    try:
        tp.reforecast(tp.Goal(goal_type="continuous"), weeks,
                      tsb_series={MONDAY + timedelta(days=i): 0.0 for i in range(7)},
                      availability_overrides={thu["day"]: 0.0})
    finally:
        clock.unfreeze()
    zeroed = {x.day.isoformat(): x for x in weeks[0].sessions}[thu["day"]]
    assert (zeroed.session_type, zeroed.status) == ("rest", "pending")


def test_a_miss_in_the_previous_plan_week_is_not_moved_into_this_one():
    """Friday-to-Thursday plan weeks: on Friday, Thursday belongs to last week
    but the same ISO week as today."""
    fri = MONDAY + timedelta(days=4)
    def week(start, n):
        return {"week_num": n, "start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat(),
                "phase": "base", "tss_target": 300, "is_stepback": False,
                "sessions": [{"day": (start + timedelta(days=i)).isoformat(), "day_name": "",
                              "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
                              "status": "pending", "zwo_file": ""} for i in range(7)]}
    plan = {"goal": {"type": "continuous", "available_days": [0, 1, 2, 3, 4, 5, 6], "rest_days": []},
            "availability": {}, "weeks": [week(fri - timedelta(days=7), 1), week(fri, 2)]}
    thu = plan["weeks"][0]["sessions"][6]
    thu.update({"session_type": "vo2max", "duration_min": 60, "tss_estimate": 80})
    _, moves = _reconcile([], today=fri, plan=plan)
    assert moves == []


def test_a_ride_stored_without_a_load_still_counts():
    no_tss = {**SQLITE_ROW, "tss": None, "raw_json": json.dumps({"moving_time": 5440, "icu_intensity": 89})}
    by_day, moves = _reconcile([no_tss])
    assert by_day[MON]["status"] in ("done", "done_partial")
    assert moves == []


def test_a_long_ride_reported_at_a_low_load_is_partly_done():
    """90 min at 45 TSS: an HR-based load, or a higher FTP on intervals.icu."""
    by_day, moves = _reconcile([_row(45, 90, 55)])
    assert by_day[MON]["status"] == "done_partial"
    assert moves == []


def test_a_session_without_a_planned_load_is_judged_by_duration():
    day = {"session_type": "sweetspot", "duration_min": 79, "tss_estimate": 0}
    by_day, moves = _reconcile([_row(120, 90, 89)], day_type=day)
    assert by_day[MON]["status"] in ("done", "done_partial", "ambiguous")
    assert moves == []


def test_a_race_ridden_well_under_its_placeholder_is_not_missed():
    """A 60-minute crit against a 300-minute, 250 TSS placeholder."""
    by_day, _ = _reconcile([_row(100, 60, 95)], plan=_plan(race=True))
    assert by_day[MON]["status"] == "done_partial"


def test_a_miss_is_not_moved_into_the_next_plan_week():
    """Friday-to-Thursday plan weeks, today Thursday: Tuesday's miss may only
    move within the week that ends today, never onto next week's Friday."""
    fri = MONDAY - timedelta(days=3)
    def week(start, n):
        return {"week_num": n, "start": start.isoformat(), "end": (start + timedelta(days=6)).isoformat(),
                "phase": "base", "tss_target": 300, "is_stepback": False,
                "sessions": [{"day": (start + timedelta(days=i)).isoformat(), "day_name": "",
                              "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
                              "status": "pending", "zwo_file": ""} for i in range(7)]}
    plan = {"goal": {"type": "continuous", "available_days": [0, 1, 2, 3, 4, 5, 6], "rest_days": []},
            "availability": {}, "weeks": [week(fri, 1), week(fri + timedelta(days=7), 2)]}
    tue = plan["weeks"][0]["sessions"][4]
    tue.update({"session_type": "vo2max", "duration_min": 60, "tss_estimate": 80})
    plan["weeks"][0]["sessions"][6]["session_type"] = "z2"          # Thursday: busy
    plan["weeks"][0]["sessions"][6].update({"duration_min": 60, "tss_estimate": 40, "status": "done"})
    thursday = MONDAY + timedelta(days=3)
    _, moves = _reconcile([], today=thursday, plan=plan)
    assert all(m["to"] <= thursday.isoformat() for m in moves), moves


def test_a_ride_that_arrives_after_its_session_was_moved_brings_it_back():
    """The fifth review: Monday's ride reached intervals.icu on Tuesday
    afternoon. Tuesday morning's sync had marked Monday missed and moved the
    session; the ride then landed on a rest stub nothing matched."""
    plan = _plan()
    by_day, moves = _reconcile([], plan=plan)                   # Tuesday morning: no ride yet
    assert [m["from"] for m in moves] == [MON]
    dst = moves[0]["to"]
    by_day, moves = _reconcile([SQLITE_ROW], plan=plan)         # Tuesday afternoon: it arrives
    assert by_day[MON]["session_type"] == "sweetspot"
    assert by_day[MON]["status"] == "done"
    assert (by_day[dst]["session_type"], by_day[dst]["status"]) == ("rest", "pending"), "what it displaced is back"
    assert moves == []
    assert plan.get("auto_moves") == []


def test_a_late_ride_undoes_the_move_even_after_the_destination_was_judged():
    """The sixth review: the plan-open rematch judged the moved session on its
    new day against that day's easy ride before the undo ran, and the undo
    then refused to act. Every day gets its own session back and is judged
    again against its own rides."""
    plan = _plan()
    _, moves = _reconcile([], plan=plan)
    dst = moves[0]["to"]
    for w in plan["weeks"]:
        for x in w["sessions"]:
            if x["day"] == dst:
                x["status"] = "done_partial"
    by_day, _ = _reconcile([SQLITE_ROW], plan=plan)
    assert (by_day[MON]["session_type"], by_day[MON]["status"]) == ("sweetspot", "done")
    assert by_day[dst]["session_type"] == "rest"


def test_a_missed_session_dragged_onto_a_day_to_come_is_owed_there():
    """Left "missed", the auto-reschedule moved it off the day it was put on."""
    plan = _plan()
    plan["weeks"][0]["sessions"][0]["status"] = "missed"
    wed = (MONDAY + timedelta(days=2)).isoformat()
    clock.freeze(TODAY)
    try:
        moved = app_module._apply_move_session(plan, MON, wed)
    finally:
        clock.unfreeze()
    assert moved["status"] == "pending"
    _, moves = _reconcile([], plan=plan)
    assert wed not in [m["from"] for m in moves]



def test_undoing_a_move_gives_the_easy_day_it_took_over_back():
    plan = _plan()
    for x in plan["weeks"][0]["sessions"][2:5]:                     # Wed-Fri: easy rides, no rest slot
        x.update({"session_type": "z2", "duration_min": 60, "tss_estimate": 40,
                  "zwo_file": "endurance_steady_68pct_75min.zwo"})
    _, moves = _reconcile([], plan=plan)
    assert [m["from"] for m in moves] == [MON]
    dst = moves[0]["to"]
    by_day, _ = _reconcile([SQLITE_ROW], plan=plan)
    assert by_day[MON]["session_type"] == "sweetspot"
    assert (by_day[dst]["session_type"], by_day[dst]["duration_min"]) in (("z2", 73), ("z2", 60))


def test_a_copy_of_a_ride_without_a_load_is_the_same_ride():
    """The fifth review: a copy stored without a load fell in another TSS
    bucket, so both copies were counted and their minutes doubled."""
    easy = {"session_type": "long_z2", "duration_min": 120, "tss_estimate": 90, "zwo_file": ""}
    real = {**_row(25, 35, 60, rid="real"), "raw_json": json.dumps({"moving_time": 2100, "icu_intensity": 60,
                                                                     "start_date_local": f"{MONDAY}T10:00:00"})}
    copy_ = {**real, "id": "copy", "tss": None,
             "raw_json": json.dumps({"moving_time": 2100, "start_date_local": f"{MONDAY}T10:00:04"})}
    for rows in ([real, copy_], [copy_, real]):
        plan = _plan()
        week, _ = app_module._load_current_week_dto({**plan, "weeks": [{**plan["weeks"][0],
                                                     "sessions": [{**plan["weeks"][0]["sessions"][0], **easy}]
                                                     + plan["weeks"][0]["sessions"][1:]}]}, TODAY)
        clock.freeze(TODAY)
        try:
            with patch("db.query_activities", return_value=rows), patch("ride_storage.list_rides", return_value=[]):
                acts = app_module._collect_week_activities(week, TODAY, include_today=True)
        finally:
            clock.unfreeze()
        assert [(a["id"], a["tss"]) for a in acts] == [("real", 25.0)], rows[0]["id"]


def test_a_session_moved_twice_still_goes_back_to_its_day():
    """Monday to Wednesday, then (Wednesday unridden) on to Thursday; Monday's
    ride arrives on Friday."""
    plan = _plan()
    for x in plan["weeks"][0]["sessions"][2:5]:
        x.update({"session_type": "rest"})
    plan["weeks"][0]["sessions"][1]["status"] = "done"          # Tuesday ridden
    _, m1 = _reconcile([], plan=plan)
    assert [m["from"] for m in m1] == [MON]
    first = m1[0]["to"]
    nxt = date.fromisoformat(first) + timedelta(days=1)
    _, m2 = _reconcile([], today=nxt, plan=plan)
    assert [m["from"] for m in m2] == [first]
    friday = date.fromisoformat(m2[0]["to"])
    by_day, _ = _reconcile([SQLITE_ROW], today=friday, plan=plan)
    assert by_day[MON]["session_type"] == "sweetspot" and by_day[MON]["status"] in ("done", "pending")
    assert by_day[first]["session_type"] == "rest"
    assert by_day[m2[0]["to"]]["session_type"] == "rest"


def test_an_undo_brings_back_the_session_as_planned_not_as_edited_where_it_was_moved():
    plan = _plan()
    _, moves = _reconcile([], plan=plan)
    dst = moves[0]["to"]
    for w in plan["weeks"]:
        for x in w["sessions"]:
            if x["day"] == dst:
                x.update({"duration_min": 30, "description": "shortened by a reforecast"})
    by_day, _ = _reconcile([SQLITE_ROW], plan=plan)
    assert by_day[MON]["duration_min"] == 79
    assert by_day[MON]["status"] == "done"


def test_a_move_from_a_week_no_longer_judged_is_not_undone():
    plan = _plan()
    _, moves = _reconcile([], plan=plan)
    later = MONDAY + timedelta(days=9)             # the next week's Wednesday
    nxt = MONDAY + timedelta(days=7)
    plan["weeks"].append({"week_num": 2, "start": nxt.isoformat(), "end": (nxt + timedelta(days=6)).isoformat(),
                          "phase": "base", "tss_target": 300, "is_stepback": False,
                          "sessions": [{"day": (nxt + timedelta(days=i)).isoformat(), "day_name": "",
                                        "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
                                        "status": "pending", "zwo_file": ""} for i in range(7)]})
    undone = None
    clock.freeze(later)
    try:
        with patch("db.query_activities", return_value=[SQLITE_ROW]), patch("ride_storage.list_rides", return_value=[]):
            undone = app_module._undo_auto_moves_for_ridden_days(plan, later)
    finally:
        clock.unfreeze()
    assert undone == []


def test_the_manual_rematch_undoes_a_move_first(tmp_path):
    from fastapi.testclient import TestClient
    plan = _plan()
    _, moves = _reconcile([], plan=plan)
    (tmp_path / "current_plan.json").write_text(json.dumps(plan))
    clock.freeze(TODAY)
    try:
        with patch.object(app_module, "_plan_dir", return_value=tmp_path), \
             patch("db.query_activities", return_value=[SQLITE_ROW]), patch("ride_storage.list_rides", return_value=[]):
            r = TestClient(app_module.app).post("/api/plan/rematch?apply=1",
                                                headers={"Origin": "http://127.0.0.1:22400"})
    finally:
        clock.unfreeze()
    assert r.status_code == 200, r.text
    saved = json.loads((tmp_path / "current_plan.json").read_text())
    mon = saved["weeks"][0]["sessions"][0]
    assert (mon["session_type"], mon["status"]) == ("sweetspot", "done")



def _chain_plan():
    """Monday's sweetspot moved to Wednesday (Tuesday sync), then on to
    Thursday (Thursday sync: Wednesday unridden). Returns plan, wed, thu."""
    plan = _plan()
    for x in plan["weeks"][0]["sessions"][2:5]:
        x.update({"session_type": "rest"})
    plan["weeks"][0]["sessions"][1]["status"] = "done"
    _, m1 = _reconcile([], plan=plan)
    wed = m1[0]["to"]
    _, m2 = _reconcile([], today=date.fromisoformat(wed) + timedelta(days=1), plan=plan)
    return plan, wed, m2[0]["to"]


def test_a_late_ride_on_a_day_the_session_passed_through_brings_it_back_there():
    plan, wed, thu = _chain_plan()
    wed_ride = {**_row(115, 85, 89, day=date.fromisoformat(wed), rid="wed")}
    friday = date.fromisoformat(thu) + timedelta(days=1)
    by_day, moves = _reconcile([wed_ride], today=friday, plan=plan)
    assert (by_day[wed]["session_type"], by_day[wed]["status"]) == ("sweetspot", "done")
    assert by_day[thu]["session_type"] == "rest"
    assert by_day[MON]["status"].startswith("moved_from:")
    assert moves == []


def test_a_session_dragged_onto_a_day_the_move_passed_through_is_left_alone():
    plan, wed, thu = _chain_plan()
    for w in plan["weeks"]:
        for x in w["sessions"]:
            if x["day"] == wed:
                x.update({"session_type": "vo2max", "duration_min": 60, "tss_estimate": 80,
                          "status": "pending", "user_moved": True})
    friday = date.fromisoformat(thu) + timedelta(days=1)
    by_day, _ = _reconcile([SQLITE_ROW], today=friday, plan=plan)
    kinds = [x["session_type"] for w in plan["weeks"] for x in w["sessions"]]
    assert kinds.count("vo2max") == 1, "the dragged session survives (rescheduled if unridden)"
    assert kinds.count("sweetspot") == 1
    assert all(r.get("from") != MON for r in plan.get("auto_moves") or [])


def test_a_destination_ridden_as_the_session_keeps_it_over_a_smaller_late_ride():
    plan = _plan()
    _, moves = _reconcile([], plan=plan)
    dst = moves[0]["to"]
    dst_ride = _row(110, 80, 89, day=date.fromisoformat(dst), rid="dst")
    after = date.fromisoformat(dst) + timedelta(days=1)
    _reconcile([dst_ride], today=after, plan=plan)
    by_day, _ = _reconcile([dst_ride, _row(55, 45, 80, rid="small-monday")], today=after, plan=plan)
    assert (by_day[dst]["session_type"], by_day[dst]["status"]) == ("sweetspot", "done")
    assert by_day[MON]["status"].startswith("moved_from:")


def test_a_restored_session_is_not_marked_as_the_riders_own_move():
    plan = _plan()
    plan["weeks"][0]["sessions"][0]["user_moved"] = True
    _reconcile([], plan=plan)
    by_day, _ = _reconcile([SQLITE_ROW], plan=plan)
    assert by_day[MON]["user_moved"] is False


def test_the_manual_rematch_judges_yesterdays_week_after_an_undo(tmp_path):
    """Saturday's session moved onto Sunday; on Monday Saturday's ride is in.
    The endpoint rematched only today's week and left both days unjudged."""
    from fastapi.testclient import TestClient
    plan = _plan()
    sessions = plan["weeks"][0]["sessions"]
    for x in sessions[:5]:
        x.update({"session_type": "rest", "status": "pending", "zwo_file": ""})
    sat, sun = sessions[5], sessions[6]
    sat.update({"session_type": "sweetspot", "duration_min": 79, "tss_estimate": 105})
    plan["goal"]["available_days"] = [0, 1, 2, 3, 4, 5, 6]
    plan["goal"]["rest_days"] = []
    saturday, sunday = date.fromisoformat(sat["day"]), date.fromisoformat(sun["day"])
    nxt = MONDAY + timedelta(days=7)
    plan["weeks"].append({"week_num": 2, "start": nxt.isoformat(), "end": (nxt + timedelta(days=6)).isoformat(),
                          "phase": "base", "tss_target": 300, "is_stepback": False,
                          "sessions": [{"day": (nxt + timedelta(days=i)).isoformat(), "day_name": "",
                                        "session_type": "rest", "duration_min": 0, "tss_estimate": 0,
                                        "status": "pending", "zwo_file": ""} for i in range(7)]})
    _, moves = _reconcile([], today=sunday, plan=plan)
    assert [(m["from"], m["to"]) for m in moves] == [(sat["day"], sun["day"])]
    (tmp_path / "current_plan.json").write_text(json.dumps(plan))
    sat_ride = _row(120, 91, 89, day=saturday, rid="sat")
    clock.freeze(nxt)
    try:
        with patch.object(app_module, "_plan_dir", return_value=tmp_path), \
             patch("db.query_activities", return_value=[sat_ride]), patch("ride_storage.list_rides", return_value=[]):
            r = TestClient(app_module.app).post("/api/plan/rematch?apply=1",
                                                headers={"Origin": "http://127.0.0.1:22400"})
    finally:
        clock.unfreeze()
    assert r.status_code == 200, r.text
    saved = {x["day"]: x for w in json.loads((tmp_path / "current_plan.json").read_text())["weeks"] for x in w["sessions"]}
    assert (saved[sat["day"]]["session_type"], saved[sat["day"]]["status"]) == ("sweetspot", "done")
    assert saved[sun["day"]]["session_type"] == "rest"
