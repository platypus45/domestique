"""3.14.0 F2 — rides the rider adds themselves (a group ride, a ride of their own).

An own ride is a session row with an ``own`` record: the planner plans around
it and never rewrites it (``_protect_race`` means "race or own"); its one
writer is ``training_planner.apply_own_sessions``. Each change fits the week:
room from later easy riding and its give-back, 48 h easing from tomorrow on
(never a pinned neighbour), the week's hard-day cap, an FTP test moved to a
rested day. Every test here fails without the fix it names (grill_sessions
H/M/L ids, the owner's O-decisions and the coordinator's C-decisions in
MASTER_DECISIONS_314).

Hermetic: hand-made plans and the stubbed HOME of test_icu_push; no network,
no live data. Hooper thresholds are never read here (severity_override).
"""
from __future__ import annotations

import copy
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

import clock
import training_planner as tp
import week_plan
import app as app_module
from test_icu_push import stub  # noqa: F401  (fixture)

TODAY = date(2026, 10, 19)            # a Monday
GOAL = {"goal_type": "continuous", "max_weekday_hours": 5.0, "max_weekend_hours": 6.0}


def d(i: int) -> str:
    return (TODAY + timedelta(days=i)).isoformat()


def row(day, st="z2", dur=60, tss=45, **kw):
    r = {"day": day, "day_name": "", "session_type": st, "duration_min": dur,
         "tss_estimate": tss, "description": f"{st} ({dur}min)", "status": "pending",
         "zwo_file": "", "zwo_name": ""}
    r.update(kw)
    return r


def week(start, rows, budget, phase="base", stepback=False, num=1):
    return {"week_num": num, "start": d(start), "end": d(start + 6), "phase": phase,
            "tss_target": budget, "is_stepback": stepback,
            "sessions": [row(d(start + k), *r[:3], **(r[3] if len(r) > 3 else {}))
                         for k, r in enumerate(rows)]}


def mkplan(*weeks, goal=None):
    return {"goal": dict(goal or GOAL), "weeks": list(weeks)}


def by_day(p):
    return {s["day"]: s for w in p["weeks"] for s in w["sessions"]}


def load(p, week_idx=0):
    return sum(float(s["tss_estimate"] or 0) for s in p["weeks"][week_idx]["sessions"]
               if s["session_type"] != "rest"
               and (s.get("status") or "pending") not in ("missed", "dismissed"))


def apply(p, set_items=(), remove=(), library=None, **kw):
    return tp.apply_own_sessions(p, list(set_items), list(remove),
                                 library=[] if library is None else library, **kw)


def ride(day, dur=180, intensity="hard", name="Club ride", **kw):
    return dict({"date": day, "name": name, "duration_min": dur, "intensity": intensity}, **kw)


@pytest.fixture(autouse=True)
def _today(freeze_clock):
    freeze_clock(TODAY)


# A full base week (cap 2): Mon rest, Tue z2, Wed VO2, Thu z2, Fri VO2, Sat long, Sun z2.
FULL = [("rest", 0, 0), ("z2", 60, 45), ("vo2max", 60, 75), ("z2", 90, 68),
        ("vo2max", 60, 75), ("long_z2", 150, 112), ("z2", 90, 68)]


# ═══ A. the writer ═══════════════════════════════════════════════════════════

def test_a1_hard_ride_on_the_long_ride_makes_room_from_later_easy_riding():
    p = mkplan(week(0, FULL, 443))
    r = apply(p, [ride(d(5))])
    s = by_day(p)[d(5)]
    assert (s["session_type"], s["duration_min"], s["tss_estimate"], s["zwo_file"]) == ("sweetspot", 180, 240, "")
    own = s["own"]
    assert (own["name"], own["intensity"], own["source"], own["load_from"]) == ("Club ride", "hard", "manual", "intensity")
    assert own["icu_event_id"] is None and own["replaced"]["session_type"] == "long_z2"
    assert load(p) <= 443 + 1                                   # the week keeps its budget
    shortened = [x for x in by_day(p).values() if x.get("shortened_from")]
    assert shortened and all(x["session_type"] in tp._ROOM_FROM_TYPES for x in shortened)
    assert by_day(p)[d(2)]["session_type"] == "vo2max"          # intensity is never cut for room
    assert r["applied"] == [{"action": "add", "date": d(5)}] and not r["refused"]


def test_a2_room_is_bounded_by_what_the_change_added():
    """A week already 30 over its budget sheds only what the ride adds."""
    p = mkplan(week(0, [("rest", 0, 0), ("z2", 120, 90), ("z2", 120, 90), ("rest", 0, 0),
                        ("z2", 120, 90), ("rest", 0, 0), ("z2", 60, 45)], 285))
    before = load(p)                                            # 315 = 30 over
    apply(p, [ride(d(3), dur=60, intensity="easy")])            # +45
    assert load(p) == pytest.approx(before, abs=2)              # 45 shed, not 75


def test_a3_no_easy_room_left_the_ride_stays_and_the_week_runs_over():
    p = mkplan(week(0, [("rest", 0, 0), ("vo2max", 60, 75), ("rest", 0, 0), ("threshold", 60, 90),
                        ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)], 165))
    r = apply(p, [ride(d(5), dur=120, intensity="moderate")])
    assert by_day(p)[d(5)]["own"]["name"] == "Club ride"
    assert r["over_budget"] == {d(0): pytest.approx(130, abs=1)}
    assert any(w["code"] == "over_budget" for w in r["warnings"])


def test_a4_load_typed_wins_load_alone_gives_how_hard_and_an_icu_load_is_kept_as_icu():
    p = mkplan(week(0, FULL, 900))
    apply(p, [ride(d(1), dur=120, intensity="easy", load=150),
              {"date": d(3), "name": "Hard bunch", "duration_min": 120, "load": 160},
              {"date": d(6), "name": "Imported", "duration_min": 60, "intensity": "easy",
               "load": 70, "load_from": "icu", "icu_event_id": 812}])
    b = by_day(p)
    assert (b[d(1)]["tss_estimate"], b[d(1)]["own"]["load_from"], b[d(1)]["own"]["intensity"]) == (150, "typed", "easy")
    assert b[d(3)]["own"]["intensity"] == "hard" and b[d(3)]["session_type"] == "sweetspot"  # IF 0.89
    assert (b[d(6)]["own"]["load_from"], b[d(6)]["own"]["source"], b[d(6)]["own"]["icu_event_id"]) == ("icu", "icu", 812)
    assert b[d(6)]["own"]["icu_day"] == d(6)


def test_a5_removal_brings_back_the_session_and_gives_the_week_its_length_back():
    p = mkplan(week(0, FULL, 443))
    orig = by_day(copy.deepcopy(p))
    apply(p, [ride(d(5))])
    apply(p, remove=[d(5)])
    b = by_day(p)
    assert b[d(5)]["session_type"] == "long_z2" and b[d(5)]["duration_min"] == 150 and not b[d(5)].get("own")
    assert b[d(4)]["session_type"] == "vo2max"                  # the eased neighbour is back
    for k in (1, 3, 6):                                          # shortened rides grew back
        assert b[d(k)]["duration_min"] >= orig[d(k)]["duration_min"] - 5
    assert load(p) <= 443 + 1


def test_a6_removal_on_a_stub_day_restores_the_stub_and_a_rest_day_stays_rest():
    stub_row = ("rest", 0, 0, {"status": f"moved_from:{d(4)}"})
    p = mkplan(week(0, [("rest", 0, 0), ("z2", 60, 45), stub_row, ("rest", 0, 0),
                        ("z2", 60, 45), ("rest", 0, 0), ("rest", 0, 0)], 400))
    apply(p, [ride(d(2), intensity="easy", dur=60), ride(d(3), intensity="easy", dur=60, name="Spin")])
    apply(p, remove=[d(2), d(3)])
    b = by_day(p)
    assert b[d(2)]["status"] == f"moved_from:{d(4)}" and b[d(2)]["session_type"] == "rest"
    assert b[d(3)]["session_type"] == "rest" and not b[d(3)].get("own")


def test_a7_hard_ride_eases_tomorrows_neighbours_both_sides_never_pinned_ridden_or_race():
    rows = [("rest", 0, 0), ("z2", 60, 45), ("vo2max", 60, 75), ("rest", 0, 0),
            ("threshold", 60, 90), ("rest", 0, 0), ("rest", 0, 0)]
    p = mkplan(week(0, rows, 900))
    r = apply(p, [ride(d(3))])                                  # Thu: Wed and Fri are hard
    b = by_day(p)
    assert b[d(2)]["session_type"] == "z2" and b[d(4)]["session_type"] == "z2"
    assert b[d(2)]["own_eased_from"]["session_type"] == "vo2max"
    assert b[d(2)]["own_eased_from"]["for"] == d(3)
    assert {e["day"] for e in r["eased"]} == {d(2), d(4)}
    # Pinned (dragged / swapped / held in intervals.icu), ridden and race neighbours stay.
    for kw in ({"user_moved": True}, {"user_swapped": True}, {"status": "done"}, {"is_race": True}):
        p = mkplan(week(0, [("rest", 0, 0), ("z2", 60, 45), ("vo2max", 60, 75, kw), ("rest", 0, 0),
                            ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)], 900))
        r = apply(p, [ride(d(3))])
        assert by_day(p)[d(2)]["session_type"] == "vo2max", kw
    p = mkplan(week(0, [("rest", 0, 0), ("z2", 60, 45), ("vo2max", 60, 75), ("rest", 0, 0),
                        ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)], 900))
    r = apply(p, [ride(d(3))], pinned_days=[d(2)])              # C9: held in intervals.icu
    assert by_day(p)[d(2)]["session_type"] == "vo2max"
    assert [w["code"] for w in r["warnings"]] == ["pinned_next_to_ride"]


def test_m2_o6_a_ride_tomorrow_never_changes_todays_session_and_says_so():
    p = mkplan(week(0, [("vo2max", 60, 75), ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0),
                        ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)], 900))
    before = copy.deepcopy(by_day(p)[d(0)])
    r = apply(p, [ride(d(1))])
    assert by_day(p)[d(0)] == before                            # byte-identical
    assert [w["code"] for w in r["warnings"]] == ["today_hard_next_to_ride"]
    assert "Today" in r["warnings"][0]["text"]


def test_a8_cap_counts_the_ride_eases_the_longest_later_hard_session():
    rows = [("rest", 0, 0), ("vo2max", 60, 75), ("rest", 0, 0), ("threshold", 90, 135),
            ("rest", 0, 0), ("rest", 0, 0), ("sweetspot", 75, 100)]
    p = mkplan(week(0, rows, 900, phase="base"))                # cap 2: 3 hard + the ride
    r = apply(p, [ride(d(5), intensity="easy", dur=60)])        # an easy ride counts nothing
    assert not r["eased"]
    apply(p, remove=[d(5)])
    r = apply(p, [ride(d(5))])                                  # hard on Sat: Sun eased by 48 h
    b = by_day(p)
    assert b[d(6)]["session_type"] == "z2"                      # 48 h
    assert b[d(3)]["session_type"] == "z2"                      # cap: the longest of the rest
    assert b[d(1)]["session_type"] == "vo2max"
    assert sum(1 for x in b.values() if tp._session_is_hit(x)) <= 2


def test_c11_a_deload_week_keeps_the_ride_and_eases_the_other_hard_sessions():
    rows = [("rest", 0, 0), ("vo2max", 45, 56), ("rest", 0, 0), ("rest", 0, 0),
            ("z2", 60, 45), ("rest", 0, 0), ("rest", 0, 0)]
    p = mkplan(week(0, rows, 900, stepback=True))
    apply(p, [ride(d(5))])
    b = by_day(p)
    assert b[d(5)]["own"] and b[d(5)]["session_type"] == "sweetspot"
    assert b[d(1)]["session_type"] == "z2" and b[d(1)]["own_eased_from"]["for"] == d(5)


def test_a9_l2_eased_sessions_come_back_only_when_no_hard_ride_needs_them():
    rows = [("rest", 0, 0), ("rest", 0, 0), ("vo2max", 60, 75), ("rest", 0, 0),
            ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)]
    p = mkplan(week(0, rows, 900, phase="build1"))
    apply(p, [ride(d(1)), ride(d(3), name="Thursday ride")])    # Wed between two hard rides
    assert by_day(p)[d(2)]["session_type"] == "z2"
    r = apply(p, remove=[d(1)])                                 # blocked by Thursday: kept marked
    assert by_day(p)[d(2)]["session_type"] == "z2" and by_day(p)[d(2)].get("own_eased_from")
    r = apply(p, remove=[d(3)])                                 # L2: now it comes back
    assert by_day(p)[d(2)]["session_type"] == "vo2max" and not by_day(p)[d(2)].get("own_eased_from")
    assert [x["day"] for x in r["restored"]] == [d(2)]
    # Changed since the easing: never written back.
    apply(p, [ride(d(3))])
    by_day(p)[d(2)]["duration_min"] = 30
    apply(p, remove=[d(3)])
    assert by_day(p)[d(2)]["session_type"] == "z2" and not by_day(p)[d(2)].get("own_eased_from")


@pytest.mark.parametrize("item,reason", [
    (ride(d(-1)), "past"),
    (ride((TODAY + timedelta(days=30)).isoformat()), "not_in_plan"),
    (ride(d(5), dur=10), "bad_length"),
    (ride(d(5), dur=700), "bad_length"),
    (dict(ride(d(5)), intensity="brutal"), "bad_intensity"),
    ({"date": d(5), "name": "x", "duration_min": 60}, "bad_intensity"),
    (dict(ride(d(5)), load=5000), "bad_load"),
    (dict(ride(d(5)), date="not a day"), "bad_date"),
])
def test_a10_refusals_change_nothing(item, reason):
    p = mkplan(week(0, FULL, 443))
    before = copy.deepcopy(p)
    r = apply(p, [item])
    assert [x["reason"] for x in r["refused"]] == [reason] and r["refused"][0]["text"]
    assert p == before and not r["applied"]


def test_a10_race_ridden_and_two_row_days_are_refused_and_valid_items_still_apply():
    rows = [("rest", 0, 0), ("z2", 60, 45, {"status": "done"}), ("vo2max", 60, 75, {"is_race": True}),
            ("z2", 90, 68), ("rest", 0, 0), ("long_z2", 150, 112), ("z2", 90, 68)]
    p = mkplan(week(0, rows, 900))
    p["weeks"][0]["sessions"].append(row(d(3), "z2", 30, 20))   # Thu holds two rows
    r = apply(p, [ride(d(0)), ride(d(1)), ride(d(2)), ride(d(3)), ride(d(6), intensity="easy")])
    assert sorted((x["date"], x["reason"]) for x in r["refused"]) == [
        (d(1), "ridden"), (d(2), "race_day"), (d(3), "two_sessions")]
    assert by_day(p)[d(6)]["own"] and by_day(p)[d(0)]["own"]


def test_m8_two_rides_for_one_day_and_a_second_intervals_icu_ride_on_a_ride_day():
    p = mkplan(week(0, FULL, 900))
    r = apply(p, [ride(d(5)), ride(d(5), name="Coffee spin")])
    assert [x["reason"] for x in r["refused"]] == ["duplicate_day"]
    assert by_day(p)[d(5)]["own"]["name"] == "Club ride"
    imp = {"date": d(5), "name": "Club ride (icu)", "duration_min": 180, "intensity": "hard",
           "icu_event_id": 77}
    r = apply(p, [imp])
    assert [x["reason"] for x in r["refused"]] == ["day_has_your_ride"]
    r = apply(p, [dict(imp, link=True)])                         # the manual ride becomes it
    own = by_day(p)[d(5)]["own"]
    assert (own["source"], own["icu_event_id"]) == ("icu", 77) and not r["refused"]
    assert own["replaced"]["session_type"] == "long_z2"          # still restores the planner's day
    r = apply(p, [dict(imp, date=d(6))])                         # the same event twice
    assert [x["reason"] for x in r["refused"]] == ["duplicate"]


def test_a11_codec_carries_own_and_own_eased_from():
    p = mkplan(week(0, FULL, 900))
    apply(p, [ride(d(5))])
    w = p["weeks"][0]
    back = tp.week_to_dict(tp.week_from_dict(w))
    assert back["sessions"][5]["own"] == w["sessions"][5]["own"]
    assert back["sessions"][4]["own_eased_from"] == w["sessions"][4]["own_eased_from"]


def test_m1_a_move_onto_a_day_that_refuses_keeps_the_ride_where_it_was():
    rows = [("rest", 0, 0), ("z2", 60, 45), ("vo2max", 60, 75, {"is_race": True}),
            ("z2", 90, 68), ("rest", 0, 0), ("long_z2", 150, 112), ("z2", 90, 68)]
    p = mkplan(week(0, rows, 900))
    apply(p, [ride(d(5))])
    before = copy.deepcopy(p)
    r = tp.apply_own_sessions(p, moves=[{"from": d(5), "to": d(2)}], library=[])
    assert [x["reason"] for x in r["refused"]] == ["race_day"] and p == before
    r = tp.apply_own_sessions(p, moves=[{"from": d(5), "to": d(6), "icu_day": d(6)}], library=[])
    b = by_day(p)
    assert b[d(6)]["own"]["name"] == "Club ride" and b[d(6)]["moved_from"] == d(5)   # C3
    assert b[d(5)]["session_type"] == "long_z2" and not b[d(5)].get("own")


def test_m7_removal_over_a_dismissed_session_keeps_it_dismissed():
    rows = [("rest", 0, 0), ("z2", 60, 45), ("rest", 0, 0),
            ("threshold", 60, 90, {"status": "dismissed", "dismissed_at": "2026-10-18T09:00:00"}),
            ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)]
    p = mkplan(week(0, rows, 900))
    apply(p, [ride(d(3), intensity="easy", dur=60)])
    apply(p, remove=[d(3)])
    s = by_day(p)[d(3)]
    assert (s["session_type"], s["status"]) == ("threshold", "dismissed") and s["dismissed_at"]


def test_m4_l3_m5_the_restored_session_fits_the_day_as_it_is_now():
    rows = [("rest", 0, 0), ("rest", 0, 0), ("vo2max", 60, 75, {"zwo_file": "gone.zwo", "zwo_name": "Gone"}),
            ("rest", 0, 0), ("long_z2", 150, 112), ("rest", 0, 0), ("rest", 0, 0)]
    p = mkplan(week(0, rows, 900, phase="build1"))
    apply(p, [ride(d(2), intensity="easy", dur=60), ride(d(4), intensity="easy", dur=60)])
    # Since then: the week became a deload (a rebuild), Friday's hours dropped to 1 h.
    p["weeks"][0]["is_stepback"] = True
    p["availability"] = {d(4): {"hours": 1.0, "type": "available"}}
    lib = [{"File": "other.zwo", "Name": "Other", "Duration(min)": 60}]
    r = apply(p, remove=[d(2), d(4)], library=lib)
    b = by_day(p)
    assert b[d(2)]["session_type"] == "z2"                       # M4: no hard day in a deload
    assert b[d(2)]["zwo_file"] != "gone.zwo"                     # M5: the file left the library
    assert b[d(4)]["duration_min"] == 60                         # L3: the day's hours now
    p2 = mkplan(week(0, rows, 900, phase="build1"))
    apply(p2, [ride(d(4), intensity="easy", dur=60)])
    p2["availability"] = {d(4): {"hours": 0, "type": "unavailable"}}
    apply(p2, remove=[d(4)])
    assert by_day(p2)[d(4)]["session_type"] == "rest"            # L3: an unavailable day rests


def test_l8_l11_c10_warnings_back_to_back_hard_rides_today_and_race_days():
    rows = [("z2", 60, 45), ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0),
            ("z2", 45, 30, {"is_opener": True}), ("recovery", 60, 30, {"is_race": True}), ("rest", 0, 0)]
    p = mkplan(week(0, rows, 900))
    r = apply(p, [ride(d(1)), ride(d(2), name="Second")])
    assert "hard_rides_back_to_back" in [w["code"] for w in r["warnings"]]
    r = apply(p, [ride(d(0), intensity="easy", dur=60, name="Morning")])
    assert "replaces_today" in [w["code"] for w in r["warnings"]]
    r = apply(p, [ride(d(4), intensity="easy", dur=60, name="Eve spin")])
    codes = [w["code"] for w in r["warnings"]]
    assert "race_tomorrow" in codes and "opener_replaced" in codes   # C10: allowed, said


# ═══ O7 / H2: the FTP test and an own ride ══════════════════════════════════

def test_o7_a_ride_on_the_test_day_moves_the_test_to_the_next_rested_day():
    rows = [("rest", 0, 0), ("z2", 60, 45), ("ftp_test", 60, 70), ("z2", 60, 45),
            ("z2", 60, 45), ("long_z2", 150, 112), ("z2", 60, 45)]
    p = mkplan(week(0, rows, 900))
    r = apply(p, [ride(d(2), intensity="easy", dur=90)])
    b = by_day(p)
    assert b[d(2)]["own"] and b[d(3)]["session_type"] == "ftp_test"
    assert r["ftp_test_moved"][0]["from"] == d(2) and r["ftp_test_moved"][0]["to"] == d(3)
    apply(p, remove=[d(2)])                                     # the ride goes: no second test
    assert by_day(p)[d(2)]["session_type"] == "z2"
    assert sum(1 for x in by_day(p).values() if x["session_type"] == "ftp_test") == 1


def test_o7_h2d_a_moderate_ride_the_day_before_a_test_moves_the_test():
    rows = [("rest", 0, 0), ("z2", 60, 45), ("ftp_test", 60, 70), ("z2", 60, 45),
            ("z2", 60, 45), ("long_z2", 150, 112), ("z2", 60, 45)]
    p = mkplan(week(0, rows, 900))
    r = apply(p, [ride(d(1), intensity="moderate", dur=90)])    # tempo: "not easy"
    b = by_day(p)
    assert b[d(2)]["session_type"] == "z2" and b[d(3)]["session_type"] == "ftp_test"
    assert r["ftp_test_moved"] and r["ftp_test_moved"][0]["to"] == d(3)


def test_o7_no_rested_day_within_a_week_warns_or_refuses_and_keeps_the_test():
    rows = [("rest", 0, 0), ("z2", 60, 45), ("ftp_test", 60, 70), ("vo2max", 60, 75),
            ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)]
    p = mkplan(week(0, rows, 900))
    r = apply(p, [ride(d(1), intensity="moderate", dur=60)])
    assert by_day(p)[d(2)]["session_type"] == "ftp_test"
    assert [w["code"] for w in r["warnings"]] == ["ftp_test_kept"]
    before = copy.deepcopy(p)
    r = apply(p, [ride(d(2), intensity="easy", dur=60)])       # on the test day itself
    assert [x["reason"] for x in r["refused"]] == ["ftp_test_day"] and p == before


def test_h2_fresh_legs_never_rewrites_an_own_ride_before_a_test():
    wk = tp.week_from_dict(week(0, [("rest", 0, 0), ("tempo", 120, 130), ("ftp_test", 60, 70),
                                    ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)], 900))
    wk.sessions[1].own = {"name": "Club ride", "intensity": "moderate"}
    tp._ensure_fresh_legs_before_ftp_tests([wk])
    assert (wk.sessions[1].session_type, wk.sessions[1].duration_min) == ("tempo", 120)


def test_h2_the_rested_test_day_sees_kept_days_and_never_takes_one():
    """Recalc places the test after the preserved swap (grill H2a/b)."""
    wk = tp.week_from_dict(week(0, [("rest", 0, 0), ("z2", 60, 45), ("z2", 60, 45), ("z2", 60, 45),
                                    ("z2", 60, 45), ("z2", 60, 45), ("z2", 60, 45)], 300, stepback=True))
    own = wk.sessions[5]
    own.session_type, own.own = "sweetspot", {"name": "Club ride", "intensity": "hard"}
    pick = tp._rested_test_day(wk, [], keep_days={own.day})
    assert pick is not None and pick.day != own.day
    assert pick.day != own.day + timedelta(days=1) and pick.day != own.day - timedelta(days=1)


# ═══ B. every other writer leaves the own ride alone ═════════════════════════

def _own_week(budget=443):
    p = mkplan(week(0, FULL, budget))
    apply(p, [ride(d(5))])
    return p


def test_b1_b2_sync_reforecast_keeps_the_ride_byte_identical_and_0h_does_not_rest_it():
    p = _own_week()
    before = copy.deepcopy(by_day(p)[d(5)])
    tsb = {TODAY + timedelta(days=i): -45.0 for i in range(-7, 15)}
    tp.reforecast_dict(p, tsb_series=tsb,
                       availability_overrides={d(i): (0.0 if i == 5 else 1.0) for i in range(7)},
                       actual_polarization={"z1_pct": 50, "z2_pct": 10, "z3_pct": 40},
                       target_polarization={"z1_pct": 80, "z2_pct": 5, "z3_pct": 15})
    assert by_day(p)[d(5)] == before


def test_b3_room_making_never_shortens_an_easy_own_ride():
    p = mkplan(week(0, FULL, 443))
    apply(p, [ride(d(6), intensity="easy", dur=150)])
    before = copy.deepcopy(by_day(p)[d(6)])
    tp.reforecast_dict(p, tsb_series={TODAY + timedelta(days=i): 0.0 for i in range(-7, 8)},
                       availability_overrides={d(0): 2.0}, explicit_days={d(0)})
    assert by_day(p)[d(6)] == before


def test_b4_l6_reforecast_write_back_never_rewrites_an_own_row():
    p = _own_week()
    before = copy.deepcopy(by_day(p)[d(5)])
    weeks = tp._plan_dict_to_planned_weeks(p)
    weeks[0].sessions[5].session_type, weeks[0].sessions[5].duration_min = "z2", 30
    tp._apply_reforecast_to_dict(p, weeks, {d(5)})
    assert by_day(p)[d(5)] == before


def test_c12_m6_a_missed_session_takes_no_room():
    """3.13.2's room-making counted a missed session's load (P4 inverted)."""
    rows = [("z2", 60, 45, {"status": "done"}), ("sweetspot", 60, 90, {"status": "missed"}),
            ("rest", 0, 0), ("z2", 90, 68), ("rest", 0, 0), ("long_z2", 150, 112), ("rest", 0, 0)]
    p = mkplan(week(0, rows, 315))
    clock.freeze(TODAY + timedelta(days=2))
    try:
        tp.reforecast_dict(p, tsb_series={TODAY + timedelta(days=i): 0.0 for i in range(-7, 14)},
                           availability_overrides={d(2): 1.0}, explicit_days={d(2)})
    finally:
        clock.freeze(TODAY)
    b = by_day(p)
    assert b[d(2)]["duration_min"] == 60
    assert b[d(5)]["duration_min"] == 150 and "shortened_from" not in b[d(5)]


def test_m3_a_sync_keeps_the_eased_day_so_the_removal_gives_it_back():
    rows = [("rest", 0, 0), ("z2", 60, 45), ("vo2max", 60, 75), ("rest", 0, 0),
            ("rest", 0, 0), ("long_z2", 150, 112), ("rest", 0, 0)]
    p = mkplan(week(0, rows, 900))
    apply(p, [ride(d(3))])
    assert by_day(p)[d(2)]["session_type"] == "z2"
    tp.reforecast_dict(p, tsb_series={TODAY + timedelta(days=i): 0.0 for i in range(-7, 8)},
                       availability_overrides={d(i): 1.5 for i in range(7)})
    apply(p, remove=[d(3)])
    assert by_day(p)[d(2)]["session_type"] == "vo2max"


def test_b9_h3_refit_after_a_missed_hard_keeps_the_room_and_the_budget():
    rows = [("rest", 0, 0), ("vo2max", 60, 75), ("z2", 60, 45), ("z2", 60, 45),
            ("z2", 60, 45), ("long_z2", 150, 112), ("long_z2", 150, 112)]
    p = mkplan(week(0, rows, 450))
    apply(p, [ride(d(5))])
    assert any(s.get("shortened_from") for s in by_day(p).values())
    by_day(p)[d(1)]["status"] = "missed"
    weeks = tp._plan_dict_to_planned_weeks(p)
    clock.freeze(TODAY + timedelta(days=2))
    try:
        out, info = tp.refit_remaining_week(tp.goal_from_dict(GOAL), weeks, TODAY + timedelta(days=2),
                                            seed_salt=7)
    finally:
        clock.freeze(TODAY)
    wk = out[0]
    own = next(s for s in wk.sessions if s.own)
    assert (own.session_type, own.duration_min, own.tss_estimate) == ("sweetspot", 180, 240)
    assert own.day.isoformat() not in info.get("refit_days", [])
    total = sum(float(s.tss_estimate or 0) for s in wk.sessions
                if s.session_type != "rest" and s.status not in ("missed", "dismissed"))
    assert total <= 450 * 1.05
    assert all(getattr(s, "shortened_from", 0) for s in wk.sessions
               if s.day.isoformat() in {x for x in by_day(p) if by_day(p)[x].get("shortened_from")})


def test_b10_self_heal_never_files_an_own_ride():
    p = _own_week()
    assert not any(s.get("own") for _w, _ws, s, _d in tp._iter_pending_unmatched(p, TODAY))
    before = copy.deepcopy(by_day(p)[d(5)])
    tp.heal_unmatched_sessions_dict(p, tp.load_workout_library(), TODAY)
    assert by_day(p)[d(5)] == before


def test_b11_c8_m16_an_own_ride_ridden_longer_and_easier_is_done():
    wk = tp.week_from_dict(week(0, [("rest", 0, 0)] * 7, 900))
    s = wk.sessions[2]
    s.session_type, s.duration_min, s.tss_estimate = "sweetspot", 180, 240
    s.own = {"name": "Club ride", "intensity": "hard"}
    clock.freeze(TODAY + timedelta(days=3))
    try:
        ride_ = {"date": d(2), "id": "a1", "tss": 262, "duration_min": 240, "intensity_factor": 0.81}
        prev = tp.rematch_week(wk, [ride_])
        m = next(x for x in prev["matches"] if x["session_date"] == d(2))
        assert m["new_status"] == "done"                        # 4 h at IF 0.81, not "partly"
        prev = tp.rematch_week(wk, [{"date": d(2), "id": "a2", "tss": 40, "duration_min": 40,
                                     "intensity_factor": 0.6}])
        m = next(x for x in prev["matches"] if x["session_date"] == d(2))
        assert m["new_status"] == "done_partial"                # a short ride still counts
        prev = tp.rematch_week(wk, [])
        m = next(x for x in prev["matches"] if x["session_date"] == d(2))
        assert m["new_status"] == "missed"                      # never "missed_race"
    finally:
        clock.freeze(TODAY)


def test_b18_a_race_added_on_an_own_day_wins_the_day():
    wk = tp.week_from_dict(week(0, FULL, 900))
    wk.sessions[5].own = {"name": "Club ride", "intensity": "hard"}
    goal = tp.goal_from_dict({"goal_type": "general", "events": [
        {"date": d(5), "priority": "B", "name": "Crit", "event_km": 40, "event_type": "crit"}]})
    tp._mark_race_days([wk], goal)
    assert wk.sessions[5].is_race and wk.sessions[5].own is None


def test_b20_week_plan_hard_is_race_not_own():
    s = tp.week_from_dict(week(0, [("z2", 120, 90)] + [("rest", 0, 0)] * 6, 900)).sessions[0]
    s.own = {"name": "Easy spin", "intensity": "easy"}
    assert week_plan._hard(s) is False
    s.is_race = True
    assert week_plan._hard(s) is True


def test_b22_the_weekly_hit_cap_counts_a_hard_own_ride_and_never_demotes_it():
    wk = tp.week_from_dict(week(0, [("rest", 0, 0), ("vo2max", 60, 75), ("rest", 0, 0),
                                    ("threshold", 60, 90), ("rest", 0, 0), ("sweetspot", 180, 240),
                                    ("rest", 0, 0)], 900, phase="base"))
    wk.sessions[5].own = {"name": "Club ride", "intensity": "hard"}
    lib = tp.load_workout_library()
    tp._enforce_weekly_hit_cap([wk], lib)
    assert wk.sessions[5].own and wk.sessions[5].session_type == "sweetspot"
    assert sum(1 for s in wk.sessions if tp._session_is_hit(s)) <= 2


def test_b5_the_48h_pass_eases_a_hard_day_before_an_own_ride_never_the_ride():
    wk = tp.week_from_dict(week(0, [("rest", 0, 0), ("rest", 0, 0), ("vo2max", 60, 75),
                                    ("sweetspot", 180, 240), ("rest", 0, 0), ("rest", 0, 0),
                                    ("rest", 0, 0)], 900))
    wk.sessions[3].own = {"name": "Club ride", "intensity": "hard"}
    tp._space_hard_days_across_plan([wk], library=None)
    assert wk.sessions[3].session_type == "sweetspot" and wk.sessions[3].own
    assert wk.sessions[2].session_type == "z2"


# ═══ endpoints (stubbed HOME, TestClient; no network) ════════════════════════

def _write(p):
    pdir = Path(tp.PLAN_DIR)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "current_plan.json").write_text(json.dumps(p), encoding="utf-8")


def _read():
    return json.loads((Path(tp.PLAN_DIR) / "current_plan.json").read_text(encoding="utf-8"))


def _own_plan_on(days, intensity="hard", rows=FULL, budget=900):
    p = mkplan(week(0, rows, budget))
    apply(p, [ride(d(i), intensity=intensity, name=f"Ride {i}") for i in days])
    return p


def test_own_sessions_endpoint_adds_edits_moves_removes_and_heals_nothing(stub):
    _write(mkplan(week(0, FULL, 443)))
    r = stub.client.post("/api/plan/own-sessions", json={"set": [ride(d(5))]})
    assert r.status_code == 200, r.text
    j = r.json()
    assert j["ok"] and j["applied"] == [{"action": "add", "date": d(5)}]
    # B10: the heal middleware ran after the POST and gave the ride no file.
    s = by_day(_read())[d(5)]
    assert s["own"]["name"] == "Club ride" and s["zwo_file"] == ""
    r = stub.client.post("/api/plan/own-sessions",
                         json={"set": [ride(d(5), dur=150, intensity="moderate", name="Shorter")]})
    assert r.json()["applied"] == [{"action": "edit", "date": d(5)}]
    s = by_day(_read())[d(5)]
    assert (s["duration_min"], s["session_type"], s["own"]["name"]) == (150, "tempo", "Shorter")
    assert s["own"]["replaced"]["session_type"] == "long_z2"     # an edit keeps the snapshot
    r = stub.client.post("/api/plan/own-sessions", json={"move": [{"from": d(5), "to": d(6)}]})
    assert r.json()["applied"] == [{"action": "move", "from": d(5), "to": d(6)}]
    r = stub.client.post("/api/plan/own-sessions", json={"remove": [d(6)]})
    assert r.status_code == 200 and not any(s.get("own") for s in by_day(_read()).values())
    r = stub.client.post("/api/plan/own-sessions", json={"remove": [d(6)]})
    assert r.status_code == 400 and r.json()["refused"][0]["reason"] == "no_own_ride"
    assert stub.client.post("/api/plan/own-sessions", json={}).status_code == 400


def test_put_back_marks_a_manual_ride_and_refuses_an_imported_one(stub):
    p = mkplan(week(0, FULL, 900))
    apply(p, [ride(d(5)), dict(ride(d(3), intensity="easy", dur=60), icu_event_id=5)])
    _write(p)
    r = stub.client.post("/api/plan/own-sessions",
                         json={"put_back": [{"day": d(5), "icu_day": d(6)}, {"day": d(3), "icu_day": d(4)}]})
    j = r.json()
    assert [a["action"] for a in j["applied"]] == ["put_back"]
    assert [x["reason"] for x in j["refused"]] == ["not_pushed"]
    assert by_day(_read())[d(5)]["own"]["put_back_from"] == d(6)


def test_m9_a_list_read_for_one_rider_never_lands_in_anothers_plan(stub):
    pid_a = stub.pid
    _write(mkplan(week(0, FULL, 443)))
    pid_b = stub.pm.create_profile("Other rider")
    stub.pm.switch(pid_b)
    _write(mkplan(week(0, FULL, 443)))
    before = _read()
    r = stub.client.post("/api/plan/own-sessions",
                         json={"profile_id": pid_a, "set": [dict(ride(d(5)), icu_event_id=9)]})
    assert r.status_code == 409 and r.json()["error"] == "profile_changed"
    assert _read() == before
    r = stub.client.post("/api/plan/own-sessions", json={"profile_id": pid_b, "set": [ride(d(5))]})
    assert r.status_code == 200


def test_c9_days_held_in_intervals_icu_are_pinned_from_the_last_calendar_run(stub, monkeypatch):
    p = mkplan(week(0, [("rest", 0, 0), ("z2", 60, 45), ("vo2max", 60, 75), ("rest", 0, 0),
                        ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)], 900))
    _write(p)
    monkeypatch.setattr(app_module, "_icu_push_last_result",
                        {"profile_id": stub.pid, "held_days": [d(2)]})
    j = stub.client.post("/api/plan/own-sessions", json={"set": [ride(d(3))]}).json()
    assert by_day(_read())[d(2)]["session_type"] == "vo2max"
    assert [w["code"] for w in j["warnings"]] == ["pinned_next_to_ride"]


@pytest.mark.parametrize("path,body", [
    ("/api/readiness/apply-tier-down", {"date": d(0)}),
    ("/api/plan/auto-adjust", {"scope": "today", "severity": "tier_down"}),
    ("/api/plan/auto-adjust", {"scope": "today", "severity": "rest"}),
])
def test_h1_c14_ease_today_and_auto_adjust_refuse_an_own_ride(stub, path, body):
    p = _own_plan_on([0])
    _write(p)
    before = _read()
    r = stub.client.post(path, json=body)
    assert r.status_code == 400 and "your own ride" in r.json()["error"]
    assert _read() == before


def test_h1_auto_adjust_rest_for_tomorrow_refuses_an_own_ride(stub):
    p = _own_plan_on([1])
    _write(p)
    before = _read()
    r = stub.client.post("/api/plan/auto-adjust", json={"scope": "day", "severity": "rest"})
    assert r.status_code == 400 and _read() == before


@pytest.mark.parametrize("path,body", [
    ("/api/plan/dismiss-session", {"date": d(2)}),
    ("/api/plan/re-draw", {"date": d(2)}),
    ("/api/plan/accept-redraw", {"date": d(2), "zwo_file": "x.zwo", "zwo_name": "X",
                                 "duration_min": 60, "tss_estimate": 50}),
    ("/api/plan/swap-type", {"date": d(2), "session_type": "vo2max", "duration_min": 60}),
    ("/api/plan/move-session", {"date": d(2), "new_date": d(3)}),
    ("/api/plan/move-session", {"date": d(3), "new_date": d(2)}),
])
def test_b13_b14_every_day_writer_refuses_an_own_ride(stub, path, body):
    p = _own_plan_on([2])
    _write(p)
    before = _read()
    r = stub.client.post(path, json=body)
    j = r.json()
    assert r.status_code == 400 or j.get("ok") is False, (r.status_code, j)
    assert "your own ride" in json.dumps(j)
    assert _read() == before


def test_today_persist_refuses_a_fixed_day(stub):
    _write(_own_plan_on([0]))
    before = _read()
    r = stub.client.post("/api/today-session/persist",
                         json={"session_type": "recovery", "duration_min": 30, "reason": "x"})
    assert r.status_code == 400 and _read() == before


def test_x1_c13_readiness_never_rewrites_todays_own_ride_and_gives_a_line(stub, monkeypatch):
    _write(_own_plan_on([0]))
    real = tp.adjust_today_session

    def _gate_fires(planned, readiness, **kw):
        # Any gate firing on the own-typed row (the test never reads thresholds).
        return tp.PlannedSession(day=planned.day, day_name=planned.day_name, session_type="recovery",
                                 duration_min=30, tss_estimate=15, description="eased"), "gate fired"
    monkeypatch.setattr(tp, "adjust_today_session", _gate_fires)
    monkeypatch.setattr(app_module, "_kick_lazy_icu_sync", lambda **kw: None)
    j = stub.client.get("/api/today-session").json()
    assert j["was_modified"] is False and j["reason"] == ""
    assert j["adjusted"]["session_type"] == j["planned"]["session_type"] == "sweetspot"
    assert j["own"]["name"] == "Ride 0" and j["own_advisory"]
    assert "replaced" not in j["own"]
    monkeypatch.setattr(tp, "adjust_today_session", real)


def test_b12_auto_moves_never_move_an_own_ride_nor_onto_one():
    pinned = {"user_moved": True, "moved_from": d(0)}
    # A missed VO2 and a week whose only easy day is the rider's own ride.
    rows = [("vo2max", 60, 75, {"status": "missed"}), ("z2", 60, 45)] + [("z2", 60, 45, pinned)] * 5
    p = mkplan(week(0, rows, 900))
    p["weeks"][0]["sessions"][1]["own"] = {"name": "Spin", "intensity": "easy"}
    # A missed own ride and a free rest day after it.
    p2 = mkplan(week(0, [("z2", 60, 45, {"status": "missed"}), ("rest", 0, 0)]
                     + [("z2", 60, 45, pinned)] * 5, 900))
    p2["weeks"][0]["sessions"][0]["own"] = {"name": "Club ride", "intensity": "easy"}
    clock.freeze(TODAY + timedelta(days=1))
    try:
        sug = app_module._compute_missed_suggestions(p, TODAY + timedelta(days=1))
        sug2 = app_module._compute_missed_suggestions(p2, TODAY + timedelta(days=1))
    finally:
        clock.freeze(TODAY)
    assert [x["suggested_date"] for x in sug] == []        # never takes over the own day
    assert [x["missed_date"] for x in sug2] == []          # a missed own ride stays put


def test_b19_deload_revert_keeps_own_rides_as_they_are_now(stub):
    p = mkplan(week(0, FULL, 443))
    snap = copy.deepcopy(p["weeks"][0])
    snap["sessions"][4] = dict(snap["sessions"][4], own={"name": "Old ride", "intensity": "easy"})
    apply(p, [ride(d(6), intensity="easy", dur=60, name="New ride")])
    p["deload_advance"] = {"week_num": 1, "reverted": False, "original_week": snap}
    _write(p)
    r = stub.client.post("/api/plan/continuous/deload-revert", json={})
    assert r.json().get("reverted") is True
    b = by_day(_read())
    assert b[d(6)]["own"]["name"] == "New ride"                 # added since: stays
    assert not b[d(4)].get("own")                               # removed since: not back


def test_next_ftp_test_slot_skips_the_day_after_a_ride_that_is_not_easy(stub):
    nxt = 7  # next Monday
    rows = [("tempo", 90, 98), ("vo2max", 60, 75), ("z2", 60, 45), ("rest", 0, 0),
            ("rest", 0, 0), ("rest", 0, 0), ("rest", 0, 0)]
    p = mkplan(week(0, FULL, 900), week(nxt, rows, 900, num=2))
    p["weeks"][1]["sessions"][0]["own"] = {"name": "Club ride", "intensity": "moderate"}
    _write(p)
    assert app_module._next_ftp_test_slot() != d(nxt + 1)


def test_h2_swap_type_refuses_a_test_the_day_after_an_own_ride_that_is_not_easy(stub):
    p = _own_plan_on([2], intensity="moderate")
    _write(p)
    before = _read()
    r = stub.client.post("/api/plan/swap-type", json={"date": d(3), "session_type": "ftp_test",
                                                      "duration_min": 60})
    assert r.status_code == 400 and "own ride" in r.json()["error"] and _read() == before


def test_execution_score_is_never_written_for_an_own_ride():
    p = _own_plan_on([2])
    prev = {"matches": [{"session_date": d(2), "new_status": "done", "activity_id": "a1",
                         "matched_axes": 3, "score": 1.0, "axes": {}, "details": {}}]}
    app_module._apply_rematch_preview_to_plan(p, 0, prev)
    s = by_day(p)[d(2)]
    assert s["status"] == "done" and s["execution"] is None and s["completion_matches"]


def test_calendar_payload_carries_the_own_ride_and_its_card_state():
    p = _own_plan_on([2])
    s = by_day(p)[d(2)]
    assert app_module._classify_card_state(s, has_actual=False, library_lookup={}) == "own"
    view = app_module._own_for_view(s["own"])
    assert view["name"] == "Ride 2" and "replaced" not in view


# ═══ rebuild paths (real library; heavier) ═══════════════════════════════════

def _event_goal(weeks=16):
    return tp.Goal(goal_type="event", target_date=TODAY + timedelta(weeks=weeks),
                   event_name="Fondo", event_km=120, hours_per_week=8.0,
                   max_weekday_hours=2.0, max_weekend_hours=4.0, rest_days=[0])


def _generated(goal, seed=11):
    _phases, weeks = tp.generate_plan(goal, seed_salt=seed)
    return {"goal": tp.goal_to_dict(goal), "weeks": [tp.week_to_dict(w) for w in weeks]}


def _first_day(plan, pred, after=1):
    for w in plan["weeks"]:
        for s in w["sessions"]:
            if s["day"] >= d(after) and pred(s):
                return s["day"]
    raise AssertionError("no such day")


def test_b5_regenerate_keeps_the_ride_unfiled_unclamped_and_eases_the_day_before():
    goal = _event_goal(12)
    p = _generated(goal)
    day = _first_day(p, lambda s: s["session_type"] != "rest"
                     and date.fromisoformat(s["day"]).weekday() in (2, 3), after=8)
    r = apply(p, [{"date": day, "name": "Midweek chaingang", "duration_min": 300, "intensity": "hard"}],
              library=tp.load_workout_library())
    assert not r["refused"], r["refused"]
    own_before = copy.deepcopy(by_day(p)[day])
    _ph, weeks, _info = tp.regenerate_from_today(goal, tp._plan_dict_to_planned_weeks(p),
                                                 current_ctl=45.0, seed_salt=5)
    s = next(x for w in weeks for x in w.sessions if x.day.isoformat() == day)
    assert s.own and s.own["name"] == "Midweek chaingang"
    assert (s.session_type, s.duration_min, s.tss_estimate, s.zwo_file) == (
        own_before["session_type"], 300, own_before["tss_estimate"], "")   # (a)(b)(c)
    prev = next(x for w in weeks for x in w.sessions
                if x.day == date.fromisoformat(day) - timedelta(days=1))
    assert not (tp._session_is_hit(prev) and prev.status == "pending")       # (e) 48 h


def test_b7_h2a_recalc_keeps_the_ride_and_places_the_test_around_it(monkeypatch):
    goal = _event_goal(20)
    p = _generated(goal)
    calls = []
    real = tp._rested_test_day

    def _spy(week, around=(), keep_days=()):
        pick = real(week, around, keep_days)
        calls.append(({s.day for s in week.sessions},
                      {s.day for s in week.sessions if getattr(s, "own", None)},
                      pick.day if pick is not None else None))
        return pick
    monkeypatch.setattr(tp, "_rested_test_day", _spy)
    tp.recalculate_plan(goal, tp._plan_dict_to_planned_weeks(copy.deepcopy(p)), current_ctl=5.0)
    test_weeks = [(days, pick) for days, _own, pick in calls if pick is not None]
    assert test_weeks, "recalc placed no test: the case does not arise"
    days, pick = test_weeks[0]
    # The rider's ride on the day the test would take.
    r = apply(p, [{"date": pick.isoformat(), "name": "Club ride", "duration_min": 150,
                   "intensity": "easy"}], library=tp.load_workout_library())
    assert not r["refused"], r["refused"]
    calls.clear()
    _ph, weeks, info = tp.recalculate_plan(goal, tp._plan_dict_to_planned_weeks(p), current_ctl=5.0)
    assert info["action"] == "recalculated"
    hit = [c for c in calls if pick in c[0]]
    assert hit and pick in hit[0][1]               # placed AFTER the kept ride is in the week
    placed = hit[0][2]
    assert placed != pick                          # never on the rider's kept day
    s = next(x for w in weeks for x in w.sessions if x.day == pick)
    assert s.own and s.zwo_file == ""              # kept, unfiled
    tests = sorted(x.day for w in weeks for x in w.sessions if x.session_type == "ftp_test")
    if placed is not None:
        assert placed in tests                     # the kept day no longer writes over it
    else:
        assert any(t > pick for t in tests)        # no anchor: a later unload week takes it


def test_b21_h6_a_new_plan_carries_the_rides_with_their_record(stub):
    old = mkplan(week(0, FULL, 443), week(7, FULL, 443, num=2))
    apply(old, [{"date": d(9), "name": "Club ride", "duration_min": 120, "intensity": "moderate",
                 "load": 130, "load_from": "icu", "icu_event_id": 4242, "icu_day": d(9)},
                ride(d(12), intensity="easy", dur=90, name="Manual spin")])
    _write(old)
    r = stub.client.post("/api/plan/generate", json={
        "goal": "ftp", "hours_per_week": 8.0, "max_weekday": 2.0, "max_weekend": 4.0,
        "plan_weeks": 8, "rest_days": [0], "daily_availability": {}})
    assert r.status_code == 200, r.text[:300]
    j = r.json()
    b = by_day(_read())
    imp = b[d(9)]["own"]
    assert (imp["source"], imp["icu_event_id"], imp["load_from"], imp["icu_day"]) == ("icu", 4242, "icu", d(9))
    assert b[d(9)]["tss_estimate"] == 130 and b[d(12)]["own"]["source"] == "manual"
    assert imp["replaced"]["day"] == d(9) and not imp["replaced"].get("own")   # the NEW plan's day
    assert {a["date"] for a in j["own_carry"]["applied"]} == {d(9), d(12)}


def test_l7_a_rebuild_says_which_own_rides_it_dropped(stub):
    goal = tp.Goal(goal_type="ftp", hours_per_week=8.0, plan_weeks=16, rest_days=[0])
    p = _generated(goal)
    far = _first_day(p, lambda s: s["session_type"] != "rest", after=13 * 7 + 1)
    r = apply(p, [ride(far, intensity="easy", dur=60, name="Far ride")])
    assert not r["refused"]
    plan_dict, info = app_module._regenerate_plan_dict(p, current_ctl=40.0, activities=[], seed_salt=3)
    assert [x["date"] for x in info.get("own_dropped", [])] == [far]


def test_m12_the_deload_advance_counts_the_own_ride_in_its_budget(stub):
    goal = tp.Goal(goal_type="continuous", hours_per_week=8.0, max_weekday_hours=2.0,
                   max_weekend_hours=4.0, rest_days=[0])
    p = _generated(goal)
    cur_idx = next(i for i, w in enumerate(p["weeks"]) if w["start"] <= d(0) <= w["end"])
    wk = p["weeks"][cur_idx]
    day = next(s["day"] for s in wk["sessions"] if s["day"] > d(0) and s["session_type"] != "rest")
    r = apply(p, [ride(day, dur=180)], library=tp.load_workout_library())
    assert not r["refused"]
    _write(p)
    res = app_module._advance_continuous_deload(
        p, Path(tp.PLAN_DIR) / "current_plan.json", cur_idx,
        {"trigger": "acwr", "value": 1.6, "reason": "load"}, TODAY)
    assert res is not None
    wk = _read()["weeks"][cur_idx]
    owed = sum(float(s["tss_estimate"] or 0) for s in wk["sessions"]
               if s["session_type"] != "rest" and (s.get("status") or "pending") not in ("missed", "dismissed"))
    assert by_day(_read())[day]["own"]["name"] == "Club ride"
    assert owed <= float(wk["tss_target"]) + 1
